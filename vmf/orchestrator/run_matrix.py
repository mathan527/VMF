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
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import yaml

from vmf import api, classifier, geometry, oem_packs, perf, provision, report, runner, storage
from vmf.config import APPIUM_URL, RESULTS_DIR
from vmf.perf.perf import EMULATOR_PROCESS_NAMES, Thresholds
from vmf.results import Verdict


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
# Minimum free disk in GB before a run starts. The architecture document states
# 100 GB in one place and 20 GB in another; 20 is used here.
PREFLIGHT_MIN_DISK_GB = 20
# Seconds to wait for Appium's /status endpoint.
APPIUM_STATUS_TIMEOUT_S = 5
# Seconds to wait for `adb shell pidof`.
PIDOF_TIMEOUT_S = 10
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


# ---------------------------------------------------------------------------
# Profiles, names, slots (M10 section 2)
# ---------------------------------------------------------------------------

def profile_name(profile: dict) -> str:
    """``profile["name"]`` if present, else ``f"{brand}_{tier}_{api}".lower()``.

    ``api`` falls back to the tier's API level (see ``TIER_API``). Characters
    that are unsafe in a folder name become ``_``.
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
    return re.sub(r"[^\w.-]+", "_", raw.strip())


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


class SlotPool:
    """Thread-safe pool of ``size`` slots; each owns a unique pair of ports."""

    def __init__(self, size: int):
        if size < 1:
            raise ValueError("SlotPool size must be >= 1")
        self.size = size
        self._free: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._held: set[int] = set()
        for n in range(size):
            self._free.put(Slot(n, BASE_CONSOLE_PORT + CONSOLE_PORT_STEP * n,
                                BASE_SYSTEM_PORT + n))

    def acquire(self, timeout: float | None = None) -> Slot:
        slot = self._free.get(timeout=timeout)
        with self._lock:
            self._held.add(slot.index)
        return slot

    def release(self, slot: Slot) -> None:
        with self._lock:
            if slot.index not in self._held:
                raise ValueError(f"slot {slot.index} is not held")
            self._held.discard(slot.index)
        self._free.put(slot)


def resolve_max_parallel(options: dict, profile_count: int,
                         environ: dict | None = None) -> int:
    """options["max_parallel"], else $VMF_MAX_PARALLEL, else DEFAULT_MAX_PARALLEL;
    validated >= 1 and never more than the number of profiles."""
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
    return min(value, max(1, profile_count))


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
    pid_alive_factory: Callable   # (serial, package) -> Callable[[], bool]
    list_emulator_processes: Callable   # () -> list[{"pid": int, "name": str}]
    appium_status: Callable       # () -> bool
    free_disk_gb: Callable        # (path) -> float
    kill_process: Callable        # (pid) -> None


def _pid_alive_factory(serial: str, package: str) -> Callable[[], bool]:
    """Callable that asks the device whether ``package`` has a live process.

    If adb itself fails the answer is unknown and it returns True with a
    warning: a process is only declared gone when the device says so.
    """
    def alive() -> bool:
        argv = ["adb", "-s", serial, "shell", "pidof", package]
        try:
            result = subprocess.run(argv, capture_output=True, text=True,
                                    timeout=PIDOF_TIMEOUT_S)
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("pidof check failed (%s); assuming %s is alive", e, package)
            return True
        if result.stdout.strip():
            return True
        if result.stderr.strip():
            log.warning("pidof check inconclusive (%s); assuming %s is alive",
                        result.stderr.strip(), package)
            return True
        return False
    return alive


def _list_emulator_processes() -> list[dict]:
    import psutil
    found = []
    for proc in psutil.process_iter(["pid", "name"]):
        name = (proc.info.get("name") or "")
        if any(part in name.lower() for part in EMULATOR_PROCESS_NAMES):
            found.append({"pid": proc.info["pid"], "name": name})
    return found


def _kill_process(pid: int) -> None:
    import psutil
    psutil.Process(pid).kill()


def _appium_status() -> bool:
    url = f"{APPIUM_URL}/status"
    try:
        with urllib.request.urlopen(url, timeout=APPIUM_STATUS_TIMEOUT_S) as response:
            return 200 <= response.status < 300
    except (OSError, ValueError) as e:
        log.warning("Appium not reachable at %s: %s", url, e)
        return False


def _free_disk_gb(path: str) -> float:
    return shutil.disk_usage(path).free / 1024 ** 3


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
    )


@dataclass(frozen=True)
class RunContext:
    deps: Deps
    run_id: int
    apk: str
    scenario: str
    package: str
    apply_settings: bool = True
    stop_event: threading.Event | None = None
    thresholds: Thresholds = field(default_factory=Thresholds)
    baselines: dict = field(default_factory=dict)

    def stopping(self) -> bool:
        return self.stop_event is not None and self.stop_event.is_set()


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


def _write_findings(run_id: int, name: str, findings: list[dict]) -> str:
    out_dir = Path(RESULTS_DIR) / str(run_id) / name
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "findings.json"
    path.write_text(json.dumps({"profile": name, "run_id": run_id, "count": len(findings),
                                "findings": findings}, indent=2), encoding="utf-8")
    return str(path)


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


def _guarded(label: str, fn: Callable, errors: list[str]) -> None:
    """Run a cleanup step; log (with traceback) and record a failure, never raise."""
    try:
        fn()
    except Exception as e:
        log.exception("cleanup step %r failed", label)
        errors.append(f"{label}: {type(e).__name__}: {e}")


def run_profile(profile: dict, ctx: RunContext, pool: SlotPool, slot: Slot) -> dict:
    """Functional pipeline for one profile on ``slot``; always releases the slot.

    Order: start_device, apply_profile_settings, display zones, start logcat,
    runner, stop logcat, classify, geometry on every hierarchy, save findings,
    save_profile_config, save_device_result. Cleanup (always, each step guarded):
    stop logcat if still running, reset_profile, stop_device, release the slot.

    DECISION: ``pid_alive`` asks the device at the END of the scenario whether
    the package still has a process. The app is expected to be alive then, so a
    dead process means a crash with cause "process gone". A scenario that ends
    after killing the app on purpose (for example an oem.background_kill as its
    last step) would be reported as a crash.

    An exception anywhere is logged with its traceback and saved as an "error"
    result for this profile only; it never propagates.
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
    result = RESULT_ERROR
    cause = ""
    findings_count = 0
    failed = False
    try:
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
        returned_id, steps = deps.runner_run(serial, ctx.apk, ctx.scenario, slot.system_port,
                                             run_id=run_id, profile=name)
        run_id = returned_id if returned_id is not None else run_id

        stage = "stop_logcat"
        deps.stop_logcat(proc)
        logcat_stopped = True

        stage = "classify"
        verdict = deps.classify(logcat_path, steps, package=ctx.package,
                                pid_alive=deps.pid_alive_factory(serial, ctx.package))

        stage = "geometry"
        config = _geometry_config(profile, readback, zones, ctx.package)
        findings = _collect_findings(ctx, steps, config)
        _write_findings(run_id, name, findings)
        findings_count = len(findings)

        final = _final_verdict(verdict, findings)
        stage = "save_profile_config"
        deps.save_profile_config(run_id, name, {
            **readback, "profile": name, "serial": serial, "port": slot.port,
            "system_port": slot.system_port, "tier": profile.get("tier"), "zones": zones})
        stage = "save_device_result"
        deps.save_device_result(run_id, name, final)
        result, cause = final.result, final.cause
    except Exception as e:
        log.exception("profile %s failed at stage %s", name, stage)
        failed = True
        result, cause = RESULT_ERROR, _save_error(ctx, name, e, stage)
    finally:
        if proc is not None and not logcat_stopped:
            _guarded("stop_logcat", lambda: deps.stop_logcat(proc), cleanup_errors)
        if serial is not None:
            _guarded("reset_profile", lambda: deps.reset_profile(serial, ctx.package),
                     cleanup_errors)
            _guarded("stop_device", lambda: deps.stop_device(serial), cleanup_errors)
        _guarded("release_slot", lambda: pool.release(slot), cleanup_errors)
    return {"profile": name, "serial": serial, "tier": profile.get("tier"),
            "result": result, "cause": cause, "findings_count": findings_count,
            "duration_s": round(time.monotonic() - started, 3), "error": failed,
            "warnings": warnings, "cleanup_errors": cleanup_errors}


def _cancelled_result(profile: dict) -> dict:
    return {"profile": profile_name(profile), "serial": None, "tier": profile.get("tier"),
            "result": RESULT_CANCELLED, "cause": "stop requested before this profile started",
            "findings_count": 0, "duration_s": 0.0, "error": False,
            "warnings": [], "cleanup_errors": []}


def _functional_task(profile: dict, ctx: RunContext, pool: SlotPool) -> dict:
    if ctx.stopping():
        return _cancelled_result(profile)
    slot = pool.acquire()
    if ctx.stopping():
        pool.release(slot)
        return _cancelled_result(profile)
    return run_profile(profile, ctx, pool, slot)


# ---------------------------------------------------------------------------
# Pass A: functional pass (M10.1, M10.3)
# ---------------------------------------------------------------------------

def _functional_pass(ctx: RunContext, profiles: list[dict], max_parallel: int) -> dict[str, dict]:
    """Run every profile, at most ``max_parallel`` at once, each on its own slot.

    Stopping is cooperative: once ``stop_event`` is set, profiles that have not
    started are recorded as "cancelled" and never boot; profiles already running
    finish their pipeline and clean up normally.
    """
    pool = SlotPool(max_parallel)
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=max_parallel, thread_name_prefix="vmf-profile") as ex:
        futures = {ex.submit(_functional_task, p, ctx, pool): p for p in profiles}
        for future in as_completed(futures):
            profile = futures[future]
            name = profile_name(profile)
            try:
                results[name] = future.result()
            except Exception as e:
                log.exception("worker for profile %s crashed", name)
                results[name] = {
                    "profile": name, "serial": None, "tier": profile.get("tier"),
                    "result": RESULT_ERROR, "cause": _save_error(ctx, name, e, "worker"),
                    "findings_count": 0, "duration_s": 0.0, "error": True,
                    "warnings": [], "cleanup_errors": []}
    return {profile_name(p): results[profile_name(p)] for p in profiles}


# ---------------------------------------------------------------------------
# Pass B: performance pass (M10.2)
# ---------------------------------------------------------------------------

def _perf_one(profile: dict, ctx: RunContext, pool: SlotPool) -> dict:
    deps = ctx.deps
    name = profile_name(profile)
    slot = pool.acquire()
    serial = None
    stage = "start_device"
    cleanup_errors: list[str] = []
    try:
        serial = deps.start_device(profile, slot.port)
        stage = "apply_profile_settings"
        if ctx.apply_settings:
            deps.apply_profile_settings(serial, profile)
        stage = "measure"

        def flow(serial=serial, system_port=slot.system_port, name=name):
            return deps.runner_run(serial, ctx.apk, ctx.scenario, system_port,
                                   run_id=None, profile=name, record=False)

        rows = deps.perf_run_and_store(
            ctx.run_id, name, serial, profile, package=ctx.package, apk=ctx.apk, flow=flow,
            baselines=ctx.baselines.get(name), thresholds=ctx.thresholds)
        regressions = [r.metric for r in rows if deps.perf_is_regression(r, ctx.thresholds)]
        return {"profile": name, "status": "ok", "rows": len(rows),
                "regressions": regressions, "cleanup_errors": cleanup_errors}
    except Exception as e:
        log.exception("performance pass failed for %s at stage %s", name, stage)
        return {"profile": name, "status": "error",
                "error": f"{type(e).__name__}: {e} (stage: {stage})",
                "regressions": [], "cleanup_errors": cleanup_errors}
    finally:
        if serial is not None:
            _guarded("reset_profile", lambda: deps.reset_profile(serial, ctx.package),
                     cleanup_errors)
            _guarded("stop_device", lambda: deps.stop_device(serial), cleanup_errors)
        _guarded("release_slot", lambda: pool.release(slot), cleanup_errors)


def _performance_pass(ctx: RunContext, profiles: list[dict],
                      functional: dict[str, dict]) -> dict[str, dict]:
    """One profile at a time, never in parallel, only for profiles whose
    functional result is not "error" or "cancelled". A regression does NOT
    change the functional result; it is only counted in the summary (Gagan's
    report combines both)."""
    pool = SlotPool(1)
    results: dict[str, dict] = {}
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
            results[name] = _perf_one(profile, ctx, pool)
    return results


# ---------------------------------------------------------------------------
# Pre-flight and leftovers (M10.5)
# ---------------------------------------------------------------------------

def preflight(deps: Deps, options: dict) -> None:
    """Raise PreflightError naming every failed check. Starts nothing, kills nothing."""
    problems = []
    try:
        if not deps.appium_status():
            problems.append(f"Appium is not reachable at {APPIUM_URL}/status")
    except Exception as e:
        problems.append(f"Appium check failed: {type(e).__name__}: {e}")
    minimum = options.get("min_disk_gb", PREFLIGHT_MIN_DISK_GB)
    try:
        free = deps.free_disk_gb(os.getcwd())
        if free < minimum:
            problems.append(f"free disk is {free:.1f} GB, need at least {minimum} GB")
    except Exception as e:
        problems.append(f"disk check failed: {type(e).__name__}: {e}")
    try:
        orphans = deps.list_emulator_processes()
        if orphans:
            listed = ", ".join(f"{p['name']} (pid {p['pid']})" for p in orphans)
            problems.append(f"orphan emulator processes already running: {listed}")
    except Exception as e:
        problems.append(f"orphan process check failed: {type(e).__name__}: {e}")
    if problems:
        raise PreflightError("pre-flight failed: " + "; ".join(problems))


def _check_leftovers(deps: Deps, kill: bool) -> tuple[list[dict], list[int]]:
    """Report emulator processes still alive; kill them only when ``kill``."""
    try:
        leftovers = list(deps.list_emulator_processes())
    except Exception:
        log.exception("leftover emulator check failed")
        return [], []
    killed: list[int] = []
    if leftovers:
        log.warning("leftover emulator processes: %s",
                    ", ".join(f"{p['name']} (pid {p['pid']})" for p in leftovers))
        if kill:
            for p in leftovers:
                try:
                    deps.kill_process(p["pid"])
                    killed.append(p["pid"])
                except Exception:
                    log.exception("could not kill pid %s", p["pid"])
    return leftovers, killed


# ---------------------------------------------------------------------------
# Entry points (M10.4)
# ---------------------------------------------------------------------------

def _scenario_info(path: str) -> tuple[str, str | None]:
    with open(path, encoding="utf-8") as f:
        spec = yaml.safe_load(f) or {}
    return spec.get("name") or os.path.basename(path), spec.get("package")


def main(run_id: int | None, options: dict, deps: Deps | None = None) -> dict:
    """Run the whole matrix and return a summary dict.

    ``options``: apk, scenario, profiles (list of dicts) or profiles_path,
    package (else the scenario YAML's ``package``), performance_pass (True),
    apply_settings (True), max_parallel, baselines ({profile: {metric: value}}),
    thresholds, stop_event, kill_leftovers, min_disk_gb.

    ``run_id`` None is command-line mode: one run is created with
    ``create_run`` (before pre-flight, so a failed pre-flight can still be
    closed). A given ``run_id`` is API mode: ``create_run`` is never called.
    ``finish_run`` is called exactly once, in a finally, even on failure.
    Order: pre-flight, Pass A, Pass B, leftover check, finish_run.
    """
    deps = deps or default_deps()
    started = time.monotonic()
    apk = options["apk"]
    scenario = options["scenario"]
    profiles = options.get("profiles")
    if profiles is None:
        profiles = load_profiles(options["profiles_path"])
    if not profiles:
        raise ValueError("no profiles to run")
    names = [profile_name(p) for p in profiles]
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate profile names: {sorted({n for n in names if names.count(n) > 1})}")
    scenario_name, scenario_package = _scenario_info(scenario)
    package = options.get("package") or scenario_package
    if not package:
        raise ValueError("no package: pass options['package'] or set 'package' in the scenario")
    max_parallel = resolve_max_parallel(options, len(profiles))

    if run_id is None:
        run_id = deps.create_run(os.path.basename(apk), scenario_name, names)

    ctx = RunContext(deps=deps, run_id=run_id, apk=apk, scenario=scenario, package=package,
                     apply_settings=options.get("apply_settings", True),
                     stop_event=options.get("stop_event"),
                     thresholds=options.get("thresholds") or Thresholds(),
                     baselines=options.get("baselines") or {})
    leftovers: list[dict] = []
    killed: list[int] = []
    failed = True
    try:
        preflight(deps, options)
        functional = _functional_pass(ctx, profiles, max_parallel)
        perf_results: dict[str, dict] = {}
        if options.get("performance_pass", True):
            perf_results = _performance_pass(ctx, profiles, functional)
        leftovers, killed = _check_leftovers(deps, bool(options.get("kill_leftovers")))
        failed = False
    finally:
        try:
            deps.finish_run(run_id)
        except Exception:
            log.exception("finish_run failed for run %s", run_id)
            if not failed:
                raise

    results = [functional[n] for n in names]
    counts: dict[str, int] = {}
    for r in results:
        counts[r["result"]] = counts.get(r["result"], 0) + 1
    return {
        "run_id": run_id,
        "counts": counts,
        "profiles": results,
        "performance": [perf_results[n] for n in names] if perf_results else [],
        "perf_regressions": [{"profile": n, "metrics": perf_results[n]["regressions"]}
                             for n in names if n in perf_results and perf_results[n]["regressions"]],
        "cancelled": [r["profile"] for r in results if r["result"] == RESULT_CANCELLED],
        "leftover_processes": leftovers,
        "leftovers_killed": killed,
        "max_parallel": max_parallel,
        "duration_s": round(time.monotonic() - started, 3),
    }


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
    summary = main(args.run_id, options)
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
