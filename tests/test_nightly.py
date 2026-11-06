"""Safe contract tests for the nightly runner's storage boundary."""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace

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


def test_capture_evidence_retries_transient_blank_screenshots(tmp_path, monkeypatch, caplog):
    validity_results = iter([
        (False, "Screenshot appears blank or uniform"),
        (False, "Screenshot appears blank or uniform"),
        (False, "Screenshot appears blank or uniform"),
        (True, "Valid PNG 720x1600"),
    ])
    screenshot_attempts = 0
    animation_scales = {setting: "1.0" for setting in nightly.WINDOW_ANIMATION_SETTINGS}

    def fake_run(command, **kwargs):
        nonlocal screenshot_attempts
        if "screencap" in command:
            screenshot_attempts += 1
            kwargs["stdout"].write(b"png data")
            return SimpleNamespace(returncode=0, stdout="")
        if "settings" in command:
            setting = command[-1] if "get" in command else command[-2]
            if "get" in command:
                return SimpleNamespace(returncode=0, stdout=animation_scales[setting])
            if "put" in command:
                animation_scales[setting] = command[-1]
                return SimpleNamespace(returncode=0, stdout="")
        if "uiautomator" in command and set(animation_scales.values()) != {"0"}:
            return SimpleNamespace(returncode=0, stdout="ERROR: could not get idle state.\n")
        return SimpleNamespace(
            returncode=0,
            stdout="<hierarchy></hierarchy>UI Hierarchy dumped to: /dev/tty",
        )

    monkeypatch.setattr(nightly.time, "sleep", lambda _: None)
    monkeypatch.setattr(nightly.subprocess, "run", fake_run)
    monkeypatch.setattr(nightly, "check_screenshot_validity", lambda _: next(validity_results))
    monkeypatch.setattr(nightly, "REPO_ROOT", tmp_path)

    step = nightly.capture_evidence("emulator-5554", tmp_path / "evidence", 1, "boot")

    assert step.status == "pass"
    assert screenshot_attempts == 4
    assert not [record for record in caplog.records if record.levelname == "WARNING"]
    assert (tmp_path / "evidence" / "step_01_boot.png").read_bytes() == b"png data"
    assert (tmp_path / "evidence" / "step_01_boot_hierarchy.xml").read_text() == (
        "<hierarchy></hierarchy>"
    )
    assert set(animation_scales.values()) == {"1.0"}
