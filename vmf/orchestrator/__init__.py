"""Run orchestrator — orchestrator.py, owner: Mathan

Sub-module:
  run_matrix  — ARCH dry-run and M10 multi-profile orchestration.

``run_matrix`` is imported on first use (not here) so that
``python -m vmf.orchestrator.run_matrix`` does not import the module twice.
"""
import importlib

__all__ = ["run_matrix"]


def __getattr__(name):
    if name == "run_matrix":
        return importlib.import_module("vmf.orchestrator.run_matrix")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
