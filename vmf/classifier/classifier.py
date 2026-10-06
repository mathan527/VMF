"""classifier.py — Deterministic pass/crash/anr/hang verdict classifier.

Owner: Mathan (M6)

Verdicts are decided only from hard signals: logcat text, process state
(``pid_alive``) and sha256 hashes of the evidence files. No AI, no vision.
"""
import hashlib
import logging
import re
import subprocess
from pathlib import Path
from typing import Callable

from vmf.results import Step, Verdict

# Consecutive identical-evidence steps must add up to at least this many
# seconds to count as a hang. NOTE: the value 30.0 still needs to be agreed
# with the lead.
HANG_THRESHOLD_S = 30.0

_CRASH_WINDOW = 5
_LOGCAT_CLEAR_TIMEOUT_S = 10
_PROCESS_RE = re.compile(r"Process: (\S+?), PID: (\d+)")
_THREADTIME_RE = re.compile(
    r"^\d\d-\d\d\s+\d\d:\d\d:\d\d\.\d+\s+\d+\s+\d+\s+[VDIWEFA]\s+[^:]*:\s?(.*)$"
)
_LINE_PID_RE = re.compile(r"^\d\d-\d\d\s+\d\d:\d\d:\d\d\.\d+\s+(\d+)\s+\d+\s+[VDIWEFA]\s")


# ---------------------------------------------------------------------------
# Logcat capture (M6.1)
# ---------------------------------------------------------------------------

def start_logcat(serial: str, out_path: str) -> subprocess.Popen:
    """Start ``adb -s <serial> logcat -v threadtime`` writing to ``out_path``.

    The device log buffer is cleared first (``logcat -c``, failure ignored) so
    crashes from earlier runs are not captured. Runs as its own subprocess,
    independent of Appium. Parent directories of ``out_path`` are created.
    Pair every call with :func:`stop_logcat`.
    """
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(["adb", "-s", serial, "logcat", "-c"], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=_LOGCAT_CLEAR_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as e:
        logging.warning("classifier: logcat -c failed for %s: %s", serial, e)
    with open(out_path, "wb") as out:
        return subprocess.Popen(
            ["adb", "-s", serial, "logcat", "-v", "threadtime"],
            stdout=out, stderr=subprocess.DEVNULL,
        )


def stop_logcat(proc: subprocess.Popen) -> None:
    """Terminate a logcat process, escalating to kill if it does not exit."""
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


# ---------------------------------------------------------------------------
# Signal detectors
# ---------------------------------------------------------------------------

def _message(line: str) -> str:
    m = _THREADTIME_RE.match(line)
    return (m.group(1) if m else line).strip()


def _process_matches(name: str, package: str | None) -> bool:
    """True for ``<package>`` or ``<package>:<suffix>`` (any when None)."""
    return package is None or name == package or name.startswith(package + ":")


def _find_crash(lines: list[str], package: str | None) -> str | None:
    """Return the exception text of the first matching FATAL EXCEPTION."""
    for i, line in enumerate(lines):
        if "FATAL EXCEPTION" not in line:
            continue
        window = lines[i + 1:i + 1 + _CRASH_WINDOW]
        crash_pid = None
        for w in window:
            m = _PROCESS_RE.search(w)
            if m and _process_matches(m.group(1), package):
                crash_pid = m.group(2)
                break
        if crash_pid is None and package is not None:
            continue
        candidates = [w for w in window if not _PROCESS_RE.search(w)]
        if crash_pid is not None:
            same_pid = [w for w in candidates
                        if (p := _LINE_PID_RE.match(w)) and p.group(1) == crash_pid]
            candidates = same_pid or candidates
        for w in candidates:
            text = _message(w)
            if text:
                return text
        return _message(line)
    return None


def _find_anr(lines: list[str], package: str | None) -> str | None:
    pattern = re.compile(
        r"ANR in " + (re.escape(package) + r"(?!\w)" if package else ""))
    for line in lines:
        if pattern.search(line):
            return line.strip()
    return None


def _file_hash(path: str) -> str | None:
    """sha256 of the file, or None if it is missing or empty."""
    try:
        data = Path(path).read_bytes()
    except (OSError, ValueError):
        return None
    return hashlib.sha256(data).hexdigest() if data else None


def _is_hang(steps: list[Step], threshold_s: float) -> bool:
    """True if a run of steps with unchanged evidence spans >= threshold.

    A step extends the run when both its screenshot and hierarchy hashes equal
    the previous step's. The run's duration is the sum of ``duration_ms`` of
    the steps that matched their predecessor. Unreadable files make a pair
    "not comparable", which breaks the run.
    """
    run_ms = 0
    prev: tuple[str, str] | None = None
    for step in steps:
        shot = _file_hash(step.screenshot_path)
        hier = _file_hash(step.hierarchy_path)
        cur = (shot, hier) if shot is not None and hier is not None else None
        if cur is not None and cur == prev:
            run_ms += step.duration_ms
            if run_ms >= threshold_s * 1000:
                return True
        else:
            run_ms = 0
        prev = cur
    return False


def _failed_step(steps: list[Step]) -> int | None:
    if not steps:
        return None
    for step in steps:
        if step.status == "fail":
            return step.step_index
    return steps[-1].step_index


def _trigger_behaviour(steps: list[Step], failed_step: int | None) -> str | None:
    if failed_step is None:
        return None
    trigger = None
    for step in steps:
        if step.step_index > failed_step:
            break
        if step.action.startswith("oem."):
            trigger = step.action[len("oem."):]
    return trigger


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def classify(logcat_path, steps: list[Step] | None = None, *,
             package: str | None = None,
             pid_alive: Callable[[], bool] | None = None,
             hang_threshold_s: float = HANG_THRESHOLD_S) -> Verdict:
    """Classify a run as pass / crash / anr / hang from hard signals only.

    Precedence when several fire: crash > anr > hang > pass.

    * crash: ``FATAL EXCEPTION`` followed within 5 lines by
      ``Process: <package>, PID: <n>`` (any package if ``package`` is None),
      or ``pid_alive()`` returning False (cause ``"process gone"``).
    * anr: a logcat line containing ``ANR in <package>``.
    * hang: steps with identical screenshot and hierarchy hashes whose
      durations sum to >= ``hang_threshold_s`` while the process is alive.

    ``failed_step`` limitation: ``Step`` has no timestamps, so logcat events
    cannot be mapped to steps. It is the index of the first step with status
    "fail", else the last step's index, and None for no steps or a pass.
    ``trigger_behaviour`` is the name (without ``oem.``) of the last oem step
    at or before ``failed_step``, else None.

    ``pid_alive`` raising is logged and treated as "unknown": it is neither
    crash evidence nor proof the process is alive.

    If ``logcat_path`` is missing or unreadable, a warning is logged and the
    verdict is based on steps and ``pid_alive`` only. For backward
    compatibility ``classify(steps)`` (steps passed first) is still accepted.
    """
    if isinstance(logcat_path, list):  # legacy call: classify(steps[, logcat])
        steps, logcat_path = logcat_path, None
    steps = steps or []

    lines: list[str] = []
    if logcat_path:
        try:
            lines = Path(logcat_path).read_text(
                encoding="utf-8", errors="replace").splitlines()
        except OSError as e:
            logging.warning("classifier: cannot read logcat %s: %s", logcat_path, e)
    else:
        logging.warning("classifier: no logcat provided; using steps/pid only")

    alive: bool | None = None  # None = unknown: neither crash nor alive evidence
    if pid_alive is not None:
        try:
            alive = bool(pid_alive())
        except Exception as e:
            logging.warning("classifier: pid_alive raised %s: %s; treating as unknown",
                            type(e).__name__, e)

    result, cause = "pass", ""
    crash = _find_crash(lines, package)
    anr = _find_anr(lines, package)
    if crash is not None:
        result, cause = "crash", crash
    elif alive is False:
        result, cause = "crash", "process gone"
    elif anr is not None:
        result, cause = "anr", anr
    elif _is_hang(steps, hang_threshold_s):
        result, cause = "hang", "UI unchanged for >= %gs" % hang_threshold_s

    if result == "pass":
        return Verdict(result="pass", cause="", failed_step=None, trigger_behaviour=None)
    failed = _failed_step(steps)
    return Verdict(result=result, cause=cause, failed_step=failed,
                   trigger_behaviour=_trigger_behaviour(steps, failed))
