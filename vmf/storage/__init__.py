"""storage — Persistent storage package.

Implementation lives in :mod:`vmf.storage.storage`.
This __init__.py re-exports the public API so both import styles work:

    from vmf import storage
    storage.create_run(...)   # package-level import

    from vmf.storage.storage import create_run   # module-level import
"""
from vmf.storage.storage import (  # noqa: F401
    create_run,
    save_device_result,
    save_step,
    save_profile_config,
    save_perf,
    finish_run,
)
