"""Tests for vmf.perf (M8). No device, no adb, no real psutil sampling needed.

Every number measured here is emulator-relative.
"""
import threading
from itertools import count
from pathlib import Path
from types import SimpleNamespace

import pytest

from vmf import perf
from vmf.perf import perf as perf_mod
from vmf.perf import (AdbError, FlowError, HostSampler, IsolationError, PerfParseError,
                      SnapshotError, Thresholds, assert_isolated,
                      flags_regression_on_all_tiers, format_perf_row, is_regression,
                      load_snapshot, load_thresholds, measure, measure_once, median,
                      parse_am_start_w, parse_devices, parse_gfxinfo, parse_meminfo,
                      repeatability_ok, resolve_launcher_activity, run_and_store,
                      run_performance, save_baseline_snapshot, spread_pct, stabilise,
                      tier_ratio)
from vmf.results import PerfRow

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "perf"
SERIAL = "emulator-5554"
PACKAGE = "com.example.app"
ACTIVITY = ".MainActivity"


def fx(name):
    return (FIX / f"{name}.txt").read_text(encoding="utf-8")


# --- text builders (same shape as the fixtures) -----------------------------------

def am_text(ms):
    return f"Starting: Intent {{ }}\nStatus: ok\nLaunchState: COLD\nTotalTime: {int(ms)}\nWaitTime: {int(ms)}\nComplete\n"


def gfx_text(jank_pct):
    return (f"Total frames rendered: 10000\nJanky frames: {round(jank_pct * 100)} ({jank_pct:.2f}%)\n"
            "90th percentile: 19ms\n")


def mem_text(kb):
    return f"           TOTAL PSS:   {int(kb)}            TOTAL RSS:   1\n"


class FakeAdb:
    """Scripted adb. Records every call; per-run numbers come from queues."""

    def __init__(self, launch, jank, pss, devices="devices_one", snapshot="snapshot_ok"):
        self.calls = []
        self.launch, self.jank, self.pss = iter(launch), iter(jank), iter(pss)
        self.devices, self.snapshot = fx(devices), fx(snapshot)

    def __call__(self, serial, *args, timeout=None):
        self.calls.append((serial, args))
        if args == ("devices",):
            return self.devices
        if args[:1] == ("install",):
            return "Performing Streamed Install\nSuccess\n"
        if args[:3] == ("emu", "avd", "snapshot"):
            return self.snapshot
        if "resolve-activity" in args:
            return fx("resolve_activity")
        if "force-stop" in args:
            return ""
        if args[:3] == ("shell", "am", "start"):
            return am_text(next(self.launch))
        if "gfxinfo" in args:
            return "" if args[-1] == "reset" else gfx_text(next(self.jank))
        if "meminfo" in args:
            return mem_text(next(self.pss))
        raise AssertionError(f"unexpected adb call {args}")

    def labels(self):
        out = []
        for call in self.calls:
            if call == "flow":
                out.append("flow")
                continue
            _, a = call
            if a == ("devices",):
                out.append("devices")
            elif a[:1] == ("install",):
                out.append("install")
            elif a[:3] == ("emu", "avd", "snapshot"):
                out.append(a[3])  # "save" or "load"
            elif "resolve-activity" in a:
                out.append("resolve")
            elif "force-stop" in a:
                out.append("force-stop")
            elif a[:3] == ("shell", "am", "start"):
                out.append("start")
            elif "gfxinfo" in a:
                out.append("gfx-reset" if a[-1] == "reset" else "gfx")
            elif "meminfo" in a:
                out.append("meminfo")
        return out


class FakeSampler:
    def __init__(self, noisy=False):
        self.noisy = noisy
        self.host_cpu_outside_emulator = 99.0 if noisy else 1.0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def steady_adb(n_runs=5, **kw):
    """One warm-up value (huge, must be discarded) then n_runs steady values."""
    return FakeAdb(launch=[99999] + [1000, 1010, 990, 1005, 995][:n_runs],
                   jank=[90.0] + [10.0, 10.2, 9.8, 10.0, 10.1][:n_runs],
                   pss=[900000] + [200000, 201000, 199000, 200500, 199500][:n_runs], **kw)


def run_measure(adb, profile_override=None, **kw):
    kw.setdefault("sampler_factory", FakeSampler)
    kw.setdefault("activity", ACTIVITY)
    kw.setdefault("apk", "app.apk")
    return measure(SERIAL, {"package": PACKAGE, **(profile_override or {})}, **kw, adb=adb)


def row(metric="launch_time", median=1000.0, spread=1.0, baseline=None, regression=None,
        noisy=False, unstable=False):
    return PerfRow(metric, median, spread, baseline, regression, noisy, unstable)


# --- parsers ---------------------------------------------------------------------------

def test_parse_am_start_w_ok():
    assert parse_am_start_w(fx("am_start_w_ok")) == 2633.0


def test_parse_am_start_w_rejects_timeout_error_and_missing_total():
    with pytest.raises(PerfParseError, match="timeout"):
        parse_am_start_w(fx("am_start_w_timeout"))
    with pytest.raises(PerfParseError, match="no Status"):
        parse_am_start_w("Error type 3\nError: Activity class {x/.Y} does not exist.\n")
    with pytest.raises(PerfParseError, match="TotalTime"):
        parse_am_start_w("Status: ok\nWaitTime: 5\n")
    with pytest.raises(PerfParseError):
        parse_am_start_w("")


def test_parse_gfxinfo_reads_jank_and_p90():
    assert parse_gfxinfo(fx("gfxinfo")) == (10.29, 19.0)


def test_parse_gfxinfo_zero_frames_raises():
    with pytest.raises(PerfParseError, match="no frames rendered"):
        parse_gfxinfo(fx("gfxinfo_no_frames"))


def test_parse_gfxinfo_computes_jank_when_no_percentage_and_p90_optional():
    assert parse_gfxinfo("Total frames rendered: 200\nJanky frames: 10\n") == (5.0, None)


def test_parse_gfxinfo_missing_fields_raise():
    with pytest.raises(PerfParseError):
        parse_gfxinfo("nothing useful")
    with pytest.raises(PerfParseError, match="Janky"):
        parse_gfxinfo("Total frames rendered: 5\n")


def test_parse_meminfo_both_formats():
    assert parse_meminfo(fx("meminfo_new")) == 187654.0
    assert parse_meminfo(fx("meminfo_old")) == 123456.0


def test_parse_meminfo_prefers_summary_line_and_rejects_garbage():
    assert parse_meminfo("        TOTAL   111   x\n   TOTAL PSS:   222   TOTAL RSS: 3\n") == 222.0
    with pytest.raises(PerfParseError):
        parse_meminfo("No process found for: com.example.app")


def test_parse_devices():
    assert parse_devices(fx("devices_one")) == [SERIAL]
    assert parse_devices(fx("devices_two")) == [SERIAL, "emulator-5556"]
    assert parse_devices(fx("devices_offline")) == []
    text = ("* daemon started successfully\nList of devices attached\n"
            "R58M123\tunauthorized\nemulator-5554\tdevice product:sdk\n")
    assert parse_devices(text) == [SERIAL]


def test_resolve_launcher_activity():
    assert resolve_launcher_activity(fx("resolve_activity")) == "com.example.app/.MainActivity"
    with pytest.raises(PerfParseError):
        resolve_launcher_activity("No activity found")
    with pytest.raises(PerfParseError):
        resolve_launcher_activity("")


# --- statistics ----------------------------------------------------------------------------

def test_median_and_spread():
    assert median([1, 2, 3, 4]) == 2.5
    assert spread_pct([100, 110]) == pytest.approx(10 / 105 * 100)
    assert spread_pct([95, 100, 100, 105, 100]) == pytest.approx(10.0)
    with pytest.raises(PerfParseError):
        median([])


def test_spread_divides_by_median_not_max():
    assert spread_pct([50, 100, 150]) == pytest.approx(100.0)


def test_spread_median_zero_guard():
    assert spread_pct([0, 0, 0]) == 0.0
    with pytest.raises(PerfParseError, match="median is 0"):
        spread_pct([0, 0, 5])


def _batches(*batches):
    it = iter(batches)
    calls = []

    def collect():
        calls.append(1)
        return list(next(it))
    return collect, calls


def test_stabilise_stable_first_batch_collects_once():
    collect, calls = _batches([99, 100, 101])
    assert stabilise(collect, 10.0) == ([99, 100, 101], False)
    assert len(calls) == 1


def test_stabilise_uses_second_batch_when_it_is_stable():
    collect, calls = _batches([90, 100, 115], [98, 100, 103])
    assert stabilise(collect, 10.0) == ([98, 100, 103], False)
    assert len(calls) == 2


def test_stabilise_two_bad_batches_are_unstable_after_exactly_two():
    collect, calls = _batches([90, 100, 115], [80, 100, 105], [1, 2, 3])
    values, unstable = stabilise(collect, 10.0)
    assert values == [80, 100, 105] and unstable is True
    assert len(calls) == 2


# --- isolation and snapshots -----------------------------------------------------------------

def _text_adb(text):
    calls = []

    def adb(serial, *args, timeout=None):
        calls.append((serial, args))
        return text
    adb.calls = calls
    return adb


def test_assert_isolated_passes_for_one_device():
    assert assert_isolated(_text_adb(fx("devices_one"))) is None
    assert assert_isolated(_text_adb(fx("devices_one")), SERIAL) is None


@pytest.mark.parametrize("fixture, expected", [
    ("devices_two", "emulator-5556"),
    ("devices_offline", "emulator-5554 (offline)"),
])
def test_assert_isolated_raises_and_lists_serials(fixture, expected):
    with pytest.raises(IsolationError, match="exactly one") as e:
        assert_isolated(_text_adb(fx(fixture)))
    assert expected in str(e.value)


def test_assert_isolated_raises_for_no_devices_and_wrong_serial():
    with pytest.raises(IsolationError, match="none"):
        assert_isolated(_text_adb("List of devices attached\n\n"))
    with pytest.raises(IsolationError, match="not emulator-5556"):
        assert_isolated(_text_adb(fx("devices_one")), "emulator-5556")


def test_snapshot_ok_and_command_shape():
    adb = _text_adb(fx("snapshot_ok"))
    save_baseline_snapshot(SERIAL, adb)
    load_snapshot(SERIAL, adb)
    assert adb.calls == [(SERIAL, ("emu", "avd", "snapshot", "save", "perf_base")),
                         (SERIAL, ("emu", "avd", "snapshot", "load", "perf_base"))]


def test_snapshot_ko_and_missing_ok_raise_with_console_text():
    with pytest.raises(SnapshotError, match="KO: snapshot 'perf_base' not found"):
        load_snapshot(SERIAL, _text_adb(fx("snapshot_ko")))
    with pytest.raises(SnapshotError):
        save_baseline_snapshot(SERIAL, _text_adb(""))
    with pytest.raises(SnapshotError, match="something odd"):
        save_baseline_snapshot(SERIAL, _text_adb("something odd\n"))


# --- HostSampler -----------------------------------------------------------------------------

class FakePsutil:
    """psutil-like object; `enough` is set once a few readings were taken."""

    def __init__(self, system, procs, cores=4, fail_after=None):
        self.system, self.procs, self.cores = system, procs, cores
        self.fail_after = fail_after
        self.reads = 0
        self.enough = threading.Event()

    def cpu_percent(self, interval=None):
        self.reads += 1
        if self.reads >= 4:
            self.enough.set()
        if self.fail_after is not None and self.reads > self.fail_after:
            self.enough.set()
            raise RuntimeError("psutil broke")
        return self.system

    def cpu_count(self, logical=True):
        return self.cores

    def virtual_memory(self):
        return SimpleNamespace(available=8_000_000_000)

    def process_iter(self, attrs=None):
        return [SimpleNamespace(info={"name": n, "cpu_percent": c}) for n, c in self.procs]


def _sample(fake, **kw):
    with HostSampler(fake, interval_s=0.001, **kw) as s:
        assert fake.enough.wait(5), "sampler thread did not sample"
    return s


def test_host_sampler_noisy_above_threshold():
    fake = FakePsutil(80, [("qemu-system-x86_64.exe", 100), ("chrome.exe", 300)])
    s = _sample(fake)
    assert s.host_cpu_outside_emulator == pytest.approx(55.0)  # 80 - 100/4; chrome ignored
    assert s.noisy is True
    assert s.min_free_ram_bytes == 8_000_000_000


def test_host_sampler_not_noisy_below_threshold():
    fake = FakePsutil(50, [("qemu-system-x86_64.exe", 100), ("emulator.exe", 0)])
    s = _sample(fake)
    assert s.host_cpu_outside_emulator == pytest.approx(25.0)
    assert s.noisy is False


def test_host_sampler_never_reports_negative_outside_load():
    s = _sample(FakePsutil(10, [("qemu-system-x86_64.exe", 400)]))
    assert s.host_cpu_outside_emulator == 0.0


def test_host_sampler_custom_threshold():
    s = _sample(FakePsutil(50, [("qemu-system-x86_64.exe", 100)]), noisy_threshold=20.0)
    assert s.noisy is True


def test_host_sampler_thread_stops_on_exception():
    fake = FakePsutil(10, [])
    sampler = HostSampler(fake, interval_s=0.001)
    with pytest.raises(RuntimeError, match="boom"):
        with sampler:
            assert fake.enough.wait(5)
            raise RuntimeError("boom")
    assert not sampler._thread.is_alive()


def test_host_sampler_survives_a_failing_psutil():
    fake = FakePsutil(10, [], fail_after=2)
    with HostSampler(fake, interval_s=0.001) as s:
        assert fake.enough.wait(5)
    assert not s._thread.is_alive()
    assert isinstance(s.error, RuntimeError)


def test_host_sampler_takes_one_sample_when_window_is_shorter_than_interval():
    fake = FakePsutil(40, [])
    with HostSampler(fake, interval_s=3600) as s:
        pass
    assert s.host_cpu_outside_emulator == pytest.approx(40.0)
    assert not s._thread.is_alive()


# --- measure_once and measure --------------------------------------------------------------------

def test_measure_once_call_order_is_exact():
    adb = steady_adb()
    result = measure_once(SERIAL, PACKAGE, ACTIVITY, adb, lambda: adb.calls.append("flow"))
    assert adb.labels() == ["load", "force-stop", "start", "gfx-reset", "flow", "gfx", "meminfo"]
    assert result == {"launch_time": 99999.0, "jank_pct": 90.0, "pss": 900000.0}
    assert adb.calls[2][1] == ("shell", "am", "start", "-W", "-n", "com.example.app/.MainActivity")
    assert adb.calls[3][1] == ("shell", "dumpsys", "gfxinfo", PACKAGE, "reset")


def test_measure_once_without_flow_skips_it():
    adb = steady_adb()
    measure_once(SERIAL, PACKAGE, "com.example.app/.MainActivity", adb)
    assert adb.labels() == ["load", "force-stop", "start", "gfx-reset", "gfx", "meminfo"]
    assert adb.calls[2][1][-1] == "com.example.app/.MainActivity"


def test_measure_once_names_serial_and_step_on_failure():
    inner = FakeAdb([0], [0], [0])

    def timed_out(serial, *args, timeout=None):
        if args[:3] == ("shell", "am", "start"):
            return fx("am_start_w_timeout")
        return inner(serial, *args, timeout=timeout)
    with pytest.raises(PerfParseError, match=r"emulator-5554: am start -W: .*timeout"):
        measure_once(SERIAL, PACKAGE, ACTIVITY, timed_out)


def test_measure_once_wraps_flow_errors():
    def flow():
        raise KeyError("tap failed")
    with pytest.raises(FlowError, match="emulator-5554: flow: KeyError"):
        measure_once(SERIAL, PACKAGE, ACTIVITY, steady_adb(), flow)


def test_measure_returns_three_rows_and_discards_warmup():
    adb = steady_adb()
    rows = run_measure(adb)
    assert [r.metric for r in rows] == ["launch_time", "jank_pct", "pss"]
    assert all(isinstance(r, PerfRow) for r in rows)
    by = {r.metric: r for r in rows}
    assert by["launch_time"].median == 1000.0
    assert by["jank_pct"].median == pytest.approx(10.0)
    assert by["pss"].median == 200000.0
    for r in rows:  # warm-up (99999 / 90.0 / 900000) never reaches a row
        assert r.spread_pct < 10 and not r.unstable and not r.noisy
        assert r.baseline is None and r.regression_pct is None


def test_measure_collects_exactly_runs_measured_runs_plus_one_warmup():
    adb = steady_adb()
    run_measure(adb, runs=5)
    labels = adb.labels()
    assert labels.count("start") == 6 and labels.count("load") == 6
    assert labels[:3] == ["devices", "install", "save"]
    adb3 = FakeAdb([99999, 1000, 1010, 990], [90.0, 10.0, 10.2, 9.8],
                   [900000, 200000, 201000, 199000])
    run_measure(adb3, runs=3)
    assert adb3.labels().count("start") == 4


def test_measure_resolves_activity_when_not_given():
    adb = steady_adb()
    measure(SERIAL, {"package": PACKAGE}, adb=adb, sampler_factory=FakeSampler, apk="app.apk")
    assert "resolve" in adb.labels()
    start = [c for c in adb.calls if c != "flow" and c[1][:3] == ("shell", "am", "start")][0]
    assert start[1][-1] == "com.example.app/.MainActivity"


def test_measure_skips_snapshot_save_when_profile_says_it_exists():
    adb = steady_adb()
    measure(SERIAL, {"package": PACKAGE, "perf_snapshot_ready": True}, adb=adb,
            activity=ACTIVITY, sampler_factory=FakeSampler)
    assert "save" not in adb.labels() and "install" not in adb.labels()
    assert adb.labels()[0] == "devices"


def test_measure_calls_flow_on_every_run():
    adb = steady_adb()
    run_measure(adb, flow=lambda: adb.calls.append("flow"))
    assert adb.labels().count("flow") == 6


def test_measure_recollects_once_when_a_metric_is_unstable_then_uses_second_batch():
    adb = FakeAdb(launch=[99999, 1000, 1500, 700, 1200, 800, 1000, 1010, 990, 1005, 995],
                  jank=[90.0] + [10.0] * 10, pss=[900000] + [200000] * 10)
    rows = run_measure(adb)
    assert adb.labels().count("start") == 11  # warm-up + two batches of 5
    by = {r.metric: r for r in rows}
    assert by["launch_time"].median == 1000.0
    assert by["launch_time"].unstable is False


def test_measure_marks_unstable_after_two_bad_batches_and_never_a_third():
    bad = [1000, 1500, 700, 1200, 800]
    adb = FakeAdb(launch=[99999] + bad + bad, jank=[90.0] + [10.0] * 10,
                  pss=[900000] + [200000] * 10)
    rows = run_measure(adb)
    assert adb.labels().count("start") == 11
    by = {r.metric: r for r in rows}
    assert by["launch_time"].unstable is True and by["launch_time"].spread_pct > 10
    assert by["jank_pct"].unstable is False


def test_measure_zero_median_with_differing_values_is_unstable_not_a_crash():
    zeros = [0, 0, 0, 0, 5000]  # pss median 0: relative spread is undefined
    adb = FakeAdb(launch=[99999] + [1000] * 10, jank=[90.0] + [10.0] * 10,
                  pss=[900000] + zeros + zeros)
    by = {r.metric: r for r in run_measure(adb)}
    assert by["pss"].spread_pct == perf_mod.UNDEFINED_SPREAD_PCT
    assert by["pss"].unstable is True


def test_measure_noisy_comes_from_the_sampler():
    assert all(r.noisy for r in run_measure(steady_adb(), sampler_factory=lambda: FakeSampler(True)))
    assert not any(r.noisy for r in run_measure(steady_adb(), sampler_factory=FakeSampler))


def test_measure_regression_launch_is_percent_and_jank_is_points():
    adb = FakeAdb(launch=[1] + [3344] * 5, jank=[1.0] + [10.0] * 5, pss=[1] + [230000] * 5)
    rows = run_measure(adb, baselines={"launch_time": 2633, "jank_pct": 6.0, "pss": 200000})
    by = {r.metric: r for r in rows}
    assert by["launch_time"].baseline == 2633
    assert by["launch_time"].regression_pct == pytest.approx(27.0, abs=0.01)
    assert by["jank_pct"].regression_pct == pytest.approx(4.0)  # points, not 66.7%
    assert by["pss"].regression_pct == pytest.approx(15.0)


def test_measure_regression_is_none_without_baseline():
    adb = FakeAdb(launch=[1] + [3344] * 5, jank=[1.0] + [10.0] * 5, pss=[1] + [230000] * 5)
    rows = run_measure(adb, baselines={"launch_time": 2633})
    by = {r.metric: r for r in rows}
    assert by["launch_time"].regression_pct is not None
    assert by["jank_pct"].baseline is None and by["jank_pct"].regression_pct is None
    assert by["pss"].baseline is None and by["pss"].regression_pct is None
    assert all(r.regression_pct is None for r in run_measure(steady_adb(), baselines=None))


def test_measure_zero_baseline_does_not_divide_by_zero():
    rows = run_measure(steady_adb(), baselines={"launch_time": 0})
    assert rows[0].baseline == 0 and rows[0].regression_pct is None


def test_measure_requires_package_and_positive_runs():
    with pytest.raises(ValueError, match="package"):
        measure(SERIAL, {}, adb=steady_adb())
    with pytest.raises(ValueError, match="runs"):
        measure(SERIAL, {"package": PACKAGE}, runs=0, adb=steady_adb())


def test_measure_uses_package_from_argument_over_profile():
    adb = steady_adb()
    measure(SERIAL, {"package": "wrong.pkg"}, package=PACKAGE, activity=ACTIVITY,
            adb=adb, sampler_factory=FakeSampler, apk="app.apk")
    assert all(PACKAGE in c[1] for c in adb.calls if c != "flow" and "force-stop" in c[1])


def test_measure_isolation_failure_names_serial_and_lists_devices():
    with pytest.raises(IsolationError, match=r"emulator-5556"):
        run_measure(steady_adb(devices="devices_two"))
    adb = steady_adb(devices="devices_two")
    with pytest.raises(IsolationError, match="emulator-5554: isolation check"):
        run_measure(adb)
    assert adb.labels() == ["devices"]  # nothing ran after the failed check


def test_measure_snapshot_failure_names_serial_and_step():
    with pytest.raises(SnapshotError, match=r"emulator-5554: snapshot save: .*KO"):
        run_measure(steady_adb(snapshot="snapshot_ko"))


def test_measure_default_adb_errors_are_adb_errors(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("adb")
    monkeypatch.setattr(perf_mod.subprocess, "run", boom)
    with pytest.raises(AdbError, match="could not run"):
        perf_mod._default_adb(SERIAL, "shell", "true")
    monkeypatch.setattr(perf_mod.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="no device"))
    with pytest.raises(AdbError, match="exited 1: no device"):
        perf_mod._default_adb(SERIAL, "shell", "true")


def test_default_adb_builds_argv_and_passes_timeout(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        return SimpleNamespace(returncode=0, stdout="out", stderr="")
    monkeypatch.setattr(perf_mod.subprocess, "run", fake_run)
    assert perf_mod._default_adb(SERIAL, "shell", "ls", timeout=7) == "out"
    assert seen["argv"] == ["adb", "-s", SERIAL, "shell", "ls"] and seen["kw"]["timeout"] == 7
    perf_mod._default_adb(None, "devices")
    assert seen["argv"] == ["adb", "devices"]


# --- comparison helpers -----------------------------------------------------------------------------

@pytest.mark.parametrize("metric, at_limit, over", [
    ("launch_time", 20.0, 21.0),
    ("jank_pct", 5.0, 6.0),
    ("pss", 15.0, 16.0),
])
def test_is_regression_boundaries(metric, at_limit, over):
    t = Thresholds()
    assert is_regression(row(metric, regression=at_limit), t) is False
    assert is_regression(row(metric, regression=over), t) is True
    assert is_regression(row(metric, regression=-50.0), t) is False
    assert is_regression(row(metric, regression=None), t) is False


def test_is_regression_uses_the_supplied_thresholds_and_ignores_unknown_metrics():
    t = Thresholds(launch_regression_pct=50.0)
    assert is_regression(row("launch_time", regression=40.0), t) is False
    assert is_regression(row("other", regression=999.0), t) is False


def test_tier_ratio():
    low = [row("launch_time", 3000), row("jank_pct", 8), row("pss", 400000)]
    high = [row("launch_time", 1500), row("jank_pct", 0), row("pss", 200000)]
    assert tier_ratio(low, high) == {"launch_time": 2.0, "pss": 2.0}
    assert tier_ratio(low, [row("launch_time", 1500)]) == {"launch_time": 2.0}


def _runs_by_profile(n, scale=1.0, off=()):
    out = {}
    for i in range(n):
        k = 1.5 if i in off else scale
        out[f"p{i}"] = [row("launch_time", 1000 * k), row("jank_pct", 10 * k), row("pss", 200000 * k)]
    return out


def test_repeatability_ok_needs_22_profiles_within_tolerance():
    base = _runs_by_profile(22)
    assert repeatability_ok(base, _runs_by_profile(22, scale=1.05)) is True
    assert repeatability_ok(base, _runs_by_profile(22, scale=1.09)) is True
    assert repeatability_ok(base, _runs_by_profile(22, off={3})) is False  # 21 of 22 agree
    assert repeatability_ok(_runs_by_profile(21), _runs_by_profile(21)) is False  # only 21 profiles
    assert repeatability_ok(base, _runs_by_profile(22, scale=1.2)) is False
    assert repeatability_ok(base, _runs_by_profile(22, off={3}), min_profiles=21) is True


def test_repeatability_ok_handles_zero_medians():
    a = {"p": [row("jank_pct", 0.0)]}
    assert repeatability_ok(a, {"p": [row("jank_pct", 0.0)]}, min_profiles=1) is True
    assert repeatability_ok(a, {"p": [row("jank_pct", 1.0)]}, min_profiles=1) is False


def _flagged(flag):
    return [row("launch_time", 1500, baseline=1000, regression=50.0 if flag else 1.0)]


def test_flags_regression_on_all_tiers():
    t = Thresholds()
    tiers = {"a": "low", "b": "low", "c": "mid", "d": "high"}
    rows = {"a": _flagged(True), "b": _flagged(False), "c": _flagged(True), "d": _flagged(True)}
    assert flags_regression_on_all_tiers(rows, tiers, t) is True
    rows["d"] = _flagged(False)
    assert flags_regression_on_all_tiers(rows, tiers, t) is False
    assert flags_regression_on_all_tiers(rows, lambda p: "all", t) is True
    assert flags_regression_on_all_tiers({}, tiers, t) is False
    with pytest.raises(ValueError, match="'zz'"):
        flags_regression_on_all_tiers({"zz": _flagged(True)}, tiers, t)


def test_format_perf_row_always_labelled():
    with_base = format_perf_row(row("launch_time", 3344, baseline=2633, regression=27.0))
    assert "emulator-relative" in with_base and "+27% vs baseline 2633" in with_base
    no_base = format_perf_row(row("pss", 187654))
    assert "emulator-relative" in no_base and "no approved baseline" in no_base
    jank = format_perf_row(row("jank_pct", 10.0, baseline=6.0, regression=4.0))
    assert "emulator-relative" in jank and "+4.00 pts vs baseline 6.00" in jank
    flags = format_perf_row(row("launch_time", 1, noisy=True, unstable=True))
    assert "noisy host" in flags and "unstable" in flags and "emulator-relative" in flags
    for r in (row("launch_time"), row("jank_pct"), row("pss"), row("mystery")):
        s = format_perf_row(r)
        assert "emulator-relative" in s and ("no approved baseline" in s or "vs baseline" in s)


def test_run_and_store_measures_then_saves(monkeypatch):
    saved = []
    monkeypatch.setattr(perf_mod.storage, "save_perf",
                        lambda run_id, profile, rows: saved.append((run_id, profile, rows)))
    rows = run_and_store(7, "p-low", SERIAL, {"package": PACKAGE}, adb=steady_adb(),
                         activity=ACTIVITY, sampler_factory=FakeSampler, apk="app.apk")
    assert len(rows) == 3
    assert saved == [(7, "p-low", rows)]


def test_run_and_store_does_not_save_when_measure_fails(monkeypatch):
    saved = []
    monkeypatch.setattr(perf_mod.storage, "save_perf", lambda *a: saved.append(a))
    with pytest.raises(IsolationError):
        run_and_store(7, "p", SERIAL, {"package": PACKAGE}, adb=steady_adb(devices="devices_two"),
                      activity=ACTIVITY, sampler_factory=FakeSampler, apk="app.apk")
    assert saved == []


# --- thresholds ------------------------------------------------------------------------------------------

def test_default_thresholds_are_the_proposed_values():
    t = Thresholds()
    assert (t.launch_regression_pct, t.jank_increase_pts, t.pss_regression_pct,
            t.max_spread_pct, t.noisy_host_cpu_pct, t.runs) == (20.0, 5.0, 15.0, 10.0, 30.0, 5)
    with pytest.raises(Exception):
        t.runs = 9  # frozen


def test_load_thresholds_no_file_returns_defaults_and_warns(tmp_path, caplog):
    with caplog.at_level("WARNING"):
        assert load_thresholds(None) == Thresholds()
        assert load_thresholds(str(tmp_path / "missing.yaml")) == Thresholds()
    assert sum("NOT YET APPROVED" in r.message for r in caplog.records) == 2
    assert not (tmp_path / "missing.yaml").exists()


def test_load_thresholds_partial_file_overrides_only_given_keys(tmp_path):
    p = tmp_path / "t.yaml"
    p.write_text("launch_regression_pct: 25\nruns: 7\n", encoding="utf-8")
    t = load_thresholds(str(p))
    assert t.launch_regression_pct == 25.0 and t.runs == 7
    assert t.jank_increase_pts == 5.0 and t.pss_regression_pct == 15.0


def test_load_thresholds_full_file(tmp_path):
    p = tmp_path / "t.yaml"
    p.write_text("launch_regression_pct: 1\njank_increase_pts: 2\npss_regression_pct: 3\n"
                 "max_spread_pct: 4\nnoisy_host_cpu_pct: 5\nruns: 6\n", encoding="utf-8")
    assert load_thresholds(str(p)) == Thresholds(1.0, 2.0, 3.0, 4.0, 5.0, 6)


def test_load_thresholds_rejects_bad_files_and_ignores_unknown_keys(tmp_path, caplog):
    p = tmp_path / "t.yaml"
    p.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ValueError, match="mapping"):
        load_thresholds(str(p))
    p.write_text("runs: many\n", encoding="utf-8")
    with pytest.raises(ValueError, match="'runs'"):
        load_thresholds(str(p))
    p.write_text("mystery: 1\nruns: 3\n", encoding="utf-8")
    with caplog.at_level("WARNING"):
        assert load_thresholds(str(p)).runs == 3
    assert any("mystery" in r.message for r in caplog.records)
    p.write_text("", encoding="utf-8")
    assert load_thresholds(str(p)) == Thresholds()


def test_measure_honours_custom_spread_threshold():
    loose = Thresholds(max_spread_pct=50.0)
    adb = FakeAdb(launch=[99999, 1000, 1200, 800, 1100, 900], jank=[90.0] + [10.0] * 5,
                  pss=[900000] + [200000] * 5)
    rows = run_measure(adb, thresholds=loose)
    assert adb.labels().count("start") == 6 and not rows[0].unstable


# --- stub compatibility and exports -------------------------------------------------------------------------

def test_old_run_performance_stub_still_works(caplog):
    with caplog.at_level("INFO"):
        rows = run_performance(SERIAL, PACKAGE, {"density": 420})
    assert len(rows) == 1 and isinstance(rows[0], PerfRow)
    assert rows[0].baseline is None and rows[0].regression_pct is None
    assert any("perf.measure()" in r.message for r in caplog.records)


def test_package_exports():
    for name in ("measure", "run_and_store", "load_thresholds", "Thresholds", "run_performance",
                 "is_regression", "tier_ratio", "repeatability_ok", "flags_regression_on_all_tiers",
                 "format_perf_row", "PerfError", "PerfParseError", "IsolationError",
                 "SnapshotError", "AdbError", "HostSampler"):
        assert name in perf.__all__
        assert hasattr(perf, name)
    assert issubclass(PerfParseError, perf.PerfError) and issubclass(IsolationError, perf.PerfError)


def test_module_never_sleeps():
    src = Path(perf_mod.__file__).read_text(encoding="utf-8")
    assert "time.sleep" not in src and "TODO" not in src and "NotImplementedError" not in src


# --- snapshot preparation: install the APK, then save (M8.3) ----------------------------------------

def adb_wrapper(fn, recorder):
    """Give a wrapping function the call log and label helper of the FakeAdb it wraps."""
    fn.calls = recorder.calls
    fn.labels = recorder.labels
    return fn


def test_measure_installs_apk_before_saving_snapshot_and_never_launches_it():
    adb = steady_adb()
    run_measure(adb, apk="C:/builds/app.apk")
    labels = adb.labels()
    assert labels[:3] == ["devices", "install", "save"]
    assert adb.calls[1] == (SERIAL, ("install", "-r", "C:/builds/app.apk"))
    assert labels.index("save") < labels.index("start")  # first launch is the warm-up
    assert labels.count("install") == 1 and labels.count("save") == 1


def test_measure_without_apk_and_without_ready_snapshot_raises_clear_error():
    adb = steady_adb()
    with pytest.raises(SnapshotError, match=r"emulator-5554: .*com\.example\.app must be installed"):
        measure(SERIAL, {"package": PACKAGE}, adb=adb, activity=ACTIVITY, sampler_factory=FakeSampler)
    assert adb.labels() == ["devices"]  # nothing installed, saved or launched


def test_measure_ready_snapshot_needs_no_apk_and_ignores_one():
    adb = steady_adb()
    run_measure(adb, apk=None, profile_override={"perf_snapshot_ready": True})
    assert "install" not in adb.labels() and "save" not in adb.labels()
    adb2 = steady_adb()
    run_measure(adb2, apk="app.apk", profile_override={"perf_snapshot_ready": True})
    assert "install" not in adb2.labels() and "save" not in adb2.labels()


def test_install_failure_names_serial_and_step_and_stops():
    adb = steady_adb()
    inner = adb.__call__

    def failing(serial, *args, timeout=None):
        if args[:1] == ("install",):
            adb.calls.append((serial, args))
            raise AdbError("install exited 1: INSTALL_FAILED_OLDER_SDK")
        return inner(serial, *args, timeout=timeout)
    with pytest.raises(AdbError, match=r"emulator-5554: install apk: .*INSTALL_FAILED_OLDER_SDK"):
        run_measure(adb_wrapper(failing, adb))
    assert "save" not in adb.labels()


def test_install_apk_requires_success_in_output():
    ok = _text_adb("Performing Streamed Install\nSuccess\n")
    perf.install_apk(SERIAL, "a.apk", ok)
    assert ok.calls == [(SERIAL, ("install", "-r", "a.apk"))]
    with pytest.raises(AdbError, match="did not report Success"):
        perf.install_apk(SERIAL, "a.apk", _text_adb("Failure [INSTALL_FAILED_INVALID_APK]"))


# --- snapshot loaded but the package is not installed ---------------------------------------------------

def test_start_of_a_package_that_is_not_installed_names_package_and_step():
    inner = FakeAdb([0], [0], [0])

    def not_installed(serial, *args, timeout=None):
        if args[:3] == ("shell", "am", "start"):
            return fx("am_start_w_not_installed")
        return inner(serial, *args, timeout=timeout)
    with pytest.raises(PerfParseError) as e:
        measure_once(SERIAL, PACKAGE, ACTIVITY, not_installed)
    msg = str(e.value)
    assert msg.startswith("emulator-5554: am start -W: package com.example.app ")
    assert "must be installed before the perf_base snapshot is saved" in msg
    assert "does not exist" in msg  # the device's own words are kept


def test_measure_with_package_missing_from_snapshot_fails_on_warmup_naming_package():
    adb = steady_adb()
    inner = adb.__call__

    def not_installed(serial, *args, timeout=None):
        if args[:3] == ("shell", "am", "start"):
            adb.calls.append((serial, args))
            return fx("am_start_w_not_installed")
        return inner(serial, *args, timeout=timeout)
    with pytest.raises(PerfParseError, match=r"emulator-5554: am start -W: package com\.example\.app"):
        run_measure(adb_wrapper(not_installed, adb))
    assert adb.labels().count("start") == 1  # failed on the warm-up; no measured run happened


def test_resolve_activity_failure_names_package():
    inner = steady_adb()

    def none_found(serial, *args, timeout=None):
        if "resolve-activity" in args:
            return "No activity found"
        return inner(serial, *args, timeout=timeout)
    with pytest.raises(PerfParseError,
                       match=r"emulator-5554: resolve launcher activity: package com\.example\.app"):
        measure(SERIAL, {"package": PACKAGE}, adb=none_found, apk="a.apk", sampler_factory=FakeSampler)


# --- jank spread rule (absolute points below a 1.0 median) --------------------------------------------------

def test_metric_spread_jank_below_one_percent_is_absolute_points():
    assert perf.metric_spread("jank_pct", [0.5, 0.9, 1.4]) == pytest.approx(0.9)  # relative would be 100
    assert perf.metric_spread("jank_pct", [0.0, 0.0, 0.0]) == 0.0
    assert perf.metric_spread("jank_pct", [0, 0, 0, 0, 4]) == 4.0  # median 0, no exception


def test_metric_spread_jank_at_or_above_one_percent_is_relative():
    assert perf.metric_spread("jank_pct", [0.5, 1.0, 1.5]) == pytest.approx(100.0)  # median exactly 1.0
    assert perf.metric_spread("jank_pct", [9, 10, 11]) == pytest.approx(20.0)


def test_metric_spread_other_metrics_stay_relative_even_with_small_medians():
    assert perf.metric_spread("launch_time", [0.5, 0.9, 1.4]) == pytest.approx(spread_pct([0.5, 0.9, 1.4]))
    assert perf.metric_spread("pss", [90, 100, 110]) == pytest.approx(20.0)
    assert perf.metric_spread("pss", [0, 0, 5]) == perf_mod.UNDEFINED_SPREAD_PCT


def test_measure_low_jank_within_points_limit_is_stable_without_recollecting():
    adb = FakeAdb(launch=[99999, 1000, 1010, 990, 1005, 995],
                  jank=[90.0, 0.0, 0.0, 0.0, 0.0, 6.0],  # median 0, spread 6 pts <= 10
                  pss=[900000, 200000, 201000, 199000, 200500, 199500])
    by = {r.metric: r for r in run_measure(adb)}
    assert adb.labels().count("start") == 6
    assert by["jank_pct"].spread_pct == pytest.approx(6.0) and not by["jank_pct"].unstable


def test_measure_low_jank_beyond_points_limit_recollects_then_is_unstable():
    batch = [0.0, 0.0, 0.0, 0.0, 12.0]  # median 0, spread 12 pts > 10
    adb = FakeAdb(launch=[99999] + [1000] * 10, jank=[90.0] + batch + batch,
                  pss=[900000] + [200000] * 10)
    by = {r.metric: r for r in run_measure(adb)}
    assert adb.labels().count("start") == 11
    assert by["jank_pct"].spread_pct == pytest.approx(12.0) and by["jank_pct"].unstable is True


def test_format_perf_row_shows_points_for_low_jank_spread():
    low = format_perf_row(row("jank_pct", 0.4, spread=2.5))
    assert "spread 2.50 pts" in low and "emulator-relative" in low
    assert "spread 4.0%" in format_perf_row(row("jank_pct", 10.0, spread=4.0))


# --- jank and PSS are read after the flow only ----------------------------------------------------------------

def test_jank_and_pss_are_read_after_the_flow_and_only_once_per_run():
    adb = steady_adb()
    measure_once(SERIAL, PACKAGE, ACTIVITY, adb, lambda: adb.calls.append("flow"))
    labels = adb.labels()
    assert labels.count("gfx") == 1 and labels.count("meminfo") == 1
    assert labels.index("gfx-reset") < labels.index("flow") < labels.index("gfx") < labels.index("meminfo")
