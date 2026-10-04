"""geometry — UI geometry validation package.

Owner: Mathan (M7)

Implementation lives in :mod:`vmf.geometry.geometry`.
This __init__.py re-exports the public API so both import styles work:

    from vmf import geometry
    geometry.validate_geometry(...)   # package-level import

    from vmf.geometry.geometry import validate_geometry   # module-level import
"""
from vmf.geometry.geometry import (  # noqa: F401
    validate_geometry,
)
