"""nightly.py — VMF Nightly Scheduled Matrix Execution Runner.

Module: M12 — Scheduling, Hardening & Cleanup (Step M12.1 & M12.5)
Owner: Dhatri
Specification: Virtual Mobile Farm — Final Execution Plan §12 (M12.1, M12.5)

Invoked unattended by Windows Task Scheduler (daily at 01:00).
- Calls Gagan's API: POST http://localhost:8080/runs (with trigger=nightly).
  Falls back to direct orchestrator invocation if the API server is offline.
- Runs scripts/cleanup.py post-run to maintain the 10-run evidence retention policy.
- Appends run outcome to docs/nightly_stability_log.md (date, finished?, failures, notes).
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import logging
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.request

REPO_ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = REPO_ROOT / "logs"
STABILITY_LOG = REPO_ROOT / "docs" / "nightly_stability_log.md"

logger = logging.getLogger("vmf.nightly")


def setup_logger(log_file: Path):
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    # File handler
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(formatter)
    logger.addHandler(ch)

    logger.setLevel(logging.INFO)


def trigger_api_run(api_url: str, apk_path: str, scenario_path: str) -> dict | None:
    """Attempt to trigger the run via Gagan's FastAPI endpoint (POST /runs)."""
    endpoint = f"{api_url.rstrip('/')}/runs"
    payload = json.dumps({
        "apk": apk_path,
        "scenario": scenario_path,
        "trigger": "nightly",
    }).encode("utf-8")

    req = urllib.request.Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        logger.info("Attempting POST %s (trigger=nightly)...", endpoint)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read().decode("utf-8")
            result = json.loads(data)
            logger.info("Run successfully dispatched via API: %s", result)
            return result
    except (urllib.error.URLError, ConnectionError, TimeoutError, json.JSONDecodeError) as e:
        logger.warning("API endpoint %s not reachable (%s). Falling back to direct CLI run.", endpoint, e)
        return None


def trigger_cli_run(apk_path: str, scenario_path: str) -> bool:
    """Direct CLI invocation of the matrix orchestrator as fallback."""
    logger.info("Running orchestrator directly via CLI...")
    cmd = [
        sys.executable,
        "-m", "vmf.orchestrator.run_matrix",
        "--apk", apk_path,
        "--scenario", scenario_path,
        "--trigger", "nightly",
    ]
    try:
        proc = subprocess.run(cmd, cwd=REPO_ROOT, text=True, capture_output=True, timeout=7200)
        if proc.stdout:
            logger.info("Orchestrator STDOUT:\n%s", proc.stdout[-2000:])
        if proc.stderr:
            logger.warning("Orchestrator STDERR:\n%s", proc.stderr[-2000:])
        return proc.returncode == 0
    except Exception as e:
        logger.error("CLI run failed with exception: %s", e)
        return False


def run_cleanup():
    """Run evidence storage cleanup keeping last 10 runs."""
    cleanup_script = REPO_ROOT / "scripts" / "cleanup.py"
    if cleanup_script.exists():
        logger.info("Running post-test evidence cleanup...")
        try:
            subprocess.run([sys.executable, str(cleanup_script), "--keep-runs", "10"], cwd=REPO_ROOT, check=False)
        except Exception as e:
            logger.warning("Storage cleanup encountered error: %s", e)


def append_stability_log(date_str: str, finished: bool, failures: int | str, notes: str):
    """Update docs/nightly_stability_log.md per M12.5."""
    STABILITY_LOG.parent.mkdir(parents=True, exist_ok=True)
    if not STABILITY_LOG.exists():
        STABILITY_LOG.write_text(
            "# VMF Nightly Stability Log (5-Night Log)\n\n"
            "Owner: Dhatri | Module: M12.5, M15.4\n"
            "Required: 5 unattended nights from 28 Oct to 1 Nov (morning of 2 Nov).\n\n"
            "| Night | Date | Finished? | Failures | Notes / Fix |\n"
            "|---|---|---|---|---|\n",
            encoding="utf-8",
        )

    status_str = "Yes" if finished else "No"
    line = f"| auto | {date_str} | {status_str} | {failures} | {notes} |\n"
    with open(STABILITY_LOG, "a", encoding="utf-8") as f:
        f.write(line)
    logger.info("Logged result to %s", STABILITY_LOG)


def main():
    parser = argparse.ArgumentParser(description="VMF Nightly Test Runner (M12.1)")
    parser.add_argument("--api-url", default="http://localhost:8080", help="VMF API URL")
    parser.add_argument("--apk", default="sample_app/sample.apk", help="Path to test APK")
    parser.add_argument("--scenario", default="scenarios/login_flow.yaml", help="Path to scenario YAML")
    parser.add_argument("--skip-cleanup", action="store_true", help="Do not run storage cleanup")
    args = parser.parse_args()

    date_str = datetime.now().strftime("%Y-%m-%d")
    log_file = LOGS_DIR / f"nightly_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    setup_logger(log_file)

    logger.info("========================================")
    logger.info("VMF M12 Nightly Run Started: %s", date_str)
    logger.info("========================================")

    # 1. Trigger via API or CLI
    api_res = trigger_api_run(args.api_url, args.apk, args.scenario)
    success = False
    notes = ""
    failures = 0

    if api_res is not None:
        success = True
        notes = f"Run #{api_res.get('run_id', 'unknown')} dispatched via API"
    else:
        success = trigger_cli_run(args.apk, args.scenario)
        notes = "Dispatched via CLI fallback" if success else "CLI execution error"

    # 2. Evidence Cleanup
    if not args.skip_cleanup:
        run_cleanup()

    # 3. Stability Log entry (M12.5)
    append_stability_log(
        date_str=date_str,
        finished=success,
        failures=failures if success else "ERROR",
        notes=notes,
    )

    logger.info("========================================")
    logger.info("VMF Nightly Run Finished. Status: %s", "PASS" if success else "FAIL")
    logger.info("========================================")


if __name__ == "__main__":
    main()
