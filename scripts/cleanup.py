"""cleanup.py — VMF Storage Retention and Evidence Cleanup.

Module: M12 — Scheduling, Hardening & Cleanup (Step M12.4)
Owner: Dhatri
Contract: VMF-ARC-001 §8.2

Keeps evidence folders of the last N runs (default: 10, configurable).
Deletes older run folders from disk and marks their evidence paths as 'REMOVED'
in the SQLite steps table so the console displays 'removed' without broken image links.
All verdict, run, and summary rows are strictly preserved.
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import sys

logger = logging.getLogger("vmf.cleanup")


def get_directory_size(path: Path) -> int:
    """Return total bytes used by directory tree."""
    total = 0
    if not path.is_dir():
        return 0
    try:
        for entry in path.rglob("*"):
            if entry.is_file():
                total += entry.stat().st_size
    except Exception:
        pass
    return total


def cleanup_old_runs(
    keep_runs: int = 10,
    db_path: str | Path = "farm.db",
    results_dir: str | Path = "results",
    dry_run: bool = False,
) -> dict:
    """Retain the last `keep_runs` runs, purging older evidence from disk and marking DB.

    Args:
        keep_runs: Number of most recent runs to keep evidence for (default: 10).
        db_path: Path to the SQLite farm.db database.
        results_dir: Path to the results directory holding run evidence folders.
        dry_run: If True, preview what would be removed without deleting files or modifying DB.

    Returns:
        Summary dict containing counts of preserved runs, purged runs, and freed bytes.
    """
    db_file = Path(db_path).resolve()
    results_path = Path(results_dir).resolve()

    summary = {
        "runs_checked": 0,
        "runs_retained": 0,
        "runs_purged": 0,
        "folders_deleted": 0,
        "steps_updated": 0,
        "bytes_freed": 0,
        "dry_run": dry_run,
    }

    if not db_file.exists():
        logger.warning("Database not found at %s. No database records to update.", db_file)
        # Still clean orphan folders if results dir exists
        return _cleanup_orphan_folders(results_path, keep_runs, dry_run, summary)

    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    try:
        # Query all runs ordered newest to oldest
        cursor = conn.cursor()
        cursor.execute("SELECT run_id, started_at FROM runs ORDER BY run_id DESC")
        rows = cursor.fetchall()

        summary["runs_checked"] = len(rows)
        if len(rows) <= keep_runs:
            logger.info(
                "Total runs (%d) is <= retention limit (%d). Nothing to clean.",
                len(rows),
                keep_runs,
            )
            summary["runs_retained"] = len(rows)
            return summary

        retained_runs = rows[:keep_runs]
        purge_runs = rows[keep_runs:]

        summary["runs_retained"] = len(retained_runs)
        summary["runs_purged"] = len(purge_runs)

        logger.info(
            "Retaining latest %d runs (IDs: %s). Purging evidence for %d older runs.",
            keep_runs,
            [r["run_id"] for r in retained_runs],
            len(purge_runs),
        )

        for run in purge_runs:
            run_id = run["run_id"]
            run_folder = results_path / str(run_id)

            # 1. Calculate space and remove folder from disk
            if run_folder.exists() and run_folder.is_dir():
                folder_bytes = get_directory_size(run_folder)
                summary["bytes_freed"] += folder_bytes
                if dry_run:
                    logger.info("[DRY RUN] Would delete %s (%.2f MB)", run_folder, folder_bytes / (1024 * 1024))
                else:
                    logger.info("Deleting evidence folder %s (%.2f MB)", run_folder, folder_bytes / (1024 * 1024))
                    shutil.rmtree(run_folder, ignore_errors=True)
                    summary["folders_deleted"] += 1

            # 2. Mark paths as 'REMOVED' in SQLite steps table
            if not dry_run:
                cursor.execute(
                    """
                    UPDATE steps
                    SET screenshot_path = 'REMOVED', hierarchy_path = 'REMOVED'
                    WHERE run_id = ? AND (screenshot_path != 'REMOVED' OR hierarchy_path != 'REMOVED')
                    """,
                    (run_id,),
                )
                summary["steps_updated"] += cursor.rowcount
            else:
                cursor.execute(
                    """
                    SELECT COUNT(*) as cnt FROM steps
                    WHERE run_id = ? AND (screenshot_path != 'REMOVED' OR hierarchy_path != 'REMOVED')
                    """,
                    (run_id,),
                )
                cnt = cursor.fetchone()["cnt"]
                if cnt > 0:
                    logger.info("[DRY RUN] Would mark %d steps as 'REMOVED' for run %d", cnt, run_id)
                    summary["steps_updated"] += cnt

        if not dry_run:
            conn.commit()

    finally:
        conn.close()

    logger.info(
        "Cleanup finished: %d purged, %d folders removed, %.2f MB freed.",
        summary["runs_purged"],
        summary["folders_deleted"],
        summary["bytes_freed"] / (1024 * 1024),
    )
    return summary


def _cleanup_orphan_folders(
    results_path: Path,
    keep_runs: int,
    dry_run: bool,
    summary: dict,
) -> dict:
    """Fallback when database is absent: cleans oldest directories by modification time."""
    if not results_path.is_dir():
        return summary

    subdirs = [p for p in results_path.iterdir() if p.is_dir()]
    # Sort by directory name if numeric (run_id), else mtime
    def _sort_key(p: Path):
        try:
            return int(p.name)
        except ValueError:
            return p.stat().st_mtime

    sorted_dirs = sorted(subdirs, key=_sort_key, reverse=True)
    summary["runs_checked"] = len(sorted_dirs)

    if len(sorted_dirs) <= keep_runs:
        summary["runs_retained"] = len(sorted_dirs)
        return summary

    purge_dirs = sorted_dirs[keep_runs:]
    summary["runs_retained"] = keep_runs
    summary["runs_purged"] = len(purge_dirs)

    for p in purge_dirs:
        folder_bytes = get_directory_size(p)
        summary["bytes_freed"] += folder_bytes
        if dry_run:
            logger.info("[DRY RUN] Would delete orphan directory %s", p)
        else:
            logger.info("Deleting orphan directory %s", p)
            shutil.rmtree(p, ignore_errors=True)
            summary["folders_deleted"] += 1

    return summary


def main():
    parser = argparse.ArgumentParser(description="VMF Evidence Storage Cleanup (M12.4)")
    parser.add_argument(
        "--keep-runs",
        type=int,
        default=10,
        help="Number of most recent runs to keep evidence for (default: 10)",
    )
    parser.add_argument(
        "--db-path",
        default="farm.db",
        help="Path to SQLite database (default: farm.db)",
    )
    parser.add_argument(
        "--results-dir",
        default="results",
        help="Path to results evidence directory (default: results)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate cleanup without deleting any files or updating database",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    print("========================================")
    print("VMF M12.4 - Storage Retention Cleanup")
    print("========================================")
    print(f"Retention limit : Keep last {args.keep_runs} runs")
    print(f"Database        : {args.db_path}")
    print(f"Results dir     : {args.results_dir}")
    print(f"Dry run         : {args.dry_run}")
    print("----------------------------------------")

    summary = cleanup_old_runs(
        keep_runs=args.keep_runs,
        db_path=args.db_path,
        results_dir=args.results_dir,
        dry_run=args.dry_run,
    )

    print("\n========================================")
    print("M12.4 CLEANUP SUMMARY")
    print("========================================")
    print(f"Runs checked    : {summary['runs_checked']}")
    print(f"Runs retained   : {summary['runs_retained']}")
    print(f"Runs purged     : {summary['runs_purged']}")
    print(f"Folders deleted : {summary['folders_deleted']}")
    print(f"Steps updated   : {summary['steps_updated']}")
    print(f"Disk space freed: {summary['bytes_freed'] / (1024 * 1024):.2f} MB")
    if summary["dry_run"]:
        print("\nNote: DRY RUN mode enabled. No changes were applied.")
    print("PASS: Storage cleanup completed successfully.")


if __name__ == "__main__":
    main()
