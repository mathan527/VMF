"""geometry — UI geometry validation package.

Owner: Mathan (M7)

Implementation lives in :mod:`vmf.geometry.geometry`.
This __init__.py re-exports the public API so both import styles work:

    from vmf import geometry
    geometry.check(...)   # package-level import

    from vmf.geometry.geometry import check   # module-level import

``validate_geometry`` is kept as a wrapper that delegates to ``check``.
"""
from vmf.geometry.geometry import (  # noqa: F401
    check,
    get_display_zones,
    parse_cutout_rects,
    parse_gesture_rects,
    validate_geometry,
)

__all__ = ["check", "validate_geometry", "parse_cutout_rects",
           "parse_gesture_rects", "get_display_zones"]
