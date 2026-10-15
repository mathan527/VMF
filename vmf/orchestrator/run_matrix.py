"""run_matrix.py — M10 matrix orchestrator, plus the ARCH dry run.

Owner: Mathan (M10 / ARCH)

``main(run_id, options, deps)`` runs the whole device matrix: a functional pass
on up to MAX_PARALLEL profiles at once (Pass A), then a performance pass one
profile at a time (Pass B). One failing profile never stops the others, and
every emulator it started is stopped. Every external call goes through the
frozen ``Deps`` dataclass so the logic is testable without a device.

Result values saved per profile: the classifier's "pass" / "crash" / "anr" /
"hang", plus "layout" (classifier said pass but geometry found problems) and
"error" (the orchestrator itself failed for that profile). "layout" and "error"
are new values for ``device_results.result`` and need confirming with Gagan.

``run_single_profile`` and ``dry_run`` are the ARCH single-profile path and are
unchanged; ``python -m vmf --dry-run`` uses them.
"""
import argparse
import hashlib
import json
import logging
import os
import queue
import shutil
import statistics
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import yaml

from vmf import api, classifier, geometry, oem_packs, perf, provision, report, runner, storage
from vmf.config import APPIUM_URL, RESULTS_DIR
from vmf.perf.baselines import get_store, sanitise_profile
from vmf.perf.perf import (EMULATOR_PROCESS_NAMES, HostSampler, Thresholds, load_thresholds,
                           threshold_status, tier_ratio)
from vmf.results import PerfRow, Verdict
from vmf.runner.locator_fallback import (FallbackConfig, config_from_options, read_fallback_counts,
                                         strip_tag, validate_fallback_steps)


# ---------------------------------------------------------------------------
# M10 constants
# ---------------------------------------------------------------------------

# Dhatri's capacity test sets the real value (M3.7).
DEFAULT_MAX_PARALLEL = 2
# Environment variable that overrides DEFAULT_MAX_PARALLEL.
MAX_PARALLEL_ENV = "VMF_MAX_PARALLEL"
# Slot n owns console port 5554 + 2n and UiAutomator2 systemPort 8200 + n (VMF-ARC-001).
BASE_CONSOLE_PORT = 5554
CONSOLE_PORT_STEP = 2
BASE_SYSTEM_PORT = 8200
# Appium capability ports, one pair per slot so parallel sessions never share them.
BASE_MJPEG_PORT = 7810
BASE_CHROMEDRIVER_PORT = 9515
# adb only auto-discovers emulators on console ports up to about 5585, which is
# slot index 15. Later slots would boot but never appear in `adb devices`.
MAX_SLOTS = 16
# Seconds a task waits for a slot between checks of the stop request.
SLOT_POLL_S = 0.5
# Seconds a single cleanup step (stop logcat, reset_profile, stop_device) may take
# before it is abandoned and recorded as failed, so cleanup can never block forever.
CLEANUP_TIMEOUT_S = 120
# Minimum free disk in GB before a run starts. The architecture document states
# 100 GB in one place and 20 GB in another; 20 is used here.
PREFLIGHT_MIN_DISK_GB = 20
# Seconds to wait for Appium's /status endpoint.
APPIUM_STATUS_TIMEOUT_S = 5
# Seconds to wait for `adb shell pidof`.
PIDOF_TIMEOUT_S = 10
# Seconds to keep capturing logcat after the scenario's last step before it is stopped and
# classified. A crash that happens in the last moments (14 Oct: `am crash` as the final event, 2
# of 5 trials classified "pass") is written to logcat and the process dies a moment AFTER the
# last step returned; stopping at once missed it. Also the time the process gets to die before
# pidof is asked.
LOGCAT_GRACE_S = 2.0
# Scenario actions that kill the app on purpose. If a scenario ENDS with one of them the app is
# expected to be gone, so the "process gone" crash rule is not applied.
DELIBERATE_KILL_ACTIONS = ("oem.background_kill",)
# Results that an overloaded host can cause (a stalled emulator makes an app miss its 5 s input
# deadline). A verdict of one of these is annotated when the host was busy during the run.
HOST_SENSITIVE_RESULTS = ("anr", "hang", "crash")
# Android API level per tier, from the catalogue header (low=10, medium=12, high=15).
# Only used to build a profile name when the profile has no "api" key.
TIER_API = {"low": 29, "medium": 31, "high": 35}
# Longest finding detail shown in a verdict cause.
CAUSE_DETAIL_MAX_CHARS = 120

RESULT_PASS = "pass"
RESULT_LAYOUT = "layout"
RESULT_ERROR = "error"
RESULT_CANCELLED = "cancelled"

log = logging.getLogger(__name__)


class PreflightError(RuntimeError):
    """A pre-flight check failed; no device was started."""


class RunNotFoundError(RuntimeError):
    """The given run_id has no run row; nothing was started and nothing is finished."""


class NoUsableSlot(RuntimeError):
    """Every slot is quarantined, so no profile can be started."""


class SlotWaitCancelled(Exception):
    """A task stopped waiting for a slot because a stop was requested."""


class ScenarioFailed(RuntimeError):
    """A step of the performance flow failed, so no number may be reported."""


# ---------------------------------------------------------------------------
# Profiles, names, slots (M10 section 2)
# ---------------------------------------------------------------------------

def profile_name(profile: dict) -> str:
    """``profile["name"]`` if present, else ``f"{brand}_{tier}_{api}".lower()``.

    ``api`` falls back to the tier's API level (see ``TIER_API``). Anything that
    is not a letter, digit, ``_``, ``.`` or ``-`` (spaces included) becomes ``_``,
    so a name is always safe as a folder name. The SANITISED name is the profile
    id used for evidence folders, every storage call and ``baselines`` keys:
    baselines for a profile named "Samsung A07" must be keyed "Samsung_A07".
    """
    explicit = profile.get("name")
    if explicit:
        raw = str(explicit)
    else:
        try:
            brand, tier = profile["brand"], profile["tier"]
        except KeyError as e:
            raise ValueError(f"profile has no name and no {e.args[0]!r}: {profile}") from None
        api_level = profile.get("api", TIER_API.get(str(tier).lower()))
        if api_level is None:
            raise ValueError(f"profile has no name and no api for tier {tier!r}: {profile}")
        raw = f"{brand}_{tier}_{api_level}".lower()
    return sanitise_profile(raw)


def load_profiles(path: str) -> list[dict]:
    """Read a YAML list of profile dicts. A mapping with a ``models`` or
    ``profiles`` list (the catalogue layout) is accepted too."""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if isinstance(data, dict):
        data = data.get("profiles", data.get("models"))
    if not isinstance(data, list) or not all(isinstance(p, dict) for p in data):
        raise ValueError(f"{path} must contain a YAML list of profile mappings")
    return data


@dataclass(frozen=True)
class Slot:
    index: int
    port: int          # emulator console port
    system_port: int   # UiAutomator2 systemPort

    @property
    def mjpeg_port(self) -> int:
        return BASE_MJPEG_PORT + self.index

    @property
    def chromedriver_port(self) -> int:
        return BASE_CHROMEDRIVER_PORT + self.index


class SlotPool:
    """Thread-safe pool of ``size`` slots; each owns a unique set of ports.

    A slot is leased with :meth:`lease`, which releases it exactly once in a
    finally owned by the caller. :meth:`release` is idempotent. A slot whose
    emulator could not be cleaned up is :meth:`quarantine`-d: it is never handed
    out again. When every slot is quarantined, waiting tasks get
    :class:`NoUsableSlot` instead of blocking.
    """

    def __init__(self, size: int):
        if size < 1:
            raise ValueError("SlotPool size must be >= 1")
        if size > MAX_SLOTS:
            raise ValueError(
                f"SlotPool size {size} exceeds {MAX_SLOTS}: adb only auto-discovers emulators "
                f"on console ports up to about 5585 (slot index 15), so later slots would "
                f"boot but never be visible in `adb devices`")
        self.size = size
        self._free: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._held: set[int] = set()
        self._quarantined: dict[int, Slot] = {}
        for n in range(size):
            self._free.put(Slot(n, BASE_CONSOLE_PORT + CONSOLE_PORT_STEP * n,
                                BASE_SYSTEM_PORT + n))

    def quarantined_ports(self) -> list[int]:
        with self._lock:
            return sorted(s.port for s in self._quarantined.values())

    def acquire(self, should_stop: Callable[[], bool] | None = None,
                poll_s: float = SLOT_POLL_S) -> Slot:
        """Wait for a free slot, re-checking every ``poll_s`` seconds.

        Raises SlotWaitCancelled if ``should_stop()`` becomes true and
        NoUsableSlot if every slot is quarantined. Never blocks forever.
        """
        while True:
            if should_stop is not None and should_stop():
                raise SlotWaitCancelled()
            with self._lock:
                if len(self._quarantined) >= self.size:
                    ports = sorted(s.port for s in self._quarantined.values())
                    raise NoUsableSlot(
                        f"no usable slot (quarantined: {', '.join(map(str, ports))})")
            try:
                slot = self._free.get(timeout=poll_s)
            except queue.Empty:
                continue
            with self._lock:
                self._held.add(slot.index)
            return slot

    def release(self, slot: Slot) -> bool:
        """Return a held slot to the pool. Idempotent: False if it was not held
        (already released, or quarantined)."""
        with self._lock:
            if slot.index not in self._held:
                return False
            self._held.discard(slot.index)
        self._free.put(slot)
        return True

    def quarantine(self, slot: Slot) -> None:
        """Take a held slot out of circulation for good."""
        with self._lock:
            self._held.discard(slot.index)
            self._quarantined[slot.index] = slot
        log.warning("slot %d (console port %d) is quarantined", slot.index, slot.port)

    @contextmanager
    def lease(self, should_stop: Callable[[], bool] | None = None,
              poll_s: float = SLOT_POLL_S):
        slot = self.acquire(should_stop, poll_s)
        try:
            yield slot
        finally:
            self.release(slot)


def resolve_max_parallel(options: dict, profile_count: int,
                         environ: dict | None = None) -> int:
    """options["max_parallel"], else $VMF_MAX_PARALLEL, else DEFAULT_MAX_PARALLEL;
    validated >= 1, never more than the number of profiles, and at most MAX_SLOTS."""
    environ = os.environ if environ is None else environ
    raw = options.get("max_parallel")
    if raw is None:
        raw = environ.get(MAX_PARALLEL_ENV)
    if raw is None:
        raw = DEFAULT_MAX_PARALLEL
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"max_parallel must be an integer >= 1, got {raw!r}") from None
    if value < 1:
        raise ValueError(f"max_parallel must be >= 1, got {value}")
    effective = min(value, max(1, profile_count))
    if effective > MAX_SLOTS:
        raise ValueError(f"max_parallel {effective} exceeds MAX_SLOTS ({MAX_SLOTS}): adb only "
                         f"auto-discovers emulators on ports up to about 5585")
    return effective


# ---------------------------------------------------------------------------
# Dependency injection
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Deps:
    """Every external call the orchestrator makes, so tests can pass fakes."""
    start_device: Callable
    stop_device: Callable
    apply_profile_settings: Callable
    reset_profile: Callable
    runner_run: Callable
    start_logcat: Callable
    stop_logcat: Callable
    classify: Callable
    geometry_check: Callable
    get_display_zones: Callable
    perf_run_and_store: Callable
    perf_is_regression: Callable
    create_run: Callable
    save_device_result: Callable
    save_profile_config: Callable
    finish_run: Callable
    # (serial, package, on_failure=None) -> Callable[[], bool]; on_failure(msg) is
    # called whenever the device could not answer and "alive" was assumed.
    pid_alive_factory: Callable
    # () -> list[{"pid", "name", "create_time", "cmdline"}]
    list_emulator_processes: Callable
    appium_status: Callable       # () -> bool
    free_disk_gb: Callable        # (path) -> float
    kill_process: Callable        # (pid) -> None
    process_info: Callable        # (pid) -> {"pid", "name", "create_time"} | None
    # (run_id) -> True | False | None. None means "cannot be verified": storage has
    # no public function to look a run up, so the default cannot answer.
    run_exists: Callable
    # () -> context manager with .summary() and .noisy (HostSampler). Watches the host while
    # the scenario runs, so an ANR or hang caused by an overloaded laptop can be told apart.
    host_sampler: Callable = HostSampler


def _pid_alive_factory(serial: str, package: str,
                       on_failure: Callable[[str], None] | None = None) -> Callable[[], bool]:
    """Callable that asks the device whether ``package`` has a live process.

    If adb itself fails the answer is unknown and it returns True: a process is
    only declared gone when the device says so. Every such failure is logged with
    its traceback and reported through ``on_failure`` so a masked crash is visible.
    """
    def report_failure(message: str) -> None:
        if on_failure is not None:
            on_failure(message)

    def alive() -> bool:
        argv = ["adb", "-s", serial, "shell", "pidof", package]
        try:
            result = subprocess.run(argv, capture_output=True, text=True,
                                    timeout=PIDOF_TIMEOUT_S)
        except (OSError, subprocess.SubprocessError) as e:
            log.exception("pidof check failed; assuming %s is alive", package)
            report_failure(f"pidof failed: {type(e).__name__}: {e}")
            return True
        if result.stdout.strip():
            return True
        if result.stderr.strip():
            log.warning("pidof check inconclusive (%s); assuming %s is alive",
                        result.stderr.strip(), package)
            report_failure(f"pidof inconclusive: {result.stderr.strip()}")
            return True
        return False
    return alive


def _list_emulator_processes() -> list[dict]:
    import psutil
    found = []
    for proc in psutil.process_iter(["pid", "name", "create_time", "cmdline"]):
        name = (proc.info.get("name") or "")
        if any(part in name.lower() for part in EMULATOR_PROCESS_NAMES):
            found.append({"pid": proc.info["pid"], "name": name,
                          "create_time": proc.info.get("create_time"),
                          "cmdline": " ".join(proc.info.get("cmdline") or [])})
    return found


def _process_info(pid: int) -> dict | None:
    import psutil
    try:
        proc = psutil.Process(pid)
        return {"pid": pid, "name": proc.name(), "create_time": proc.create_time()}
    except psutil.NoSuchProcess:
        return None


def _kill_process(pid: int) -> None:
    import psutil
    psutil.Process(pid).kill()


def _appium_status() -> bool:
    url = f"{APPIUM_URL}/status"
    try:
        with urllib.request.urlopen(url, timeout=APPIUM_STATUS_TIMEOUT_S) as response:
            return 200 <= response.status < 300
    except (OSError, ValueError):
        log.exception("Appium not reachable at %s", url)
        return False


def _free_disk_gb(path: str) -> float:
    return shutil.disk_usage(path).free / 1024 ** 3


def _run_exists(run_id: int) -> bool | None:
    """Storage has no public function to look a run up, and the database must not
    be opened from here, so existence cannot be verified: answer None."""
    return None


def default_deps() -> Deps:
    """Wire the real modules."""
    return Deps(
        start_device=provision.start_device,
        stop_device=provision.stop_device,
        apply_profile_settings=oem_packs.apply_profile_settings,
        reset_profile=oem_packs.reset_profile,
        runner_run=runner.run,
        start_logcat=classifier.start_logcat,
        stop_logcat=classifier.stop_logcat,
        classify=classifier.classify,
        geometry_check=geometry.check,
        get_display_zones=geometry.get_display_zones,
        perf_run_and_store=perf.run_and_store,
        perf_is_regression=perf.is_regression,
        create_run=storage.create_run,
        save_device_result=storage.save_device_result,
        save_profile_config=storage.save_profile_config,
        finish_run=storage.finish_run,
        pid_alive_factory=_pid_alive_factory,
        list_emulator_processes=_list_emulator_processes,
        appium_status=_appium_status,
        free_disk_gb=_free_disk_gb,
        kill_process=_kill_process,
        process_info=_process_info,
        run_exists=_run_exists,
    )


@dataclass(frozen=True)
class RunContext:
    deps: Deps
    run_id: int
    apk: str
    scenario: str
    package: str
    stop_event: threading.Event                     # always present; set on interrupt
    apply_settings: bool = True
    external_stop: threading.Event | None = None    # options["stop_event"], if any
    thresholds: Thresholds = field(default_factory=Thresholds)
    baselines: dict = field(default_factory=dict)
    thresholds_status: dict = field(default_factory=dict)   # approval status of the thresholds
    max_parallel: int = 1
    slot_poll_s: float = SLOT_POLL_S
    locator_fallback: FallbackConfig | None = None   # M14, functional pass only
    logcat_grace_s: float = field(default_factory=lambda: LOGCAT_GRACE_S)

    def stopping(self) -> bool:
        return self.stop_event.is_set() or (
            self.external_stop is not None and self.external_stop.is_set())


# ---------------------------------------------------------------------------
# Cleanup helpers
# ---------------------------------------------------------------------------

def _host_load(sampler) -> tuple[bool, dict | None]:
    """(busy, summary) from a finished sampler. Never raises: a sampler problem is logged and
    reported as "not busy, no data"."""
    try:
        summary = sampler.summary()
        busy = bool(sampler.noisy) and bool(summary and summary.get("cpu_outside_emulator_pct"))
        return busy, summary
    except Exception:
        log.exception("host load summary failed; continuing without it")
        return False, None


def _ends_with_deliberate_kill(steps) -> bool:
    """True if the scenario's last step kills the app on purpose."""
    return bool(steps) and strip_tag(steps[-1].action) in DELIBERATE_KILL_ACTIONS


def _guarded(label: str, fn: Callable, errors: list[str],
             timeout: float = CLEANUP_TIMEOUT_S) -> bool:
    """Run a cleanup step with a time limit; log (with traceback) and record a
    failure or timeout, never raise. Returns True when the step succeeded.

    The step runs in a daemon thread so a hung cleanup call can neither block the
    caller beyond ``timeout`` nor keep the process alive at exit.
    """
    outcome: dict = {}

    def target() -> None:
        try:
            fn()
        except Exception as e:
            outcome["exc_info"] = sys.exc_info()
            outcome["error"] = e

    worker = threading.Thread(target=target, name=f"vmf-cleanup-{label}", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        log.error("cleanup step %r timed out after %ss and was abandoned", label, timeout)
        errors.append(f"{label}: timed out after {timeout}s")
        return False
    if "error" in outcome:
        log.error("cleanup step %r failed", label, exc_info=outcome["exc_info"])
        errors.append(f"{label}: {type(outcome['error']).__name__}: {outcome['error']}")
        return False
    return True


def _emulator_snapshot(deps: Deps) -> set | None:
    """PIDs of emulator processes right now, or None if they cannot be listed."""
    try:
        return {p["pid"] for p in deps.list_emulator_processes()}
    except Exception:
        log.exception("could not list emulator processes")
        return None


def _mentions_port(proc: dict, port: int) -> bool:
    cmd = proc.get("cmdline") or ""
    return f"-port {port}" in cmd or f"-ports {port}" in cmd or f"emulator-{port}" in cmd


def _new_emulator_processes(ctx: RunContext, before: set | None, slot: Slot) -> list[dict] | None:
    """Emulator processes that appeared since ``before`` and can be attributed to
    ``slot``; None if that cannot be determined.

    Attribution: with max_parallel == 1 every new emulator process is ours; with
    several profiles running, only a process whose command line names the slot's
    port (``-port <n>``, an assumption about how provision launches emulators)
    is attributed, because other profiles legitimately start emulators too.
    """
    if before is None:
        return None
    try:
        now = ctx.deps.list_emulator_processes()
    except Exception:
        log.exception("could not list emulator processes after a failed start")
        return None
    new = [p for p in now if p["pid"] not in before]
    return [p for p in new if ctx.max_parallel == 1 or _mentions_port(p, slot.port)]


def _cleanup_failed_start(ctx: RunContext, slot: Slot, before: set | None,
                          cleanup_errors: list[str]) -> bool:
    """start_device raised, so there is no serial. The emulator may still have
    launched: stop the device derived from the port. Returns True when the slot
    must be quarantined (the stop failed, or a new emulator process we can
    attribute to this slot is still alive, or that could not be checked)."""
    orphan = f"emulator-{slot.port}"
    stopped = _guarded(f"stop_device({orphan}) after failed start",
                       lambda: ctx.deps.stop_device(orphan), cleanup_errors)
    log.info("cleanup after failed start on slot %d: stop_device(%s) %s", slot.index, orphan,
             "succeeded" if stopped else "FAILED")
    suspects = _new_emulator_processes(ctx, before, slot)
    quarantine = (not stopped) or suspects is None or bool(suspects)
    if quarantine:
        log.warning("quarantining slot %d (port %d): stop %s; attributable emulator processes: %s",
                    slot.index, slot.port, "succeeded" if stopped else "failed",
                    "unknown" if suspects is None else
                    [p["pid"] for p in suspects] or "none")
    return quarantine


def _result_dict(profile: dict, *, result: str, cause: str = "", serial=None,
                 findings_count: int = 0, duration_s: float = 0.0, error: bool = False,
                 warnings: list | None = None, cleanup_errors: list | None = None,
                 quarantine: bool = False, locator_fallbacks: dict | None = None) -> dict:
    return {"profile": profile_name(profile), "serial": serial, "tier": profile.get("tier"),
            "result": result, "cause": cause, "findings_count": findings_count,
            "duration_s": duration_s, "error": error, "warnings": warnings or [],
            "cleanup_errors": cleanup_errors or [], "quarantine": quarantine,
            "locator_fallbacks": locator_fallbacks or {"ai_assisted": 0, "unresolved": 0}}


def _cancelled_result(profile: dict) -> dict:
    return _result_dict(profile, result=RESULT_CANCELLED,
                        cause="stop requested before this profile started")


# ---------------------------------------------------------------------------
# Per-profile functional pipeline (M10.1)
# ---------------------------------------------------------------------------

def _geometry_config(profile: dict, readback: dict | None, zones: dict, package: str) -> dict:
    """profile_config for geometry.check. The readback (what oem_packs actually
    applied) wins over the requested profile values."""
    src = {**profile, **(readback or {})}
    config: dict = {"package": package}
    for key in ("width_px", "height_px", "resolution"):
        if src.get(key) is not None:
            config[key] = src[key]
    density = src.get("density_dpi", src.get("density"))
    if density is not None:
        config["density_dpi"] = density
    config.update(zones)
    return config


def _collect_findings(ctx: RunContext, steps: list, config: dict) -> list[dict]:
    """Run geometry on every step's existing, non-empty hierarchy file; combine,
    de-duplicate (remembering which steps saw each finding) and sort."""
    merged: dict[tuple, dict] = {}
    for step in steps:
        path = getattr(step, "hierarchy_path", "")
        if not path or not os.path.isfile(path) or os.path.getsize(path) == 0:
            continue
        with open(path, encoding="utf-8") as f:
            xml = f.read()
        for finding in ctx.deps.geometry_check(xml, config):
            key = (finding.kind, finding.element_id, tuple(finding.bounds), finding.detail)
            entry = merged.setdefault(key, {
                "kind": finding.kind, "element_id": finding.element_id,
                "bounds": list(finding.bounds), "detail": finding.detail, "steps": []})
            entry["steps"].append(step.step_index)
    return [merged[k] for k in sorted(merged)]


def _write_json(run_id: int, name: str, filename: str, payload: dict) -> str:
    out_dir = Path(RESULTS_DIR) / str(run_id) / name
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / filename
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return str(path)


def _write_findings(run_id: int, name: str, findings: list[dict]) -> str:
    return _write_json(run_id, name, "findings.json",
                       {"profile": name, "run_id": run_id, "count": len(findings),
                        "findings": findings})


def _final_verdict(verdict: Verdict, findings: list[dict]) -> Verdict:
    """crash/anr/hang from the classifier are kept. pass + findings -> "layout".
    pass + no findings -> "pass"."""
    if verdict.result != RESULT_PASS or not findings:
        return Verdict(result=verdict.result, cause=verdict.cause,
                       failed_step=verdict.failed_step,
                       trigger_behaviour=verdict.trigger_behaviour)
    first = findings[0]
    detail = " ".join(first["detail"].split())[:CAUSE_DETAIL_MAX_CHARS]
    return Verdict(result=RESULT_LAYOUT,
                   cause=f"{len(findings)} layout finding(s); first: {first['kind']} "
                         f"{first['element_id']}: {detail}",
                   failed_step=verdict.failed_step,
                   trigger_behaviour=verdict.trigger_behaviour)


def _save_error(ctx: RunContext, name: str, exc: BaseException, stage: str) -> str:
    """Save an "error" result for ``name``. A failure here is logged, never raised."""
    cause = f"{type(exc).__name__}: {exc} (stage: {stage})"
    try:
        ctx.deps.save_device_result(ctx.run_id, name, Verdict(
            result=RESULT_ERROR, cause=cause, failed_step=None, trigger_behaviour=None))
    except Exception:
        log.exception("could not save the error result for profile %s", name)
    return cause


def run_profile(profile: dict, ctx: RunContext, slot: Slot) -> dict:
    """Functional pipeline for one profile on ``slot``.

    The caller owns the slot (see :func:`_functional_task`); this function never
    releases it. It only reports ``quarantine`` in its result when a failed
    start left an emulator it could not clean up.

    Order: start_device, apply_profile_settings, display zones, start logcat,
    runner, stop logcat, classify, geometry on every hierarchy, save findings,
    save_profile_config, save_device_result. Cleanup (always, each step guarded
    and time-limited): stop logcat if still running, reset_profile, stop_device;
    after a failed start_device, stop_device on ``emulator-<port>``.

    DECISION: ``pid_alive`` asks the device at the END of the scenario (after
    LOGCAT_GRACE_S) whether the package still has a process. The app is expected
    to be alive then, so a dead process means a crash with cause "process gone".
    A scenario whose LAST step kills the app on purpose (DELIBERATE_KILL_ACTIONS,
    for example oem.background_kill) skips that rule, with a warning; a kill in
    the middle of a scenario is not affected. If adb cannot answer, the app is
    assumed alive and the stored profile config gets ``pid_check_failed: true``.

    An Exception anywhere is logged with its traceback and saved as an "error"
    result for this profile only; it never propagates. A BaseException still
    runs the cleanup and then propagates.
    """
    deps = ctx.deps
    name = profile_name(profile)
    started = time.monotonic()
    serial = None
    proc = None
    logcat_stopped = False
    stage = "start_device"
    run_id = ctx.run_id
    warnings: list[str] = []
    cleanup_errors: list[str] = []
    pid_failures: list[str] = []
    result = RESULT_ERROR
    cause = ""
    findings_count = 0
    failed = False
    start_failed = False
    quarantine = False
    before = None
    fallback_counts = None
    try:
        before = _emulator_snapshot(deps)
        serial = deps.start_device(profile, slot.port)
        stage = "apply_profile_settings"
        readback = deps.apply_profile_settings(serial, profile) if ctx.apply_settings else {}
        readback = readback or {}

        stage = "display_zones"
        try:
            zones = deps.get_display_zones(serial)
        except Exception as e:
            log.exception("display zones failed for %s; continuing without them", name)
            warnings.append(f"display zones: {type(e).__name__}: {e}")
            zones = {"cutout_rects": [], "gesture_rects": []}

        stage = "logcat"
        logcat_path = f"{RESULTS_DIR}/{run_id}/{name}/logcat.txt"
        proc = deps.start_logcat(serial, logcat_path)

        stage = "runner"
        sampler = deps.host_sampler()
        runner_kwargs = {"run_id": run_id, "profile": name, "mjpeg_port": slot.mjpeg_port,
                         "chromedriver_port": slot.chromedriver_port}
        if ctx.locator_fallback is not None:  # M14: only passed when enabled, never to the perf flow
            runner_kwargs["locator_fallback"] = ctx.locator_fallback
        with sampler:
            returned_id, steps = deps.runner_run(
                serial, ctx.apk, ctx.scenario, slot.system_port, **runner_kwargs)
        run_id = returned_id if returned_id is not None else run_id
        if ctx.locator_fallback is not None:
            fallback_counts = read_fallback_counts(f"{RESULTS_DIR}/{run_id}/{name}")

        stage = "stop_logcat"
        if ctx.logcat_grace_s > 0:
            ctx.stop_event.wait(ctx.logcat_grace_s)   # late crash lines, and the process dying
        deps.stop_logcat(proc)
        logcat_stopped = True

        stage = "classify"
        if _ends_with_deliberate_kill(steps):
            pid_alive = None
            warnings.append("process-gone check skipped: the scenario ends after "
                            f"{strip_tag(steps[-1].action)}, which kills the app on purpose")
        else:
            pid_alive = deps.pid_alive_factory(serial, ctx.package, pid_failures.append)
        verdict = deps.classify(logcat_path, steps, package=ctx.package, pid_alive=pid_alive)
        if pid_failures:
            warnings.extend(pid_failures)

        stage = "geometry"
        config = _geometry_config(profile, readback, zones, ctx.package)
        findings = _collect_findings(ctx, steps, config)
        _write_findings(run_id, name, findings)
        findings_count = len(findings)

        final = _final_verdict(verdict, findings)
        host_busy, host_load = _host_load(sampler)
        if host_busy and final.result in HOST_SENSITIVE_RESULTS:
            # The laptop was overloaded while the scenario ran (14 Oct: an idle app ANRed at start-up
            # while tests ran next to the emulator). The verdict stands, but it is marked.
            note = (f" [host busy: {host_load['cpu_outside_emulator_pct']['mean']:.0f}% CPU outside "
                    "the emulator during the run; re-run to confirm]")
            final = Verdict(result=final.result, cause=final.cause + note,
                            failed_step=final.failed_step, trigger_behaviour=final.trigger_behaviour)
            warnings.append("host busy during the run: the " + final.result + " may be a stall")
        stage = "save_profile_config"
        deps.save_profile_config(run_id, name, {
            **readback, "profile": name, "serial": serial, "port": slot.port,
            "system_port": slot.system_port, "tier": profile.get("tier"), "zones": zones,
            "pid_check_failed": bool(pid_failures), "host_busy": host_busy, "host_load": host_load})
        stage = "save_device_result"
        deps.save_device_result(run_id, name, final)
        result, cause = final.result, final.cause
    except Exception as e:
        log.exception("profile %s failed at stage %s", name, stage)
        failed = True
        start_failed = serial is None and stage == "start_device"
        result, cause = RESULT_ERROR, _save_error(ctx, name, e, stage)
    finally:
        if proc is not None and not logcat_stopped:
            _guarded("stop_logcat", lambda: deps.stop_logcat(proc), cleanup_errors)
        if serial is not None:
            _guarded("reset_profile", lambda: deps.reset_profile(serial, ctx.package),
                     cleanup_errors)
            _guarded("stop_device", lambda: deps.stop_device(serial), cleanup_errors)
        elif start_failed:
            quarantine = _cleanup_failed_start(ctx, slot, before, cleanup_errors)
    return _result_dict(profile, result=result, cause=cause, serial=serial,
                        findings_count=findings_count,
                        duration_s=round(time.monotonic() - started, 3), error=failed,
                        warnings=warnings, cleanup_errors=cleanup_errors,
                        quarantine=quarantine, locator_fallbacks=fallback_counts)


def _functional_task(profile: dict, ctx: RunContext, pool: SlotPool) -> dict:
    """One Pass A task. It owns the slot: acquired here, released here (exactly
    once, in the lease's finally) whatever run_profile does.

    Any Exception becomes an "error" result. A BaseException (KeyboardInterrupt,
    SystemExit, ...) is recorded as an "error" result too, the slot is released,
    and then it is re-raised so the main thread sees it.
    """
    name = profile_name(profile)
    try:
        if ctx.stopping():
            return _cancelled_result(profile)
        try:
            with pool.lease(ctx.stopping, ctx.slot_poll_s) as slot:
                if ctx.stopping():  # stop requested between acquire and start
                    return _cancelled_result(profile)
                result = run_profile(profile, ctx, slot)
                if result["quarantine"]:
                    pool.quarantine(slot)
                return result
        except SlotWaitCancelled:
            return _cancelled_result(profile)
        except NoUsableSlot as e:
            log.exception("no usable slot for profile %s", name)
            return _result_dict(profile, result=RESULT_ERROR, error=True,
                                cause=_save_error(ctx, name, e, "slot"))
    except BaseException as e:
        log.exception("worker for profile %s failed", name)
        cause = _save_error(ctx, name, e, "worker")
        if isinstance(e, Exception):
            return _result_dict(profile, result=RESULT_ERROR, cause=cause, error=True)
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            ctx.stop_event.set()
        raise


# ---------------------------------------------------------------------------
# Pass A: functional pass (M10.1, M10.3)
# ---------------------------------------------------------------------------

def _shutdown_executor(executor) -> None:
    """Cancel queued tasks and wait for running ones to finish their cleanup.

    A second interrupt while waiting stops the waiting: running workers keep
    going on their own threads and finish their own cleanup (every cleanup step
    is time-limited), but main no longer blocks on them. The interpreter still
    joins those threads at exit, so a worker stuck in a call that has no timeout
    of its own (for example inside provision.start_device) can delay exit.
    """
    try:
        executor.shutdown(wait=True, cancel_futures=True)
    except KeyboardInterrupt:
        log.warning("second interrupt: no longer waiting for running profiles; their "
                    "workers finish cleanup on their own")
        executor.shutdown(wait=False, cancel_futures=True)


def _collect_future(ctx: RunContext, future, profile: dict) -> tuple[dict, BaseException | None]:
    """The result dict for a finished future, and a BaseException to re-raise later
    (never an ordinary Exception)."""
    name = profile_name(profile)
    if future.cancelled():
        return _cancelled_result(profile), None
    try:
        return future.result(), None
    except CancelledError:
        return _cancelled_result(profile), None
    except BaseException as e:
        if isinstance(e, Exception):
            log.exception("worker for profile %s crashed", name)
            cause = _save_error(ctx, name, e, "worker")
            return _result_dict(profile, result=RESULT_ERROR, cause=cause, error=True), None
        log.error("worker for profile %s ended with %s after cleanup", name, type(e).__name__,
                  exc_info=(type(e), e, e.__traceback__))
        cause = f"{type(e).__name__}: {e} (stage: worker)"  # the worker already saved it
        return _result_dict(profile, result=RESULT_ERROR, cause=cause, error=True), e


def _functional_pass(ctx: RunContext, profiles: list[dict], max_parallel: int
                     ) -> tuple[dict[str, dict], BaseException | None, list[int]]:
    """Run every profile, at most ``max_parallel`` at once, each on its own slot.

    Returns ``(results by profile name, interrupt, quarantined ports)``.

    Stopping is cooperative. On KeyboardInterrupt/SystemExit in the main thread
    (or when ``stop_event`` is set) the stop event is set, queued profiles are
    cancelled and never boot, and running profiles finish their pipeline and
    clean up normally. ``interrupt`` is the exception to re-raise at the end of
    ``main`` once finish_run has run.
    """
    pool = SlotPool(max_parallel)
    executor = ThreadPoolExecutor(max_workers=max_parallel, thread_name_prefix="vmf-profile")
    futures: dict = {}
    results: dict[str, dict] = {}
    interrupt: BaseException | None = None
    try:
        for p in profiles:
            futures[executor.submit(_functional_task, p, ctx, pool)] = p
        for future in as_completed(futures):
            result, base = _collect_future(ctx, future, futures[future])
            results[result["profile"]] = result
            interrupt = interrupt or base
    except (KeyboardInterrupt, SystemExit) as e:
        interrupt = e
        log.warning("%s: no new profile will start; waiting for running profiles to clean up",
                    type(e).__name__)
        ctx.stop_event.set()
    finally:
        _shutdown_executor(executor)
    for future, profile in futures.items():
        name = profile_name(profile)
        if name in results:
            continue
        if future.done() or future.cancelled():
            results[name], base = _collect_future(ctx, future, profile)
            interrupt = interrupt or base
        else:
            results[name] = _result_dict(
                profile, result=RESULT_ERROR, error=True,
                cause="abandoned after a second interrupt; its cleanup may still be running")
    for profile in profiles:
        results.setdefault(profile_name(profile), _cancelled_result(profile))
    ordered = {profile_name(p): results[profile_name(p)] for p in profiles}
    return ordered, interrupt, pool.quarantined_ports()


# ---------------------------------------------------------------------------
# Pass B: performance pass (M10.2)
# ---------------------------------------------------------------------------

def _perf_flow(ctx: RunContext, serial: str, slot: Slot, name: str) -> None:
    """Replay the scenario unrecorded on the already-installed app. A failed step
    raises ScenarioFailed so no number is ever reported from a failed scenario."""
    _, steps = ctx.deps.runner_run(
        serial, ctx.apk, ctx.scenario, slot.system_port, run_id=None, profile=name,
        record=False, reuse_installed_app=True, mjpeg_port=slot.mjpeg_port,
        chromedriver_port=slot.chromedriver_port)
    failed = [s.step_index for s in steps if s.status == "fail"]
    if failed:
        raise ScenarioFailed(f"scenario steps {failed} failed on profile {name}")


PERF_META_NAME = "perf_meta.json"
# Bytes read at a time when hashing the APK.
_HASH_CHUNK = 1024 * 1024


def _sha256_file(path: str) -> str | None:
    """sha256 of a file, or None (logged with the traceback) if it cannot be read."""
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(_HASH_CHUNK), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        log.exception("could not hash %s for perf_meta.json", path)
        return None


def _write_perf_meta(ctx: RunContext, profile: dict, name: str, rows: list, host_load: dict) -> str | None:
    """Write results/<run_id>/<profile>/perf_meta.json and return its path; None (logged,
    never raised) if it could not be written.

    storage's perf_results table has no host_load or build column (requested from Gagan),
    so this file is the interim record. It also holds the per-metric medians that
    ``python -m vmf.perf promote`` reads. Every number is EMULATOR-RELATIVE.
    """
    try:
        metrics = [asdict(r) for r in rows]
        meta = {
            "profile": name, "run_id": ctx.run_id, "label": "emulator-relative",
            "tier": profile.get("tier"),
            "build": {"apk": os.path.basename(ctx.apk), "sha256": _sha256_file(ctx.apk)},
            "runs": ctx.thresholds.runs,
            "metrics": metrics,
            "spread_pct": {m["metric"]: m["spread_pct"] for m in metrics},
            "noisy": any(m["noisy"] for m in metrics),
            "unstable": any(m["unstable"] for m in metrics),
            "host_load": host_load,
            "thresholds": threshold_values(ctx.thresholds),
            "thresholds_approved": bool(ctx.thresholds_status.get("approved")),
            "baseline_compared": {m["metric"]: m["baseline"] is not None for m in metrics},
        }
        return _write_json(ctx.run_id, name, PERF_META_NAME, meta)
    except Exception:
        log.exception("could not write %s for %s", PERF_META_NAME, name)
        return None


def _perf_on_slot(profile: dict, ctx: RunContext, slot: Slot) -> dict:
    deps = ctx.deps
    name = profile_name(profile)
    serial = None
    stage = "start_device"
    cleanup_errors: list[str] = []
    start_failed = False
    quarantine = False
    before = None
    entry: dict = {}
    try:
        before = _emulator_snapshot(deps)
        serial = deps.start_device(profile, slot.port)
        stage = "apply_profile_settings"
        if ctx.apply_settings:
            deps.apply_profile_settings(serial, profile)
        stage = "measure"
        host_load: dict = {}
        rows = deps.perf_run_and_store(
            ctx.run_id, name, serial, profile, package=ctx.package, apk=ctx.apk,
            flow=lambda: _perf_flow(ctx, serial, slot, name),
            baselines=ctx.baselines.get(name), thresholds=ctx.thresholds,
            runs=ctx.thresholds.runs, host_load_out=host_load)
        regressions = [r.metric for r in rows if deps.perf_is_regression(r, ctx.thresholds)]
        entry = {"profile": name, "status": "ok", "rows": len(rows),
                 "regressions": regressions, "cleanup_errors": cleanup_errors,
                 "metrics": [asdict(r) for r in rows]}
        meta_path = _write_perf_meta(ctx, profile, name, rows, host_load)
        if meta_path:
            entry["perf_meta"] = meta_path
    except Exception as e:
        log.exception("performance pass failed for %s at stage %s", name, stage)
        start_failed = serial is None and stage == "start_device"
        entry = {"profile": name, "status": "error",
                 "error": f"{type(e).__name__}: {e} (stage: {stage})",
                 "regressions": [], "cleanup_errors": cleanup_errors}
        try:
            entry["error_file"] = _write_json(ctx.run_id, name, "perf_error.json", {
                "profile": name, "run_id": ctx.run_id, "stage": stage,
                "error": f"{type(e).__name__}: {e}",
                "traceback": traceback.format_exc()})
        except Exception:
            log.exception("could not write perf_error.json for %s", name)
    finally:
        if serial is not None:
            _guarded("reset_profile", lambda: deps.reset_profile(serial, ctx.package),
                     cleanup_errors)
            _guarded("stop_device", lambda: deps.stop_device(serial), cleanup_errors)
        elif start_failed:
            quarantine = _cleanup_failed_start(ctx, slot, before, cleanup_errors)
    entry["quarantine"] = quarantine
    return entry


def _performance_pass(ctx: RunContext, profiles: list[dict], functional: dict[str, dict]
                      ) -> tuple[dict[str, dict], BaseException | None]:
    """One profile at a time, never in parallel, only for profiles whose
    functional result is not "error" or "cancelled". A regression does NOT
    change the functional result; it is only counted in the summary (Gagan's
    report combines both). The stop event is checked between profiles; an
    interrupt during a profile cleans that profile up, marks the rest
    cancelled and is returned for ``main`` to re-raise at the end.

    Perf errors are written to results/<run_id>/<profile>/perf_error.json and
    listed in the summary: storage has no API for them.
    """
    pool = SlotPool(1)
    results: dict[str, dict] = {}
    interrupt: BaseException | None = None
    for profile in profiles:
        name = profile_name(profile)
        if functional[name]["result"] in (RESULT_ERROR, RESULT_CANCELLED):
            results[name] = {"profile": name, "status": "skipped",
                             "reason": f"functional result was {functional[name]['result']}",
                             "regressions": []}
        elif ctx.stopping():
            results[name] = {"profile": name, "status": RESULT_CANCELLED,
                             "reason": "stop requested", "regressions": []}
        else:
            try:
                results[name] = _perf_one(profile, ctx, pool)
            except (KeyboardInterrupt, SystemExit) as e:
                log.warning("%s during the performance pass of %s", type(e).__name__, name)
                interrupt = e
                ctx.stop_event.set()
                results[name] = {"profile": name, "status": "interrupted", "regressions": []}
    return results, interrupt


def _perf_one(profile: dict, ctx: RunContext, pool: SlotPool) -> dict:
    name = profile_name(profile)
    try:
        with pool.lease(ctx.stopping, ctx.slot_poll_s) as slot:
            entry = _perf_on_slot(profile, ctx, slot)
            if entry.pop("quarantine", False):
                pool.quarantine(slot)
            return entry
    except SlotWaitCancelled:
        return {"profile": name, "status": RESULT_CANCELLED, "reason": "stop requested",
                "regressions": []}
    except NoUsableSlot as e:
        log.exception("no usable slot for the performance run of %s", name)
        return {"profile": name, "status": "error", "error": f"{e} (stage: slot)",
                "regressions": []}


# ---------------------------------------------------------------------------
# Pre-flight and leftovers (M10.5)
# ---------------------------------------------------------------------------

def preflight(deps: Deps, options: dict) -> set:
    """Raise PreflightError naming every failed check. Starts nothing, kills nothing.

    Returns the PIDs of emulator processes seen (always empty when it returns,
    because an existing emulator fails the check); ``main`` keeps the set so the
    leftover kill can never touch a process that pre-dated the run.
    """
    problems = []
    seen: set = set()
    try:
        if not deps.appium_status():
            log.error("pre-flight: Appium is not reachable at %s/status", APPIUM_URL)
            problems.append(f"Appium is not reachable at {APPIUM_URL}/status")
    except Exception as e:
        log.exception("pre-flight: Appium check raised")
        problems.append(f"Appium check failed: {type(e).__name__}: {e}")
    minimum = options.get("min_disk_gb", PREFLIGHT_MIN_DISK_GB)
    try:
        free = deps.free_disk_gb(os.getcwd())
        if free < minimum:
            log.error("pre-flight: free disk is %.1f GB, need %s GB", free, minimum)
            problems.append(f"free disk is {free:.1f} GB, need at least {minimum} GB")
    except Exception as e:
        log.exception("pre-flight: disk check raised")
        problems.append(f"disk check failed: {type(e).__name__}: {e}")
    try:
        orphans = deps.list_emulator_processes()
        seen = {p["pid"] for p in orphans}
        if orphans:
            listed = ", ".join(f"{p['name']} (pid {p['pid']})" for p in orphans)
            log.error("pre-flight: orphan emulator processes: %s", listed)
            problems.append(f"orphan emulator processes already running: {listed}")
    except Exception as e:
        log.exception("pre-flight: orphan process check raised")
        problems.append(f"orphan process check failed: {type(e).__name__}: {e}")
    if problems:
        raise PreflightError("pre-flight failed: " + "; ".join(problems))
    return seen


def _check_leftovers(deps: Deps, kill: bool, run_started: float,
                     preflight_pids: set) -> dict:
    """Report emulator processes still alive after the run.

    Returns ``{"check", "processes", "killed", "skipped", "kill_failures"}``.
    ``check`` is "ok" or "failed: <error>"; when it failed ``processes`` is None
    (unknown), never an empty list. With ``kill`` a process is killed only if it
    (a) was not present at pre-flight, (b) was created at or after
    ``run_started``, and (c) still has the same emulator-looking name when
    re-read immediately before the kill. Everything else is skipped and logged
    "skipped: not ours".
    """
    report = {"check": "ok", "processes": [], "killed": [], "skipped": [], "kill_failures": []}
    try:
        leftovers = list(deps.list_emulator_processes())
    except Exception as e:
        log.exception("leftover emulator check failed")
        report.update(check=f"failed: {type(e).__name__}: {e}", processes=None)
        return report
    report["processes"] = leftovers
    if not leftovers:
        return report
    log.warning("leftover emulator processes: %s",
                ", ".join(f"{p['name']} (pid {p['pid']})" for p in leftovers))
    if not kill:
        return report
    for proc in leftovers:
        pid = proc["pid"]
        try:
            reason = _not_ours(deps, proc, run_started, preflight_pids)
        except Exception as e:
            log.exception("could not re-read pid %s before killing it", pid)
            report["kill_failures"].append({"pid": pid, "error": f"{type(e).__name__}: {e}"})
            continue
        if reason:
            log.warning("pid %s skipped: not ours (%s)", pid, reason)
            report["skipped"].append({"pid": pid, "reason": reason})
            continue
        try:
            deps.kill_process(pid)
            report["killed"].append(pid)
        except Exception as e:
            log.exception("could not kill pid %s", pid)
            report["kill_failures"].append({"pid": pid, "error": f"{type(e).__name__}: {e}"})
    return report


def _not_ours(deps: Deps, proc: dict, run_started: float, preflight_pids: set) -> str | None:
    """Why ``proc`` must not be killed, or None if it is safe to kill."""
    pid = proc["pid"]
    if pid in preflight_pids:
        return "it existed at pre-flight"
    now = deps.process_info(pid)
    if now is None:
        return "it is already gone"
    created = now.get("create_time")
    if created is None or created < run_started:
        return "it started before this run"
    name = (now.get("name") or "").lower()
    if name != (proc.get("name") or "").lower() or not any(
            part in name for part in EMULATOR_PROCESS_NAMES):
        return f"its name is now {now.get('name')!r} (pid reused)"
    return None


# ---------------------------------------------------------------------------
# Entry points (M10.4)
# ---------------------------------------------------------------------------

def _scenario_info(path: str) -> tuple[str, str | None]:
    try:
        with open(path, encoding="utf-8") as f:
            spec = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"option 'scenario' is not valid YAML: {path}: {e}") from e
    if not isinstance(spec, dict):
        raise ValueError(f"option 'scenario' must be a YAML mapping: {path}")
    return spec.get("name") or os.path.basename(path), spec.get("package")


@dataclass(frozen=True)
class _Plan:
    apk: str
    scenario: str
    scenario_name: str
    package: str
    profiles: list
    names: list
    max_parallel: int
    baselines: dict
    thresholds: Thresholds
    performance_pass: bool
    apply_settings: bool
    locator_fallback: FallbackConfig | None = None
    thresholds_status: dict = field(default_factory=dict)
    baseline_source: str = "options"
    baseline_error: str | None = None
    no_baseline: list = field(default_factory=list)


def _resolve_thresholds(options: dict, performance_pass: bool) -> tuple[Thresholds, dict]:
    """``options["thresholds"]`` wins; else perf_config.yaml (see perf.load_thresholds).

    Also returns the approval status. It is never reported as approved unless
    perf_config.yaml itself carries ``approved_by`` and ``approved_on``; thresholds
    passed in options carry no approval at all.
    """
    given = options.get("thresholds")
    if given:
        return given, {"approved": False, "approved_by": None, "approved_on": None,
                       "source": "options['thresholds']"}
    if not performance_pass:
        return Thresholds(), {"approved": False, "approved_by": None, "approved_on": None,
                              "source": "not used (performance pass off)"}
    return load_thresholds(), threshold_status()


def _resolve_baselines(options: dict, names: list, performance_pass: bool) -> tuple[dict, str, str | None]:
    """``options["baselines"]`` wins; else the baseline store (perf.baselines.get_store).

    Returns ``(baselines by profile, where they came from, error text or None)``. A
    store that cannot be read is logged and the profiles are measured WITHOUT baselines.
    """
    given = options.get("baselines")
    if given is not None:
        if not isinstance(given, dict):
            raise ValueError("option 'baselines' must be a dict keyed by (sanitised) profile name")
        for key in given:
            if key not in names:
                log.warning("baselines key %r matches no profile (profile names are sanitised, "
                            "for example spaces become '_'; known: %s)", key, names)
        return given, "options", None
    if not performance_pass:
        return {}, "not used (performance pass off)", None
    store = get_store()
    try:
        found = store.load_all(names)
    except Exception as e:
        log.exception("could not load baselines from %s; measuring without baselines", store.name)
        return {}, store.name, f"{type(e).__name__}: {e}"
    return found, store.name, None


def _validate_options(options: dict) -> _Plan:
    """Check every input before anything happens. Raises ValueError naming the
    option; has no side effects."""
    if not isinstance(options, dict):
        raise ValueError("options must be a dict")
    apk = options.get("apk")
    if not apk or not isinstance(apk, str):
        raise ValueError("option 'apk' is required (a path string)")
    scenario = options.get("scenario")
    if not scenario or not isinstance(scenario, str):
        raise ValueError("option 'scenario' is required (a path string)")
    if not os.path.isfile(scenario):
        raise ValueError(f"option 'scenario' file not found: {scenario}")
    profiles = options.get("profiles")
    if profiles is None:
        path = options.get("profiles_path")
        if not path:
            raise ValueError("option 'profiles' (a list) or 'profiles_path' is required")
        if not os.path.isfile(path):
            raise ValueError(f"option 'profiles_path' file not found: {path}")
        profiles = load_profiles(path)
    if not isinstance(profiles, list) or not profiles:
        raise ValueError("option 'profiles' must be a non-empty list of profile dicts "
                         "(no profiles to run)")
    if not all(isinstance(p, dict) for p in profiles):
        raise ValueError("option 'profiles' must contain only dicts")
    names = [profile_name(p) for p in profiles]
    if len(set(names)) != len(names):
        raise ValueError("option 'profiles' has duplicate profile names: "
                         f"{sorted({n for n in names if names.count(n) > 1})}")
    scenario_name, scenario_package = _scenario_info(scenario)
    package = options.get("package") or scenario_package
    if not package:
        raise ValueError("no package: pass option 'package' or set 'package' in the scenario")
    performance_pass = bool(options.get("performance_pass", True))
    thresholds, thresholds_status_ = _resolve_thresholds(options, performance_pass)
    baselines, baseline_source, baseline_error = _resolve_baselines(options, names, performance_pass)
    no_baseline = [n for n in names if n not in baselines] if performance_pass else []
    locator_fallback = config_from_options(options)
    if locator_fallback is not None:  # reject a malformed `fallback: true` step before anything starts
        with open(scenario, encoding="utf-8") as f:
            validate_fallback_steps(yaml.safe_load(f) or {})
    return _Plan(apk=apk, scenario=scenario, scenario_name=scenario_name, package=package,
                 profiles=profiles, names=names,
                 max_parallel=resolve_max_parallel(options, len(profiles)),
                 baselines=baselines, thresholds=thresholds,
                 performance_pass=performance_pass,
                 apply_settings=bool(options.get("apply_settings", True)),
                 locator_fallback=locator_fallback, thresholds_status=thresholds_status_,
                 baseline_source=baseline_source, baseline_error=baseline_error,
                 no_baseline=no_baseline)


def main(run_id: int | None, options: dict, deps: Deps | None = None) -> dict:
    """Run the whole matrix and return a summary dict.

    ``options``: apk, scenario, profiles (list of dicts) or profiles_path,
    package (else the scenario YAML's ``package``), performance_pass (True),
    apply_settings (True), max_parallel, baselines ({sanitised profile name:
    {metric: value}}), thresholds, stop_event, kill_leftovers, min_disk_gb.

    ``run_id`` None is command-line mode: the inputs are validated first (an
    invalid input creates no run), then one run is created with ``create_run``
    (before pre-flight, so a failed pre-flight can still be closed). A given
    ``run_id`` is API mode: ``create_run`` is never called; a missing run row
    raises RunNotFoundError before anything starts and without ``finish_run``
    (nothing to finish). Once the run is known, ``finish_run`` is called exactly
    once in a finally, even if validation, pre-flight or a pass raises.
    ``storage.finish_run`` has no "failed" status, so a failed run is closed the
    same way as a finished one.

    Order: pre-flight, Pass A, Pass B, leftover check, finish_run. A
    KeyboardInterrupt/SystemExit stops new profiles, lets running ones clean up,
    still runs the leftover check and finish_run, and is re-raised at the very end.
    """
    deps = deps or default_deps()
    started = time.monotonic()
    run_started = time.time()  # wall clock, comparable with process create_time
    plan: _Plan | None = None
    if run_id is None:
        plan = _validate_options(options)
        run_id = deps.create_run(os.path.basename(plan.apk), plan.scenario_name, plan.names)
    else:
        exists = deps.run_exists(run_id)
        if exists is False:
            raise RunNotFoundError(f"run {run_id} does not exist; nothing was started")
        if exists is None:
            log.warning("cannot verify that run %s exists (storage has no lookup); "
                        "continuing", run_id)

    failed = True
    interrupt: BaseException | None = None
    try:
        plan = plan or _validate_options(options)
        ctx = RunContext(deps=deps, run_id=run_id, apk=plan.apk, scenario=plan.scenario,
                         package=plan.package, stop_event=threading.Event(),
                         apply_settings=plan.apply_settings,
                         external_stop=options.get("stop_event"),
                         thresholds=plan.thresholds, baselines=plan.baselines,
                         max_parallel=plan.max_parallel,
                         slot_poll_s=options.get("slot_poll_s", SLOT_POLL_S),
                         locator_fallback=plan.locator_fallback,
                         logcat_grace_s=options.get("logcat_grace_s", LOGCAT_GRACE_S),
                         thresholds_status=plan.thresholds_status)
        if plan.performance_pass:
            if not plan.thresholds_status.get("approved"):
                log.warning("perf thresholds are PROPOSED, not approved by the lead")
            for missing in plan.no_baseline:
                log.warning("no baseline for %s: it is measured but NOT compared with a baseline", missing)
        preflight_pids = preflight(deps, options)
        functional, interrupt, quarantined = _functional_pass(ctx, plan.profiles,
                                                              plan.max_parallel)
        perf_results: dict[str, dict] = {}
        if plan.performance_pass:
            perf_results, perf_interrupt = _performance_pass(ctx, plan.profiles, functional)
            interrupt = interrupt or perf_interrupt
        leftover = _check_leftovers(deps, bool(options.get("kill_leftovers")), run_started,
                                    preflight_pids)
        failed = False
    finally:
        try:
            deps.finish_run(run_id)
        except Exception:
            log.exception("finish_run failed for run %s", run_id)
            if not failed:
                raise
    if interrupt is not None:
        raise interrupt

    names = plan.names
    results = [functional[n] for n in names]
    counts: dict[str, int] = {}
    for r in results:
        counts[r["result"]] = counts.get(r["result"], 0) + 1
    if leftover["check"] != "ok":
        status = "unverified"        # could not verify that no emulators were left
    elif leftover["processes"]:
        status = "leftovers"
    else:
        status = "ok"
    summary = {
        "run_id": run_id,
        "status": status,
        "counts": counts,
        "profiles": results,
        "performance": [perf_results[n] for n in names] if perf_results else [],
        "perf_regressions": [{"profile": n, "metrics": perf_results[n]["regressions"]}
                             for n in names if n in perf_results and perf_results[n]["regressions"]],
        "perf_errors": [{"profile": n, "error": perf_results[n]["error"],
                         "file": perf_results[n].get("error_file")}
                        for n in names if perf_results.get(n, {}).get("status") == "error"],
        "cancelled": [r["profile"] for r in results if r["result"] == RESULT_CANCELLED],
        "quarantined_ports": quarantined,
        "locator_fallbacks": {r["profile"]: r["locator_fallbacks"] for r in results
                              if r["locator_fallbacks"]["ai_assisted"] or r["locator_fallbacks"]["unresolved"]},
        "thresholds": {**plan.thresholds_status, "values": threshold_values(plan.thresholds)},
        "baselines": {"source": plan.baseline_source, "error": plan.baseline_error},
        "no_baseline": plan.no_baseline,
        "baseline_notes": _baseline_notes(plan, perf_results),
        "perf_meta": {n: perf_results[n]["perf_meta"] for n in names
                      if perf_results.get(n, {}).get("perf_meta")},
        "leftover_check": leftover["check"],
        "leftover_processes": leftover["processes"],
        "leftovers_killed": leftover["killed"],
        "leftovers_skipped": leftover["skipped"],
        "kill_failures": leftover["kill_failures"],
        "max_parallel": plan.max_parallel,
        "duration_s": round(time.monotonic() - started, 3),
    }
    ratios = _tier_ratio_summary(plan, perf_results)
    if ratios:   # only when a low-tier AND a high-tier profile were both measured
        summary["tier_ratio"] = ratios
    return summary


def threshold_values(thresholds: Thresholds) -> dict:
    """The threshold numbers only. Approval is reported once, from perf_config.yaml, under
    summary["thresholds"]["approved"]; a Thresholds object never vouches for itself."""
    return {k: v for k, v in asdict(thresholds).items() if not k.startswith("approved")}


def _baseline_notes(plan: _Plan, perf_results: dict) -> list[str]:
    """"no baseline for X" for every profile without one, and "no baseline for X/metric"
    for a measured metric that had none, so no number is reported without saying
    whether it was compared with a baseline (M8.12)."""
    notes = [f"no baseline for {n}" for n in plan.no_baseline]
    for name in plan.names:
        if name in plan.no_baseline:
            continue
        for m in perf_results.get(name, {}).get("metrics", []):
            if m.get("baseline") is None:
                notes.append(f"no baseline for {name}/{m['metric']}")
    return notes


def _tier_ratio_summary(plan: _Plan, perf_results: dict) -> dict | None:
    """low-tier median / high-tier median per metric (perf.tier_ratio), from the profiles
    of those tiers that were measured successfully; None unless both tiers have one."""
    tiers = {profile_name(p): str(p.get("tier", "")).lower() for p in plan.profiles}
    by_tier: dict[str, dict[str, list[float]]] = {}
    for name, entry in perf_results.items():
        if entry.get("status") != "ok":
            continue
        for m in entry.get("metrics", []):
            by_tier.setdefault(tiers.get(name, ""), {}).setdefault(m["metric"], []).append(m["median"])
    if not by_tier.get("low") or not by_tier.get("high"):
        return None

    def rows(tier: str) -> list[PerfRow]:
        return [PerfRow(metric, statistics.median(values), 0.0, None, None, False, False)
                for metric, values in by_tier[tier].items()]
    return tier_ratio(rows("low"), rows("high")) or None


def cli(argv: list[str] | None = None) -> int:
    """``python -m vmf.orchestrator.run_matrix --apk A --scenario S --profiles P.yaml``.

    With no arguments the ARCH dry run is executed, as before."""
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        dry_run()
        return 0
    parser = argparse.ArgumentParser(prog="python -m vmf.orchestrator.run_matrix")
    parser.add_argument("--apk", required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--profiles", required=True, help="YAML list of profile dicts")
    parser.add_argument("--max-parallel", type=int)
    parser.add_argument("--run-id", type=int)
    parser.add_argument("--no-perf", action="store_true", help="skip the performance pass")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    options = {"apk": args.apk, "scenario": args.scenario, "profiles_path": args.profiles,
               "performance_pass": not args.no_perf}
    if args.max_parallel is not None:
        options["max_parallel"] = args.max_parallel
    try:
        summary = main(args.run_id, options)
    except (ValueError, KeyError, OSError) as exc:
        # bad input (missing file, unknown option, duplicate names, ...): one clear line,
        # exit code 2, the traceback is kept in the DEBUG log
        log.debug("invalid input: %s", exc, exc_info=True)  # traceback only with -v/DEBUG
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (PreflightError, RunNotFoundError) as exc:
        log.debug("run not started: %s", exc, exc_info=True)
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2, default=str))
    return 0


# ---------------------------------------------------------------------------
# Public ARCH entry points
# ---------------------------------------------------------------------------

def run_single_profile(profile: dict, apk: str, scenario: str,
                       port: int = 5554, stub: bool = True) -> dict:
    """Execute the full VMF pipeline for one device profile.

    During ARCH (``stub=True``) this calls the stubs of every interface.
    M10 will call with ``stub=False`` against real emulators.

    Args:
        profile:  Device profile dict (brand, tier, cutout, density, ...).
        apk:      Path (real or stub) to the APK under test.
        scenario: Path (real or stub) to the scenario YAML file.
        port:     Base emulator port (default 5554).
        stub:     If True, runner operates in dry-run mode (no Appium).

    Returns:
        Summary dict: serial, run_id, steps, verdict, findings,
        perf_rows, report_path.
    """
    log = logging.getLogger(__name__)

    # 1. Provision --------------------------------------------------------
    log.info("[STUB] provision.start_device()")
    serial = provision.start_device(profile, port)

    try:
        # 2. OEM profile settings -----------------------------------------
        log.info("[STUB] oem_packs.apply_profile_settings()")
        oem_packs.apply_profile_settings(serial, profile)

        # 3. Storage — open run record ------------------------------------
        log.info("[STUB] storage.create_run()")
        # NOTE: run_id is created inside runner.run_scenario() when stub=True,
        # so we obtain it from the return value rather than calling create_run
        # here directly.  This avoids double-counting the run.

        # 4. Runner — actually call runner.run_scenario() -----------------
        log.info("[STUB] runner.run_scenario()")
        run_id, steps = runner.run_scenario(
            serial=serial,
            apk=apk,
            scenario=scenario,
            system_port=8200,
            dry_run=stub,
        )
        log.info("runner.run_scenario() returned run_id=%s steps=%d", run_id, len(steps))

        # 5. Classifier — determine verdict --------------------------------
        log.info("[STUB] classifier.classify()")
        verdict = classifier.classify(steps)

        # 6. Geometry — validate UI layout ---------------------------------
        log.info("[STUB] geometry.validate_geometry()")
        findings = geometry.validate_geometry("<hierarchy/>", profile)

        # 7. Performance — collect metrics ---------------------------------
        log.info("[STUB] perf.run_performance()")
        perf_rows = perf.run_performance(serial, "com.example.app", profile)

        # 8. Storage — persist results -------------------------------------
        log.info("[STUB] storage.save_device_result()")
        storage.save_device_result(run_id, serial, verdict)

        log.info("[STUB] storage.save_perf()")
        storage.save_perf(run_id, serial, perf_rows)

        log.info("[STUB] storage.save_profile_config()")
        storage.save_profile_config(run_id, serial, profile)

        log.info("[STUB] storage.finish_run()")
        storage.finish_run(run_id)

        # 9. Report --------------------------------------------------------
        log.info("[STUB] report.generate_report()")
        report_path = report.generate_report(run_id)

        # 10. API — confirm factory is importable --------------------------
        log.info("[STUB] api.create_app()")
        _ = api.create_app()

        return {
            "serial": serial,
            "run_id": run_id,
            "steps": steps,
            "verdict": verdict,
            "findings": findings,
            "perf_rows": perf_rows,
            "report_path": report_path,
        }

    finally:
        # 11. Provision — stop device -------------------------------------
        log.info("[STUB] provision.stop_device()")
        provision.stop_device(serial)


# ---------------------------------------------------------------------------
# Dry-run convenience wrapper
# ---------------------------------------------------------------------------

def dry_run() -> None:
    """Execute a single-profile ARCH dry-run and print a formatted summary.

    Calls the real interface of every module in sequence.
    No emulator, ADB, or Appium required.
    """
    print()
    print("=== VMF ARCH DRY RUN ===")
    print()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    profile = {
        "brand": "TEST",
        "tier": "low",
        "cutout": "notch",
        "density": 420,
        "font_scale": 1.3,
        "dark_mode": True,
    }

    result = run_single_profile(
        profile=profile,
        apk="app.apk",
        scenario="scenarios/login_flow.yaml",
        port=5554,
        stub=True,
    )

    print()
    print("--- DRY RUN SUMMARY ---")
    print(f"  serial     : {result['serial']}")
    print(f"  run_id     : {result['run_id']}")
    print(f"  steps      : {len(result['steps'])}")
    print(f"  verdict    : {result['verdict'].result}")
    print(f"  findings   : {len(result['findings'])}")
    print(f"  perf rows  : {len(result['perf_rows'])}")
    print(f"  report     : {result['report_path']}")
    print()
    print("=== ARCH DRY RUN PASSED ===")
    print()


if __name__ == "__main__":
    raise SystemExit(cli())
