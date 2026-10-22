"""storage — Persistent storage package.

Implementation lives in :mod:`vmf.storage.storage`.
This __init__.py re-exports the public API so both import styles work:

    from vmf import storage
    storage.create_run(...)   # package-level import

    from vmf.storage.storage import create_run   # module-level import
"""
from vmf.storage.storage import (  # noqa: F401
    create_run, finish_run, finish_run_if_running, get_run, list_baseline_entries, list_runs, load_baselines, save_baseline,
    save_device_result, save_perf, save_profile_config, save_step,
)

__all__ = ["create_run", "finish_run", "finish_run_if_running", "get_run", "list_baseline_entries", "list_runs", "load_baselines", "save_baseline",
           "save_device_result", "save_perf", "save_profile_config", "save_step"]
