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
_BASELINE_COLUMNS = {
    "build_sha256": "TEXT", "promoted_by": "TEXT", "promoted_on": "TEXT", "run_id": "INTEGER"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolved_db_path() -> Path:
    return Path(_DB_PATH).expanduser().resolve()


def _open_connection(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
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
                existing = {row[1] for row in conn.execute("PRAGMA table_info(perf_baselines)")}
                for column, kind in _BASELINE_COLUMNS.items():
                    if column not in existing:
                        conn.execute(f"ALTER TABLE perf_baselines ADD COLUMN {column} {kind}")
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


def finish_run(run_id: int, status: str = "FINISHED") -> None:
    """Mark a run with ``status`` and record its UTC finish timestamp.

    The default preserves the original one-argument contract used by the
    orchestrator.  The optional status supports the nightly runner, which
    distinguishes a clean completion from one with failed profiles.
    """
    if not isinstance(status, str) or not status.strip():
        raise ValueError("status must be a non-empty string")
    status = status.strip()
    logging.info("storage.finish_run run_id=%s status=%s", run_id, status)
    with _connection() as conn:
        with conn:
            conn.execute(
                """
                UPDATE runs
                SET status = ?, finished_at = ?
                WHERE run_id = ?
                """,
                (status, _utc_now(), run_id),
            )


def finish_run_if_running(run_id: int, status: str) -> bool:
    """Finish only a still-running run, preserving an existing terminal status."""
    if not isinstance(status, str) or not status.strip():
        raise ValueError("status must be a non-empty string")
    with _connection() as conn:
        with conn:
            cursor = conn.execute(
                "UPDATE runs SET status = ?, finished_at = ? WHERE run_id = ? AND status = ?",
                (status.strip(), _utc_now(), run_id, "RUNNING"),
            )
    return cursor.rowcount == 1


def load_baselines(profile: str) -> dict[str, float]:
    """Return the current emulator-relative baseline values for ``profile``."""
    with _connection() as conn:
        rows = conn.execute(
            "SELECT metric, baseline FROM perf_baselines WHERE profile = ? ORDER BY id", (profile,)).fetchall()
    return {str(metric): float(value) for metric, value in rows}


def list_baseline_entries(profile_names: list[str] | None = None) -> list[dict]:
    """Return baseline values with provenance for reporting and CLI inspection."""
    query, params = "SELECT profile, metric, baseline AS value, build_sha256, promoted_by, promoted_on, run_id FROM perf_baselines", []
    if profile_names:
        placeholders = ", ".join("?" for _ in profile_names)
        query += f" WHERE profile IN ({placeholders})"
        params = list(profile_names)
    query += " ORDER BY profile, metric"
    with _connection() as conn:
        return [dict(row) for row in conn.execute(query, params)]


def save_baseline(profile: str, metric: str, value: float, *, build_sha256: str | None = None,
                  promoted_by: str | None = None, run_id: int | None = None) -> None:
    """Store one explicitly approved baseline, replacing only that metric/profile pair."""
    if not profile or not metric:
        raise ValueError("profile and metric are required")
    with _connection() as conn:
        with conn:
            conn.execute("DELETE FROM perf_baselines WHERE profile = ? AND metric = ?", (profile, metric))
            conn.execute(
                """INSERT INTO perf_baselines
                (profile, metric, baseline, build_sha256, promoted_by, promoted_on, run_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (profile, metric, float(value), build_sha256, promoted_by, _utc_now(), run_id),
            )


def list_runs(*, limit: int = 100, offset: int = 0, status: str | None = None) -> list[dict]:
    """List persisted runs newest first, with profile/result counts for the console."""
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    where, params = "", []
    if status:
        where, params = "WHERE r.status = ?", [status]
    query = f"""SELECT r.run_id, r.apk_name, r.scenario, r.started_at, r.finished_at, r.status,
        COUNT(DISTINCT d.profile) AS profiles,
        SUM(CASE WHEN d.result = 'pass' THEN 1 ELSE 0 END) AS passed
        FROM runs r LEFT JOIN device_results d ON d.run_id = r.run_id
        {where} GROUP BY r.run_id ORDER BY r.run_id DESC LIMIT ? OFFSET ?"""
    with _connection() as conn:
        rows = conn.execute(query, [*params, limit, offset]).fetchall()
    return [dict(row) for row in rows]


def get_run(run_id: int) -> dict | None:
    """Reconstruct one run from persisted rows; never queries a live device."""
    with _connection() as conn:
        run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if run is None:
            return None
        results = [dict(row) for row in conn.execute(
            "SELECT profile, result, cause, failed_step, trigger_behaviour FROM device_results "
            "WHERE run_id = ? ORDER BY profile", (run_id,))]
        steps = [dict(row) for row in conn.execute(
            "SELECT profile, step_index, action, status, duration_ms, screenshot_path, hierarchy_path FROM steps "
            "WHERE run_id = ? ORDER BY profile, step_index", (run_id,))]
        configs = {row["profile"]: json.loads(row["config_json"]) for row in conn.execute(
            "SELECT profile, config_json FROM profile_config WHERE run_id = ?", (run_id,))}
        perf = [dict(row) for row in conn.execute(
            "SELECT profile, metric, median, spread_pct, baseline, regression_pct, noisy, unstable FROM perf_results "
            "WHERE run_id = ? ORDER BY profile, metric", (run_id,))]
    record = dict(run)
    record.update({"device_results": results, "steps": steps, "profile_config": configs, "perf_results": perf})
    return record
