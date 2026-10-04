"""perf — Performance measurement package.

Owner: Mathan (M8)

Implementation lives in :mod:`vmf.perf.perf`.
This __init__.py re-exports the public API so both import styles work:

    from vmf import perf
    perf.run_performance(...)   # package-level import

    from vmf.perf.perf import run_performance   # module-level import
"""
from vmf.perf.perf import (  # noqa: F401
    run_performance,
)
