"""Tests for the M10 orchestrator (vmf.orchestrator.run_matrix). No device, no Appium.

Every external call is a thread-safe recording fake passed through ``Deps``. The
fakes are strict on purpose: a console port can only be started once until it is
stopped, every save_* and finish_run needs an existing run row (foreign key), and
an orphan stop can be made to fail. Nothing sleeps: concurrency is proved with a
Barrier or an Event, ordering with a shared event log, and tests that could
deadlock run ``main`` under a deadline.
"""
import inspect
import json
import logging
import sqlite3
import sys
import threading
import time
from concurrent.futures import CancelledError, Future
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from vmf import classifier
from vmf.orchestrator import run_matrix as rm
from vmf.perf.perf import Thresholds
from vmf.results import Finding, PerfRow, Step, Verdict
from vmf.runner import runner as runner_mod
from vmf.runner.locator_fallback import FallbackConfig

PACKAGE = "com.example.testapp"
GLOBAL_KINDS = {"create_run", "finish_run", "appium", "disk", "procs", "kill", "run_exists"}
FUTURE = time.time() + 10_000          # a create_time later than any run start
PAST = 1.0                              # a create_time earlier than any run start


class Boom(BaseException):
    """A BaseException that is neither KeyboardInterrupt nor SystemExit."""


class FakeHostSampler:
    """Context manager with HostSampler's surface; ``busy`` and ``cpu`` are set by the test."""
    busy = False
    cpu = 5.0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def noisy(self):
        return type(self).busy

    def summary(self):
        return {"samples": 3, "cpu_pct": {"min": 1.0, "mean": 2.0, "max": 3.0},
                "cpu_outside_emulator_pct": {"min": self.cpu, "mean": self.cpu, "max": self.cpu},
                "free_ram_min_bytes": 1, "noisy_threshold_pct": 30}


class Fakes:
    """Thread-safe, strict recording fakes for every Deps callable."""

    def __init__(self, tmp_path, *, verdicts=None, fail=None, steps_for=None,
                 findings_for=None, barrier_parties=0, perf_regress=(), hold_perf=False,
                 runs=(77,)):
        self.tmp = tmp_path
        self.lock = threading.Lock()
        self.events = []                 # (kind, name, extra)
        self.saved = {}                  # name -> [Verdict]
        self.configs = {}
        self.serial_name = {}
        self.returned = set()            # serials start_device actually returned
        self.occupied = set()            # console ports with a live or half-started emulator
        self.runs = set(runs)
        self.verdicts = verdicts or {}
        self.fail = fail or {}           # (stage, name) -> exception; "start" raises AFTER occupying
        self.steps_for = steps_for
        self.flow_steps_for = None       # steps returned to the unrecorded perf flow
        self.findings_for = findings_for or (lambda xml: [])
        self.barrier = threading.Barrier(barrier_parties) if barrier_parties else None
        self.perf_regress = set(perf_regress)
        self.hold_perf = hold_perf
        self.runner_calls = 0
        self.active = 0
        self.peak = 0
        self.perf_active = 0
        self.perf_peak = 0
        self.port_clashes = []
        self.status_ok = True
        self.disk_gb = 100.0
        self.orphans = []
        self.leftover = []
        self.proc_calls = 0
        self.listing_hook = None         # callable(call_no) -> list | None (may raise)
        self.proc_info = {}              # pid -> {"pid", "name", "create_time"}
        self.orphan_stop_fails = False
        self.gate = None                 # Event the first runner call waits on
        self.runner_started = threading.Event()
        self.geometry_configs = []
        self.save_error_fails = False
        self.run_exists_answer = "auto"  # "auto" | True | False | None
        self.fallback_args = []           # the locator_fallback kwarg of every runner_run call
        self.heal_counts = {}             # profile -> healed steps the fake runner reports (M14)
        self.unresolved_counts = {}       # profile -> unresolved steps the fake runner reports (M14)
        self.host_load = {}               # what the fake perf run reports as host load (perf_meta.json)
        self.perf_median = {}             # profile -> launch_time median the fake perf run returns
        self.perf_flags = {}              # profile -> (noisy, unstable) the fake perf run returns

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

    def _need_run(self, run_id):
        if run_id not in self.runs:
            raise sqlite3.IntegrityError("FOREIGN KEY constraint failed")

    # -- Deps callables -----------------------------------------------------
    def start_device(self, profile, port):
        name = rm.profile_name(profile)
        self.ev("start", name, port=port)
        with self.lock:
            if port in self.occupied:
                self.port_clashes.append(port)
                raise RuntimeError(f"console port {port} is already in use")
            self.occupied.add(port)
            self.serial_name[f"emulator-{port}"] = name
        self._maybe_fail("start", name)       # raises after the port is occupied
        with self.lock:
            self.returned.add(f"emulator-{port}")
        return f"emulator-{port}"

    def stop_device(self, serial):
        port = int(serial.split("-")[1])
        name = self.serial_name.get(serial)
        self.ev("stop", name, port=port, orphan=serial not in self.returned)
        if serial not in self.returned and self.orphan_stop_fails:
            raise RuntimeError(f"cannot stop {serial}")
        self._maybe_fail("stop", name)
        with self.lock:
            self.occupied.discard(port)
            self.returned.discard(serial)   # a later start on this port begins from scratch

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
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text("", encoding="utf-8")
        return SimpleNamespace(name=name)

    def stop_logcat(self, proc):
        self.ev("logcat_stop", proc.name)
        self._maybe_fail("logcat_stop", proc.name)

    def runner_run(self, serial, apk, scenario, system_port, run_id=None, profile=None,
                   record=True, reuse_installed_app=False, mjpeg_port=None,
                   chromedriver_port=None, locator_fallback=None):
        name = self.serial_name[serial]
        with self.lock:
            self.fallback_args.append(locator_fallback)
        if locator_fallback is not None and (self.heal_counts.get(name) or self.unresolved_counts.get(name)):
            side = Path(rm.RESULTS_DIR) / str(run_id) / name
            side.mkdir(parents=True, exist_ok=True)
            (side / "locator_fallbacks.json").write_text(
                json.dumps({"healed": [{"step": i} for i in range(self.heal_counts.get(name, 0))],
                            "unresolved": [{"step": i} for i in range(self.unresolved_counts.get(name, 0))]}),
                encoding="utf-8")
        self.ev("runner", name, system_port=system_port, run_id=run_id, profile=profile,
                record=record, reuse=reuse_installed_app, mjpeg=mjpeg_port,
                chromedriver=chromedriver_port)
        if record:
            self._need_run(run_id)
        with self.lock:
            self.runner_calls += 1
            ticket = self.runner_calls
            self.active += 1
            self.peak = max(self.peak, self.active)
        self.runner_started.set()
        try:
            if self.gate is not None and ticket == 1:
                self.gate.wait(timeout=10)
            if self.barrier is not None and ticket <= self.barrier.parties:
                self.barrier.wait(timeout=10)
            self._maybe_fail("runner", name)
            if not record and self.flow_steps_for:
                steps = self.flow_steps_for(name)
            elif self.steps_for:
                steps = self.steps_for(name)
            else:
                steps = [Step(0, "launch", "pass", 0, "", "")]
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
        self.ev("perf", profile_name, run_id=run_id, kw=dict(kw), profile=profile)
        with self.lock:
            self.perf_active += 1
            self.perf_peak = max(self.perf_peak, self.perf_active)
        try:
            if self.hold_perf:
                threading.Event().wait(0.05)  # lets a (wrong) parallel pass overlap
            self._maybe_fail("perf", profile_name)
            kw["flow"]()
            reg = 50.0 if profile_name in self.perf_regress else 1.0
            out = kw.get("host_load_out")
            if out is not None:
                out.update(self.host_load)
            noisy, unstable = self.perf_flags.get(profile_name, (False, False))
            return [PerfRow("launch_time", self.perf_median.get(profile_name, 1000.0), 1.0,
                            kw.get("baselines", {}) and kw["baselines"].get("launch_time", 900.0) or 900.0,
                            reg, noisy, unstable)]
        finally:
            with self.lock:
                self.perf_active -= 1

    def perf_is_regression(self, row, thresholds):
        return row.regression_pct is not None and row.regression_pct > thresholds.launch_regression_pct

    def create_run(self, apk_name, scenario, profiles):
        self.ev("create_run", None, apk=apk_name, scenario=scenario, profiles=list(profiles))
        with self.lock:
            self.runs.add(77)
        return 77

    def save_device_result(self, run_id, profile, verdict):
        self._need_run(run_id)
        if self.save_error_fails and verdict.result == "error":
            raise RuntimeError("db locked")
        self.ev("save_result", profile, result=verdict.result)
        with self.lock:
            self.saved.setdefault(profile, []).append(verdict)

    def save_profile_config(self, run_id, profile, config):
        self._need_run(run_id)
        self.ev("save_config", profile)
        with self.lock:
            self.configs[profile] = config

    def finish_run(self, run_id):
        self._need_run(run_id)
        self.ev("finish_run", None, run_id=run_id)

    def pid_alive_factory(self, serial, package, on_failure=None):
        return lambda: True

    def list_emulator_processes(self):
        with self.lock:
            self.proc_calls += 1
            n = self.proc_calls
        self.ev("procs", None)
        if self.listing_hook is not None:
            answer = self.listing_hook(n)
            if answer is not None:
                return list(answer)
        return list(self.orphans if n == 1 else self.leftover)

    def process_info(self, pid):
        return self.proc_info.get(pid)

    def appium_status(self):
        self.ev("appium", None)
        return self.status_ok

    def free_disk_gb(self, path):
        self.ev("disk", None)
        return self.disk_gb

    def kill_process(self, pid):
        self.ev("kill", None, pid=pid)

    def run_exists(self, run_id):
        self.ev("run_exists", None)
        if self.run_exists_answer == "auto":
            return run_id in self.runs
        return self.run_exists_answer

    def deps(self, **overrides):
        d = rm.Deps(
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
            kill_process=self.kill_process, process_info=self.process_info,
            run_exists=self.run_exists, host_sampler=FakeHostSampler)
        return replace(d, **overrides) if overrides else d


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


def go(fakes, scenario, profiles, run_id=77, deps=None, **opts):
    options = {"apk": "C:/builds/app.apk", "scenario": scenario, "profiles": profiles,
               "performance_pass": False, "slot_poll_s": 0.01, **opts}
    return rm.main(run_id, options, deps=deps or fakes.deps())


def one(fakes, scenario, **kw):
    summary = go(fakes, scenario, make_profiles(1), **kw)
    return summary["profiles"][0], summary


def run_with_deadline(fn, stop=None, seconds=20):
    """Run ``fn`` in a thread; fail (instead of hanging pytest) if it deadlocks."""
    box = {}

    def target():
        try:
            box["value"] = fn()
        except BaseException as e:
            box["error"] = e
    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        if stop is not None:
            stop.set()  # frees workers blocked waiting for a slot
        t.join(5)
        pytest.fail("deadlock: main() did not return within the deadline")
    if "error" in box:
        raise box["error"]
    return box["value"]


def hierarchy_steps(tmp_path):
    f = tmp_path / "h.xml"
    f.write_text("<hierarchy/>", encoding="utf-8")
    return lambda name: [Step(0, "tap", "pass", 1, "", str(f))]


# --- per-profile pipeline ----------------------------------------------------------------------------

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
    assert runner_extra == {"system_port": 8200, "run_id": 77, "profile": "p0", "record": True,
                            "reuse": False, "mjpeg": 7810, "chromedriver": 9515}
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
    assert saved["pid_check_failed"] is False


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
    assert fakes.occupied == set()  # the strict fake: nothing left running


@pytest.mark.parametrize("stage, label", [
    ("apply", "apply_profile_settings"), ("runner", "runner"), ("classify", "classify"),
    ("logcat_start", "logcat"), ("logcat_stop", "stop_logcat"),
])
def test_error_cause_names_the_stage(tmp_path, scenario, stage, label):
    fakes = Fakes(tmp_path, fail={(stage, "p0"): ValueError("x")})
    result, _ = one(fakes, scenario)
    assert f"(stage: {label})" in result["cause"]


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
    assert any(r.exc_info and "reset broke" in str(r.exc_info[1]) for r in caplog.records)


def test_a_hung_cleanup_step_times_out_and_is_recorded(caplog):
    release, errors = threading.Event(), []
    with caplog.at_level(logging.ERROR):
        ok = rm._guarded("stop_device", lambda: release.wait(10), errors, timeout=0.05)
    release.set()
    assert ok is False and errors == ["stop_device: timed out after 0.05s"]
    assert any("timed out" in r.message for r in caplog.records)


def test_guarded_returns_true_on_success_and_logs_failures_with_traceback(caplog):
    errors = []
    assert rm._guarded("fine", lambda: None, errors) is True and errors == []
    with caplog.at_level(logging.ERROR):
        assert rm._guarded("bad", lambda: 1 / 0, errors) is False
    assert errors == ["bad: ZeroDivisionError: division by zero"]
    assert any(r.exc_info and r.exc_info[0] is ZeroDivisionError for r in caplog.records)


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


# --- failed start_device: orphan cleanup and quarantine -----------------------------------------------------

def test_failed_start_stops_the_device_derived_from_the_port(tmp_path, scenario):
    fakes = Fakes(tmp_path, fail={("start", "p0"): OSError("boot timeout")})
    result, summary = one(fakes, scenario)
    assert fakes.kinds() == ["start", "save_result", "stop"]
    ((_, stop),) = fakes.extras("stop")
    assert stop == {"port": 5554, "orphan": True}  # emulator-5554, never returned by start_device
    assert result["serial"] is None and "(stage: start_device)" in result["cause"]
    assert fakes.occupied == set() and summary["quarantined_ports"] == []


def test_successful_orphan_cleanup_lets_the_next_profile_reuse_the_slot(tmp_path, scenario):
    fakes = Fakes(tmp_path, fail={("start", "p0"): OSError("boot timeout")})
    summary = go(fakes, scenario, make_profiles(2), max_parallel=1)
    assert [e["port"] for _, e in fakes.extras("start")] == [5554, 5554]
    assert summary["counts"] == {"error": 1, "pass": 1} and fakes.port_clashes == []
    assert summary["quarantined_ports"] == []


def test_failed_orphan_cleanup_quarantines_the_slot_and_the_next_profile_uses_another(tmp_path, scenario):
    fakes = Fakes(tmp_path, fail={("start", "p0"): OSError("boot timeout")})
    fakes.orphan_stop_fails = True
    summary = go(fakes, scenario, make_profiles(3), max_parallel=2)
    bad_port = [e["port"] for n, e in fakes.extras("start") if n == "p0"][0]
    other_port = 5554 + 5556 - bad_port
    assert summary["quarantined_ports"] == [bad_port]
    assert [e["port"] for n, e in fakes.extras("start") if n != "p0"] == [other_port] * 2
    assert fakes.port_clashes == []  # the quarantined port was never started again
    by = {r["profile"]: r for r in summary["profiles"]}
    assert by["p0"]["result"] == "error" and by["p0"]["quarantine"] is True
    assert by["p1"]["result"] == "pass" and by["p2"]["result"] == "pass"
    assert any("cannot stop" in e for e in by["p0"]["cleanup_errors"])


def test_all_slots_quarantined_errors_the_rest_without_deadlock(tmp_path, scenario):
    fakes = Fakes(tmp_path, fail={("start", "p0"): OSError("boot timeout")})
    fakes.orphan_stop_fails = True
    stop = threading.Event()
    summary = run_with_deadline(
        lambda: go(fakes, scenario, make_profiles(3), max_parallel=1, stop_event=stop), stop)
    by = {r["profile"]: r for r in summary["profiles"]}
    assert by["p0"]["result"] == "error"
    for name in ("p1", "p2"):
        assert by[name]["result"] == "error"
        assert "no usable slot (quarantined: 5554)" in by[name]["cause"]
        assert fakes.saved[name][0].result == "error"
    assert fakes.count("start") == 1 and summary["quarantined_ports"] == [5554]
    assert fakes.count("finish_run") == 1


def test_a_new_emulator_process_in_a_sequential_run_quarantines_the_slot(tmp_path, scenario):
    fakes = Fakes(tmp_path, fail={("start", "p0"): OSError("boot timeout")})
    ghost = {"pid": 4242, "name": "qemu-system-x86_64.exe", "create_time": FUTURE, "cmdline": ""}
    fakes.listing_hook = lambda n: [ghost] if fakes.count("stop") >= 1 else None
    summary = go(fakes, scenario, make_profiles(1))
    assert summary["quarantined_ports"] == [5554]


@pytest.mark.parametrize("attributed, quarantined", [(True, True), (False, False)])
def test_in_a_parallel_run_only_a_process_naming_the_slots_port_is_attributed(
        tmp_path, scenario, attributed, quarantined):
    fakes = Fakes(tmp_path, fail={("start", "p0"): OSError("boot timeout")})

    def hook(n):
        if fakes.count("stop") < 1:
            return None
        p0_port = [e["port"] for name, e in fakes.extras("start") if name == "p0"][0]
        port = p0_port if attributed else p0_port + 100
        return [{"pid": 4242, "name": "qemu-system-x86_64.exe", "create_time": FUTURE,
                 "cmdline": f"emulator -avd x -port {port}"}]
    fakes.listing_hook = hook
    summary = go(fakes, scenario, make_profiles(2), max_parallel=2)
    p0_port = [e["port"] for name, e in fakes.extras("start") if name == "p0"][0]
    assert summary["quarantined_ports"] == ([p0_port] if quarantined else [])


def test_unlistable_processes_after_a_failed_start_quarantine_the_slot(tmp_path, scenario, caplog):
    fakes = Fakes(tmp_path, fail={("start", "p0"): OSError("boot timeout")})

    def hook(n):
        if fakes.count("stop") >= 1:
            raise RuntimeError("psutil failed")
    fakes.listing_hook = hook
    with caplog.at_level(logging.ERROR):
        summary = go(fakes, scenario, make_profiles(1))
    assert summary["quarantined_ports"] == [5554]
    assert any(r.exc_info and "psutil failed" in str(r.exc_info[1]) for r in caplog.records)


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


def test_a_worker_bug_becomes_an_error_record_and_does_not_leak_its_slot(tmp_path, scenario, monkeypatch):
    """The fake does NOT release the slot: the caller (_functional_task) owns it. With
    one slot, a leak would block profile p2 forever."""
    fakes = Fakes(tmp_path)
    real = rm.run_profile

    def buggy(profile, ctx, slot):
        if profile["name"] == "p1":
            raise RuntimeError("worker bug before the try")
        return real(profile, ctx, slot)
    monkeypatch.setattr(rm, "run_profile", buggy)
    stop = threading.Event()
    summary = run_with_deadline(
        lambda: go(fakes, scenario, make_profiles(3), max_parallel=1, stop_event=stop), stop)
    assert summary["counts"] == {"pass": 2, "error": 1}
    saved = fakes.saved["p1"][0]
    assert saved.result == "error" and "(stage: worker)" in saved.cause


def test_run_profile_does_not_release_the_slot_itself(tmp_path, scenario):
    fakes = Fakes(tmp_path)
    pool = rm.SlotPool(1)
    ctx = rm.RunContext(deps=fakes.deps(), run_id=77, apk="a.apk", scenario=scenario,
                        package=PACKAGE, stop_event=threading.Event())
    slot = pool.acquire()
    rm.run_profile(make_profiles(1)[0], ctx, slot)
    assert pool.release(slot) is True   # still held after run_profile returned
    assert pool.release(slot) is False  # and release is idempotent


# --- interrupts and BaseException ---------------------------------------------------------------------------

def _interrupt_after_first_submit(monkeypatch, fakes):
    """Make the main thread see a KeyboardInterrupt while p0 is still running, and
    release p0 only once the executor is being shut down."""
    fakes.gate = threading.Event()

    def interrupting(futures):
        assert fakes.runner_started.wait(10)
        raise KeyboardInterrupt
        yield  # pragma: no cover - makes this a generator
    monkeypatch.setattr(rm, "as_completed", interrupting)
    real = rm._shutdown_executor

    def shutdown_and_release(executor):
        fakes.gate.set()
        real(executor)
    monkeypatch.setattr(rm, "_shutdown_executor", shutdown_and_release)


def test_keyboard_interrupt_mid_matrix_cancels_queued_profiles_and_still_finishes(tmp_path, scenario, monkeypatch):
    fakes = Fakes(tmp_path)
    _interrupt_after_first_submit(monkeypatch, fakes)
    stop = threading.Event()
    with pytest.raises(KeyboardInterrupt):
        run_with_deadline(lambda: go(fakes, scenario, make_profiles(4), max_parallel=1,
                                     stop_event=stop), stop)
    assert fakes.count("start") == 1                      # the queued profiles never booted
    assert fakes.kinds()[-2:] == ["reset", "stop"]        # the running one cleaned up
    assert fakes.saved["p0"][0].result == "pass"
    assert sorted(fakes.saved) == ["p0"]
    assert fakes.count("finish_run") == 1                 # exactly once, before the re-raise
    assert fakes.count("procs") >= 3                      # pre-flight, before start, leftover check
    assert fakes.occupied == set()


def test_keyboard_interrupt_still_runs_the_leftover_check(tmp_path, scenario, monkeypatch, caplog):
    fakes = Fakes(tmp_path)
    fakes.leftover = [{"pid": 31, "name": "qemu-system-x86_64.exe", "create_time": FUTURE, "cmdline": ""}]
    _interrupt_after_first_submit(monkeypatch, fakes)
    stop = threading.Event()
    with caplog.at_level(logging.WARNING), pytest.raises(KeyboardInterrupt):
        run_with_deadline(lambda: go(fakes, scenario, make_profiles(3), max_parallel=1,
                                     stop_event=stop), stop)
    assert any("pid 31" in r.message for r in caplog.records)


def test_base_exception_in_a_worker_cleans_up_records_an_error_and_is_reraised_at_the_end(tmp_path, scenario):
    fakes = Fakes(tmp_path, fail={("classify", "p1"): Boom("worker died")})
    stop = threading.Event()
    with pytest.raises(Boom):
        run_with_deadline(lambda: go(fakes, scenario, make_profiles(3), max_parallel=1,
                                     stop_event=stop), stop)
    # the finally cleaned up first; only then does the task record the error and re-raise
    assert fakes.kinds("p1")[-3:] == ["reset", "stop", "save_result"]
    assert fakes.saved["p1"][0].result == "error" and "Boom" in fakes.saved["p1"][0].cause
    assert fakes.saved["p0"][0].result == "pass" and fakes.saved["p2"][0].result == "pass"
    assert fakes.count("finish_run") == 1 and fakes.occupied == set()


def test_system_exit_in_a_worker_stops_further_profiles(tmp_path, scenario):
    fakes = Fakes(tmp_path, fail={("classify", "p0"): SystemExit(3)})
    stop = threading.Event()
    with pytest.raises(SystemExit):
        run_with_deadline(lambda: go(fakes, scenario, make_profiles(3), max_parallel=1,
                                     stop_event=stop), stop)
    assert fakes.count("start") == 1 and fakes.count("finish_run") == 1
    assert fakes.saved["p0"][0].result == "error"


def test_keyboard_interrupt_during_the_performance_pass_cleans_up_and_cancels_the_rest(tmp_path, scenario):
    fakes = Fakes(tmp_path, fail={("perf", "p1"): KeyboardInterrupt()})
    stop = threading.Event()
    with pytest.raises(KeyboardInterrupt):
        run_with_deadline(lambda: go(fakes, scenario, make_profiles(3), performance_pass=True,
                                     stop_event=stop), stop)
    assert [n for n, _ in fakes.extras("perf")] == ["p0", "p1"]      # p2 never ran
    # 3 functional starts + perf p0 + perf p1; every one of them was stopped
    assert fakes.count("start") == fakes.count("stop") == 5
    assert fakes.count("finish_run") == 1 and fakes.occupied == set()


def test_collect_future_handles_cancelled_and_failed_futures(tmp_path, scenario, caplog):
    fakes = Fakes(tmp_path)
    ctx = rm.RunContext(deps=fakes.deps(), run_id=77, apk="a", scenario=scenario, package=PACKAGE,
                        stop_event=threading.Event())
    profile = make_profiles(1)[0]
    cancelled = Future()
    assert cancelled.cancel()
    assert rm._collect_future(ctx, cancelled, profile)[0]["result"] == "cancelled"
    raises_cancel = Future()
    raises_cancel.set_exception(CancelledError())
    assert rm._collect_future(ctx, raises_cancel, profile)[0]["result"] == "cancelled"
    failed = Future()
    failed.set_exception(RuntimeError("bug"))
    with caplog.at_level(logging.ERROR):
        result, base = rm._collect_future(ctx, failed, profile)
    assert result["result"] == "error" and base is None
    assert fakes.saved["p0"][0].result == "error"
    assert any(r.exc_info for r in caplog.records)
    dead = Future()
    dead.set_exception(Boom("x"))
    result, base = rm._collect_future(ctx, dead, profile)
    assert result["result"] == "error" and isinstance(base, Boom)


def test_executor_shutdown_cancels_futures_and_survives_a_second_interrupt(caplog):
    class FakeExecutor:
        def __init__(self):
            self.calls = []

        def shutdown(self, wait=True, cancel_futures=False):
            self.calls.append((wait, cancel_futures))
            if len(self.calls) == 1:
                raise KeyboardInterrupt
    ex = FakeExecutor()
    with caplog.at_level(logging.WARNING):
        rm._shutdown_executor(ex)
    assert ex.calls == [(True, True), (False, True)]
    assert any("second interrupt" in r.message for r in caplog.records)


def test_a_real_executor_shutdown_drops_queued_tasks():
    from concurrent.futures import ThreadPoolExecutor
    started, gate, ran = threading.Event(), threading.Event(), []

    def work(i):
        ran.append(i)
        started.set()
        gate.wait(10)
    ex = ThreadPoolExecutor(max_workers=1)
    futs = [ex.submit(work, i) for i in range(4)]
    assert started.wait(10)
    threading.Timer(0.05, gate.set).start()
    rm._shutdown_executor(ex)
    assert ran == [0] and [f.cancelled() for f in futs] == [False, True, True, True]


def test_stop_event_set_between_acquire_and_start_cancels_the_profile_and_frees_the_slot(tmp_path, scenario, monkeypatch):
    stop = threading.Event()
    real = rm.SlotPool.acquire

    def acquire_then_stop(self, should_stop=None, poll_s=rm.SLOT_POLL_S):
        slot = real(self, should_stop, poll_s)
        stop.set()                      # the stop arrives right after the slot is taken
        return slot
    monkeypatch.setattr(rm.SlotPool, "acquire", acquire_then_stop)
    fakes = Fakes(tmp_path)
    summary = run_with_deadline(
        lambda: go(fakes, scenario, make_profiles(3), max_parallel=1, stop_event=stop), stop)
    assert fakes.count("start") == 0 and summary["counts"] == {"cancelled": 3}


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
    runner_calls = {n: e for n, e in fakes.extras("runner")}
    assert sorted(ports.values()) == [5554, 5556, 5558]
    assert sorted(e["system_port"] for e in runner_calls.values()) == [8200, 8201, 8202]
    assert sorted(e["mjpeg"] for e in runner_calls.values()) == [7810, 7811, 7812]
    assert sorted(e["chromedriver"] for e in runner_calls.values()) == [9515, 9516, 9517]
    for name, port in ports.items():
        n = (port - 5554) // 2
        assert runner_calls[name]["system_port"] == 8200 + n
        assert runner_calls[name]["mjpeg"] == 7810 + n and runner_calls[name]["chromedriver"] == 9515 + n


def test_slot_pool_ports_reuse_and_release_rules():
    pool = rm.SlotPool(3)
    slots = [pool.acquire() for _ in range(3)]
    assert [(s.index, s.port, s.system_port) for s in slots] == [
        (0, 5554, 8200), (1, 5556, 8201), (2, 5558, 8202)]
    assert (slots[2].mjpeg_port, slots[2].chromedriver_port) == (7812, 9517)
    with pytest.raises(rm.SlotWaitCancelled):
        pool.acquire(should_stop=lambda: True)  # a stop request ends the wait
    assert pool.release(slots[1]) is True
    again = pool.acquire()
    assert again.index == 1 and again.port == 5556  # a released slot is reused
    assert pool.release(again) is True and pool.release(again) is False  # idempotent
    with pytest.raises(ValueError):
        rm.SlotPool(0)


def test_slot_pool_rejects_more_than_sixteen_slots_with_the_adb_reason():
    assert rm.SlotPool(16).size == 16
    with pytest.raises(ValueError, match="5585"):
        rm.SlotPool(17)
    assert rm.MAX_SLOTS == 16


def test_acquire_never_blocks_forever_it_watches_the_stop_request():
    pool = rm.SlotPool(1)
    held = pool.acquire()
    stop = threading.Event()
    out = {}

    def waiter():
        try:
            pool.acquire(stop.is_set, poll_s=0.01)
        except rm.SlotWaitCancelled:
            out["cancelled"] = True
    t = threading.Thread(target=waiter)
    t.start()
    stop.set()
    t.join(10)
    assert out == {"cancelled": True} and not t.is_alive()
    pool.release(held)


def test_a_quarantined_slot_never_comes_back_and_exhaustion_raises():
    pool = rm.SlotPool(2)
    a, b = pool.acquire(), pool.acquire()
    pool.quarantine(a)
    assert pool.release(a) is False                   # a quarantined slot can never return
    pool.release(b)
    assert pool.acquire(poll_s=0.01).index == b.index  # one usable slot is left
    single = rm.SlotPool(1)
    only = single.acquire()
    single.quarantine(only)
    with pytest.raises(rm.NoUsableSlot, match=r"no usable slot \(quarantined: 5554\)"):
        single.acquire(poll_s=0.01)
    assert single.quarantined_ports() == [5554]


def test_lease_releases_exactly_once_even_when_the_body_raises():
    pool = rm.SlotPool(1)
    with pytest.raises(RuntimeError):
        with pool.lease() as slot:
            raise RuntimeError("body failed")
    deadline = time.monotonic() + 2   # a leaked slot must fail this test, not hang it
    again = pool.acquire(lambda: time.monotonic() > deadline, 0.01)
    assert again.index == slot.index
    pool.release(again)


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


def test_pass_b_arguments_are_compared_by_value_per_profile(tmp_path, scenario):
    fakes = Fakes(tmp_path)
    thresholds = Thresholds(launch_regression_pct=33.0)
    baselines = {"p0": {"launch_time": 900.0}, "p1": {"launch_time": 1800.0, "pss": 5.0}}
    profiles = make_profiles(2)
    go(fakes, scenario, profiles, performance_pass=True, baselines=baselines, thresholds=thresholds)
    perf_calls = {n: e for n, e in fakes.extras("perf")}
    for name, profile in zip(("p0", "p1"), profiles):
        e = perf_calls[name]
        assert e["run_id"] == 77 and e["profile"] == profile
        kw = e["kw"]
        assert kw["package"] == PACKAGE and kw["apk"] == "C:/builds/app.apk"
        assert kw["baselines"] == baselines[name]            # that exact profile's baselines
        assert kw["thresholds"] is thresholds                # the thresholds that were passed
        assert callable(kw["flow"])
    assert perf_calls["p0"]["kw"]["baselines"] != perf_calls["p1"]["kw"]["baselines"]


def test_pass_b_missing_baselines_are_none(tmp_path, scenario):
    fakes = Fakes(tmp_path)
    go(fakes, scenario, make_profiles(1), performance_pass=True)
    (_, e), = fakes.extras("perf")
    assert e["kw"]["baselines"] is None and e["kw"]["thresholds"] == Thresholds()


def test_perf_flow_is_unrecorded_reuses_the_installed_app_and_uses_slot_ports(tmp_path, scenario):
    fakes = Fakes(tmp_path)
    go(fakes, scenario, make_profiles(1), performance_pass=True)
    flow_calls = [e for n, e in fakes.extras("runner") if not e["record"]]
    assert flow_calls == [{"system_port": 8200, "run_id": None, "profile": "p0", "record": False,
                           "reuse": True, "mjpeg": 7810, "chromedriver": 9515}]
    functional = [e for n, e in fakes.extras("runner") if e["record"]]
    assert functional[0]["reuse"] is False


def test_perf_flow_with_a_failed_step_is_a_perf_error_and_the_pass_continues(tmp_path, scenario, results_dir):
    fakes = Fakes(tmp_path)
    fakes.flow_steps_for = lambda name: (
        [Step(0, "tap", "pass", 1, "", ""), Step(1, "tap", "fail", 1, "", "")]
        if name == "p0" else [Step(0, "tap", "pass", 1, "", "")])
    summary = go(fakes, scenario, make_profiles(2), performance_pass=True)
    perf_by = {p["profile"]: p for p in summary["performance"]}
    assert perf_by["p0"]["status"] == "error"
    assert "ScenarioFailed: scenario steps [1] failed on profile p0" in perf_by["p0"]["error"]
    assert perf_by["p1"]["status"] == "ok"                       # the pass continues
    assert summary["counts"] == {"pass": 2}                      # functional results untouched
    assert fakes.count("start") == fakes.count("stop") == 4      # cleaned up
    assert [e["profile"] for e in summary["perf_errors"]] == ["p0"]
    assert (results_dir / "77" / "p0" / "perf_error.json").is_file()


def test_perf_flow_function_raises_scenario_failed_directly(tmp_path, scenario):
    fakes = Fakes(tmp_path, steps_for=lambda name: [Step(3, "tap", "fail", 1, "", "")])
    ctx = rm.RunContext(deps=fakes.deps(), run_id=77, apk="a", scenario=scenario, package=PACKAGE,
                        stop_event=threading.Event())
    fakes.serial_name["emulator-5554"] = "p0"
    with pytest.raises(rm.ScenarioFailed, match=r"\[3\]"):
        rm._perf_flow(ctx, "emulator-5554", rm.Slot(0, 5554, 8200), "p0")


def test_pass_b_error_is_recorded_written_to_disk_and_does_not_stop_the_pass(tmp_path, scenario, results_dir):
    fakes = Fakes(tmp_path, fail={("perf", "p0"): RuntimeError("perf broke")})
    summary = go(fakes, scenario, make_profiles(2), performance_pass=True)
    perf_by = {p["profile"]: p for p in summary["performance"]}
    assert perf_by["p0"]["status"] == "error" and "perf broke" in perf_by["p0"]["error"]
    assert perf_by["p1"]["status"] == "ok"
    assert summary["counts"] == {"pass": 2}  # functional results untouched
    assert fakes.count("stop") == fakes.count("start") == 4
    data = json.loads((results_dir / "77" / "p0" / "perf_error.json").read_text(encoding="utf-8"))
    assert data["stage"] == "measure" and "perf broke" in data["error"] and "Traceback" in data["traceback"]
    assert summary["perf_errors"] == [{"profile": "p0", "error": perf_by["p0"]["error"],
                                       "file": perf_by["p0"]["error_file"]}]


def test_pass_b_start_failure_cleans_up_by_port_and_quarantines_when_it_cannot(tmp_path, scenario):
    fakes = Fakes(tmp_path)
    fakes.orphan_stop_fails = True
    calls = {"n": 0}

    def start(profile, port):
        calls["n"] += 1
        if calls["n"] == 3:                       # the first Pass B start (after two Pass A starts)
            fakes.fail[("start", rm.profile_name(profile))] = OSError("perf boot")
        return fakes.start_device(profile, port)
    summary = go(fakes, scenario, make_profiles(2), performance_pass=True,
                 deps=fakes.deps(start_device=start))
    perf_by = {p["profile"]: p for p in summary["performance"]}
    assert perf_by["p0"]["status"] == "error" and "perf boot" in perf_by["p0"]["error"]
    assert perf_by["p1"]["status"] == "error" and "no usable slot" in perf_by["p1"]["error"]
    assert fakes.port_clashes == []


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


# --- the real pid_alive factory with the real classifier ----------------------------------------------------------------

def _real_pid_deps(fakes):
    return fakes.deps(pid_alive_factory=rm._pid_alive_factory, classify=classifier.classify)


def _fake_adb(monkeypatch, *, stdout="", stderr="", raises=None):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        if raises is not None:
            raise raises
        return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=0)
    monkeypatch.setattr(rm.subprocess, "run", run)
    return calls


def test_real_pidof_with_empty_output_makes_the_real_classifier_report_a_crash(tmp_path, scenario, monkeypatch):
    calls = _fake_adb(monkeypatch, stdout="")
    fakes = Fakes(tmp_path)
    result, _ = one(fakes, scenario, deps=_real_pid_deps(fakes))
    assert calls == [["adb", "-s", "emulator-5554", "shell", "pidof", PACKAGE]]
    assert result["result"] == "crash" and "process gone" in result["cause"]
    assert fakes.saved["p0"][0].result == "crash"
    assert fakes.configs["p0"]["pid_check_failed"] is False


def test_real_pidof_with_a_live_pid_is_a_pass(tmp_path, scenario, monkeypatch):
    _fake_adb(monkeypatch, stdout="4321\n")
    fakes = Fakes(tmp_path)
    result, _ = one(fakes, scenario, deps=_real_pid_deps(fakes))
    assert result["result"] == "pass"


def test_real_pidof_adb_failure_means_alive_with_a_warning_and_a_flag(tmp_path, scenario, monkeypatch, caplog):
    _fake_adb(monkeypatch, raises=FileNotFoundError("adb"))
    fakes = Fakes(tmp_path)
    with caplog.at_level(logging.ERROR):
        result, _ = one(fakes, scenario, deps=_real_pid_deps(fakes))
    assert result["result"] == "pass"                       # not guessed to be a crash
    assert fakes.configs["p0"]["pid_check_failed"] is True  # but visible in the stored config
    assert any("pidof failed" in w for w in result["warnings"])
    assert any(r.exc_info and r.exc_info[0] is FileNotFoundError for r in caplog.records)


def test_real_pidof_nonzero_exit_with_an_error_message_means_alive_with_a_flag(tmp_path, scenario, monkeypatch):
    _fake_adb(monkeypatch, stdout="", stderr="error: device offline")
    fakes = Fakes(tmp_path)
    result, _ = one(fakes, scenario, deps=_real_pid_deps(fakes))
    assert result["result"] == "pass" and fakes.configs["p0"]["pid_check_failed"] is True
    assert any("device offline" in w for w in result["warnings"])


# --- run id, run existence, finish_run, validation, preflight --------------------------------------------------------------

def test_given_run_id_never_calls_create_run(tmp_path, scenario):
    fakes = Fakes(tmp_path, runs=(123,))
    summary = go(fakes, scenario, make_profiles(2), run_id=123)
    assert fakes.count("create_run") == 0 and summary["run_id"] == 123
    assert [e["run_id"] for _, e in fakes.extras("finish_run")] == [123]
    assert {e["run_id"] for _, e in fakes.extras("runner")} == {123}


def test_no_run_id_calls_create_run_once_with_apk_scenario_and_profile_names(tmp_path, scenario):
    fakes = Fakes(tmp_path, runs=())
    summary = go(fakes, scenario, make_profiles(3), run_id=None)
    ((_, extra),) = fakes.extras("create_run")
    assert extra == {"apk": "app.apk", "scenario": "Login", "profiles": ["p0", "p1", "p2"]}
    assert summary["run_id"] == 77


def test_a_missing_run_row_raises_before_anything_starts_and_finishes_nothing(tmp_path, scenario):
    fakes = Fakes(tmp_path, runs=(77,))
    with pytest.raises(rm.RunNotFoundError, match="run 999"):
        go(fakes, scenario, make_profiles(2), run_id=999)
    assert fakes.kinds(include_global=True) == ["run_exists"]  # no preflight, no device, no finish_run


def test_run_existence_that_cannot_be_verified_proceeds_with_a_warning(tmp_path, scenario, caplog):
    fakes = Fakes(tmp_path)
    fakes.run_exists_answer = None
    with caplog.at_level(logging.WARNING):
        summary = go(fakes, scenario, make_profiles(1))
    assert summary["counts"] == {"pass": 1}
    assert any("cannot verify that run 77 exists" in r.message for r in caplog.records)


def test_the_default_run_exists_cannot_answer_and_never_touches_the_database():
    assert rm._run_exists(1) is None
    src = inspect.getsource(rm._run_exists)
    assert "sqlite" not in src and "connect" not in src and "storage" not in src.split('"""')[2]


def test_an_unverifiable_missing_run_fails_loudly_through_the_foreign_key_not_silently(tmp_path, scenario, caplog):
    fakes = Fakes(tmp_path, runs=(77,))
    fakes.run_exists_answer = None
    with caplog.at_level(logging.ERROR), pytest.raises(sqlite3.IntegrityError):
        go(fakes, scenario, make_profiles(2), run_id=555)
    assert fakes.saved == {}                                       # nothing was persisted
    assert fakes.count("start") == 2 and fakes.occupied == set()   # devices were still cleaned up
    assert sum("could not save the error result" in r.message for r in caplog.records) == 2
    assert any("finish_run failed" in r.message for r in caplog.records)


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
    fakes = Fakes(tmp_path, runs=())
    fakes.status_ok = False
    with pytest.raises(rm.PreflightError, match="Appium"):
        go(fakes, scenario, make_profiles(2), run_id=None)
    assert fakes.count("start") == 0 and fakes.count("finish_run") == 1
    assert fakes.count("create_run") == 1


def test_preflight_checks_disk_and_orphans_and_names_each_failure(tmp_path, scenario):
    fakes = Fakes(tmp_path)
    fakes.disk_gb = 5.0
    fakes.orphans = [{"pid": 4242, "name": "qemu-system-x86_64.exe", "create_time": PAST, "cmdline": ""}]
    with pytest.raises(rm.PreflightError) as e:
        go(fakes, scenario, make_profiles(1))
    msg = str(e.value)
    assert "5.0 GB" in msg and "20 GB" in msg and "pid 4242" in msg
    assert "Appium" not in msg and fakes.count("start") == 0
    assert fakes.count("kill") == 0  # pre-flight only reports


def test_preflight_logs_the_traceback_of_each_check_that_raises(tmp_path, caplog):
    fakes = Fakes(tmp_path)

    def boom(*a):
        raise RuntimeError("check broke")
    deps = fakes.deps(appium_status=boom, free_disk_gb=boom, list_emulator_processes=boom)
    with caplog.at_level(logging.ERROR), pytest.raises(rm.PreflightError) as e:
        rm.preflight(deps, {})
    assert str(e.value).count("check broke") == 3
    tracebacks = [r for r in caplog.records if r.exc_info and "check broke" in str(r.exc_info[1])]
    assert len(tracebacks) == 3


def test_preflight_returns_the_pids_it_saw(tmp_path):
    assert rm.preflight(Fakes(tmp_path).deps(), {}) == set()


def test_preflight_threshold_is_twenty_gb():
    assert rm.PREFLIGHT_MIN_DISK_GB == 20


def test_appium_status_logs_a_traceback_when_unreachable(monkeypatch, caplog):
    def refused(url, timeout):
        raise ConnectionRefusedError("no appium")
    monkeypatch.setattr(rm.urllib.request, "urlopen", refused)
    with caplog.at_level(logging.ERROR):
        assert rm._appium_status() is False
    assert any(r.exc_info and r.exc_info[0] is ConnectionRefusedError for r in caplog.records)


def test_finish_run_failure_is_logged_and_raised_when_nothing_else_failed(tmp_path, scenario, caplog):
    fakes = Fakes(tmp_path)

    def bad_finish(run_id):
        raise RuntimeError("db gone")
    deps = fakes.deps(finish_run=bad_finish)
    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError, match="db gone"):
        go(fakes, scenario, make_profiles(1), deps=deps)
    assert any("finish_run failed" in r.message for r in caplog.records)


BAD_OPTIONS = [
    ("apk", {"apk": None}, "option 'apk'"),
    ("scenario-missing-key", {"scenario": None}, "option 'scenario'"),
    ("scenario-file", {"scenario": "does/not/exist.yaml"}, "option 'scenario' file not found"),
    ("no-profiles-key", {"profiles": None}, "option 'profiles'"),
    ("empty-profiles", {"profiles": []}, "no profiles to run"),
    ("profiles-not-dicts", {"profiles": ["x"]}, "only dicts"),
    ("duplicate-names", {"profiles": [{"name": "same"}, {"name": "same"}]}, "duplicate profile names"),
    ("profiles-path", {"profiles": None, "profiles_path": "nope.yaml"}, "option 'profiles_path'"),
    ("max-parallel-zero", {"max_parallel": 0}, "max_parallel"),
    ("max-parallel-text", {"max_parallel": "many"}, "max_parallel"),
    ("baselines-type", {"baselines": ["x"]}, "option 'baselines'"),
]


@pytest.mark.parametrize("label, override, message", BAD_OPTIONS, ids=[b[0] for b in BAD_OPTIONS])
def test_invalid_input_in_command_line_mode_creates_no_run_and_starts_nothing(
        tmp_path, scenario, label, override, message):
    fakes = Fakes(tmp_path, runs=())
    options = {"apk": "a.apk", "scenario": scenario, "profiles": make_profiles(1), **override}
    with pytest.raises(ValueError, match=message):
        rm.main(None, {k: v for k, v in options.items() if v is not None}, deps=fakes.deps())
    assert fakes.kinds(include_global=True) == []  # no create_run, no preflight, no finish_run


@pytest.mark.parametrize("label, override, message", BAD_OPTIONS, ids=[b[0] for b in BAD_OPTIONS])
def test_invalid_input_in_api_mode_still_finishes_the_run_once_then_raises(
        tmp_path, scenario, label, override, message):
    fakes = Fakes(tmp_path)
    options = {"apk": "a.apk", "scenario": scenario, "profiles": make_profiles(1), **override}
    with pytest.raises(ValueError, match=message):
        rm.main(77, {k: v for k, v in options.items() if v is not None}, deps=fakes.deps())
    assert fakes.count("create_run") == 0 and fakes.count("finish_run") == 1
    assert fakes.count("start") == 0 and fakes.count("appium") == 0  # no pre-flight either


def test_missing_package_is_a_validation_error_in_both_modes(tmp_path):
    bare = tmp_path / "bare.yaml"
    bare.write_text("name: X\nsteps: []\n", encoding="utf-8")
    opts = {"apk": "a.apk", "scenario": str(bare), "profiles": make_profiles(1)}
    with pytest.raises(ValueError, match="no package"):
        rm.main(None, dict(opts), deps=Fakes(tmp_path, runs=()).deps())
    fakes = Fakes(tmp_path)
    with pytest.raises(ValueError, match="no package"):
        rm.main(77, dict(opts), deps=fakes.deps())
    assert fakes.count("finish_run") == 1


def test_more_than_sixteen_parallel_is_rejected_by_validation(tmp_path, scenario):
    fakes = Fakes(tmp_path, runs=())
    with pytest.raises(ValueError, match="MAX_SLOTS"):
        go(fakes, scenario, [{"name": f"q{i}"} for i in range(20)], run_id=None, max_parallel=20)
    assert fakes.kinds(include_global=True) == []


def test_package_option_overrides_the_scenario_package(tmp_path, scenario):
    fakes = Fakes(tmp_path)
    go(fakes, scenario, make_profiles(1), package="other.pkg")
    assert fakes.extras("reset")[0][1]["pkg"] == "other.pkg"


def test_a_baselines_key_that_matches_no_profile_logs_a_warning(tmp_path, scenario, caplog):
    fakes = Fakes(tmp_path)
    with caplog.at_level(logging.WARNING):
        go(fakes, scenario, make_profiles(1), baselines={"Samsung A07": {"pss": 1}})
    assert any("'Samsung A07' matches no profile" in r.message and "sanitised" in r.message
               for r in caplog.records)


def test_baselines_must_be_keyed_by_the_sanitised_profile_name(tmp_path, scenario):
    fakes = Fakes(tmp_path)
    profiles = [{"name": "Samsung A07", "tier": "low"}]
    go(fakes, scenario, profiles, performance_pass=True,
       baselines={"Samsung_A07": {"launch_time": 900.0}})
    (n, e), = fakes.extras("perf")
    assert n == "Samsung_A07" and e["kw"]["baselines"] == {"launch_time": 900.0}
    fakes2 = Fakes(tmp_path)
    go(fakes2, scenario, profiles, performance_pass=True, baselines={"Samsung A07": {"launch_time": 1.0}})
    assert fakes2.extras("perf")[0][1]["kw"]["baselines"] is None  # the unsanitised key never matches


# --- cooperative stop, leftovers --------------------------------------------------------------------------------------------------

def test_stop_event_cancels_profiles_that_have_not_started(tmp_path, scenario):
    stop = threading.Event()
    fakes = Fakes(tmp_path)
    real = fakes.runner_run

    def runner_then_stop(*a, **k):
        stop.set()
        return real(*a, **k)
    summary = go(fakes, scenario, make_profiles(4), max_parallel=1, stop_event=stop,
                 performance_pass=True, deps=fakes.deps(runner_run=runner_then_stop))
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
    fakes.leftover = [{"pid": 999, "name": "qemu-system-x86_64.exe", "create_time": FUTURE, "cmdline": ""}]
    with caplog.at_level(logging.WARNING):
        summary = go(fakes, scenario, make_profiles(1))
    assert summary["leftover_processes"] == fakes.leftover and summary["leftovers_killed"] == []
    assert summary["status"] == "leftovers" and summary["leftover_check"] == "ok"
    assert any("pid 999" in r.message for r in caplog.records)
    assert fakes.count("kill") == 0


def test_leftover_emulators_are_killed_only_when_asked_and_only_if_ours(tmp_path, scenario):
    fakes = Fakes(tmp_path)
    fakes.leftover = [{"pid": 999, "name": "emulator.exe", "create_time": FUTURE, "cmdline": ""}]
    fakes.proc_info = {999: {"pid": 999, "name": "emulator.exe", "create_time": FUTURE}}
    summary = go(fakes, scenario, make_profiles(1), kill_leftovers=True)
    assert [e["pid"] for _, e in fakes.extras("kill")] == [999]
    assert summary["leftovers_killed"] == [999] and summary["leftovers_skipped"] == []


def test_clean_run_has_no_leftovers_and_status_ok(tmp_path, scenario):
    summary = go(Fakes(tmp_path), scenario, make_profiles(2))
    assert summary["leftover_processes"] == [] and summary["status"] == "ok"
    assert summary["leftover_check"] == "ok"


def _leftover_deps(tmp_path, info, leftover):
    fakes = Fakes(tmp_path)
    fakes.proc_info = info
    fakes.leftover = leftover
    fakes.proc_calls = 1  # the next listing is not the pre-flight one
    return fakes, fakes.deps()


def test_kill_skips_a_process_older_than_the_run(tmp_path, caplog):
    proc = {"pid": 7, "name": "emulator.exe", "create_time": PAST, "cmdline": ""}
    fakes, deps = _leftover_deps(tmp_path, {7: {"pid": 7, "name": "emulator.exe", "create_time": PAST}}, [proc])
    with caplog.at_level(logging.WARNING):
        report = rm._check_leftovers(deps, True, time.time(), set())
    assert fakes.count("kill") == 0
    assert report["skipped"] == [{"pid": 7, "reason": "it started before this run"}]
    assert any("skipped: not ours" in r.message for r in caplog.records)


def test_kill_skips_a_pid_that_was_reused_by_a_different_process(tmp_path):
    proc = {"pid": 8, "name": "emulator.exe", "create_time": FUTURE, "cmdline": ""}
    fakes, deps = _leftover_deps(tmp_path, {8: {"pid": 8, "name": "chrome.exe", "create_time": FUTURE}}, [proc])
    report = rm._check_leftovers(deps, True, time.time(), set())
    assert fakes.count("kill") == 0 and "pid reused" in report["skipped"][0]["reason"]


def test_kill_skips_a_pid_that_existed_at_preflight_or_is_already_gone(tmp_path):
    procs = [{"pid": 1, "name": "emulator.exe", "create_time": FUTURE, "cmdline": ""},
             {"pid": 2, "name": "emulator.exe", "create_time": FUTURE, "cmdline": ""}]
    fakes, deps = _leftover_deps(
        tmp_path, {1: {"pid": 1, "name": "emulator.exe", "create_time": FUTURE}}, procs)
    report = rm._check_leftovers(deps, True, time.time(), {1})
    assert fakes.count("kill") == 0
    assert [(s["pid"], s["reason"]) for s in report["skipped"]] == [
        (1, "it existed at pre-flight"), (2, "it is already gone")]


def test_kill_failures_and_unreadable_processes_are_recorded(tmp_path, caplog):
    procs = [{"pid": 5, "name": "qemu-system-x86_64.exe", "create_time": FUTURE, "cmdline": ""},
             {"pid": 6, "name": "emulator.exe", "create_time": FUTURE, "cmdline": ""}]
    info = {5: {"pid": 5, "name": "qemu-system-x86_64.exe", "create_time": FUTURE}}
    fakes, _ = _leftover_deps(tmp_path, info, procs)

    def process_info(pid):
        if pid == 6:
            raise PermissionError("access denied")
        return info[pid]

    def kill(pid):
        raise OSError("cannot kill")
    deps = fakes.deps(process_info=process_info, kill_process=kill)
    with caplog.at_level(logging.ERROR):
        report = rm._check_leftovers(deps, True, time.time(), set())
    assert report["killed"] == []
    assert report["kill_failures"] == [{"pid": 5, "error": "OSError: cannot kill"},
                                       {"pid": 6, "error": "PermissionError: access denied"}]
    assert sum(1 for r in caplog.records if r.exc_info) == 2


def test_a_failing_leftover_listing_is_visible_not_an_empty_list(tmp_path, scenario, caplog):
    fakes = Fakes(tmp_path)

    def hook(n):
        if n >= 3:
            raise RuntimeError("psutil crashed")
    fakes.listing_hook = hook
    with caplog.at_level(logging.ERROR):
        summary = go(fakes, scenario, make_profiles(1))
    assert summary["leftover_check"] == "failed: RuntimeError: psutil crashed"
    assert summary["leftover_processes"] is None            # NOT []
    assert summary["status"] == "unverified"
    assert any(r.exc_info and "psutil crashed" in str(r.exc_info[1]) for r in caplog.records)


def test_kill_failure_shows_up_in_the_summary(tmp_path, scenario):
    fakes = Fakes(tmp_path)
    fakes.leftover = [{"pid": 77, "name": "emulator.exe", "create_time": FUTURE, "cmdline": ""}]
    fakes.proc_info = {77: {"pid": 77, "name": "emulator.exe", "create_time": FUTURE}}

    def kill(pid):
        raise OSError("denied")
    summary = go(fakes, scenario, make_profiles(1), kill_leftovers=True,
                 deps=fakes.deps(kill_process=kill))
    assert summary["kill_failures"] == [{"pid": 77, "error": "OSError: denied"}]
    assert summary["leftovers_killed"] == [] and summary["status"] == "leftovers"


# --- profiles, names, max parallel ----------------------------------------------------------------------------------------------

def test_profile_name_rules():
    assert rm.profile_name({"name": "Samsung A07"}) == "Samsung_A07"       # spaces become underscores
    assert rm.profile_name({"name": "a/b:c"}) == "a_b_c"
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
    with pytest.raises(ValueError, match="MAX_SLOTS"):
        rm.resolve_max_parallel({"max_parallel": 17}, 30, environ={})


def test_max_parallel_reads_the_real_environment(monkeypatch):
    monkeypatch.setenv("VMF_MAX_PARALLEL", "3")
    assert rm.resolve_max_parallel({}, 10) == 3


def test_summary_shape(tmp_path, scenario):
    summary = go(Fakes(tmp_path), scenario, make_profiles(3), max_parallel=2, performance_pass=True)
    assert set(summary) >= {"run_id", "status", "counts", "profiles", "performance",
                            "perf_regressions", "perf_errors", "cancelled", "quarantined_ports",
                            "leftover_check", "leftover_processes", "leftovers_killed",
                            "leftovers_skipped", "kill_failures", "max_parallel", "duration_s"}
    assert summary["max_parallel"] == 2
    assert set(summary["profiles"][0]) >= {"profile", "serial", "result", "cause",
                                           "findings_count", "duration_s", "error"}
    json.dumps(summary, default=str)


# --- command line, defaults, backward compatibility ----------------------------------------------------------------------------------

def test_cli_runs_the_matrix_and_prints_json(tmp_path, scenario, monkeypatch, capsys):
    fakes = Fakes(tmp_path, runs=())
    monkeypatch.setattr(rm, "default_deps", fakes.deps)
    prof = tmp_path / "profiles.yaml"
    prof.write_text(yaml.safe_dump(make_profiles(2)), encoding="utf-8")
    code = rm.cli(["--apk", "app.apk", "--scenario", scenario, "--profiles", str(prof),
                   "--max-parallel", "2", "--no-perf"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["counts"] == {"pass": 2} and out["run_id"] == 77
    assert fakes.count("create_run") == 1 and fakes.count("perf") == 0


def test_cli_run_id_flag_skips_create_run(tmp_path, scenario, monkeypatch, capsys):
    fakes = Fakes(tmp_path, runs=(5,))
    monkeypatch.setattr(rm, "default_deps", fakes.deps)
    prof = tmp_path / "profiles.yaml"
    prof.write_text(yaml.safe_dump(make_profiles(1)), encoding="utf-8")
    rm.cli(["--apk", "a.apk", "--scenario", scenario, "--profiles", str(prof), "--run-id", "5", "--no-perf"])
    assert fakes.count("create_run") == 0 and json.loads(capsys.readouterr().out)["run_id"] == 5


def test_cli_reports_a_missing_profiles_file_in_one_line_with_exit_code_2(tmp_path, scenario, monkeypatch, capsys):
    fakes = Fakes(tmp_path, runs=())
    monkeypatch.setattr(rm, "default_deps", fakes.deps)
    code = rm.cli(["--apk", "a.apk", "--scenario", scenario, "--profiles", str(tmp_path / "nope.yaml"), "--no-perf"])
    captured = capsys.readouterr()
    assert code == 2 and captured.out == ""
    assert captured.err.startswith("error: ") and "Traceback" not in captured.err
    assert fakes.count("create_run") == 0 and fakes.count("start") == 0


def test_cli_reports_a_preflight_failure_in_one_line_with_exit_code_1(tmp_path, scenario, monkeypatch, capsys):
    fakes = Fakes(tmp_path, runs=())
    monkeypatch.setattr(rm, "default_deps", lambda: fakes.deps(appium_status=lambda: False))
    prof = tmp_path / "profiles.yaml"
    prof.write_text(yaml.safe_dump(make_profiles(1)), encoding="utf-8")
    code = rm.cli(["--apk", "a.apk", "--scenario", scenario, "--profiles", str(prof), "--no-perf"])
    captured = capsys.readouterr()
    assert code == 1 and "Traceback" not in captured.err and captured.err.startswith("error: ")
    assert fakes.count("start") == 0


def test_default_deps_wires_the_real_modules():
    from vmf import classifier as c, geometry, oem_packs, perf, provision, runner, storage
    d = rm.default_deps()
    assert d.start_device is provision.start_device and d.stop_device is provision.stop_device
    assert d.apply_profile_settings is oem_packs.apply_profile_settings
    assert d.reset_profile is oem_packs.reset_profile and d.runner_run is runner.run
    assert d.start_logcat is c.start_logcat and d.classify is c.classify
    assert d.geometry_check is geometry.check and d.get_display_zones is geometry.get_display_zones
    assert d.perf_run_and_store is perf.run_and_store and d.perf_is_regression is perf.is_regression
    assert d.create_run is storage.create_run and d.finish_run is storage.finish_run
    assert d.run_exists is rm._run_exists and d.process_info is rm._process_info
    assert rm.Deps.__dataclass_params__.frozen


def test_pid_alive_factory_uses_pidof_and_does_not_guess_on_adb_failure(monkeypatch):
    calls = []

    def fake_run(argv, **kw):
        calls.append((argv, kw["timeout"]))
        return fake_run.result
    monkeypatch.setattr(rm.subprocess, "run", fake_run)
    seen = []
    alive = rm._pid_alive_factory("emulator-5554", PACKAGE, seen.append)
    fake_run.result = SimpleNamespace(stdout="1234\n", stderr="")
    assert alive() is True
    assert calls[0] == (["adb", "-s", "emulator-5554", "shell", "pidof", PACKAGE], rm.PIDOF_TIMEOUT_S)
    fake_run.result = SimpleNamespace(stdout="", stderr="")
    assert alive() is False                       # the device says there is no such process
    assert seen == []
    fake_run.result = SimpleNamespace(stdout="", stderr="error: device offline")
    assert alive() is True                        # unknown is not "gone"
    assert len(seen) == 1 and "device offline" in seen[0]

    def boom(*a, **k):
        raise FileNotFoundError("adb")
    monkeypatch.setattr(rm.subprocess, "run", boom)
    assert alive() is True and len(seen) == 2


def test_list_emulator_processes_matches_names_without_killing(monkeypatch):
    import psutil
    procs = [SimpleNamespace(info={"pid": 1, "name": "qemu-system-x86_64.exe", "create_time": 10.0,
                                   "cmdline": ["qemu", "-port", "5554"]}),
             SimpleNamespace(info={"pid": 2, "name": "chrome.exe", "create_time": 11.0, "cmdline": []}),
             SimpleNamespace(info={"pid": 3, "name": "emulator.exe", "create_time": 12.0, "cmdline": None}),
             SimpleNamespace(info={"pid": 4, "name": None})]
    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: procs)
    assert rm._list_emulator_processes() == [
        {"pid": 1, "name": "qemu-system-x86_64.exe", "create_time": 10.0, "cmdline": "qemu -port 5554"},
        {"pid": 3, "name": "emulator.exe", "create_time": 12.0, "cmdline": ""}]


def test_process_info_returns_none_for_a_missing_process(monkeypatch):
    import psutil

    def gone(pid):
        raise psutil.NoSuchProcess(pid)
    monkeypatch.setattr(psutil, "Process", gone)
    assert rm._process_info(123) is None


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


# --- runner.run: optional arguments -----------------------------------------------------------------------------------------------

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
    calls = SimpleNamespace(create=[], steps=[], driver=FakeDriver(), options=[])

    def remote(url, options=None):
        calls.options.append(options)
        return calls.driver
    monkeypatch.setattr(runner_mod.webdriver, "Remote", remote)
    monkeypatch.setattr(runner_mod.storage, "create_run",
                        lambda apk_name, scenario, profiles: calls.create.append((apk_name, scenario, list(profiles))) or 41)
    monkeypatch.setattr(runner_mod.storage, "save_step",
                        lambda run_id, profile, step: calls.steps.append((run_id, profile, step.step_index)))
    monkeypatch.setattr(runner_mod, "RESULTS_DIR", str(tmp_path / "evidence"))
    spec = tmp_path / "s.yaml"
    spec.write_text(yaml.safe_dump({"name": "S", "package": PACKAGE, "activity": ".Main",
                                    "steps": [{"action": "home"}, {"action": "home"}]}), encoding="utf-8")
    calls.scenario = str(spec)
    calls.evidence = tmp_path / "evidence"
    calls.tmp = tmp_path
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
    assert params["record"].default is True and params["reuse_installed_app"].default is False
    assert params["mjpeg_port"].default is None and params["chromedriver_port"].default is None


def test_runner_default_capabilities_install_and_reset_the_app(runner_env):
    runner_mod.run("emulator-5554", "x/app.apk", runner_env.scenario, 8200)
    caps = runner_env.options[0].to_capabilities()
    assert caps["appium:app"] == "x/app.apk" and caps["appium:noReset"] is False
    assert caps["appium:systemPort"] == 8200 and caps["appium:udid"] == "emulator-5554"
    for key in ("appium:appPackage", "appium:appActivity", "appium:mjpegServerPort",
                "appium:chromedriverPort"):
        assert key not in caps


def test_runner_leaves_the_app_running_when_the_session_ends(runner_env):
    """Appium force-stops the app at session end unless dontStopAppOnReset is set; the
    classifier checks pidof after the runner returned, so a stopped app = false crash."""
    runner_mod.run("emulator-5554", "x/app.apk", runner_env.scenario, 8200)
    caps = runner_env.options[0].to_capabilities()
    assert caps["appium:dontStopAppOnReset"] is True


def test_runner_reuse_installed_app_attaches_without_reinstalling(runner_env):
    runner_mod.run("emulator-5554", "x/app.apk", runner_env.scenario, 8200, reuse_installed_app=True)
    caps = runner_env.options[0].to_capabilities()
    assert "appium:app" not in caps                      # no reinstall
    assert caps["appium:noReset"] is True                # data kept
    assert caps["appium:forceAppLaunch"] is True         # an already-running app is relaunched
    assert caps["appium:appPackage"] == PACKAGE and caps["appium:appActivity"] == ".Main"
    assert caps["appium:systemPort"] == 8200


def test_runner_reuse_installed_app_needs_a_package_and_opens_no_session(runner_env, tmp_path):
    bare = tmp_path / "bare.yaml"
    bare.write_text(yaml.safe_dump({"name": "B", "steps": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="package"):
        runner_mod.run("emulator-5554", "app.apk", str(bare), 8200, reuse_installed_app=True)
    assert runner_env.options == []


def test_runner_port_arguments_set_the_capabilities(runner_env):
    runner_mod.run("emulator-5554", "app.apk", runner_env.scenario, 8203,
                   mjpeg_port=7813, chromedriver_port=9518)
    caps = runner_env.options[0].to_capabilities()
    assert caps["appium:mjpegServerPort"] == 7813 and caps["appium:chromedriverPort"] == 9518
    assert caps["appium:systemPort"] == 8203


def test_runner_missing_scenario_opens_no_session(runner_env):
    with pytest.raises(FileNotFoundError):
        runner_mod.run("emulator-5554", "app.apk", "no/such/file.yaml", 8200)
    assert runner_env.options == [] and runner_env.create == []


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


def test_runner_record_false_stores_and_writes_nothing_and_closes_the_session(runner_env):
    run_id, steps = runner_mod.run("emulator-5554", "app.apk", runner_env.scenario, 8200,
                                   run_id=900, profile="p", record=False)
    assert run_id is None and len(steps) == 2 and all(s.status == "pass" for s in steps)
    assert runner_env.create == [] and runner_env.steps == []
    assert not runner_env.evidence.exists()
    assert steps[0].screenshot_path == "" and steps[0].hierarchy_path == ""
    assert runner_env.driver.quit_called and runner_env.driver.keys == [3, 3]  # the steps really ran


def test_run_scenario_passes_the_new_arguments_through(runner_env):
    run_id, steps = runner_mod.run_scenario("emulator-5554", "app.apk", runner_env.scenario, 8200,
                                            run_id=5, profile="px", reuse_installed_app=True,
                                            mjpeg_port=7811, chromedriver_port=9516)
    assert run_id == 5 and runner_env.steps[0][:2] == (5, "px")
    caps = runner_env.options[0].to_capabilities()
    assert "appium:app" not in caps and caps["appium:mjpegServerPort"] == 7811
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


# --- M14 locator fallback: orchestrator pass-through ------------------------------------------------------------

def _write_scenario_with(tmp_path, steps):
    p = tmp_path / "fb_scenario.yaml"
    p.write_text(yaml.safe_dump({"name": "Shop", "package": PACKAGE, "steps": steps}), encoding="utf-8")
    return str(p)


def test_the_fallback_is_not_passed_to_the_runner_when_it_is_off(tmp_path, scenario):
    fakes = Fakes(tmp_path)
    summary = go(fakes, scenario, make_profiles(2), performance_pass=True)
    assert fakes.fallback_args == [None] * 4            # 2 functional + 2 perf flows: never a config
    assert summary["locator_fallbacks"] == {}


def test_the_fallback_option_reaches_the_functional_pass_only(tmp_path, scenario):
    fakes = Fakes(tmp_path)
    go(fakes, scenario, make_profiles(2), performance_pass=True, locator_fallback=True)
    functional = [a for a, (_, e) in zip(fakes.fallback_args, fakes.extras("runner")) if e["record"]]
    flows = [a for a, (_, e) in zip(fakes.fallback_args, fakes.extras("runner")) if not e["record"]]
    assert len(functional) == 2 and all(isinstance(a, FallbackConfig) and a.enabled for a in functional)
    assert flows == [None, None]                        # a performance number never depends on a model guess


def test_the_environment_variable_enables_it_too(tmp_path, scenario, monkeypatch):
    monkeypatch.setenv("VMF_LOCATOR_FALLBACK", "1")
    fakes = Fakes(tmp_path)
    go(fakes, scenario, make_profiles(1))
    assert isinstance(fakes.fallback_args[0], FallbackConfig)


def test_a_config_instance_is_passed_through_unchanged(tmp_path, scenario):
    cfg = FallbackConfig(enabled=True, model="tiny:1b", max_fallbacks_per_run=2)
    fakes = Fakes(tmp_path)
    go(fakes, scenario, make_profiles(1), locator_fallback=cfg)
    assert fakes.fallback_args == [cfg]


def test_a_non_local_ollama_host_is_rejected_before_anything_starts(tmp_path, scenario):
    fakes = Fakes(tmp_path, runs=())
    with pytest.raises(ValueError, match="localhost"):
        go(fakes, scenario, make_profiles(1), run_id=None,
           locator_fallback={"host": "https://api.example.com"})
    assert fakes.kinds(include_global=True) == []


def test_the_summary_counts_healed_steps_per_profile_from_the_sidecar(tmp_path, scenario):
    fakes = Fakes(tmp_path)
    fakes.heal_counts = {"p0": 2, "p2": 1}
    fakes.unresolved_counts = {"p2": 3}
    summary = go(fakes, scenario, make_profiles(3), locator_fallback=True)
    # changed: the counts are now {"ai_assisted": n, "unresolved": m} per profile (workbook M14.2)
    assert summary["locator_fallbacks"] == {"p0": {"ai_assisted": 2, "unresolved": 0},
                                            "p2": {"ai_assisted": 1, "unresolved": 3}}   # profiles with none are omitted
    by = {r["profile"]: r for r in summary["profiles"]}
    assert by["p0"]["locator_fallbacks"] == {"ai_assisted": 2, "unresolved": 0}
    assert by["p1"]["locator_fallbacks"] == {"ai_assisted": 0, "unresolved": 0}
    assert summary["counts"] == {"pass": 3}                        # healing never changes a verdict


def test_a_missing_sidecar_counts_as_zero(tmp_path, scenario):
    summary = go(Fakes(tmp_path), scenario, make_profiles(1), locator_fallback=True)
    assert summary["locator_fallbacks"] == {}


def test_a_malformed_fallback_step_is_rejected_before_anything_starts(tmp_path):
    bad = _write_scenario_with(tmp_path, [{"action": "tap", "locator": {"text": "x"}, "fallback": True}])
    fakes = Fakes(tmp_path, runs=())
    with pytest.raises(ValueError, match=r"step 0 \(tap\).*intent"):
        go(fakes, bad, make_profiles(1), run_id=None, locator_fallback=True)
    assert fakes.kinds(include_global=True) == []
    # the same scenario is untouched when the feature is off: no new error for existing scenarios
    summary = go(Fakes(tmp_path), bad, make_profiles(1))
    assert summary["counts"] == {"pass": 1}


def test_enabling_the_fallback_does_not_change_the_other_results(tmp_path, scenario):
    off = go(Fakes(tmp_path), scenario, make_profiles(3), max_parallel=1)
    on = go(Fakes(tmp_path), scenario, make_profiles(3), max_parallel=1, locator_fallback=True)
    keep = ("counts", "status", "cancelled", "leftover_check", "quarantined_ports")
    assert {k: off[k] for k in keep} == {k: on[k] for k in keep}
    assert [(p["profile"], p["result"]) for p in off["profiles"]] == [(p["profile"], p["result"]) for p in on["profiles"]]


# --- logcat grace and deliberate kills (14 Oct device run) ----------------------------------------------------

def test_logcat_keeps_running_for_the_grace_period_after_the_last_step(tmp_path, scenario):
    """A crash in the last moments of a scenario must still reach logcat: 2 of 5 real
    `am crash` trials were classified pass when logcat was stopped at once."""
    fakes = Fakes(tmp_path)
    marks = {}

    def runner_run(*a, **kw):
        result = fakes.runner_run(*a, **kw)
        marks["runner_returned"] = time.monotonic()
        return result

    def stop_logcat(proc):
        marks["logcat_stopped"] = time.monotonic()
        return fakes.stop_logcat(proc)
    deps = fakes.deps(runner_run=runner_run, stop_logcat=stop_logcat)
    go(fakes, scenario, make_profiles(1), deps=deps, logcat_grace_s=0.3)
    assert marks["logcat_stopped"] - marks["runner_returned"] >= 0.25


def test_the_grace_period_defaults_to_the_module_constant(monkeypatch):
    monkeypatch.undo()                       # the autouse fixture sets it to 0 for the other tests
    assert rm.LOGCAT_GRACE_S >= 1.0


def test_a_zero_grace_period_adds_no_wait(tmp_path, scenario):
    fakes = Fakes(tmp_path)
    started = time.monotonic()
    go(fakes, scenario, make_profiles(1), logcat_grace_s=0)
    assert time.monotonic() - started < 2


def test_a_scenario_that_ends_with_a_deliberate_kill_skips_the_process_gone_rule(tmp_path, scenario):
    steps = [Step(0, "tap", "pass", 5, "", ""), Step(1, "oem.background_kill", "pass", 5, "", "")]
    fakes = Fakes(tmp_path, steps_for=lambda name: steps)
    result, _ = one(fakes, scenario)
    (_, cls), = fakes.extras("classify")
    assert cls["pid_alive_ok"] is False                      # no pid_alive callable was passed
    assert any("process-gone check skipped" in w for w in result["warnings"])


def test_a_kill_in_the_middle_of_a_scenario_keeps_the_process_gone_rule(tmp_path, scenario):
    steps = [Step(0, "oem.background_kill", "pass", 5, "", ""), Step(1, "tap", "pass", 5, "", "")]
    fakes = Fakes(tmp_path, steps_for=lambda name: steps)
    one(fakes, scenario)
    (_, cls), = fakes.extras("classify")
    assert cls["pid_alive_ok"] is True


def test_a_tagged_kill_action_still_counts_as_a_deliberate_kill():
    class S:
        action = "oem.background_kill [AI-ASSISTED]"
    assert rm._ends_with_deliberate_kill([S()]) is True
    assert rm._ends_with_deliberate_kill([]) is False


# --- an overloaded host is recorded, and marks the results it can cause (14 Oct device run) -----------------

@pytest.fixture
def busy_host(monkeypatch):
    monkeypatch.setattr(FakeHostSampler, "busy", True)
    monkeypatch.setattr(FakeHostSampler, "cpu", 83.0)
    yield
    monkeypatch.setattr(FakeHostSampler, "busy", False)


def test_an_anr_on_a_busy_host_is_marked_as_possibly_a_stall(tmp_path, scenario, busy_host):
    fakes = Fakes(tmp_path, verdicts={"p0": Verdict("anr", "ANR in com.app", 2, None)})
    result, _ = one(fakes, scenario)
    assert result["result"] == "anr"                           # the verdict itself is unchanged
    assert "host busy: 83% CPU outside the emulator" in result["cause"]
    assert any("may be a stall" in w for w in result["warnings"])


def test_a_pass_on_a_busy_host_is_left_alone(tmp_path, scenario, busy_host):
    result, _ = one(Fakes(tmp_path), scenario)
    assert result["result"] == "pass" and result["cause"] == "" and result["warnings"] == []


def test_a_crash_or_hang_on_a_quiet_host_is_not_marked(tmp_path, scenario):
    fakes = Fakes(tmp_path, verdicts={"p0": Verdict("hang", "UI unchanged", 1, None)})
    result, _ = one(fakes, scenario)
    assert result["result"] == "hang" and "host busy" not in result["cause"]


def test_the_host_load_is_stored_with_the_profile_config(tmp_path, scenario, busy_host):
    fakes = Fakes(tmp_path)
    one(fakes, scenario)
    config = fakes.configs["p0"]
    assert config["host_busy"] is True and config["host_load"]["samples"] == 3


def test_a_broken_sampler_never_fails_the_profile(tmp_path, scenario):
    class Broken(FakeHostSampler):
        def summary(self):
            raise RuntimeError("psutil exploded")
    fakes = Fakes(tmp_path)
    result, _ = one(fakes, scenario, deps=fakes.deps(host_sampler=Broken))
    assert result["result"] == "pass" and result["error"] is False


def test_host_busy_helper_reports_not_busy_without_data():
    class NoData:
        noisy = True

        def summary(self):
            return {"samples": 0, "cpu_outside_emulator_pct": None}
    assert rm._host_load(NoData()) == (False, {"samples": 0, "cpu_outside_emulator_pct": None})


# --- the dry run must never touch a real device (found 22 Oct, after oem_packs/provision became real) ---------

def _forbid_device_access(monkeypatch):
    """Any subprocess call (adb, emulator, avdmanager) during the test fails it."""
    import subprocess

    def refuse(*args, **kwargs):
        raise AssertionError(f"the dry run touched the system: {args[:1]}")
    for name in ("run", "Popen", "check_output", "check_call", "call"):
        monkeypatch.setattr(subprocess, name, refuse)


@pytest.fixture
def isolated_dry_run_db(tmp_path, monkeypatch):
    """Keep legacy dry-run persistence out of the repository farm.db."""
    from vmf.storage import storage as storage_module

    db_path = tmp_path / "dry-run.db"
    monkeypatch.setattr(storage_module, "_DB_PATH", db_path)
    storage_module._INITIALIZED_PATHS.clear()
    yield db_path
    storage_module._INITIALIZED_PATHS.clear()


def test_the_dry_run_never_starts_a_process_even_for_a_real_looking_profile(
        monkeypatch, isolated_dry_run_db):
    from vmf import oem_packs, provision
    _forbid_device_access(monkeypatch)
    seen = {}
    real_apply, real_stop = oem_packs.apply_profile_settings, provision.stop_device

    def apply(serial, profile):
        seen["apply"] = serial
        return real_apply(serial, profile)

    def stop(serial):
        seen["stop"] = serial
        return real_stop(serial)
    monkeypatch.setattr(oem_packs, "apply_profile_settings", apply)
    monkeypatch.setattr(provision, "stop_device", stop)
    profile = {"brand": "Samsung", "tier": "low", "name": "samsung_low_29", "density": 320,
               "font_scale": 1.3, "dark_mode": True, "cutout": "notch"}
    result = rm.run_single_profile(profile, "app.apk", "stub", port=5554, stub=True)
    assert isinstance(result["run_id"], int)
    assert seen == {"apply": rm.DRY_RUN_DEVICE, "stop": rm.DRY_RUN_DEVICE}      # a mock serial, never emulator-5554
    with sqlite3.connect(isolated_dry_run_db) as conn:
        row = conn.execute("SELECT status FROM runs WHERE run_id = ?", (result["run_id"],)).fetchone()
    assert row == ("FINISHED",)


def test_the_dry_run_asks_provision_for_a_stubbed_start_without_changing_the_callers_profile(
        monkeypatch, isolated_dry_run_db):
    from vmf import provision
    _forbid_device_access(monkeypatch)
    started = []
    monkeypatch.setattr(provision, "start_device", lambda profile, port: started.append(dict(profile)) or "emulator-5554")
    profile = {"brand": "Samsung", "tier": "low"}
    rm.run_single_profile(profile, "app.apk", "stub", stub=True)
    assert started[0]["dry_run"] is True and "dry_run" not in profile


def test_cli_dry_run_never_starts_a_process_and_uses_an_isolated_database(
        monkeypatch, isolated_dry_run_db):
    """Exercise the public CLI boundary without permitting a device process."""
    from vmf import __main__ as cli

    _forbid_device_access(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["vmf", "--dry-run"])
    cli.main()

    with sqlite3.connect(isolated_dry_run_db) as conn:
        rows = conn.execute("SELECT status FROM runs").fetchall()
    assert rows == [("FINISHED",)]


def test_dry_run_exception_teardown_uses_the_mock_device(monkeypatch, isolated_dry_run_db):
    """A failure after provisioning must still never target emulator-<port>."""
    from vmf import oem_packs, provision

    _forbid_device_access(monkeypatch)
    stopped = []
    real_stop = provision.stop_device

    def stop(serial):
        stopped.append(serial)
        return real_stop(serial)

    def fail_apply(serial, profile):
        assert serial == rm.DRY_RUN_DEVICE
        raise RuntimeError("simulated dry-run failure")

    monkeypatch.setattr(provision, "stop_device", stop)
    monkeypatch.setattr(oem_packs, "apply_profile_settings", fail_apply)
    with pytest.raises(RuntimeError, match="simulated dry-run failure"):
        rm.run_single_profile({"brand": "Samsung", "tier": "low"}, "app.apk", "stub", stub=True)
    assert stopped == [rm.DRY_RUN_DEVICE]


def test_without_stub_the_real_serial_and_profile_reach_the_device_functions(monkeypatch):
    from vmf import oem_packs, provision, runner
    calls = {}
    monkeypatch.setattr(provision, "start_device",
                        lambda profile, port: calls.setdefault("start", dict(profile)) and "emulator-5556")
    monkeypatch.setattr(oem_packs, "apply_profile_settings",
                        lambda serial, profile: calls.setdefault("apply", serial))
    monkeypatch.setattr(provision, "stop_device", lambda serial: calls.setdefault("stop", serial))

    def boom(**kwargs):
        raise RuntimeError("stop after the device calls")
    monkeypatch.setattr(runner, "run_scenario", boom)
    with pytest.raises(RuntimeError, match="stop after"):
        rm.run_single_profile({"brand": "Samsung", "tier": "low"}, "app.apk", "stub", stub=False)
    assert "dry_run" not in calls["start"]
    assert calls["apply"] == "emulator-5556" and calls["stop"] == "emulator-5556"
