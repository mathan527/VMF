from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vmf import storage
from vmf.api import api
from vmf.results import Verdict
from vmf.storage import storage as storage_module


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_module, "_DB_PATH", tmp_path / "farm.db")
    storage_module._INITIALIZED_PATHS.clear()
    monkeypatch.setattr(api, "REPORTS_ROOT", tmp_path / "reports")
    monkeypatch.setattr(api, "RESULTS_ROOT", tmp_path / "results")
    calls = []
    app = api.create_app(orchestrator=lambda run_id, options: calls.append((run_id, options)))
    yield TestClient(app), calls
    storage_module._INITIALIZED_PATHS.clear()


def test_health_empty_runs_and_console(client):
    http, _ = client
    assert http.get("/health").json() == {"status": "ok"}
    assert http.get("/runs").json()["items"] == []
    assert "Virtual Mobile Farm" in http.get("/").text
    run_id = storage.create_run("<script>alert(1)</script>", "scenario", [])
    storage.finish_run(run_id)
    assert "&lt;script&gt;" in http.get("/").text


def test_run_detail_report_and_unknown_id(client):
    http, _ = client
    run_id = storage.create_run("app.apk", "scenario", ["p"])
    storage.save_device_result(run_id, "p", Verdict("pass", "", None, None))
    storage.finish_run(run_id)
    assert http.get(f"/runs/{run_id}").json()["run_id"] == run_id
    assert "Virtual Mobile Farm report" in http.get(f"/runs/{run_id}/report").text
    assert http.get("/runs/999").status_code == 404


def test_start_run_validates_and_dispatches_without_real_orchestration(client):
    http, calls = client
    root = Path(api.ROOT)
    payload = {"apk": str(root / "sample_app" / "AndroidManifest.xml"),
               "scenario": str(root / "scenarios" / "sample_app.yaml"),
               "profiles": [{"brand": "Samsung", "tier": "low"}]}
    response = http.post("/runs", json=payload)
    assert response.status_code == 202
    assert response.json()["status"] == "accepted" and len(calls) == 1
    assert http.post("/runs", json={"apk": "/etc/passwd", "scenario": "x", "profiles": [{}]}).status_code == 400
    malformed = {**payload, "profiles": [{"brand": "Samsung"}]}
    assert http.post("/runs", json=malformed).status_code == 422


def test_background_dispatch_failure_is_recorded_without_overwriting_terminal_status(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_module, "_DB_PATH", tmp_path / "farm.db")
    storage_module._INITIALIZED_PATHS.clear()
    monkeypatch.setattr(api, "REPORTS_ROOT", tmp_path / "reports")

    def failure(run_id, options):
        raise RuntimeError("M10 unavailable")

    http = TestClient(api.create_app(orchestrator=failure))
    root = Path(api.ROOT)
    payload = {"apk": str(root / "sample_app" / "AndroidManifest.xml"),
               "scenario": str(root / "scenarios" / "sample_app.yaml"),
               "profiles": [{"brand": "Samsung", "tier": "low"}]}
    response = http.post("/runs", json=payload)
    assert response.status_code == 202
    run_id = response.json()["run_id"]
    assert storage.get_run(run_id)["status"] == "DISPATCH_FAILED"

    storage.finish_run(run_id, "FINISHED")
    api._dispatch_run(failure, run_id, {})
    assert storage.get_run(run_id)["status"] == "FINISHED"
    storage_module._INITIALIZED_PATHS.clear()


def test_evidence_path_traversal_is_rejected(client):
    http, _ = client
    assert http.get("/evidence/1/%2E%2E/%2E%2E/etc/passwd").status_code == 400
