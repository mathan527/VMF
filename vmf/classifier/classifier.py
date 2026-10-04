"""classifier.py — Deterministic pass/fail verdict classifier.

Owner: Mathan (M6)
ARCH stub provided here.

Real crash/ANR/hang detection logic is due in M6.
"""
import logging

from vmf.results import Verdict


def classify(steps: list, logcat: str | None = None) -> Verdict:
    """ARCH stub — real crash/ANR/hang detection: M6.

    Inspects the step list and optional logcat output to produce a
    pass/fail/crash/hang/anr verdict.  During ARCH the stub always
    returns ``"pass"``.

    Args:
        steps:   List of :class:`vmf.results.Step` produced by the runner.
        logcat:  Optional raw logcat text for pattern matching (M6).

    Returns:
        A :class:`vmf.results.Verdict` instance.
    """
    logging.info("[STUB] classifier.classify steps=%d logcat=%s", len(steps), bool(logcat))
    return Verdict(result="pass", cause="", failed_step=None, trigger_behaviour=None)
