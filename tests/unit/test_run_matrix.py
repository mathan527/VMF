"""Tests for the M10 orchestrator (vmf.orchestrator.run_matrix). No device, no Appium.

Every external call is a thread-safe recording fake passed through ``Deps``.
Nothing sleeps: concurrency is proved with a Barrier, ordering with a shared
event log.
"""
import inspect
import json
import logging
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from vmf.orchestrator import run_matrix as rm
from vmf.perf.perf import Thresholds
from vmf.results import Finding, PerfRow, Step, Verdict
from vmf.runner import runner as runner_mod

PACKAGE = "com.example.testapp"
GLOBAL_KINDS = {"create_run", "finish_run", "appium", "disk", "procs", "kill"}


class Fakes:
    """Thread-safe recording fakes for every Deps callable."""

    def __init__(self, tmp_path, *, verdicts=None, fail=None, steps_for=None,
                 findings_for=None, barrier_parties=0, perf_regress=(), hold_perf=False):
        self.tmp = tmp_path
        self.lock = threading.Lock()
        self.events = []                 # (kind, name, extra)
        self.saved = {}                  # name -> [Verdict]
        self.configs = {}
        self.serial_name = {}
        self.verdicts = verdicts or {}
        self.fail = fail or {}           # (stage, name) -> Exception
        self.steps_for = steps_for
        self.findings_for = findings_for or (lambda xml: [])
        self.barrier = threading.Barrier(barrier_parties) if barrier_parties else None
        self.perf_regress = set(perf_regress)
        self.hold_perf = hold_perf
        self.runner_calls = 0
        self.active = 0
        self.peak = 0
        self.perf_active = 0
        self.perf_peak = 0
        self.active_ports = set()
        self.port_clashes = []
        self.status_ok = True
        self.disk_gb = 100.0
        self.orphans = []
        self.leftover = []
        self.proc_calls = 0
        self.stop_after_runner = None    # threading.Event set by the first runner call
        self.geometry_configs = []
        self.save_error_fails = False

    # -- helpers ------------------------------------------------------------
    def ev(self, kind, name=None, **extra):
        with self.lock:
            self.events.append((kind, name, extra))

    def kinds(self, name=None, include_global=False):
        with self.lock:
            return [k for k, n, _ in self.events
                    if (include_global or k not in GLOBAL_KINDS) and (name is None or n == name)]

    def extras(self, kind):
        with self.lock:
            return [(n, e) for k, n, e in self.events if k == kind]

    def count(self, kind):
        return len(self.extras(kind))

    def _maybe_fail(self, stage, name):
        exc = self.fail.get((stage, name))
        if exc is not None:
            raise exc

    # -- Deps callables -----------------------------------------------------
    def start_device(self, profile, port):
        name = rm.profile_name(profile)
        self.ev("start", name, port=port)
        with self.lock:
            if port in self.active_ports:
                self.port_clashes.append(port)
            self.active_ports.add(port)
            self.serial_name[f"emulator-{port}"] = name
        self._maybe_fail("start", name)
        return f"emulator-{port}"

    def stop_device(self, serial):
        name = self.serial_name[serial]
        self.ev("stop", name)
        with self.lock:
            self.active_ports.discard(int(serial.split("-")[1]))
        self._maybe_fail("stop", name)

    def apply_profile_settings(self, serial, profile):
        name = self.serial_name[serial]
        self.ev("apply", name)
        self._maybe_fail("apply", name)
        return {"density_dpi": 320, "resolution": "720x1600"}

    def reset_profile(self, serial, pkg):
        name = self.serial_name[serial]
        self.ev("reset", name, pkg=pkg)
        self._maybe_fail("reset", name)

    def get_display_zones(self, serial):
        name = self.serial_name[serial]
        self.ev("zones", name)
        self._maybe_fail("zones", name)
        return {"cutout_rects": [(1, 2, 3, 4)], "gesture_rects": []}

    def start_logcat(self, serial, path):
        name = self.serial_name[serial]
        self.ev("logcat_start", name, path=path)
        self._maybe_fail("logcat_start", name)
        return SimpleNamespace(name=name)

    def stop_logcat(self, proc):
        self.ev("logcat_stop", proc.name)
        self._maybe_fail("logcat_stop", proc.name)

    def runner_run(self, serial, apk, scenario, system_port, run_id=None, profile=None, record=True):
        name = self.serial_name[serial]
        self.ev("runner", name, system_port=system_port, run_id=run_id, profile=profile,
                record=record)
        with self.lock:
            self.runner_calls += 1
            ticket = self.runner_calls
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            if self.stop_after_runner is not None:
                self.stop_after_runner.set()
            if self.barrier is not None and ticket <= self.barrier.parties:
                self.barrier.wait(timeout=10)
            self._maybe_fail("runner", name)
            steps = self.steps_for(name) if self.steps_for else [
                Step(0, "launch", "pass", 0, "", "")]
            return (run_id if record else None), steps
        finally:
            with self.lock:
                self.active -= 1

    def classify(self, logcat_path, steps=None, *, package=None, pid_alive=None):
        name = Path(logcat_path).parent.name
        self.ev("classify", name, package=package, pid_alive_ok=callable(pid_alive))
        self._maybe_fail("classify", name)
        return self.verdicts.get(name, Verdict("pass", "", None, None))

    def geometry_check(self, xml, config):
        self.ev("geometry", None)
        with self.lock:
            self.geometry_configs.append(config)
        found = self.findings_for(xml)
        if isinstance(found, Exception):
            raise found
        return found

    def perf_run_and_store(self, run_id, profile_name, serial, profile, **kw):
        self.ev("perf", profile_name, run_id=run_id, kw=sorted(kw))
        with self.lock:
            self.perf_active += 1
            self.perf_peak = max(self.perf_peak, self.perf_active)
        try:
            if self.hold_perf:
                threading.Event().wait(0.05)  # lets a (wrong) parallel pass overlap
            self._maybe_fail("perf", profile_name)
            kw["flow"]()
            reg = 50.0 if profile_name in self.perf_regress else 1.0
            return [PerfRow("launch_time", 1000.0, 1.0, 900.0, reg, False, False)]
        finally:
            with self.lock:
                self.perf_active -= 1

    def perf_is_regression(self, row, thresholds):
        return row.regression_pct is not None and row.regression_pct > thresholds.launch_regression_pct

    def create_run(self, apk_name, scenario, profiles):
        self.ev("create_run", None, apk=apk_name, scenario=scenario, profiles=list(profiles))
        return 77

    def save_device_result(self, run_id, profile, verdict):
        if self.save_error_fails and verdict.result == "error":
            raise RuntimeError("db locked")
        self.ev("save_result", profile, result=verdict.result)
        with self.lock:
            self.saved.setdefault(profile, []).append(verdict)

    def save_profile_config(self, run_id, profile, config):
        self.ev("save_config", profile)
        with self.lock:
            self.configs[profile] = config

    def finish_run(self, run_id):
        self.ev("finish_run", None, run_id=run_id)

    def pid_alive_factory(self, serial, package):
        return lambda: True

    def list_emulator_processes(self):
        with self.lock:
            self.proc_calls += 1
            first = self.proc_calls == 1
        self.ev("procs", None)
        return list(self.orphans if first else self.leftover)

    def appium_status(self):
        self.ev("appium", None)
        return self.status_ok

    def free_disk_gb(self, path):
        self.ev("disk", None)
        return self.disk_gb

    def kill_process(self, pid):
        self.ev("kill", None, pid=pid)

    def deps(self):
        return rm.Deps(
            start_device=self.start_device, stop_device=self.stop_device,
            apply_profile_settings=self.apply_profile_settings, reset_profile=self.reset_profile,
            runner_run=self.runner_run, start_logcat=self.start_logcat, stop_logcat=self.stop_logcat,
            classify=self.classify, geometry_check=self.geometry_check,
            get_display_zones=self.get_display_zones, perf_run_and_store=self.perf_run_and_store,
            perf_is_regression=self.perf_is_regression, create_run=self.create_run,
            save_device_result=self.save_device_result,
            save_profile_config=self.save_profile_config, finish_run=self.finish_run,
            pid_alive_factory=self.pid_alive_factory,
            list_emulator_processes=self.list_emulator_processes,
            appium_status=self.appium_status, free_disk_gb=self.free_disk_gb,
            kill_process=self.kill_process)


@pytest.fixture(autouse=True)
def results_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(rm, "RESULTS_DIR", str(tmp_path / "results"))
    return tmp_path / "results"


@pytest.fixture
def scenario(tmp_path):
    p = tmp_path / "scenario.yaml"
    p.write_text(yaml.safe_dump({"name": "Login", "package": PACKAGE, "steps": []}), encoding="utf-8")
    return str(p)


def make_profiles(n, tiers=("low", "medium", "high")):
    return [{"name": f"p{i}", "brand": "B", "tier": tiers[i % len(tiers)],
             "density_dpi": 320, "resolution": "720 x 1600"} for i in range(n)]


def go(fakes, scenario, profiles, run_id=77, **opts):
    options = {"apk": "C:/builds/app.apk", "scenario": scenario, "profiles": profiles,
               "performance_pass": False, **opts}
    return rm.main(run_id, options, deps=fakes.deps())


def one(fakes, scenario, **kw):
    summary = go(fakes, scenario, make_profiles(1), **kw)
    return summary["profiles"][0], summary


# --- per-profile pipeline ----------------------------------------------------------------------------

def hierarchy_steps(tmp_path):
    f = tmp_path / "h.xml"
    f.write_text("<hierarchy/>", encoding="utf-8")
    return lambda name: [Step(0, "tap", "pass", 1, "", str(f))]


def test_exact_per_profile_call_order(tmp_path, scenario):
    fakes = Fakes(tmp_path, steps_for=hierarchy_steps(tmp_path))
    result, _ = one(fakes, scenario)
    assert fakes.kinds() == ["start", "apply", "zones", "logcat_start", "runner", "logcat_stop",
                             "classify", "geometry", "save_config", "save_result", "reset", "stop"]
    assert result["result"] == "pass" and result["error"] is False
    assert result["serial"] == "emulator-5554"


def test_pipeline_passes_the_right_arguments(tmp_path, scenario, results_dir):
    fakes = Fakes(tmp_path, steps_for=hierarchy_steps(tmp_path))
    one(fakes, scenario)
    (_, runner_extra), = fakes.extras("runner")
    assert runner_extra == {"system_port": 8200, "run_id": 77, "profile": "p0", "record": True}
    (_, logcat_extra), = fakes.extras("logcat_start")
    assert logcat_extra["path"] == f"{results_dir}/77/p0/logcat.txt"
    (_, cls), = fakes.extras("classify")
    assert cls == {"package": PACKAGE, "pid_alive_ok": True}
    cfg = fakes.geometry_configs[0]
    assert cfg["density_dpi"] == 320 and cfg["resolution"] == "720x1600"  # readback wins
    assert cfg["cutout_rects"] == [(1, 2, 3, 4)] and cfg["package"] == PACKAGE
    saved = fakes.configs["p0"]
    assert saved["profile"] == "p0" and saved["serial"] == "emulator-5554"
    assert saved["port"] == 5554 and saved["system_port"] == 8200 and saved["tier"] == "low"
    assert saved["zones"]["cutout_rects"] == [(1, 2, 3, 4)] and saved["density_dpi"] == 320


def test_apply_settings_can_be_disabled(tmp_path, scenario):
    fakes = Fakes(tmp_path)
    one(fakes, scenario, apply_settings=False)
    assert "apply" not in fakes.kinds()


@pytest.mark.parametrize("stage, expect", [
    ("apply", ["start", "apply", "save_result", "reset", "stop"]),
    ("logcat_start", ["start", "apply", "zones", "logcat_start", "save_result", "reset", "stop"]),
    ("runner", ["start", "apply", "zones", "logcat_start", "runner", "save_result",
                "logcat_stop", "reset", "stop"]),
    ("classify", ["start", "apply", "zones", "logcat_start", "runner", "logcat_stop", "classify",
                  "save_result", "reset", "stop"]),
    # stop_logcat itself failing: it is retried in the cleanup, and reset/stop still run
    ("logcat_stop", ["start", "apply", "zones", "logcat_start", "runner", "logcat_stop",
                     "save_result", "logcat_stop", "reset", "stop"]),
])
def test_cleanup_runs_when_a_stage_raises(tmp_path, scenario, stage, expect):
    fakes = Fakes(tmp_path, fail={(stage, "p0"): RuntimeError("boom")})
    result, summary = one(fakes, scenario)
    assert fakes.kinds() == expect
    assert result["result"] == "error" and result["error"] is True
    assert "RuntimeError: boom" in result["cause"]
    (verdict,) = fakes.saved["p0"]
    assert verdict.result == "error" and "boom" in verdict.cause
    assert fakes.count("finish_run") == 1


@pytest.mark.parametrize("stage, label", [
    ("apply", "apply_profile_settings"), ("runner", "runner"), ("classify", "classify"),
    ("logcat_start", "logcat"), ("logcat_stop", "stop_logcat"),
])
def test_error_cause_names_the_stage(tmp_path, scenario, stage, label):
    fakes = Fakes(tmp_path, fail={(stage, "p0"): ValueError("x")})
    result, _ = one(fakes, scenario)
    assert f"(stage: {label})" in result["cause"]


def test_stop_device_not_called_when_start_device_raised(tmp_path, scenario):
    fakes = Fakes(tmp_path, fail={("start", "p0"): OSError("no emulator")})
    result, _ = one(fakes, scenario)
    assert fakes.kinds() == ["start", "save_result"]
    assert result["serial"] is None and "(stage: start_device)" in result["cause"]


def test_geometry_exception_becomes_error_and_cleans_up(tmp_path, scenario):
    fakes = Fakes(tmp_path, steps_for=hierarchy_steps(tmp_path),
                  findings_for=lambda xml: ValueError("malformed hierarchy XML"))
    result, _ = one(fakes, scenario)
    assert result["result"] == "error" and "(stage: geometry)" in result["cause"]
    assert fakes.kinds()[-2:] == ["reset", "stop"]


def test_logcat_stopped_before_classify_on_success(tmp_path, scenario):
    fakes = Fakes(tmp_path)
    one(fakes, scenario)
    kinds = fakes.kinds()
    assert kinds.index("logcat_stop") < kinds.index("classify")
    assert kinds.count("logcat_start") == kinds.count("logcat_stop") == 1


@pytest.mark.parametrize("stage, started, stopped", [
    ("apply", 0, 0),           # never started, so nothing to stop
    ("logcat_start", 1, 0),    # the start itself failed: there is no process to stop
    ("runner", 1, 1),
    ("classify", 1, 1),
])
def test_a_started_logcat_is_stopped_on_every_failure_path(tmp_path, scenario, stage, started, stopped):
    fakes = Fakes(tmp_path, fail={(stage, "p0"): RuntimeError("boom")})
    one(fakes, scenario)
    assert fakes.kinds().count("logcat_start") == started
    assert fakes.kinds().count("logcat_stop") == stopped


def test_cleanup_failure_does_not_skip_the_next_step_and_is_recorded(tmp_path, scenario, caplog):
    fakes = Fakes(tmp_path, fail={("reset", "p0"): RuntimeError("reset broke")})
    with caplog.at_level(logging.ERROR):
        result, _ = one(fakes, scenario)
    assert fakes.kinds()[-2:] == ["reset", "stop"]  # stop still ran after reset failed
    assert result["result"] == "pass"
    assert any("reset_profile: RuntimeError: reset broke" in e for e in result["cleanup_errors"])
    assert any(r.exc_info for r in caplog.records)  # logged with a traceback


def test_failure_saving_the_error_result_is_logged_and_does_not_stop_the_matrix(tmp_path, scenario, caplog):
    fakes = Fakes(tmp_path, fail={("runner", "p1"): RuntimeError("boom")})
    fakes.save_error_fails = True
    with caplog.at_level(logging.ERROR):
        summary = go(fakes, scenario, make_profiles(3), max_parallel=1)
    assert summary["counts"] == {"pass": 2, "error": 1}
    assert any("could not save the error result" in r.message for r in caplog.records)


def test_every_caught_exception_is_logged_with_traceback(tmp_path, scenario, caplog):
    fakes = Fakes(tmp_path, fail={("runner", "p0"): RuntimeError("boom")})
    with caplog.at_level(logging.ERROR):
        one(fakes, scenario)
    assert any(r.exc_info and "boom" in str(r.exc_info[1]) for r in caplog.records)


def test_display_zone_failure_gives_empty_lists_and_continues(tmp_path, scenario):
    fakes = Fakes(tmp_path, fail={("zones", "p0"): RuntimeError("adb gone")})
    result, _ = one(fakes, scenario)
    assert result["result"] == "pass" and any("adb gone" in w for w in result["warnings"])
    assert fakes.configs["p0"]["zones"] == {"cutout_rects": [], "gesture_rects": []}


# --- fault isolation -------------------------------------------------------------------------------------

def test_two_failing_profiles_do_not_stop_the_other_three(tmp_path, scenario):
    fakes = Fakes(tmp_path, fail={("runner", "p1"): RuntimeError("a"), ("classify", "p3"): ValueError("b")})
    summary = go(fakes, scenario, make_profiles(5), max_parallel=2)
    assert summary["counts"] == {"pass": 3, "error": 2}
    by = {r["profile"]: r for r in summary["profiles"]}
    assert [by[f"p{i}"]["result"] for i in range(5)] == ["pass", "error", "pass", "error", "pass"]
    assert "(stage: runner)" in by["p1"]["cause"] and "(stage: classify)" in by["p3"]["cause"]
    assert sorted(fakes.saved) == [f"p{i}" for i in range(5)]
    assert all(len(v) == 1 for v in fakes.saved.values())
    assert fakes.count("start") == fakes.count("stop") == fakes.count("reset") == 5
    assert fakes.count("finish_run") == 1


def test_a_worker_bug_becomes_an_error_record(tmp_path, scenario, monkeypatch):
    fakes = Fakes(tmp_path)
    real = rm.run_profile

    def buggy(profile, ctx, pool, slot):
        if profile["name"] == "p1":
            pool.release(slot)
            raise RuntimeError("worker bug")
        return real(profile, ctx, pool, slot)
    monkeypatch.setattr(rm, "run_profile", buggy)
    summary = go(fakes, scenario, make_profiles(3), max_parallel=1)
    assert summary["counts"] == {"pass": 2, "error": 1}
    assert fakes.saved["p1"][0].result == "error" and "(stage: worker)" in fakes.saved["p1"][0].cause


# --- parallelism and slots ----------------------------------------------------------------------------------

@pytest.mark.parametrize("n_profiles, max_parallel", [(6, 3), (5, 2), (4, 4)])
def test_concurrency_reaches_but_never_exceeds_max_parallel(tmp_path, scenario, n_profiles, max_parallel):
    fakes = Fakes(tmp_path, barrier_parties=max_parallel)
    summary = go(fakes, scenario, make_profiles(n_profiles), max_parallel=max_parallel)
    assert summary["counts"] == {"pass": n_profiles}
    assert fakes.peak == max_parallel  # the barrier only trips if that many ran together
    assert fakes.port_clashes == []


def test_concurrent_tasks_use_unique_ports_that_follow_the_formula(tmp_path, scenario):
    fakes = Fakes(tmp_path, barrier_parties=3)
    go(fakes, scenario, make_profiles(3), max_parallel=3)
    ports = {n: e["port"] for n, e in fakes.extras("start")}
    systems = {n: e["system_port"] for n, e in fakes.extras("runner")}
    assert sorted(ports.values()) == [5554, 5556, 5558]
    assert sorted(systems.values()) == [8200, 8201, 8202]
    for name, port in ports.items():
        assert systems[name] == 8200 + (port - 5554) // 2


def test_slot_pool_ports_reuse_and_release_rules():
    pool = rm.SlotPool(3)
    slots = [pool.acquire() for _ in range(3)]
    assert [(s.index, s.port, s.system_port) for s in slots] == [
        (0, 5554, 8200), (1, 5556, 8201), (2, 5558, 8202)]
    with pytest.raises(rm.queue.Empty):
        pool.acquire(timeout=0)  # none left
    pool.release(slots[1])
    again = pool.acquire()
    assert again.index == 1 and again.port == 5556  # a released slot is reused
    with pytest.raises(ValueError, match="not held"):
        pool.release(slots[1]) or pool.release(slots[1])
    with pytest.raises(ValueError):
        rm.SlotPool(0)


def test_slot_pool_never_hands_the_same_slot_to_two_threads():
    pool = rm.SlotPool(2)
    seen, lock, clashes = set(), threading.Lock(), []
    start = threading.Barrier(2)

    def worker():
        slot = pool.acquire()
        with lock:
            if slot.index in seen:
                clashes.append(slot.index)
            seen.add(slot.index)
        start.wait(timeout=10)  # both hold a slot at the same time
        pool.release(slot)
    threads = [threading.Thread(target=worker) for _ in range(2)]
    [t.start() for t in threads]
    [t.join(10) for t in threads]
    assert clashes == [] and seen == {0, 1}


# --- performance pass -------------------------------------------------------------------------------------------

def test_pass_b_runs_one_profile_at_a_time(tmp_path, scenario):
    fakes = Fakes(tmp_path, hold_perf=True)
    summary = go(fakes, scenario, make_profiles(4), max_parallel=3, performance_pass=True)
    assert fakes.count("perf") == 4 and fakes.perf_peak == 1
    assert [p["status"] for p in summary["performance"]] == ["ok"] * 4


def test_pass_b_starts_only_after_every_pass_a_task_has_finished(tmp_path, scenario):
    fakes = Fakes(tmp_path)
    go(fakes, scenario, make_profiles(4), max_parallel=2, performance_pass=True)
    kinds = fakes.kinds()
    first_perf = kinds.index("perf")
    assert kinds[:first_perf].count("stop") == 4       # all four Pass A devices already stopped
    assert kinds[:first_perf].count("save_result") == 4


def test_pass_b_skipped_when_performance_pass_is_false(tmp_path, scenario):
    fakes = Fakes(tmp_path)
    summary = go(fakes, scenario, make_profiles(2), performance_pass=False)
    assert fakes.count("perf") == 0 and summary["performance"] == []


def test_pass_b_skips_error_and_cancelled_profiles(tmp_path, scenario):
    fakes = Fakes(tmp_path, fail={("runner", "p1"): RuntimeError("x")})
    summary = go(fakes, scenario, make_profiles(3), max_parallel=1, performance_pass=True)
    perf_by = {p["profile"]: p for p in summary["performance"]}
    assert perf_by["p1"]["status"] == "skipped" and "error" in perf_by["p1"]["reason"]
    assert [n for n, _ in fakes.extras("perf")] == ["p0", "p2"]


def test_pass_b_arguments_flow_is_unrecorded_and_baselines_are_per_profile(tmp_path, scenario):
    fakes = Fakes(tmp_path)
    go(fakes, scenario, make_profiles(1), performance_pass=True,
       baselines={"p0": {"launch_time": 900}})
    (_, perf_extra), = fakes.extras("perf")
    assert perf_extra["run_id"] == 77
    assert perf_extra["kw"] == ["apk", "baselines", "flow", "package", "thresholds"]
    flow_calls = [e for n, e in fakes.extras("runner") if not e["record"]]
    assert flow_calls == [{"system_port": 8200, "run_id": None, "profile": "p0", "record": False}]


def test_pass_b_error_is_recorded_does_not_stop_the_pass_and_cleans_up(tmp_path, scenario):
    fakes = Fakes(tmp_path, fail={("perf", "p0"): RuntimeError("perf broke")})
    summary = go(fakes, scenario, make_profiles(2), performance_pass=True)
    perf_by = {p["profile"]: p for p in summary["performance"]}
    assert perf_by["p0"]["status"] == "error" and "perf broke" in perf_by["p0"]["error"]
    assert perf_by["p1"]["status"] == "ok"
    assert summary["counts"] == {"pass": 2}  # functional results untouched
    assert fakes.count("stop") == fakes.count("start") == 4


def test_perf_regression_is_counted_separately_and_never_overwrites_the_functional_result(tmp_path, scenario):
    fakes = Fakes(tmp_path, perf_regress={"p1"})
    summary = go(fakes, scenario, make_profiles(3), performance_pass=True)
    assert summary["perf_regressions"] == [{"profile": "p1", "metrics": ["launch_time"]}]
    assert summary["counts"] == {"pass": 3}
    assert fakes.saved["p1"][0].result == "pass"


# --- verdict precedence and findings -----------------------------------------------------------------------------------

def one_finding(xml):
    return [Finding("overlap", "app:id/a", (0, 0, 10, 10), "overlaps b")]


@pytest.mark.parametrize("classifier_result", ["crash", "anr", "hang"])
def test_classifier_failures_are_kept_even_with_findings(tmp_path, scenario, classifier_result):
    fakes = Fakes(tmp_path, steps_for=hierarchy_steps(tmp_path), findings_for=one_finding,
                  verdicts={"p0": Verdict(classifier_result, "why", 2, "oem.doze")})
    result, _ = one(fakes, scenario)
    saved = fakes.saved["p0"][0]
    assert result["result"] == classifier_result
    assert (saved.cause, saved.failed_step, saved.trigger_behaviour) == ("why", 2, "oem.doze")


def test_pass_with_findings_is_layout_with_a_one_line_cause(tmp_path, scenario):
    fakes = Fakes(tmp_path, steps_for=hierarchy_steps(tmp_path), findings_for=one_finding,
                  verdicts={"p0": Verdict("pass", "", None, "oem.trim_memory")})
    result, _ = one(fakes, scenario)
    saved = fakes.saved["p0"][0]
    assert saved.result == "layout" and result["findings_count"] == 1
    assert "\n" not in saved.cause and saved.cause.startswith("1 layout finding(s); first: overlap app:id/a")
    assert saved.trigger_behaviour == "oem.trim_memory"


def test_pass_without_findings_is_pass(tmp_path, scenario):
    fakes = Fakes(tmp_path, steps_for=hierarchy_steps(tmp_path))
    result, _ = one(fakes, scenario)
    assert result["result"] == "pass" and fakes.saved["p0"][0].result == "pass"


def test_findings_json_is_combined_deduplicated_and_only_from_real_files(tmp_path, scenario, results_dir):
    a, b, empty = tmp_path / "a.xml", tmp_path / "b.xml", tmp_path / "empty.xml"
    a.write_text("<a/>", encoding="utf-8")
    b.write_text("<b/>", encoding="utf-8")
    empty.write_text("", encoding="utf-8")
    steps = lambda name: [Step(0, "x", "pass", 1, "", str(a)), Step(1, "x", "pass", 1, "", str(tmp_path / "missing.xml")),
                          Step(2, "x", "pass", 1, "", ""), Step(3, "x", "pass", 1, "", str(empty)),
                          Step(4, "x", "pass", 1, "", str(b))]
    other = Finding("tap_target", "app:id/z", (1, 1, 5, 5), "small")
    fakes = Fakes(tmp_path, steps_for=steps,
                  findings_for=lambda xml: [one_finding(xml)[0]] + ([other] if xml == "<b/>" else []))
    result, _ = one(fakes, scenario)
    assert fakes.count("geometry") == 2  # only the two real, non-empty files
    data = json.loads((results_dir / "77" / "p0" / "findings.json").read_text(encoding="utf-8"))
    assert data["count"] == 2 and result["findings_count"] == 2
    assert [f["kind"] for f in data["findings"]] == ["overlap", "tap_target"]  # sorted
    assert data["findings"][0]["steps"] == [0, 4] and data["findings"][1]["steps"] == [4]


def test_findings_json_is_written_even_when_there_are_none(tmp_path, scenario, results_dir):
    fakes = Fakes(tmp_path)
    one(fakes, scenario)
    data = json.loads((results_dir / "77" / "p0" / "findings.json").read_text(encoding="utf-8"))
    assert data["count"] == 0 and data["findings"] == []


# --- run id, finish_run, preflight ----------------------------------------------------------------------------------------

def test_given_run_id_never_calls_create_run(tmp_path, scenario):
    fakes = Fakes(tmp_path)
    summary = go(fakes, scenario, make_profiles(2), run_id=123)
    assert fakes.count("create_run") == 0 and summary["run_id"] == 123
    assert [e["run_id"] for _, e in fakes.extras("finish_run")] == [123]
    assert {e["run_id"] for _, e in fakes.extras("runner")} == {123}


def test_no_run_id_calls_create_run_once_with_apk_scenario_and_profile_names(tmp_path, scenario):
    fakes = Fakes(tmp_path)
    summary = go(fakes, scenario, make_profiles(3), run_id=None)
    ((_, extra),) = fakes.extras("create_run")
    assert extra == {"apk": "app.apk", "scenario": "Login", "profiles": ["p0", "p1", "p2"]}
    assert summary["run_id"] == 77


def test_finish_run_called_exactly_once_on_success(tmp_path, scenario):
    fakes = Fakes(tmp_path)
    go(fakes, scenario, make_profiles(3), performance_pass=True)
    assert fakes.count("finish_run") == 1


def test_finish_run_called_once_when_pass_a_crashes(tmp_path, scenario, monkeypatch):
    fakes = Fakes(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("pass a exploded")
    monkeypatch.setattr(rm, "_functional_pass", boom)
    with pytest.raises(RuntimeError, match="pass a exploded"):
        go(fakes, scenario, make_profiles(2))
    assert fakes.count("finish_run") == 1


def test_preflight_failure_starts_no_device_finishes_once_and_raises(tmp_path, scenario):
    fakes = Fakes(tmp_path)
    fakes.status_ok = False
    with pytest.raises(rm.PreflightError, match="Appium"):
        go(fakes, scenario, make_profiles(2), run_id=None)
    assert fakes.count("start") == 0 and fakes.count("finish_run") == 1
    assert fakes.count("create_run") == 1


def test_preflight_checks_disk_and_orphans_and_names_each_failure(tmp_path, scenario):
    fakes = Fakes(tmp_path)
    fakes.disk_gb = 5.0
    fakes.orphans = [{"pid": 4242, "name": "qemu-system-x86_64.exe"}]
    with pytest.raises(rm.PreflightError) as e:
        go(fakes, scenario, make_profiles(1))
    msg = str(e.value)
    assert "5.0 GB" in msg and "20 GB" in msg and "pid 4242" in msg
    assert "Appium" not in msg and fakes.count("start") == 0
    assert fakes.count("kill") == 0  # pre-flight only reports


def test_preflight_threshold_is_twenty_gb():
    assert rm.PREFLIGHT_MIN_DISK_GB == 20


def test_finish_run_failure_is_logged_and_raised_when_nothing_else_failed(tmp_path, scenario, caplog):
    fakes = Fakes(tmp_path)

    def bad_finish(run_id):
        raise RuntimeError("db gone")
    deps = replace(fakes.deps(), finish_run=bad_finish)
    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError, match="db gone"):
        rm.main(77, {"apk": "a.apk", "scenario": scenario, "profiles": make_profiles(1),
                     "performance_pass": False}, deps=deps)
    assert any("finish_run failed" in r.message for r in caplog.records)


# --- cooperative stop, leftovers ------------------------------------------------------------------------------------------

def test_stop_event_cancels_profiles_that_have_not_started(tmp_path, scenario):
    stop = threading.Event()
    fakes = Fakes(tmp_path)
    fakes.stop_after_runner = stop
    summary = go(fakes, scenario, make_profiles(4), max_parallel=1, stop_event=stop,
                 performance_pass=True)
    assert fakes.count("start") == 1 and fakes.kinds().count("stop") >= 1
    assert summary["counts"] == {"pass": 1, "cancelled": 3}
    assert summary["cancelled"] == ["p1", "p2", "p3"]
    assert fakes.count("perf") == 0  # the running profile finished; the pass honours the stop too
    assert sorted(fakes.saved) == ["p0"]
    assert fakes.count("finish_run") == 1


def test_stop_event_already_set_boots_nothing(tmp_path, scenario):
    stop = threading.Event()
    stop.set()
    fakes = Fakes(tmp_path)
    summary = go(fakes, scenario, make_profiles(3), stop_event=stop)
    assert fakes.count("start") == 0 and summary["counts"] == {"cancelled": 3}


def test_leftover_emulators_are_reported_logged_and_not_killed_by_default(tmp_path, scenario, caplog):
    fakes = Fakes(tmp_path)
    fakes.leftover = [{"pid": 999, "name": "qemu-system-x86_64.exe"}]
    with caplog.at_level(logging.WARNING):
        summary = go(fakes, scenario, make_profiles(1))
    assert summary["leftover_processes"] == fakes.leftover and summary["leftovers_killed"] == []
    assert any("pid 999" in r.message for r in caplog.records)
    assert fakes.count("kill") == 0


def test_leftover_emulators_are_killed_only_when_asked(tmp_path, scenario):
    fakes = Fakes(tmp_path)
    fakes.leftover = [{"pid": 999, "name": "emulator.exe"}]
    summary = go(fakes, scenario, make_profiles(1), kill_leftovers=True)
    assert [e["pid"] for _, e in fakes.extras("kill")] == [999]
    assert summary["leftovers_killed"] == [999]


def test_clean_run_has_no_leftovers(tmp_path, scenario):
    summary = go(Fakes(tmp_path), scenario, make_profiles(2))
    assert summary["leftover_processes"] == []


# --- profiles, names, max parallel ----------------------------------------------------------------------------------------------

def test_profile_name_rules():
    assert rm.profile_name({"name": "Samsung A07"}) == "Samsung_A07"
    assert rm.profile_name({"brand": "Samsung", "tier": "low", "api": 29}) == "samsung_low_29"
    assert rm.profile_name({"brand": "OnePlus", "tier": "high"}) == "oneplus_high_35"  # api from the tier
    assert rm.profile_name({"brand": "A B", "tier": "medium", "api": 31}) == "a_b_medium_31"
    with pytest.raises(ValueError, match="'tier'"):
        rm.profile_name({"brand": "x"})


def test_load_profiles_list_and_catalogue_layout_and_errors(tmp_path):
    p = tmp_path / "p.yaml"
    p.write_text(yaml.safe_dump([{"brand": "A", "tier": "low"}, {"brand": "B", "tier": "high"}]), encoding="utf-8")
    assert [x["brand"] for x in rm.load_profiles(str(p))] == ["A", "B"]
    p.write_text(yaml.safe_dump({"models": [{"brand": "C", "tier": "low"}]}), encoding="utf-8")
    assert rm.load_profiles(str(p)) == [{"brand": "C", "tier": "low"}]
    p.write_text("just: a mapping\n", encoding="utf-8")
    with pytest.raises(ValueError, match="list of profile"):
        rm.load_profiles(str(p))
    p.write_text("- not a mapping\n", encoding="utf-8")
    with pytest.raises(ValueError):
        rm.load_profiles(str(p))


def test_load_profiles_reads_the_real_catalogue_file():
    profiles = rm.load_profiles(str(Path(rm.__file__).resolve().parents[1] / "provision" / "reference-models.yaml"))
    names = [rm.profile_name(p) for p in profiles]
    assert len(profiles) >= 24 and len(set(names)) == len(names)


def test_max_parallel_resolution():
    assert rm.DEFAULT_MAX_PARALLEL == 2
    assert rm.resolve_max_parallel({}, 10, environ={}) == 2                       # default
    assert rm.resolve_max_parallel({}, 10, environ={"VMF_MAX_PARALLEL": "4"}) == 4  # environment
    assert rm.resolve_max_parallel({"max_parallel": 3}, 10, environ={"VMF_MAX_PARALLEL": "4"}) == 3  # option wins
    assert rm.resolve_max_parallel({"max_parallel": 8}, 3, environ={}) == 3        # clamped to profiles
    assert rm.resolve_max_parallel({}, 1, environ={}) == 1
    for bad in (0, -1, "x"):
        with pytest.raises(ValueError, match="max_parallel"):
            rm.resolve_max_parallel({"max_parallel": bad}, 5, environ={})
    with pytest.raises(ValueError):
        rm.resolve_max_parallel({}, 5, environ={"VMF_MAX_PARALLEL": "0"})


def test_max_parallel_reads_the_real_environment(monkeypatch):
    monkeypatch.setenv("VMF_MAX_PARALLEL", "3")
    assert rm.resolve_max_parallel({}, 10) == 3


def test_main_validation_errors_start_nothing(tmp_path, scenario):
    fakes = Fakes(tmp_path)
    dup = [{"name": "same"}, {"name": "same"}]
    with pytest.raises(ValueError, match="duplicate"):
        go(fakes, scenario, dup)
    with pytest.raises(ValueError, match="no profiles"):
        go(fakes, scenario, [])
    bare = tmp_path / "bare.yaml"
    bare.write_text("name: X\nsteps: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no package"):
        go(fakes, str(bare), make_profiles(1))
    assert fakes.kinds(include_global=True) == []  # not even create_run or finish_run


def test_package_option_overrides_the_scenario_package(tmp_path, scenario):
    fakes = Fakes(tmp_path)
    go(fakes, scenario, make_profiles(1), package="other.pkg")
    assert fakes.extras("reset")[0][1]["pkg"] == "other.pkg"


def test_summary_shape(tmp_path, scenario):
    summary = go(Fakes(tmp_path), scenario, make_profiles(3), max_parallel=2, performance_pass=True)
    assert set(summary) >= {"run_id", "counts", "profiles", "performance", "perf_regressions",
                            "cancelled", "leftover_processes", "max_parallel", "duration_s"}
    assert summary["max_parallel"] == 2
    assert set(summary["profiles"][0]) >= {"profile", "serial", "result", "cause",
                                           "findings_count", "duration_s", "error"}
    json.dumps(summary, default=str)


# --- command line, defaults, backward compatibility ----------------------------------------------------------------------------------

def test_cli_runs_the_matrix_and_prints_json(tmp_path, scenario, monkeypatch, capsys):
    fakes = Fakes(tmp_path)
    monkeypatch.setattr(rm, "default_deps", fakes.deps)
    prof = tmp_path / "profiles.yaml"
    prof.write_text(yaml.safe_dump(make_profiles(2)), encoding="utf-8")
    code = rm.cli(["--apk", "app.apk", "--scenario", scenario, "--profiles", str(prof),
                   "--max-parallel", "2", "--no-perf"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["counts"] == {"pass": 2} and out["run_id"] == 77
    assert fakes.count("create_run") == 1 and fakes.count("perf") == 0


def test_cli_run_id_flag_skips_create_run(tmp_path, scenario, monkeypatch, capsys):
    fakes = Fakes(tmp_path)
    monkeypatch.setattr(rm, "default_deps", fakes.deps)
    prof = tmp_path / "profiles.yaml"
    prof.write_text(yaml.safe_dump(make_profiles(1)), encoding="utf-8")
    rm.cli(["--apk", "a.apk", "--scenario", scenario, "--profiles", str(prof), "--run-id", "5", "--no-perf"])
    assert fakes.count("create_run") == 0 and json.loads(capsys.readouterr().out)["run_id"] == 5


def test_default_deps_wires_the_real_modules():
    from vmf import classifier, geometry, oem_packs, perf, provision, runner, storage
    d = rm.default_deps()
    assert d.start_device is provision.start_device and d.stop_device is provision.stop_device
    assert d.apply_profile_settings is oem_packs.apply_profile_settings
    assert d.reset_profile is oem_packs.reset_profile and d.runner_run is runner.run
    assert d.start_logcat is classifier.start_logcat and d.classify is classifier.classify
    assert d.geometry_check is geometry.check and d.get_display_zones is geometry.get_display_zones
    assert d.perf_run_and_store is perf.run_and_store and d.perf_is_regression is perf.is_regression
    assert d.create_run is storage.create_run and d.finish_run is storage.finish_run
    assert rm.Deps.__dataclass_params__.frozen


def test_pid_alive_factory_uses_pidof_and_does_not_guess_on_adb_failure(monkeypatch):
    calls = []

    def fake_run(argv, **kw):
        calls.append((argv, kw["timeout"]))
        return fake_run.result
    monkeypatch.setattr(rm.subprocess, "run", fake_run)
    alive = rm._pid_alive_factory("emulator-5554", PACKAGE)
    fake_run.result = SimpleNamespace(stdout="1234\n", stderr="")
    assert alive() is True
    assert calls[0] == (["adb", "-s", "emulator-5554", "shell", "pidof", PACKAGE], rm.PIDOF_TIMEOUT_S)
    fake_run.result = SimpleNamespace(stdout="", stderr="")
    assert alive() is False                       # the device says there is no such process
    fake_run.result = SimpleNamespace(stdout="", stderr="error: device offline")
    assert alive() is True                        # unknown is not "gone"

    def boom(*a, **k):
        raise FileNotFoundError("adb")
    monkeypatch.setattr(rm.subprocess, "run", boom)
    assert alive() is True


def test_list_emulator_processes_matches_names_without_killing(monkeypatch):
    import psutil
    procs = [SimpleNamespace(info={"pid": 1, "name": "qemu-system-x86_64.exe"}),
             SimpleNamespace(info={"pid": 2, "name": "chrome.exe"}),
             SimpleNamespace(info={"pid": 3, "name": "emulator.exe"}),
             SimpleNamespace(info={"pid": 4, "name": None})]
    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: procs)
    assert rm._list_emulator_processes() == [{"pid": 1, "name": "qemu-system-x86_64.exe"},
                                             {"pid": 3, "name": "emulator.exe"}]


def test_appium_status_and_disk_defaults(monkeypatch):
    class Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
    seen = {}

    def fake_open(url, timeout):
        seen["url"], seen["timeout"] = url, timeout
        return Resp()
    monkeypatch.setattr(rm.urllib.request, "urlopen", fake_open)
    assert rm._appium_status() is True
    assert seen == {"url": "http://localhost:4723/status", "timeout": 5}

    def refused(url, timeout):
        raise ConnectionRefusedError("no appium")
    monkeypatch.setattr(rm.urllib.request, "urlopen", refused)
    assert rm._appium_status() is False
    monkeypatch.setattr(rm.shutil, "disk_usage", lambda p: SimpleNamespace(free=50 * 1024 ** 3))
    assert rm._free_disk_gb(".") == 50.0


def test_run_single_profile_keeps_its_old_call_shape():
    sig = inspect.signature(rm.run_single_profile)
    assert list(sig.parameters) == ["profile", "apk", "scenario", "port", "stub"]
    assert sig.parameters["port"].default == 5554 and sig.parameters["stub"].default is True
    assert callable(rm.dry_run)


def test_module_never_sleeps():
    src = Path(rm.__file__).read_text(encoding="utf-8")
    assert "time.sleep" not in src and "TODO" not in src and "NotImplementedError" not in src


# --- runner.run: new optional arguments -----------------------------------------------------------------------------------------------

class FakeDriver:
    def __init__(self):
        self.keys = []
        self.quit_called = False
        self.page_source = "<hierarchy/>"

    def press_keycode(self, code):
        self.keys.append(code)

    def get_screenshot_as_file(self, path):
        Path(path).write_bytes(b"png")

    def quit(self):
        self.quit_called = True


@pytest.fixture
def runner_env(tmp_path, monkeypatch):
    calls = SimpleNamespace(create=[], steps=[], driver=FakeDriver())
    monkeypatch.setattr(runner_mod.webdriver, "Remote", lambda url, options=None: calls.driver)
    monkeypatch.setattr(runner_mod.storage, "create_run",
                        lambda apk_name, scenario, profiles: calls.create.append((apk_name, scenario, list(profiles))) or 41)
    monkeypatch.setattr(runner_mod.storage, "save_step",
                        lambda run_id, profile, step: calls.steps.append((run_id, profile, step.step_index)))
    monkeypatch.setattr(runner_mod, "RESULTS_DIR", str(tmp_path / "evidence"))
    spec = tmp_path / "s.yaml"
    spec.write_text(yaml.safe_dump({"name": "S", "package": PACKAGE,
                                    "steps": [{"action": "home"}, {"action": "home"}]}), encoding="utf-8")
    calls.scenario = str(spec)
    calls.evidence = tmp_path / "evidence"
    return calls


def test_runner_old_call_shape_is_unchanged(runner_env):
    run_id, steps = runner_mod.run("emulator-5554", "x/app.apk", runner_env.scenario, 8200)
    assert run_id == 41 and len(steps) == 2
    assert runner_env.create == [("app.apk", "S", ["emulator-5554"])]
    assert runner_env.steps == [(41, "emulator-5554", 0), (41, "emulator-5554", 1)]
    assert (runner_env.evidence / "41" / "emulator-5554" / "step_00_hierarchy.xml").is_file()
    assert steps[0].hierarchy_path == f"{runner_env.evidence}/41/emulator-5554/step_00_hierarchy.xml"
    assert runner_env.driver.quit_called
    params = inspect.signature(runner_mod.run).parameters
    assert list(params)[:4] == ["serial", "apk", "scenario", "system_port"]
    assert params["run_id"].default is None and params["profile"].default is None
    assert params["record"].default is True


def test_runner_given_run_id_skips_create_run(runner_env):
    run_id, _ = runner_mod.run("emulator-5554", "app.apk", runner_env.scenario, 8200, run_id=900)
    assert run_id == 900 and runner_env.create == []
    assert (runner_env.evidence / "900" / "emulator-5554").is_dir()
    assert {s[0] for s in runner_env.steps} == {900}


def test_runner_profile_names_the_evidence_folder_and_save_step_profile(runner_env):
    _, steps = runner_mod.run("emulator-5554", "app.apk", runner_env.scenario, 8200,
                              run_id=900, profile="samsung_low_29")
    assert (runner_env.evidence / "900" / "samsung_low_29" / "step_01_screen.png").is_file()
    assert not (runner_env.evidence / "900" / "emulator-5554").exists()
    assert {s[1] for s in runner_env.steps} == {"samsung_low_29"}
    assert "samsung_low_29" in steps[0].screenshot_path


def test_runner_record_false_stores_and_writes_nothing(runner_env):
    run_id, steps = runner_mod.run("emulator-5554", "app.apk", runner_env.scenario, 8200,
                                   run_id=900, profile="p", record=False)
    assert run_id is None and len(steps) == 2 and all(s.status == "pass" for s in steps)
    assert runner_env.create == [] and runner_env.steps == []
    assert not runner_env.evidence.exists()
    assert steps[0].screenshot_path == "" and steps[0].hierarchy_path == ""
    assert runner_env.driver.quit_called and runner_env.driver.keys == [3, 3]  # the steps really ran


def test_run_scenario_passes_the_new_arguments_through(runner_env):
    run_id, steps = runner_mod.run_scenario("emulator-5554", "app.apk", runner_env.scenario, 8200,
                                            run_id=5, profile="px")
    assert run_id == 5 and runner_env.steps[0][:2] == (5, "px")
    run_id, _ = runner_mod.run_scenario("emulator-5554", "app.apk", runner_env.scenario, 8200, record=False)
    assert run_id is None


def test_run_scenario_dry_run_honours_the_new_arguments(runner_env):
    run_id, steps = runner_mod.run_scenario("emulator-5554", "app.apk", "stub", dry_run=True,
                                            run_id=8, profile="pp")
    assert run_id == 8 and runner_env.create == [] and runner_env.steps == [(8, "pp", 0)]
    run_id, _ = runner_mod.run_scenario("emulator-5554", "app.apk", "stub", dry_run=True, record=False)
    assert run_id is None and len(runner_env.steps) == 1
    run_id, _ = runner_mod.run_scenario("emulator-5554", "app.apk", "stub", dry_run=True)
    assert run_id == 41 and runner_env.steps[-1] == (41, "emulator-5554", 0)  # old shape unchanged
