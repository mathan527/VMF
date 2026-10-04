"""oem_packs — OEM behaviour pack package.

Implementation lives in :mod:`vmf.oem_packs.oem_packs`.
This __init__.py re-exports the public API so both import styles work:

    from vmf import oem_packs
    oem_packs.apply_profile_settings(...)   # package-level import

    from vmf.oem_packs.oem_packs import apply_profile_settings  # module-level
"""
from vmf.oem_packs.oem_packs import (  # noqa: F401
    apply_profile_settings,
    background_kill,
    reset_profile,
    restrict_background,
    doze,
    always_finish_activities,
    trim_memory,
    permission_revoke,
)
