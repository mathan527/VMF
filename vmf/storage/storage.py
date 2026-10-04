"""storage.py — Persistent storage interface.

Owner: Gagan (M9)
ARCH stubs provided here.

Real SQLite + evidence-folder implementation is due in M9.
No database operations are performed here.
"""
import logging

_run_counter = 0


def create_run(apk_name: str, scenario: str, profiles: list[str]) -> int:
    """STUB — real implementation: Gagan, M9.

    Open a new test run record.

    Args:
        apk_name: Filename of the APK under test.
        scenario: Scenario name or path.
        profiles: List of device profile names/serials in this run.

    Returns:
        Incrementing integer run_id (starts at 1).
    """
    global _run_counter
    _run_counter += 1
    logging.info("[STUB] storage.create_run apk_name=%s scenario=%s profiles=%s -> %s",
                 apk_name, scenario, profiles, _run_counter)
    return _run_counter


def save_device_result(run_id: int, profile: str, verdict) -> None:
    """STUB — real implementation: Gagan, M9.

    Persist the overall pass/fail verdict for one device profile.

    Args:
        run_id:  Run identifier from :func:`create_run`.
        profile: Device profile name / ADB serial.
        verdict: :class:`vmf.results.Verdict` instance.
    """
    logging.info("[STUB] storage.save_device_result run_id=%s profile=%s verdict=%s",
                 run_id, profile, verdict)


def save_step(run_id: int, profile: str, step) -> None:
    """STUB — real implementation: Gagan, M9.

    Persist one scenario step result.

    Args:
        run_id:  Run identifier.
        profile: Device profile name / ADB serial.
        step:    :class:`vmf.results.Step` instance.
    """
    logging.info("[STUB] storage.save_step run_id=%s profile=%s step=%s",
                 run_id, profile, step)


def save_profile_config(run_id: int, profile: str, config: dict) -> None:
    """STUB — real implementation: Gagan, M9.

    Persist the full device profile configuration used for a run.

    Args:
        run_id:  Run identifier.
        profile: Device profile name / ADB serial.
        config:  Profile dict (cutout, density, font_scale, ...).
    """
    logging.info("[STUB] storage.save_profile_config run_id=%s profile=%s config=%s",
                 run_id, profile, config)


def save_perf(run_id: int, profile: str, perf_rows: list) -> None:
    """STUB — real implementation: Gagan, M9.

    Persist performance metric rows for one device profile.

    Args:
        run_id:     Run identifier.
        profile:    Device profile name / ADB serial.
        perf_rows:  List of :class:`vmf.results.PerfRow` instances.
    """
    logging.info("[STUB] storage.save_perf run_id=%s profile=%s perf_rows=%s",
                 run_id, profile, perf_rows)


def finish_run(run_id: int) -> None:
    """STUB — real implementation: Gagan, M9.

    Close and finalise the run record.

    Args:
        run_id: Run identifier from :func:`create_run`.
    """
    logging.info("[STUB] storage.finish_run run_id=%s", run_id)
