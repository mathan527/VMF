"""perf.py — Performance measurement.

Owner: Mathan (M8)
ARCH stub provided here.

Real launch-time / jank / PSS measurement via ADB shell is due in M8.
"""
import logging

from vmf.results import PerfRow


def run_performance(serial: str, package: str, profile: dict) -> list[PerfRow]:
    """ARCH stub — real launch-time / jank / PSS measurement: M8.

    Collects performance metrics for ``package`` on ``serial`` by running
    ADB shell commands and computing statistics.  During ARCH the stub
    returns a single deterministic ``PerfRow`` without executing any ADB.

    Args:
        serial:  ADB device serial.
        package: App package name (e.g. ``"com.example.app"``).
        profile: Active device profile dict.

    Returns:
        List of :class:`vmf.results.PerfRow` instances.
    """
    logging.info("[STUB] perf.run_performance serial=%s package=%s", serial, package)
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
