"""perf — Performance measurement package.

Owner: Mathan (M8)

Implementation lives in :mod:`vmf.perf.perf`.
This __init__.py re-exports the public API so both import styles work:

    from vmf import perf
    perf.measure(...)           # package-level import

    from vmf.perf.perf import measure   # module-level import

``run_performance`` is the old ARCH stub, kept for existing callers.
Every number is emulator-relative.
"""
from vmf.perf.perf import (  # noqa: F401
    AdbError,
    FlowError,
    HostSampler,
    install_apk,
    IsolationError,
    PerfError,
    PerfParseError,
    SnapshotError,
    Thresholds,
    assert_isolated,
    flags_regression_on_all_tiers,
    format_perf_row,
    is_regression,
    load_snapshot,
    load_thresholds,
    measure,
    measure_once,
    metric_spread,
    median,
    parse_am_start_w,
    parse_devices,
    parse_gfxinfo,
    parse_meminfo,
    repeatability_ok,
    resolve_launcher_activity,
    run_and_store,
    run_performance,
    save_baseline_snapshot,
    spread_pct,
    stabilise,
    tier_ratio,
)

__all__ = [
    "AdbError", "FlowError", "HostSampler", "install_apk", "IsolationError", "PerfError",
    "PerfParseError", "SnapshotError", "Thresholds", "assert_isolated",
    "flags_regression_on_all_tiers", "format_perf_row", "is_regression",
    "load_snapshot", "load_thresholds", "measure", "measure_once", "median", "metric_spread",
    "parse_am_start_w", "parse_devices", "parse_gfxinfo", "parse_meminfo",
    "repeatability_ok", "resolve_launcher_activity", "run_and_store",
    "run_performance", "save_baseline_snapshot", "spread_pct", "stabilise",
    "tier_ratio",
]
