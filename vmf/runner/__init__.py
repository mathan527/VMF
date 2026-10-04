"""runner — M4 Scenario Runner package.

Owner: Mathan (M4)

Implementation lives in :mod:`vmf.runner.runner`.
This __init__.py re-exports the public API so both import styles work:

    from vmf import runner
    runner.run_scenario(...)   # package-level import

    from vmf.runner.runner import run_scenario   # module-level import

The full M4 Appium/UiAutomator2 implementation is in runner.runner.
"""
from vmf.runner.runner import (  # noqa: F401
    run,
    run_scenario,
)
