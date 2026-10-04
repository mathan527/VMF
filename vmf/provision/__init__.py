"""provision — Device provisioning package.

Implementation lives in :mod:`vmf.provision.provision`.
This __init__.py re-exports the public API so both import styles work:

    from vmf import provision
    provision.start_device(...)          # package-level import

    from vmf.provision.provision import start_device   # module-level import
"""
from vmf.provision.provision import (  # noqa: F401
    start_device,
    stop_device,
)
