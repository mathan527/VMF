"""report — Test report generation package.

Owner: Gagan (M11)

Implementation lives in :mod:`vmf.report.report`.
This __init__.py re-exports the public API so both import styles work:

    from vmf import report
    report.generate_report(...)   # package-level import

    from vmf.report.report import generate_report   # module-level import
"""
from vmf.report.report import (  # noqa: F401
    generate_report,
)
