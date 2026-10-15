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
import datetime
import logging
import os
import re
import statistics
import subprocess
import threading
import time
from dataclasses import dataclass, field, replace
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
# After a snapshot load sys.boot_completed can already read 1 (it comes from the snapshot) while
# system_server is still restarting its services; `am force-stop` then fails with
# "Can't find service: activity". These services must answer before the device counts as online.
BOOT_SERVICES = ("activity", "package")
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
# ``approved`` / ``approved_by`` / ``approved_on`` are only ever set by load_thresholds()
# from perf_config.yaml; they are not compared, so equal values stay equal.
@dataclass(frozen=True)
class Thresholds:
    launch_regression_pct: float = 20.0   # launch_time: % slower than baseline
    jank_increase_pts: float = 5.0        # jank_pct: percentage POINTS, not relative
    pss_regression_pct: float = 15.0      # pss: % more memory than baseline
    max_spread_pct: float = 10.0          # (max - min) / median across runs
    noisy_host_cpu_pct: float = 30.0      # host CPU outside the emulator
    runs: int = DEFAULT_RUNS              # measured runs per metric
    approved: bool = field(default=False, compare=False)
    approved_by: str | None = field(default=None, compare=False)
    approved_on: str | None = field(default=None, compare=False)


# The lead-approved (or still PROPOSED) thresholds live in perf_config.yaml at the repo root.
PERF_CONFIG_NAME = "perf_config.yaml"
PERF_CONFIG_ENV = "VMF_PERF_CONFIG"
REPO_ROOT = Path(__file__).resolve().parents[2]
# Keys of perf_config.yaml -> Thresholds fields. The Thresholds field names are accepted
# as aliases (and "runs"), so older config files keep working.
_CONFIG_KEYS = {
    "launch_regression_pct": "launch_regression_pct",
    "jank_increase_points": "jank_increase_pts",
    "pss_increase_pct": "pss_regression_pct",
    "max_spread_pct": "max_spread_pct",
    "noisy_host_cpu_pct": "noisy_host_cpu_pct",
    "jank_increase_pts": "jank_increase_pts",
    "pss_regression_pct": "pss_regression_pct",
    "runs": "runs",
}
_APPROVAL_KEYS = ("approved_by", "approved_on", "baseline_approvers")


def resolve_config_path(path: str | os.PathLike | None = None) -> Path:
    """The perf config file to read: ``path``, else $VMF_PERF_CONFIG, else
    perf_config.yaml at the repo root."""
    if path is not None:
        return Path(path)
    env = os.environ.get(PERF_CONFIG_ENV)
    return Path(env) if env else REPO_ROOT / PERF_CONFIG_NAME


def _read_config(path: Path) -> dict | None:
    """The parsed mapping, or None if the file does not exist. ValueError if it is
    not a mapping or has an unknown key (the message names the key)."""
    if not path.is_file():
        return None
    import yaml
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"perf config {path} must be a mapping of keys to values")
    known = set(_CONFIG_KEYS) | set(_APPROVAL_KEYS)
    for key in data:
        if key not in known:
            raise ValueError(f"perf config {path}: unknown key {key!r} "
                             f"(allowed: {', '.join(sorted(known))})")
    return data


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _thresholds_from(data: dict, path: Path) -> Thresholds:
    overrides: dict = {}
    for key, field_name in ((k, _CONFIG_KEYS[k]) for k in data if k in _CONFIG_KEYS):
        value = data[key]
        if not _is_number(value) or value <= 0:
            raise ValueError(f"perf config {path}: {key!r} must be a positive number, got {value!r}")
        if field_name in overrides:
            raise ValueError(f"perf config {path}: {key!r} repeats another key for the same setting")
        overrides[field_name] = int(value) if field_name == "runs" else float(value)
    th = Thresholds(**overrides)
    if th.noisy_host_cpu_pct > 100:
        raise ValueError(f"perf config {path}: 'noisy_host_cpu_pct' is a percentage and must be <= 100")
    for field_name in ("launch_regression_pct", "pss_regression_pct"):
        if getattr(th, field_name) <= th.max_spread_pct:
            raise ValueError(
                f"perf config {path}: {field_name} ({getattr(th, field_name)}) must be larger than "
                f"max_spread_pct ({th.max_spread_pct}); a smaller threshold cannot be told apart "
                f"from run-to-run noise")
    return th


def _check_approval_fields(data: dict, path: Path) -> None:
    for key in ("approved_by", "approved_on"):
        value = data.get(key)
        if value is not None and not (isinstance(value, (str, datetime.date)) and str(value).strip()):
            raise ValueError(f"perf config {path}: {key!r} must be empty (null) or a non-empty name/date")
    approvers = data.get("baseline_approvers")
    if approvers is not None and not (isinstance(approvers, list) and all(
            isinstance(a, str) and a.strip() for a in approvers)):
        raise ValueError(f"perf config {path}: 'baseline_approvers' must be a list of names")


def load_thresholds(path: str | os.PathLike | None = None) -> Thresholds:
    """Thresholds from the perf config file (see :func:`resolve_config_path`).

    Every key is validated: numbers must be positive, ``noisy_host_cpu_pct`` at most
    100, and the launch and PSS thresholds larger than ``max_spread_pct``. An unknown
    key raises ``ValueError`` naming it. A missing file returns the built-in PROPOSED
    defaults and logs a warning. The returned Thresholds carry ``approved``,
    ``approved_by`` and ``approved_on`` exactly as :func:`threshold_status` reports
    them: approved only if the file has both a name and a date.
    """
    cfg = resolve_config_path(path)
    data = _read_config(cfg)
    if data is None:
        logging.warning("perf: no thresholds file at %s; using built-in PROPOSED defaults "
                        "that are NOT YET APPROVED by the lead", cfg)
        return Thresholds()
    _check_approval_fields(data, cfg)
    status = _status_from(data, cfg)
    return replace(_thresholds_from(data, cfg), approved=status["approved"],
                   approved_by=status["approved_by"], approved_on=status["approved_on"])


def _status_from(data: dict, path: Path) -> dict:
    by, on = data.get("approved_by"), data.get("approved_on")
    by = str(by).strip() if by else None
    on = str(on).strip() if on else None
    return {"approved": bool(by and on), "approved_by": by, "approved_on": on, "source": str(path)}


def threshold_status(path: str | os.PathLike | None = None) -> dict:
    """``{"approved", "approved_by", "approved_on", "source"}`` for the perf config.

    ``approved`` is True only if the file has BOTH a non-empty ``approved_by`` and
    ``approved_on``; a missing file is never approved. Nothing in the code can set
    these values; the lead writes them into perf_config.yaml.
    """
    cfg = resolve_config_path(path)
    data = _read_config(cfg)
    if data is None:
        return {"approved": False, "approved_by": None, "approved_on": None,
                "source": "built-in defaults (no perf config file)"}
    _check_approval_fields(data, cfg)
    return _status_from(data, cfg)


def load_approvers(path: str | os.PathLike | None = None) -> list[str]:
    """Names allowed to promote baselines (``baseline_approvers``); [] if none."""
    data = _read_config(resolve_config_path(path))
    if data is None:
        return []
    _check_approval_fields(data, resolve_config_path(path))
    return [str(a).strip() for a in (data.get("baseline_approvers") or [])]


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


def _service_up(serial: str, adb: Callable, name: str) -> bool:
    out = adb(serial, "shell", "service", "check", name, timeout=ADB_TIMEOUT_S) or ""
    return "found" in out and "not found" not in out


def _package_installed(serial: str, adb: Callable, package: str) -> bool:
    out = adb(serial, "shell", "pm", "path", package, timeout=ADB_TIMEOUT_S) or ""
    return out.strip().startswith("package:")


def _wait_until_online(serial: str, adb: Callable, package: str | None = None) -> None:
    """Block until ``serial`` answers adb, reports sys.boot_completed == 1, the
    activity and package services are registered (BOOT_SERVICES) and, when ``package``
    is given, ``pm path <package>`` finds the app (right after a restore ``am start``
    could still fail with "Activity class ... does not exist").

    adb errors while the device is still "offline" are expected and retried.
    Raises SnapshotError if the device is not back within ADB_BOOT_TIMEOUT_S.
    """
    deadline = time.monotonic() + ADB_BOOT_TIMEOUT_S
    last = "no answer"
    while True:
        try:
            if (adb(serial, "shell", "getprop", "sys.boot_completed",
                    timeout=ADB_TIMEOUT_S) or "").strip() == "1":
                down = [n for n in BOOT_SERVICES if not _service_up(serial, adb, n)]
                if not down and (package is None or _package_installed(serial, adb, package)):
                    return
                last = (f"sys.boot_completed is 1 but service(s) {', '.join(down)} not up yet"
                        if down else f"package {package} is not visible yet")
            else:
                last = "sys.boot_completed is not 1 yet"
        except AdbError as e:
            last = str(e)
        if time.monotonic() >= deadline:
            raise SnapshotError(f"{serial} did not come back online within "
                                f"{ADB_BOOT_TIMEOUT_S}s after the snapshot load; last: {last}")
        threading.Event().wait(ADB_BOOT_POLL_S)


RESET_MODES = ("snapshot", "app")


def reset_app(serial: str, adb: Callable, package: str) -> None:
    """Return the app to its freshly installed state without touching the rest of the device:
    stop it and clear its data (``pm clear``). The alternative to a snapshot restore: on the
    Pixel_6 AVD (Android 16, 3 GB) a restore sometimes froze the guest for many minutes, and
    nothing in perf can wait that out. The system state is NOT reset, so this is only as clean
    as the emulator was when the measurement started."""
    adb(serial, "shell", "am", "force-stop", package, timeout=ADB_TIMEOUT_S)
    out = adb(serial, "shell", "pm", "clear", package, timeout=ADB_TIMEOUT_S) or ""
    if "Success" not in out:
        raise AdbError(f"pm clear {package} on {serial} did not answer Success: {out.strip()!r}")


def save_baseline_snapshot(serial: str, adb: Callable) -> None:
    """Save the current emulator state as the clean ``perf_base`` snapshot."""
    _snapshot(serial, adb, "save")


def load_snapshot(serial: str, adb: Callable, package: str | None = None) -> None:
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
            _wait_until_online(serial, adb, package)
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
        self._cpu_system: list[float] = []
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
            self._cpu_system.append(system)
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

    def summary(self) -> dict:
        """What the sampler saw: sample count, host CPU % (whole machine, and the
        part outside the emulator) as min/mean/max, and the lowest free RAM."""
        with self._lock:
            system, outside, ram = list(self._cpu_system), list(self._cpu_outside), list(self._free_ram)

        def stats(values):
            return ({"min": min(values), "mean": statistics.fmean(values), "max": max(values)}
                    if values else None)
        return {"samples": len(outside), "cpu_pct": stats(system),
                "cpu_outside_emulator_pct": stats(outside),
                "free_ram_min_bytes": min(ram) if ram else None,
                "noisy_threshold_pct": self.noisy_threshold}


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
                 flow: Callable[[], object] | None = None, sampler=None,
                 reset: str = "snapshot") -> dict:
    """One measured run, in this exact order: snapshot load, force-stop,
    ``am start -W``, gfxinfo reset, flow, gfxinfo, meminfo.

    ``activity`` is ``pkg/.Activity`` or just ``.Activity``. If ``flow`` is None
    it is skipped, and jank and PSS then reflect only the launch. PSS is read
    once, after the flow; reading it also right after launch would add an adb
    call and change the specified order. ``sampler`` is only used for the debug
    log. Returns ``{"launch_time", "jank_pct", "pss"}``.
    """
    component = activity if "/" in activity else f"{package}/{activity}"
    if reset == "snapshot":
        _step(serial, "snapshot load", lambda: load_snapshot(serial, adb, package))
    elif reset == "app":
        _step(serial, "app reset", lambda: reset_app(serial, adb, package))
    else:
        raise ValueError(f"reset must be one of {RESET_MODES}, not {reset!r}")
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
            apk: str | None = None, host_load_out: dict | None = None,
            reset: str = "snapshot") -> list[PerfRow]:
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

    ``reset`` is how every run starts from a clean app: "snapshot" (default, the specified
    behaviour: load ``perf_base``) or "app" (stop the app and ``pm clear`` it; no snapshot is
    saved or loaded, ``apk`` is installed if given). See :func:`reset_app` for why "app" exists.

    ``host_load_out``, if given, is filled in with the host-load summary of the
    measurement window (see :meth:`HostSampler.summary`), so a caller can record
    it without measuring again.

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

    if reset not in RESET_MODES:
        raise ValueError(f"reset must be one of {RESET_MODES}, not {reset!r}")
    _step(serial, "isolation check", lambda: assert_isolated(adb, serial))
    if reset == "app":
        if apk:
            _step(serial, "install apk", lambda: install_apk(serial, apk, adb))
    elif not profile.get("perf_snapshot_ready"):
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

    measure_once(serial, package, activity, adb, flow, reset=reset)  # warm-up, numbers discarded

    with sampler_factory() as sampler:
        def batch() -> list[dict]:
            return [measure_once(serial, package, activity, adb, flow, sampler, reset=reset)
                    for _ in range(runs)]

        results = batch()
        limit = thresholds.max_spread_pct
        if any(metric_spread(m, [r[m] for r in results]) > limit for m in METRICS):
            results = batch()
        noisy = bool(sampler.noisy)
        if host_load_out is not None:
            summarise = getattr(sampler, "summary", None)
            if callable(summarise):
                try:
                    host_load_out.update(summarise())
                except Exception:
                    logging.exception("perf: could not summarise the host load")

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
