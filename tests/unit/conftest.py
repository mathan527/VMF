"""Shared fixtures for the unit tests."""
import pytest

from vmf.orchestrator import run_matrix as rm
from vmf.perf import baselines


@pytest.fixture(autouse=True)
def _isolated_baseline_store(tmp_path, monkeypatch):
    """No test reads or writes the repo's perf_baselines.yaml: an orchestrator run without a
    ``baselines`` option loads them from an empty store in the test's temp folder. A test that
    needs another store patches rm.get_store itself (that patch wins)."""
    monkeypatch.setattr(rm, "get_store", lambda: baselines.YamlBaselineStore(tmp_path / "perf_baselines.yaml"))


@pytest.fixture(autouse=True)
def _no_logcat_grace(monkeypatch):
    """The orchestrator waits LOGCAT_GRACE_S before stopping logcat; unit tests do not need to."""
    monkeypatch.setattr(rm, "LOGCAT_GRACE_S", 0)
