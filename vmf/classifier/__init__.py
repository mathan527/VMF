"""classifier — Deterministic verdict classifier package.

Owner: Mathan (M6)

Implementation lives in :mod:`vmf.classifier.classifier`.
This __init__.py re-exports the public API so both import styles work:

    from vmf import classifier
    classifier.classify(...)   # package-level import

    from vmf.classifier.classifier import classify   # module-level import
"""
from vmf.classifier.classifier import (  # noqa: F401
    classify,
)
