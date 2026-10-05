import json
import sqlite3

import pytest

from vmf.results import PerfRow, Step, Verdict
from vmf.storage import storage as storage_module
from vmf.storage.storage import (
    create_run,
    finish_run,
    save_device_result,
    save_perf,
    save_profile_config,
    save_step,
)


@pytest.fixture()
def temp_storage(tmp_path, monkeypatch):
    db_path = tmp_path / "farm.db"
    monkeypatch.setattr(storage_module, "_DB_PATH", db_path)
    storage_module._INITIALIZED_PATHS.clear()
    yield db_path
    storage_module._INITIALIZED_PATHS.clear()


def connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def test_schema_initialization_creates_required_tables(temp_storage):
    run_id = create_run("app.apk", "login", ["p1"])

    with connect(temp_storage) as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]

    assert run_id == 1
    assert {
        "runs",
        "device_results",
        "steps",
        "profile_config",
        "perf_results",
        "perf_baselines",
    }.issubset(tables)
    assert journal_mode == "wal"


def test_create_run_persists_running_row(temp_storage):
    run_id = create_run("app.apk", "login", ["p1"])

    with connect(temp_storage) as conn:
        row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()

    assert row["apk_name"] == "app.apk"
    assert row["scenario"] == "login"
    assert row["status"] == "RUNNING"
    assert row["started_at"]
    assert row["finished_at"] is None


def test_save_device_result_persists_verdict_fields(temp_storage):
    run_id = create_run("app.apk", "login", ["p1"])
    verdict = Verdict("crash", "process died", 3, "background_kill")

    assert save_device_result(run_id, "p1", verdict) is None

    with connect(temp_storage) as conn:
        row = conn.execute("SELECT * FROM device_results").fetchone()

    assert row["run_id"] == run_id
    assert row["profile"] == "p1"
    assert row["result"] == "crash"
    assert row["cause"] == "process died"
    assert row["failed_step"] == 3
    assert row["trigger_behaviour"] == "background_kill"


def test_save_step_persists_all_step_fields_and_evidence_paths(temp_storage):
    run_id = create_run("app.apk", "login", ["p1"])
    step = Step(2, "tap", "pass", 123, "results/1/p1/s.png", "results/1/p1/h.xml")

    assert save_step(run_id, "p1", step) is None

    with connect(temp_storage) as conn:
        row = conn.execute("SELECT * FROM steps").fetchone()

    assert row["step_index"] == 2
    assert row["action"] == "tap"
    assert row["status"] == "pass"
    assert row["duration_ms"] == 123
    assert row["screenshot_path"] == "results/1/p1/s.png"
    assert row["hierarchy_path"] == "results/1/p1/h.xml"


def test_save_profile_config_json_round_trips(temp_storage):
    run_id = create_run("app.apk", "login", ["p1"])
    config = {"density": 420, "nested": {"dark_mode": True}, "font_scale": 1.3}

    assert save_profile_config(run_id, "p1", config) is None

    with connect(temp_storage) as conn:
        raw = conn.execute("SELECT config_json FROM profile_config").fetchone()[0]

    assert raw == json.dumps(config, sort_keys=True)
    assert json.loads(raw) == config


def test_save_perf_persists_optional_nulls_and_booleans(temp_storage):
    run_id = create_run("app.apk", "login", ["p1"])
    rows = [
        PerfRow("pss", 100.0, 5.0, None, None, True, False),
        PerfRow("launch_time", 850.0, 3.0, 900.0, -5.5, False, True),
    ]

    assert save_perf(run_id, "p1", rows) is None

    with connect(temp_storage) as conn:
        saved = conn.execute(
            "SELECT * FROM perf_results ORDER BY id"
        ).fetchall()

    assert len(saved) == 2
    assert saved[0]["baseline"] is None
    assert saved[0]["regression_pct"] is None
    assert saved[0]["noisy"] == 1
    assert saved[0]["unstable"] == 0
    assert saved[1]["metric"] == "launch_time"
    assert saved[1]["baseline"] == 900.0
    assert saved[1]["regression_pct"] == -5.5
    assert saved[1]["noisy"] == 0
    assert saved[1]["unstable"] == 1


def test_save_perf_handles_empty_lists(temp_storage):
    run_id = create_run("app.apk", "login", ["p1"])

    assert save_perf(run_id, "p1", []) is None

    with connect(temp_storage) as conn:
        count = conn.execute("SELECT COUNT(*) FROM perf_results").fetchone()[0]

    assert count == 0


def test_finish_run_marks_row_finished(temp_storage):
    run_id = create_run("app.apk", "login", ["p1"])

    assert finish_run(run_id) is None

    with connect(temp_storage) as conn:
        row = conn.execute("SELECT status, finished_at FROM runs").fetchone()

    assert row["status"] == "FINISHED"
    assert row["finished_at"]


def test_persistence_after_reopening_database(temp_storage):
    run_id = create_run("app.apk", "login", ["p1"])
    save_step(run_id, "p1", Step(0, "launch", "pass", 10, "s.png", "h.xml"))
    storage_module._INITIALIZED_PATHS.clear()

    with connect(temp_storage) as conn:
        row = conn.execute(
            "SELECT action FROM steps WHERE run_id = ?", (run_id,)
        ).fetchone()

    assert row["action"] == "launch"


def test_foreign_key_rejects_unknown_run(temp_storage):
    create_run("app.apk", "login", ["p1"])
    bad_step = Step(0, "launch", "pass", 10, "s.png", "h.xml")

    with pytest.raises(sqlite3.IntegrityError):
        save_step(999, "p1", bad_step)


def test_multiple_profiles_under_one_run(temp_storage):
    run_id = create_run("app.apk", "login", ["p1", "p2"])
    save_step(run_id, "p1", Step(0, "launch", "pass", 10, "p1.png", "p1.xml"))
    save_step(run_id, "p2", Step(0, "launch", "pass", 12, "p2.png", "p2.xml"))
    save_device_result(run_id, "p1", Verdict("pass", "", None, None))
    save_device_result(run_id, "p2", Verdict("hang", "timeout", 0, None))

    with connect(temp_storage) as conn:
        profiles = {
            row["profile"]
            for row in conn.execute("SELECT profile FROM steps ORDER BY profile")
        }
        results = {
            row["profile"]: row["result"]
            for row in conn.execute("SELECT profile, result FROM device_results")
        }

    assert profiles == {"p1", "p2"}
    assert results == {"p1": "pass", "p2": "hang"}


def test_multiple_performance_rows(temp_storage):
    run_id = create_run("app.apk", "login", ["p1"])
    save_perf(
        run_id,
        "p1",
        [
            PerfRow("launch_time", 800.0, 3.0, 900.0, -11.1, False, False),
            PerfRow("jank_pct", 2.0, 10.0, 1.5, 33.3, True, False),
            PerfRow("pss", 120.0, 7.0, None, None, False, True),
        ],
    )

    with connect(temp_storage) as conn:
        metrics = [
            row["metric"]
            for row in conn.execute("SELECT metric FROM perf_results ORDER BY id")
        ]

    assert metrics == ["launch_time", "jank_pct", "pss"]


def test_failed_save_perf_rolls_back_partial_batch(temp_storage):
    run_id = create_run("app.apk", "login", ["p1"])

    class BadPerfRow:
        metric = "bad"
        median = 1.0

    with pytest.raises(AttributeError):
        save_perf(
            run_id,
            "p1",
            [
                PerfRow("launch_time", 800.0, 3.0, 900.0, -11.1, False, False),
                BadPerfRow(),
            ],
        )

    with connect(temp_storage) as conn:
        count = conn.execute("SELECT COUNT(*) FROM perf_results").fetchone()[0]

    assert count == 0
