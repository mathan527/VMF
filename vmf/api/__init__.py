"""api — HTTP API package.

Owner: Gagan (M12)

Implementation lives in :mod:`vmf.api.api`.
This __init__.py re-exports the public API so both import styles work:

    from vmf import api
    api.create_app()   # package-level import

    from vmf.api.api import create_app   # module-level import
"""
from vmf.api.api import (  # noqa: F401
    create_app,
)
