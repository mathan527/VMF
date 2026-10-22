"""Safe contract tests for the nightly runner's storage boundary."""
from __future__ import annotations

import sqlite3

import pytest

from scripts import nightly
from vmf.storage import storage as storage_module


@pytest.fixture
def isolated_nightly_storage(tmp_path, monkeypatch):
    db_path = tmp_path / "nightly.db"
    monkeypatch.setattr(storage_module, "_DB_PATH", db_path)
    monkeypatch.setattr(nightly, "RESULTS_DIR", tmp_path / "results")
    storage_module._INITIALIZED_PATHS.clear()
    yield db_path
    storage_module._INITIALIZED_PATHS.clear()


def test_nightly_records_completed_with_failures_without_starting_a_device(
        isolated_nightly_storage, monkeypatch):
    """The two-argument finish_run call remains compatible with M9 storage."""
    monkeypatch.setattr(nightly, "cleanup_orphans", lambda: None)
    monkeypatch.setattr(nightly, "start_device", lambda profile, port: (_ for _ in ()).throw(RuntimeError("boot failed")))
    monkeypatch.setattr(nightly, "stop_device", lambda serial: None)

    ok, failures, _ = nightly.run_unattended_device_sweep([{"brand": "Samsung", "tier": "low"}])

    assert (ok, failures) == (False, 1)
    with sqlite3.connect(isolated_nightly_storage) as conn:
        status = conn.execute("SELECT status FROM runs").fetchone()[0]
    assert status == "COMPLETED_WITH_FAILURES"
