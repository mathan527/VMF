"""SQLite-backed persistent storage interface for VMF M9."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import sqlite3
import threading

_ROOT_DIR = Path(__file__).resolve().parents[2]
_SCHEMA_PATH = _ROOT_DIR / "schema.sql"
_DB_PATH = Path(os.environ.get("VMF_DB_PATH", _ROOT_DIR / "farm.db"))
_INIT_LOCK = threading.Lock()
_INITIALIZED_PATHS: set[Path] = set()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolved_db_path() -> Path:
    return Path(_DB_PATH).expanduser().resolve()


def _open_connection(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _initialize_database() -> None:
    path = _resolved_db_path()
    if path in _INITIALIZED_PATHS and path.exists():
        return

    with _INIT_LOCK:
        if path in _INITIALIZED_PATHS and path.exists():
            return
        if not _SCHEMA_PATH.exists():
            raise FileNotFoundError(f"storage schema not found: {_SCHEMA_PATH}")

        schema = _SCHEMA_PATH.read_text(encoding="utf-8")
        conn = _open_connection(path)
        try:
            with conn:
                conn.executescript(schema)
            _INITIALIZED_PATHS.add(path)
        finally:
            conn.close()


@contextmanager
def _connection():
    _initialize_database()
    conn = _open_connection(_resolved_db_path())
    try:
        yield conn
    finally:
        conn.close()


def create_run(apk_name: str, scenario: str, profiles: list[str]) -> int:
    """Open a new persistent test run record and return its SQLite run_id."""
    logging.info("storage.create_run apk_name=%s scenario=%s profiles=%s",
                 apk_name, scenario, profiles)
    with _connection() as conn:
        with conn:
            cursor = conn.execute(
                """
                INSERT INTO runs (apk_name, scenario, started_at, status)
                VALUES (?, ?, ?, ?)
                """,
                (apk_name, scenario, _utc_now(), "RUNNING"),
            )
        return int(cursor.lastrowid)


def save_device_result(run_id: int, profile: str, verdict) -> None:
    """Persist the overall pass/fail verdict for one opaque profile id."""
    logging.info("storage.save_device_result run_id=%s profile=%s verdict=%s",
                 run_id, profile, verdict)
    with _connection() as conn:
        with conn:
            conn.execute(
                """
                INSERT INTO device_results (
                    run_id, profile, result, cause, failed_step, trigger_behaviour
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    profile,
                    verdict.result,
                    verdict.cause,
                    verdict.failed_step,
                    verdict.trigger_behaviour,
                ),
            )


def save_step(run_id: int, profile: str, step) -> None:
    """Persist one scenario step result and its evidence path references."""
    logging.info("storage.save_step run_id=%s profile=%s step=%s",
                 run_id, profile, step)
    with _connection() as conn:
        with conn:
            conn.execute(
                """
                INSERT INTO steps (
                    run_id, profile, step_index, action, status, duration_ms,
                    screenshot_path, hierarchy_path
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    profile,
                    step.step_index,
                    step.action,
                    step.status,
                    step.duration_ms,
                    step.screenshot_path,
                    step.hierarchy_path,
                ),
            )


def save_profile_config(run_id: int, profile: str, config: dict) -> None:
    """Persist the complete profile configuration as deterministic JSON."""
    logging.info("storage.save_profile_config run_id=%s profile=%s config=%s",
                 run_id, profile, config)
    config_json = json.dumps(config, sort_keys=True)
    with _connection() as conn:
        with conn:
            conn.execute(
                """
                INSERT INTO profile_config (run_id, profile, config_json)
                VALUES (?, ?, ?)
                """,
                (run_id, profile, config_json),
            )


def save_perf(run_id: int, profile: str, perf_rows: list) -> None:
    """Persist performance metric rows for one opaque profile id."""
    logging.info("storage.save_perf run_id=%s profile=%s perf_rows=%s",
                 run_id, profile, perf_rows)
    with _connection() as conn:
        with conn:
            for row in perf_rows:
                conn.execute(
                    """
                    INSERT INTO perf_results (
                        run_id, profile, metric, median, spread_pct, baseline,
                        regression_pct, noisy, unstable
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        profile,
                        row.metric,
                        row.median,
                        row.spread_pct,
                        row.baseline,
                        row.regression_pct,
                        int(bool(row.noisy)),
                        int(bool(row.unstable)),
                    ),
                )


def finish_run(run_id: int) -> None:
    """Mark a run as finished and record its UTC finish timestamp."""
    logging.info("storage.finish_run run_id=%s", run_id)
    with _connection() as conn:
        with conn:
            conn.execute(
                """
                UPDATE runs
                SET status = ?, finished_at = ?
                WHERE run_id = ?
                """,
                ("FINISHED", _utc_now(), run_id),
            )
