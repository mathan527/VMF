import sqlite3

import pytest

from vmf import storage
from vmf.report.report import FIDELITY_STATEMENT, generate_report
from vmf.results import PerfRow, Step, Verdict
from vmf.storage import storage as storage_module


@pytest.fixture
def report_db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_module, "_DB_PATH", tmp_path / "farm.db")
    storage_module._INITIALIZED_PATHS.clear()
    yield tmp_path
    storage_module._INITIALIZED_PATHS.clear()


def test_html_report_uses_persisted_data_and_escapes_values(report_db):
    run_id = storage.create_run("<app>.apk", "login", ["p1"])
    storage.save_device_result(run_id, "p1", Verdict("crash", "<bad>&", 0, None))
    storage.save_step(run_id, "p1", Step(0, "tap", "fail", 4, "results/nope.png", "results/nope.xml"))
    storage.save_profile_config(run_id, "p1", {"api": 35, "ram": "4GB", "cpu_tier": "low"})
    storage.save_perf(run_id, "p1", [PerfRow("launch_time", 20, 1, 18, 11, False, False)])
    storage.finish_run(run_id)

    path = generate_report(run_id, str(report_db / "reports"))
    body = open(path, encoding="utf-8").read()

    assert FIDELITY_STATEMENT in body
    assert "Requires physical validation" in body and "emulator-relative" in body
    assert "&lt;app&gt;.apk" in body and "&lt;bad&gt;&amp;" in body
    assert "Missing evidence" in body and "cpu_tier" in body


def test_report_handles_empty_run_and_unknown_id(report_db):
    run_id = storage.create_run("app.apk", "empty", [])
    path = generate_report(run_id, str(report_db / "reports"))
    assert "No device results were persisted" in open(path, encoding="utf-8").read()
    with pytest.raises(LookupError):
        generate_report(999, str(report_db / "reports"))
