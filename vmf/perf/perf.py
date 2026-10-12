"""perf.py — Performance measurement.

Owner: Mathan (M8)

Launch time, jank and PSS from adb shell output on an emulator. EVERY number
produced here is EMULATOR-RELATIVE: useful for regressions between builds and
for tier comparison, never a claim about real phones.

Units: launch_time = ms, jank_pct = percent of frames, pss = kilobytes (KB),
spread_pct = percent (percentage POINTS for jank_pct with a median below 1.0, see
:func:`metric_spread`). ``regression_pct`` is a percentage for launch_time and
pss, and a difference in percentage POINTS for jank_pct.

Design: pure parsers and statistics are separate from device I/O. All adb calls
go through an injectable ``adb(serial_or_None, *args, timeout=...) -> str`` and
host sampling goes through an injectable psutil-like object, so every rule can
be tested without an emulator. Standard library plus PyYAML and psutil.

Entry point: :func:`measure`. :func:`run_performance` is the old ARCH stub, kept
so existing callers keep working.
"""
import logging
import re
import statistics
import subprocess
import threading
import time
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Callable

from vmf import storage
from vmf.results import PerfRow

METRICS = ("launch_time", "jank_pct", "pss")

# Name of the clean emulator snapshot every measured run restarts from.
SNAPSHOT_NAME = "perf_base"
# Default number of measured runs per metric.
DEFAULT_RUNS = 5
# Seconds to wait for an ordinary adb command.
ADB_TIMEOUT_S = 30
# Seconds to wait for `am start -W` (it blocks until the app is drawn).
ADB_LAUNCH_TIMEOUT_S = 60
# Seconds to wait for an emulator-console snapshot save or load.
ADB_SNAPSHOT_TIMEOUT_S = 120
# Seconds to wait for `adb install`.
ADB_INSTALL_TIMEOUT_S = 180
# After `emu avd snapshot load` adb shows the device "offline" for a few seconds,
# and on a Pixel_6 AVD it sometimes stays offline for minutes (a repeat test: back
# in 9 s, never, 6 s, 48 s). Loading the snapshot again recovered it. So wait at
# most ADB_BOOT_TIMEOUT_S seconds per attempt (checking every ADB_BOOT_POLL_S),
# then load again, up to SNAPSHOT_LOAD_ATTEMPTS times.
ADB_BOOT_TIMEOUT_S = 30
ADB_BOOT_POLL_S = 1.0
SNAPSHOT_LOAD_ATTEMPTS = 3
# jank_pct runs whose median is below this many percent are compared in absolute
# percentage points (max - min) against max_spread_pct, because a relative spread
# around a median near 0 is meaningless. At or above it the relative rule applies.
JANK_ABSOLUTE_SPREAD_BELOW_PCT = 1.0
# spread_pct reported when the median is 0 but the values differ (relative
# spread is undefined there, for example jank_pct of 0, 0, 4). It is a sentinel
# and always counts as exceeding the spread limit.
UNDEFINED_SPREAD_PCT = 100.0
# Emulator processes are recognised by these name fragments (lower case).
EMULATOR_PROCESS_NAMES = ("qemu-system", "emulator")
# Seconds between host samples.
SAMPLE_INTERVAL_S = 1.0
# Seconds to wait for the sampler thread to stop.
SAMPLER_JOIN_TIMEOUT_S = 5.0


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class PerfError(RuntimeError):
    """Base class for every error raised by this module."""


class PerfParseError(PerfError):
    """Device output or a set of values could not be interpreted."""


class AdbError(PerfError):
    """An adb command failed, timed out or could not start."""


class IsolationError(PerfError):
    """The host does not have exactly one device attached."""


class SnapshotError(PerfError):
    """The emulator console refused a snapshot save or load."""


class FlowError(PerfError):
    """The caller-supplied flow raised during a measured run."""


# ---------------------------------------------------------------------------
# Thresholds (M8.1)
# ---------------------------------------------------------------------------

# PROPOSED values from the Final Execution Plan, NOT YET APPROVED by the lead.
@dataclass(frozen=True)
class Thresholds:
    launch_regression_pct: float = 20.0   # launch_time: % slower than baseline
    jank_increase_pts: float = 5.0        # jank_pct: percentage POINTS, not relative
    pss_regression_pct: float = 15.0      # pss: % more memory than baseline
    max_spread_pct: float = 10.0          # (max - min) / median across runs
    noisy_host_cpu_pct: float = 30.0      # host CPU outside the emulator
    runs: int = DEFAULT_RUNS              # measured runs per metric


def load_thresholds(path: str | None) -> Thresholds:
    """Return the thresholds, overridden by the YAML file at ``path`` if present.

    Only keys present in the file are overridden. A ``None`` path or a missing
    file returns the unapproved defaults and logs that. A file that is not a
    mapping, or has a value that is not a number, raises ``ValueError``.
    """
    if path is None or not Path(path).is_file():
        logging.warning("perf: no thresholds file (%s); using PROPOSED defaults "
                        "that are NOT YET APPROVED by the lead", path)
        return Thresholds()
    import yaml
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"perf thresholds file {path} must be a mapping")
    known = {f.name: f.type for f in fields(Thresholds)}
    overrides = {}
    for key, value in data.items():
        if key not in known:
            logging.warning("perf: ignoring unknown threshold key %r in %s", key, path)
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"perf threshold {key!r} in {path} must be a number, got {value!r}")
        overrides[key] = int(value) if key == "runs" else float(value)
    return Thresholds(**overrides)


# ---------------------------------------------------------------------------
# Pure parsers (M8.6 inputs)
# ---------------------------------------------------------------------------

_STATUS_RE = re.compile(r"^\s*Status:\s*(\S+)", re.M)
_TOTAL_TIME_RE = re.compile(r"^\s*TotalTime:\s*(\d+)", re.M)
_ERROR_RE = re.compile(r"^\s*Error.*$", re.M)
_TOTAL_FRAMES_RE = re.compile(r"^\s*Total frames rendered:\s*(\d+)", re.M)
_JANKY_RE = re.compile(r"^\s*Janky frames:\s*(\d+)(?:\s*\(([\d.]+)%\))?", re.M)
_P90_RE = re.compile(r"^\s*90th percentile:\s*([\d.]+)ms", re.M)
_PSS_SUMMARY_RE = re.compile(r"TOTAL PSS:\s*(\d+)")
_PSS_TABLE_RE = re.compile(r"^\s*TOTAL\s+(\d+)\b", re.M)


def parse_am_start_w(text: str) -> float:
    """TotalTime in ms from ``am start -W`` output."""
    status = _STATUS_RE.search(text or "")
    if not status:
        errors = [m.group(0).strip() for m in _ERROR_RE.finditer(text or "")]
        raise PerfParseError("am start -W output has no Status line"
                             + (f" ({'; '.join(errors)})" if errors else ""))
    if status.group(1).lower() != "ok":
        raise PerfParseError(f"am start -W status is {status.group(1)!r}, not 'ok'")
    total = _TOTAL_TIME_RE.search(text)
    if not total:
        raise PerfParseError("am start -W output has no TotalTime")
    return float(total.group(1))


def parse_gfxinfo(text: str) -> tuple[float, float | None]:
    """``(jank_pct, p90_ms)`` from ``dumpsys gfxinfo <package>``.

    ``jank_pct`` is the percentage printed by the device, or janky / total when
    the device printed none. ``p90_ms`` is None when the percentile is absent.
    Zero frames rendered raises (no fake 0% jank).
    """
    total = _TOTAL_FRAMES_RE.search(text or "")
    if not total:
        raise PerfParseError("gfxinfo output has no 'Total frames rendered'")
    frames = int(total.group(1))
    if frames == 0:
        raise PerfParseError("no frames rendered")
    janky = _JANKY_RE.search(text)
    if not janky:
        raise PerfParseError("gfxinfo output has no 'Janky frames'")
    jank_pct = float(janky.group(2)) if janky.group(2) else int(janky.group(1)) / frames * 100
    p90 = _P90_RE.search(text)
    return jank_pct, (float(p90.group(1)) if p90 else None)


def parse_meminfo(text: str) -> float:
    """TOTAL PSS in KB from ``dumpsys meminfo <package>``.

    Supports ``TOTAL PSS:   123456`` and the older table line
    ``TOTAL   123456 ...``.
    """
    m = _PSS_SUMMARY_RE.search(text or "") or _PSS_TABLE_RE.search(text or "")
    if not m:
        raise PerfParseError("meminfo output has no TOTAL PSS value")
    return float(m.group(1))


def _device_states(text: str) -> list[tuple[str, str]]:
    pairs = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith(("List of devices", "*")):
            continue
        parts = line.split()
        if len(parts) >= 2:
            pairs.append((parts[0], parts[1]))
    return pairs


def parse_devices(text: str) -> list[str]:
    """Serials in state ``device`` from ``adb devices`` (offline and unauthorized ignored)."""
    return [serial for serial, state in _device_states(text) if state == "device"]


def resolve_launcher_activity(text: str) -> str:
    """``pkg/.Activity`` from ``cmd package resolve-activity --brief`` output."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines or "/" not in lines[-1] or " " in lines[-1]:
        raise PerfParseError(f"no launcher activity found in {text!r}")
    return lines[-1]


# ---------------------------------------------------------------------------
# Statistics (M8.7)
# ---------------------------------------------------------------------------

def median(values: list[float]) -> float:
    if not values:
        raise PerfParseError("no values to take a median of")
    return float(statistics.median(values))


def spread_pct(values: list[float]) -> float:
    """``(max - min) / median * 100``. Median 0: 0.0 if all equal, else raises."""
    med = median(values)
    if med == 0:
        if max(values) == min(values):
            return 0.0
        raise PerfParseError(f"spread is undefined: median is 0 but values differ: {values}")
    return (max(values) - min(values)) / med * 100


def stabilise(collect: Callable[[], list[float]],
              max_spread_pct: float) -> tuple[list[float], bool]:
    """Collect one batch; if its spread is too big collect ONE more and use that.

    Returns ``(values, unstable)``; ``unstable`` is True when the second batch
    still exceeds the limit. ``collect`` is called at most twice.
    """
    values = collect()
    if spread_pct(values) <= max_spread_pct:
        return values, False
    values = collect()
    return values, spread_pct(values) > max_spread_pct


def metric_spread(metric: str, values: list[float]) -> float:
    """Spread used to judge stability, in the unit the rule compares against
    ``max_spread_pct``.

    launch_time and pss: relative, ``(max - min) / median * 100``. jank_pct with
    a median below ``JANK_ABSOLUTE_SPREAD_BELOW_PCT`` (1.0 percent): absolute
    percentage points, ``max - min``. jank_pct at or above that median: relative.
    A relative spread that is undefined (median 0, values differ) is reported as
    ``UNDEFINED_SPREAD_PCT``.
    """
    if metric == "jank_pct" and median(values) < JANK_ABSOLUTE_SPREAD_BELOW_PCT:
        return max(values) - min(values)
    try:
        return spread_pct(values)
    except PerfParseError:
        return UNDEFINED_SPREAD_PCT


# ---------------------------------------------------------------------------
# adb, isolation and snapshots (M8.2 to M8.4)
# ---------------------------------------------------------------------------

def _default_adb(serial: str | None, *args: str, timeout: float = ADB_TIMEOUT_S) -> str:
    argv = ["adb"] + (["-s", serial] if serial else []) + list(args)
    try:
        result = subprocess.run(argv, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise AdbError(f"`{' '.join(argv)}` timed out after {timeout}s") from e
    except OSError as e:
        raise AdbError(f"`{' '.join(argv)}` could not run: {e}") from e
    if result.returncode != 0:
        raise AdbError(f"`{' '.join(argv)}` exited {result.returncode}: "
                       f"{(result.stderr or result.stdout).strip()}")
    return result.stdout


def assert_isolated(adb: Callable, serial: str | None = None) -> None:
    """Raise IsolationError unless exactly one device is in state ``device``.

    If ``serial`` is given, that device must be the one.
    """
    text = adb(None, "devices")
    online = parse_devices(text)
    found = ", ".join(f"{s} ({st})" for s, st in _device_states(text)) or "none"
    if len(online) != 1:
        raise IsolationError(f"expected exactly one device in state 'device', found: {found}")
    if serial is not None and online[0] != serial:
        raise IsolationError(f"the only device is {online[0]}, not {serial} (found: {found})")


def install_apk(serial: str, apk: str, adb: Callable) -> None:
    """``adb -s <serial> install -r <apk>``. The app is installed, never launched."""
    out = adb(serial, "install", "-r", apk, timeout=ADB_INSTALL_TIMEOUT_S)
    if "Success" not in (out or ""):
        raise AdbError(f"install of {apk} on {serial} did not report Success: {(out or '').strip()!r}")


def _snapshot(serial: str, adb: Callable, action: str) -> None:
    out = adb(serial, "emu", "avd", "snapshot", action, SNAPSHOT_NAME,
              timeout=ADB_SNAPSHOT_TIMEOUT_S)
    lines = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
    if any(ln.startswith("KO") for ln in lines) or "OK" not in lines:
        raise SnapshotError(f"snapshot {action} {SNAPSHOT_NAME} on {serial} was not "
                            f"acknowledged with OK; console said: {(out or '').strip()!r}")


def _wait_until_online(serial: str, adb: Callable) -> None:
    """Block until ``serial`` answers adb and reports sys.boot_completed == 1.

    adb errors while the device is still "offline" are expected and retried.
    Raises SnapshotError if the device is not back within ADB_BOOT_TIMEOUT_S.
    """
    deadline = time.monotonic() + ADB_BOOT_TIMEOUT_S
    last = "no answer"
    while True:
        try:
            if (adb(serial, "shell", "getprop", "sys.boot_completed",
                    timeout=ADB_TIMEOUT_S) or "").strip() == "1":
                return
            last = "sys.boot_completed is not 1 yet"
        except AdbError as e:
            last = str(e)
        if time.monotonic() >= deadline:
            raise SnapshotError(f"{serial} did not come back online within "
                                f"{ADB_BOOT_TIMEOUT_S}s after the snapshot load; last: {last}")
        threading.Event().wait(ADB_BOOT_POLL_S)


def save_baseline_snapshot(serial: str, adb: Callable) -> None:
    """Save the current emulator state as the clean ``perf_base`` snapshot."""
    _snapshot(serial, adb, "save")


def load_snapshot(serial: str, adb: Callable) -> None:
    """Restore the emulator to the ``perf_base`` snapshot and wait until it is back
    online.

    The device can stay offline after a load, so if it is not back within
    ADB_BOOT_TIMEOUT_S the load is repeated, up to SNAPSHOT_LOAD_ATTEMPTS times
    in all. A console refusal (KO) is not retried. Raises SnapshotError when every
    attempt left the device offline.
    """
    failures = []
    for attempt in range(1, SNAPSHOT_LOAD_ATTEMPTS + 1):
        _snapshot(serial, adb, "load")
        try:
            _wait_until_online(serial, adb)
            if failures:
                logging.warning("perf: %s came back after snapshot load attempt %d "
                                "(earlier attempts left it offline)", serial, attempt)
            return
        except SnapshotError as e:
            failures.append(str(e))
            logging.warning("perf: snapshot load attempt %d/%d on %s: %s",
                            attempt, SNAPSHOT_LOAD_ATTEMPTS, serial, e)
    raise SnapshotError(f"{serial} stayed offline after {SNAPSHOT_LOAD_ATTEMPTS} snapshot "
                        f"load attempts; last: {failures[-1]}")


# ---------------------------------------------------------------------------
# Host load (M8.4)
# ---------------------------------------------------------------------------

class HostSampler:
    """Sample host CPU % and free RAM once a second in a background thread.

    Use as a context manager around the measurement window. The thread is
    stopped and joined on exit, including when the body raised.

    ``host_cpu_outside_emulator`` = mean over the window of
    ``max(0, system_cpu - emulator_cpu)``, where emulator_cpu is the summed CPU
    of processes whose name contains "qemu-system" or "emulator", divided by the
    logical CPU count so it is on the same scale as system_cpu. ASSUMPTION to
    confirm: the mean over the window is the right statistic (not the peak).
    ``noisy`` is that value being above ``noisy_threshold``.

    ``psutil_module`` is any object with cpu_percent(), cpu_count(),
    virtual_memory().available and process_iter(attrs) yielding objects with an
    ``info`` dict of name and cpu_percent.
    """

    def __init__(self, psutil_module=None, interval_s: float = SAMPLE_INTERVAL_S,
                 noisy_threshold: float = Thresholds.noisy_host_cpu_pct):
        self._ps = psutil_module
        self._interval_s = interval_s
        self.noisy_threshold = noisy_threshold
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cpu_outside: list[float] = []
        self._free_ram: list[int] = []
        self.error: Exception | None = None

    def _sample_once(self) -> None:
        ps = self._ps
        system = float(ps.cpu_percent(None))
        cores = ps.cpu_count(logical=True) or 1
        emulator = 0.0
        for proc in ps.process_iter(["name", "cpu_percent"]):
            info = proc.info
            name = (info.get("name") or "").lower()
            if any(part in name for part in EMULATOR_PROCESS_NAMES):
                emulator += float(info.get("cpu_percent") or 0.0)
        free = int(ps.virtual_memory().available)
        with self._lock:
            self._cpu_outside.append(max(0.0, system - emulator / cores))
            self._free_ram.append(free)

    def _loop(self) -> None:
        try:
            while not self._stop.wait(self._interval_s):
                self._sample_once()
        except Exception as e:  # sampling must never take the measurement down
            self.error = e
            logging.warning("perf: host sampler stopped: %s", e)

    def __enter__(self) -> "HostSampler":
        if self._ps is None:
            try:
                import psutil
            except ImportError as e:
                raise PerfError("psutil is required for host-load sampling "
                                "(add it to requirements.txt and install it)") from e
            self._ps = psutil
        self._stop.clear()
        self._ps.cpu_percent(None)  # prime: the first reading is meaningless
        for _ in self._ps.process_iter(["name", "cpu_percent"]):
            pass
        self._thread = threading.Thread(target=self._loop, name="vmf-host-sampler",
                                        daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(SAMPLER_JOIN_TIMEOUT_S)
        if not self._cpu_outside and self.error is None:
            try:
                self._sample_once()  # window shorter than one interval
            except Exception as e:
                self.error = e
                logging.warning("perf: host sampler could not sample: %s", e)
        return False

    @property
    def host_cpu_outside_emulator(self) -> float:
        with self._lock:
            return statistics.fmean(self._cpu_outside) if self._cpu_outside else 0.0

    @property
    def min_free_ram_bytes(self) -> int | None:
        with self._lock:
            return min(self._free_ram) if self._free_ram else None

    @property
    def noisy(self) -> bool:
        return self.host_cpu_outside_emulator > self.noisy_threshold


# ---------------------------------------------------------------------------
# One measurement run (M8.6)
# ---------------------------------------------------------------------------

def _step(serial: str, step: str, fn: Callable):
    """Run ``fn``; re-raise a PerfError naming the serial and the step."""
    try:
        return fn()
    except PerfError as e:
        raise type(e)(f"{serial}: {step}: {e}") from e


def measure_once(serial: str, package: str, activity: str, adb: Callable,
                 flow: Callable[[], object] | None = None, sampler=None) -> dict:
    """One measured run, in this exact order: snapshot load, force-stop,
    ``am start -W``, gfxinfo reset, flow, gfxinfo, meminfo.

    ``activity`` is ``pkg/.Activity`` or just ``.Activity``. If ``flow`` is None
    it is skipped, and jank and PSS then reflect only the launch. PSS is read
    once, after the flow; reading it also right after launch would add an adb
    call and change the specified order. ``sampler`` is only used for the debug
    log. Returns ``{"launch_time", "jank_pct", "pss"}``.
    """
    component = activity if "/" in activity else f"{package}/{activity}"
    _step(serial, "snapshot load", lambda: load_snapshot(serial, adb))
    _step(serial, "force-stop", lambda: adb(serial, "shell", "am", "force-stop", package))

    def start() -> float:
        try:
            return parse_am_start_w(adb(serial, "shell", "am", "start", "-W", "-n", component,
                                        timeout=ADB_LAUNCH_TIMEOUT_S))
        except PerfParseError as e:
            raise PerfParseError(f"package {package} ({component}) did not start; if it is not "
                                 f"installed, it must be installed before the perf_base "
                                 f"snapshot is saved: {e}") from e

    launch = _step(serial, "am start -W", start)
    _step(serial, "gfxinfo reset",
          lambda: adb(serial, "shell", "dumpsys", "gfxinfo", package, "reset"))
    if flow is not None:
        try:
            flow()
        except Exception as e:
            raise FlowError(f"{serial}: flow: {type(e).__name__}: {e}") from e
    jank, p90 = _step(serial, "gfxinfo", lambda: parse_gfxinfo(
        adb(serial, "shell", "dumpsys", "gfxinfo", package)))
    pss = _step(serial, "meminfo", lambda: parse_meminfo(
        adb(serial, "shell", "dumpsys", "meminfo", package)))
    logging.debug("perf: %s run launch=%.0fms jank=%.2f%% p90=%s pss=%.0fKB host_cpu=%s "
                  "(emulator-relative)", serial, launch, jank, p90, pss,
                  getattr(sampler, "host_cpu_outside_emulator", None))
    return {"launch_time": launch, "jank_pct": jank, "pss": pss}


# ---------------------------------------------------------------------------
# measure() (M8.2 to M8.12)
# ---------------------------------------------------------------------------

def _regression(metric: str, current: float, baseline: float | None) -> float | None:
    if baseline is None:
        return None
    if metric == "jank_pct":
        return current - baseline  # percentage POINTS
    if baseline == 0:
        logging.warning("perf: baseline for %s is 0; regression_pct left as None", metric)
        return None
    return (current - baseline) / baseline * 100


def measure(serial: str, profile: dict, runs: int = DEFAULT_RUNS, *,
            package: str | None = None, activity: str | None = None,
            flow: Callable[[], object] | None = None,
            baselines: dict | None = None, thresholds: Thresholds | None = None,
            adb: Callable | None = None, sampler_factory: Callable | None = None,
            apk: str | None = None) -> list[PerfRow]:
    """Measure launch_time, jank_pct and pss on ``serial``; return three PerfRows.

    ``baselines`` maps metric -> value for THIS profile (read-only here; None or
    a missing metric means no baseline and no regression_pct). A warm-up run is
    discarded. ``runs`` measured runs are collected inside a HostSampler; if any
    metric's spread exceeds ``thresholds.max_spread_pct`` the whole set is
    collected once more and the second set is used (unstable if still too wide).
    ``profile["perf_snapshot_ready"]`` truthy means the ``perf_base`` snapshot
    already exists (with the app installed in it) and is not saved again.
    Otherwise ``apk`` is installed with ``adb install -r`` (never launched) and
    the snapshot is saved from that state; with neither, a SnapshotError says the
    app must be installed first.

    Every error propagates as a PerfError naming the serial and the step.
    ``ValueError`` if no package is known or ``runs`` < 1.
    """
    package = package or profile.get("package")
    if not package:
        raise ValueError("perf.measure needs a package (argument or profile['package'])")
    if runs < 1:
        raise ValueError("perf.measure needs runs >= 1")
    thresholds = thresholds or load_thresholds(None)
    adb = adb or _default_adb
    if sampler_factory is None:
        def sampler_factory():
            return HostSampler(noisy_threshold=thresholds.noisy_host_cpu_pct)

    _step(serial, "isolation check", lambda: assert_isolated(adb, serial))
    if not profile.get("perf_snapshot_ready"):
        if not apk:
            raise SnapshotError(
                f"{serial}: snapshot prepare: no apk was given and profile['perf_snapshot_ready'] "
                f"is not set; {package} must be installed before the perf_base snapshot is "
                f"saved (pass apk=..., or install it and mark the snapshot ready)")
        _step(serial, "install apk", lambda: install_apk(serial, apk, adb))
        _step(serial, "snapshot save", lambda: save_baseline_snapshot(serial, adb))

    def resolve() -> str:
        try:
            return resolve_launcher_activity(adb(
                serial, "shell", "cmd", "package", "resolve-activity", "--brief",
                "-c", "android.intent.category.LAUNCHER", package))
        except PerfParseError as e:
            raise PerfParseError(f"package {package}: {e}") from e

    if not activity:
        activity = profile.get("activity") or _step(serial, "resolve launcher activity", resolve)

    measure_once(serial, package, activity, adb, flow)  # warm-up, numbers discarded

    with sampler_factory() as sampler:
        def batch() -> list[dict]:
            return [measure_once(serial, package, activity, adb, flow, sampler)
                    for _ in range(runs)]

        results = batch()
        limit = thresholds.max_spread_pct
        if any(metric_spread(m, [r[m] for r in results]) > limit for m in METRICS):
            results = batch()
        noisy = bool(sampler.noisy)

    rows = []
    for metric in METRICS:
        values = [r[metric] for r in results]
        med = median(values)
        spread = metric_spread(metric, values)
        base = (baselines or {}).get(metric)
        row = PerfRow(metric=metric, median=med, spread_pct=spread, baseline=base,
                      regression_pct=_regression(metric, med, base),
                      noisy=noisy, unstable=spread > limit)
        logging.info("perf: %s %s", serial, format_perf_row(row))
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Comparison helpers (M8.10, M8.11) and reporting (M8.12)
# ---------------------------------------------------------------------------

def is_regression(row: PerfRow, thresholds: Thresholds) -> bool:
    """True when the row's regression is strictly above its threshold."""
    if row.regression_pct is None:
        return False
    limit = {"launch_time": thresholds.launch_regression_pct,
             "jank_pct": thresholds.jank_increase_pts,
             "pss": thresholds.pss_regression_pct}.get(row.metric)
    return limit is not None and row.regression_pct > limit


def tier_ratio(low_rows: list[PerfRow], high_rows: list[PerfRow]) -> dict[str, float]:
    """Low-tier median / high-tier median per metric; zero denominators skipped."""
    high = {r.metric: r.median for r in high_rows}
    return {r.metric: r.median / high[r.metric]
            for r in low_rows if high.get(r.metric)}


def repeatability_ok(run_a: dict[str, list[PerfRow]], run_b: dict[str, list[PerfRow]],
                     tol_pct: float = 10.0, min_profiles: int = 22) -> bool:
    """M8.11 Test A: same build measured twice.

    A profile agrees when every metric present in both runs has medians within
    ``tol_pct`` percent of the run_a median (both 0 counts as equal). True when
    at least ``min_profiles`` profiles agree.
    """
    agreeing = 0
    for name in run_a.keys() & run_b.keys():
        a = {r.metric: r.median for r in run_a[name]}
        b = {r.metric: r.median for r in run_b[name]}
        common = a.keys() & b.keys()
        if not common:
            continue
        if all(_within(a[m], b[m], tol_pct) for m in common):
            agreeing += 1
    return agreeing >= min_profiles


def _within(a: float, b: float, tol_pct: float) -> bool:
    if a == b:
        return True
    return a != 0 and abs(a - b) / abs(a) * 100 <= tol_pct


def flags_regression_on_all_tiers(rows_by_profile: dict[str, list[PerfRow]],
                                  tier_of, thresholds: Thresholds) -> bool:
    """M8.11 Test B: True only if at least one profile in EACH tier is flagged.

    ``tier_of`` is a dict or callable mapping profile name to tier.
    """
    lookup = tier_of if callable(tier_of) else tier_of.__getitem__
    flagged: dict[str, bool] = {}
    for name, rows in rows_by_profile.items():
        try:
            tier = lookup(name)
        except KeyError:
            raise ValueError(f"no tier known for profile {name!r}") from None
        flagged[tier] = flagged.get(tier, False) or any(is_regression(r, thresholds) for r in rows)
    return bool(flagged) and all(flagged.values())


_UNITS = {"launch_time": "ms", "jank_pct": "% of frames", "pss": "KB"}


def format_perf_row(row: PerfRow) -> str:
    """One line for logs and reports. Always says emulator-relative and states
    either the baseline comparison or that there is no approved baseline."""
    places = 2 if row.metric == "jank_pct" else 0
    parts = ["emulator-relative"]
    if row.baseline is None or row.regression_pct is None:
        parts.append("no approved baseline")
    elif row.metric == "jank_pct":
        parts.append(f"{row.regression_pct:+.2f} pts vs baseline {row.baseline:.{places}f}")
    else:
        parts.append(f"{row.regression_pct:+.0f}% vs baseline {row.baseline:.{places}f}")
    if row.metric == "jank_pct" and row.median < JANK_ABSOLUTE_SPREAD_BELOW_PCT:
        parts.append(f"spread {row.spread_pct:.2f} pts")
    else:
        parts.append(f"spread {row.spread_pct:.1f}%")
    if row.noisy:
        parts.append("noisy host")
    if row.unstable:
        parts.append("unstable")
    unit = _UNITS.get(row.metric, "")
    return f"{row.metric} median {row.median:.{places}f} {unit} ({'; '.join(parts)})"


def run_and_store(run_id: int, profile_name: str, serial: str, profile: dict,
                  **kwargs) -> list[PerfRow]:
    """Run :func:`measure`, save the rows with ``storage.save_perf``, return them."""
    rows = measure(serial, profile, **kwargs)
    storage.save_perf(run_id, profile_name, rows)
    return rows


# ---------------------------------------------------------------------------
# ARCH stub, kept for existing callers
# ---------------------------------------------------------------------------

def run_performance(serial: str, package: str, profile: dict) -> list[PerfRow]:
    """ARCH stub kept for existing callers; returns one placeholder PerfRow.

    The old call shape has no flow, baselines or isolation guarantee, so it does
    not touch adb. Real measurement is :func:`measure`.
    """
    logging.info("[STUB] perf.run_performance serial=%s package=%s "
                 "(placeholder row; real measurement needs perf.measure())", serial, package)
    return [
        PerfRow(
            metric="launch_time",
            median=0.0,
            spread_pct=0.0,
            baseline=None,
            regression_pct=None,
            noisy=False,
            unstable=False,
        )
    ]
