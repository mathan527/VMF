"""nightly.py — VMF Nightly Scheduled Matrix Execution Runner.

Module: M12 — Scheduling, Hardening & Cleanup (Step M12.1 & M12.5)
Owner: Dhatri
Specification: Virtual Mobile Farm — Final Execution Plan §12 (M12.1, M12.5)

Invoked unattended by Windows Task Scheduler (daily at 01:00) or on-demand:
1. If Gagan's API is online, dispatches run via POST http://localhost:8080/runs.
2. Otherwise, executes an autonomous unattended device sweep across target profiles:
   - Headless WHPX + SwiftShader emulator boot via start_device()
   - Verification of boot completion
   - High-resolution screenshot capture via ADB exec-out screencap
   - UI hierarchy dump via uiautomator dump
   - OEM profile display and system settings applied via oem_packs (M5)
   - Readback configuration saved as profile_config.json
   - Logcat excerpt captured as logcat.txt
   - Results recorded in SQLite (farm.db) via storage.py (M9)
   - Complete emulator teardown with zero orphaned processes via stop_device()
3. Runs scripts/cleanup.py post-run to maintain 10-run evidence retention policy.
4. Updates docs/nightly_stability_log.md for the 5-night acceptance log (M12.5).
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.chdir(REPO_ROOT)

# When running in Administrator mode (e.g. jasrm\admin), redirect to B2-Group2 user profile
# so emulator AVDs and .android folder are discovered seamlessly without touching vmf/ packages.
b2_profile = Path("C:/Users/B2-Group2")
if not (Path.home() / ".android" / "avd").exists() and (b2_profile / ".android" / "avd").exists():
    os.environ["USERPROFILE"] = str(b2_profile)
    os.environ["ANDROID_AVD_HOME"] = str(b2_profile / ".android" / "avd")

from vmf.provision.provision import start_device, stop_device
from vmf.provision.device_lifecycle import cleanup_orphans
from vmf.provision.android_paths import ADB
from vmf.oem_packs.oem_packs import apply_profile_settings, verify_settings
from vmf.results import Step, Verdict
from vmf import storage

LOGS_DIR = REPO_ROOT / "logs"
RESULTS_DIR = REPO_ROOT / "results"
STABILITY_LOG = REPO_ROOT / "docs" / "nightly_stability_log.md"
CATALOGUE_PATH = REPO_ROOT / "vmf" / "provision" / "reference-models.yaml"

logger = logging.getLogger("vmf.nightly")
SCREENSHOT_MAX_ATTEMPTS = 10
SCREENSHOT_RETRY_DELAY_S = 3
WINDOW_ANIMATION_SETTINGS = (
    "window_animation_scale",
    "transition_animation_scale",
    "animator_duration_scale",
)


def setup_logger(log_file: Path):
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(formatter)
    logger.addHandler(ch)

    logger.setLevel(logging.INFO)


def check_screenshot_validity(img_path: Path) -> tuple[bool, str]:
    """Validate PNG structure, dimensions and visible image content."""
    if not img_path.exists():
        return False, "File does not exist"

    size = img_path.stat().st_size
    if size < 1024:
        return False, f"Screenshot is empty or truncated ({size} bytes)"

    try:
        from PIL import Image, ImageStat

        with Image.open(img_path) as image:
            if image.format != "PNG":
                return False, f"Expected PNG, received {image.format}"
            image.verify()

        with Image.open(img_path) as image:
            image.load()
            width, height = image.size
            if width < 240 or height < 320:
                return False, f"Screenshot dimensions too small: {width}x{height}"

            stats = ImageStat.Stat(image.convert("RGB"))
            if max(stats.stddev) < 1.0:
                return False, "Screenshot appears blank or uniform"

        return True, f"Valid PNG {width}x{height} ({size / 1024:.1f} KB)"

    except Exception as exc:
        return False, f"Invalid or unreadable PNG: {exc}"


def _disable_hierarchy_capture_animations(serial: str) -> dict[str, str | None]:
    previous: dict[str, str | None] = {}
    for setting in WINDOW_ANIMATION_SETTINGS:
        command = [str(ADB), "-s", serial, "shell", "settings", "get", "global", setting]
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, errors="replace",
                timeout=10, check=False
            )
            if result.returncode != 0:
                continue
            value = (result.stdout or "").strip()
            previous[setting] = value if value and value.lower() != "null" else None
            subprocess.run(
                [str(ADB), "-s", serial, "shell", "settings", "put", "global", setting, "0"],
                capture_output=True, text=True, errors="replace", timeout=10, check=False
            )
        except Exception as exc:
            logger.info("Could not disable %s during hierarchy capture on %s: %s",
                        setting, serial, exc)
    return previous


def _restore_hierarchy_capture_animations(
        serial: str, previous: dict[str, str | None]) -> None:
    for setting, value in previous.items():
        command = [str(ADB), "-s", serial, "shell", "settings"]
        if value is None:
            command.extend(["delete", "global", setting])
        else:
            command.extend(["put", "global", setting, value])
        try:
            subprocess.run(
                command, capture_output=True, text=True, errors="replace",
                timeout=10, check=False
            )
        except Exception as exc:
            logger.warning("Could not restore %s after hierarchy capture on %s: %s",
                           setting, serial, exc)



def capture_evidence(serial: str, target_dir: Path, step_idx: int, action_name: str) -> Step:
    """Capture screenshot and UI hierarchy with retries for transient Android delays."""
    import xml.etree.ElementTree as ET

    target_dir.mkdir(parents=True, exist_ok=True)
    ss_path = target_dir / f"step_{step_idx:02d}_{action_name}.png"
    xml_path = target_dir / f"step_{step_idx:02d}_{action_name}_hierarchy.xml"
    t0 = time.time()

    # Allow Android display and SystemUI surfaces to settle before capture.
    time.sleep(SCREENSHOT_RETRY_DELAY_S)

    valid = False
    reason = "Screenshot capture was not attempted"

    for attempt in range(1, SCREENSHOT_MAX_ATTEMPTS + 1):
        temp_path = ss_path.with_name(ss_path.stem + f".attempt{attempt}.png")
        try:
            cmd = [str(ADB), "-s", serial, "exec-out", "screencap", "-p"]
            with open(temp_path, "wb") as f:
                proc = subprocess.run(
                    cmd, stdout=f, stderr=subprocess.PIPE,
                    timeout=30, check=False
                )

            valid, reason = check_screenshot_validity(temp_path)
            if proc.returncode != 0:
                valid = False
                reason = f"ADB screencap exit code {proc.returncode}"

            if valid:
                temp_path.replace(ss_path)
                logger.info("Screenshot captured for %s (%s), attempt %d",
                            serial, action_name, attempt)
                break

            logger.info("Screenshot attempt %d/%d failed for %s (%s): %s",
                        attempt, SCREENSHOT_MAX_ATTEMPTS, serial, action_name, reason)

        except Exception as exc:
            reason = str(exc)
            logger.info("Screenshot attempt %d/%d raised for %s: %s",
                        attempt, SCREENSHOT_MAX_ATTEMPTS, serial, reason)
        finally:
            if temp_path.exists():
                temp_path.unlink()

        if attempt < SCREENSHOT_MAX_ATTEMPTS:
            time.sleep(SCREENSHOT_RETRY_DELAY_S)

    if not valid:
        logger.error("Screenshot failed after %d attempts for %s (%s): %s",
                 SCREENSHOT_MAX_ATTEMPTS, serial, action_name, reason)

    # Retry hierarchy capture; accept only parseable hierarchy XML.
    hierarchy_valid = False
    previous_animations = _disable_hierarchy_capture_animations(serial)
    try:
        for attempt in range(1, 4):
            temp_xml = xml_path.with_name(xml_path.stem + f".attempt{attempt}.xml")
            try:
                cmd = [str(ADB), "-s", serial, "exec-out",
                       "uiautomator", "dump", "/dev/tty"]
                proc = subprocess.run(
                    cmd, capture_output=True, text=True,
                    errors="replace", timeout=25, check=False
                )
                output = proc.stdout or ""

                if "<hierarchy" not in output:
                    subprocess.run(
                        [str(ADB), "-s", serial, "shell", "uiautomator",
                         "dump", "/sdcard/dump.xml"],
                        capture_output=True, text=True,
                        errors="replace", timeout=20, check=False
                    )
                    pulled = subprocess.run(
                        [str(ADB), "-s", serial, "exec-out",
                         "cat", "/sdcard/dump.xml"],
                        capture_output=True, text=True,
                        errors="replace", timeout=20, check=False
                    )
                    output = pulled.stdout or ""
                    proc = pulled

                hierarchy_start = output.find("<hierarchy")
                hierarchy_end = output.find("</hierarchy>", hierarchy_start)
                if hierarchy_start >= 0 and hierarchy_end >= 0:
                    hierarchy_xml = output[hierarchy_start:hierarchy_end + len("</hierarchy>")]
                    ET.fromstring(hierarchy_xml)
                    temp_xml.write_text(hierarchy_xml, encoding="utf-8")
                    temp_xml.replace(xml_path)
                    hierarchy_valid = True
                    logger.info("Hierarchy captured for %s (%s), attempt %d",
                                serial, action_name, attempt)
                    break

                logger.info("Hierarchy dump returned no XML for %s (%s): rc=%s stdout=%r stderr=%r",
                            serial, action_name, proc.returncode,
                            (proc.stdout or "")[:240], (proc.stderr or "")[:240])
                logger.warning("Hierarchy attempt %d/3 returned no valid hierarchy for %s",
                               attempt, serial)

            except Exception as exc:
                logger.warning("Hierarchy attempt %d/3 failed for %s: %s",
                               attempt, serial, exc)
            finally:
                if temp_xml.exists():
                    temp_xml.unlink()

            if attempt < 3:
                time.sleep(2)
    finally:
        _restore_hierarchy_capture_animations(serial, previous_animations)

    if not hierarchy_valid:
        logger.error("Hierarchy capture failed after 3 attempts for %s (%s)",
                     serial, action_name)
        if not xml_path.exists():
            xml_path.write_text("ERROR: hierarchy capture failed after 3 attempts",
                                encoding="utf-8")

    dur_ms = int((time.time() - t0) * 1000)
    status = "pass" if valid else "fail"

    return Step(
        step_index=step_idx,
        action=action_name,
        status=status,
        duration_ms=dur_ms,
        screenshot_path=str(ss_path.relative_to(REPO_ROOT)),
        hierarchy_path=str(xml_path.relative_to(REPO_ROOT)),
    )



def dump_logcat(serial: str, output_file: Path, lines: int = 400):
    """Dump tail of logcat for evidence."""
    try:
        cmd = [str(ADB), "-s", serial, "logcat", "-d", "-t", str(lines)]
        with open(output_file, "w", encoding="utf-8", errors="replace") as f:
            subprocess.run(cmd, stdout=f, timeout=20, check=False)
    except Exception as e:
        logger.warning("Failed to dump logcat for %s: %s", serial, e)


def trigger_api_run(api_url: str, apk_path: str, scenario_path: str, profiles: list[dict]) -> dict | None:
    """Attempt to trigger the run via Gagan's FastAPI endpoint (POST /runs)."""
    endpoint = f"{api_url.rstrip('/')}/runs"
    payload = json.dumps({
        "apk": apk_path,
        "scenario": scenario_path,
        "profiles": profiles,
    }).encode("utf-8")

    req = urllib.request.Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        logger.info("Attempting POST %s (trigger=nightly)...", endpoint)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read().decode("utf-8")
            result = json.loads(data)
            logger.info("Run successfully dispatched via API: %s", result)
            return result
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        logger.error("API rejected POST %s: HTTP %s: %s", endpoint, e.code, body)
        raise RuntimeError(f"API request rejected with HTTP {e.code}: {body}") from e
    except (urllib.error.URLError, ConnectionError, TimeoutError, json.JSONDecodeError, OSError) as e:
        logger.warning("API endpoint %s is unavailable (%s). Falling back to direct sweep.", endpoint, e)
        return None


def run_unattended_device_sweep(
    models: list[dict],
    run_id: int | None = None,
    base_port: int = 5554,
) -> tuple[bool, int, str]:
    """Execute autonomous unattended device sweep across models.

    Boots each emulator headless, applies OEM packs, captures screenshots and
    hierarchy, records evidence and database entries, and cleanly shuts down.
    """
    logger.info("Beginning unattended device sweep for %d profiles...", len(models))
    profile_names = [f"{m.get('brand', 'Unknown')}_{m.get('tier', 'low')}_{m.get('api', 29)}" for m in models]

    if run_id is None:
        try:
            run_id = storage.create_run("VMF_Nightly_Suite", "oem_settings_verification", profile_names)
        except Exception as e:
            logger.warning("Could not create run via storage: %s. Using timestamp run_id.", e)
            run_id = int(time.time())

    run_evidence_dir = RESULTS_DIR / str(run_id)
    run_evidence_dir.mkdir(parents=True, exist_ok=True)

    failures = 0
    passed = 0

    for idx, profile in enumerate(models, start=1):
        brand = profile.get("brand", "Generic")
        tier = profile.get("tier", "low")
        api_level = profile.get("api", 29 if tier == "low" else (31 if tier == "medium" else 35))
        prof_name = f"{brand}_{tier}_{api_level}"

        logger.info("[%d/%d] Starting profile %s...", idx, len(models), prof_name)
        dev_dir = run_evidence_dir / prof_name
        dev_dir.mkdir(parents=True, exist_ok=True)

        serial = f"emulator-{base_port}"
        t_start = time.time()

        try:
            # 1. Clean any lingering emulator before boot
            cleanup_orphans()

            # 2. Boot device
            logger.info("  Booting headless %s on port %d...", prof_name, base_port)
            serial = start_device(profile, base_port)
            boot_s = time.time() - t_start
            logger.info("  Boot completed in %.1fs. Serial: %s", boot_s, serial)

            # 3. Capture boot screenshot & hierarchy (Step 1)
            step1 = capture_evidence(serial, dev_dir, 1, "boot")
            logger.info("  Step 1 (boot): %s (duration=%dms)", step1.status, step1.duration_ms)
            try:
                storage.save_step(run_id, prof_name, step1)
            except Exception:
                pass

            # 4. Apply OEM Profile Settings (Layer B)
            logger.info("  Applying OEM Layer B settings for %s...", brand)
            apply_profile_settings(serial, profile)
            readback = verify_settings(serial)
            config_file = dev_dir / "profile_config.json"
            config_file.write_text(json.dumps(readback, indent=2), encoding="utf-8")
            try:
                storage.save_profile_config(run_id, prof_name, readback)
            except Exception:
                pass

            # 5. Capture post-OEM applied screenshot & hierarchy (Step 2)
            step2 = capture_evidence(serial, dev_dir, 2, "oem_applied")
            logger.info("  Step 2 (oem_applied): %s (duration=%dms)", step2.status, step2.duration_ms)
            try:
                storage.save_step(run_id, prof_name, step2)
            except Exception:
                pass

            # 6. Dump logcat tail
            dump_logcat(serial, dev_dir / "logcat.txt", lines=400)

            # 7. Record verdict
            if step1.status == "pass" and step2.status == "pass":
                verdict = Verdict(result="pass", cause="Boot and OEM settings captured cleanly", failed_step=None, trigger_behaviour=None)
                passed += 1
                logger.info("  [PASS] %s finished cleanly.", prof_name)
            else:
                verdict = Verdict(result="fail", cause="Screenshot validation failed", failed_step=1 if step1.status != "pass" else 2, trigger_behaviour=None)
                failures += 1
                logger.warning("  [FAIL] %s failed screenshot validation.", prof_name)

            try:
                storage.save_device_result(run_id, prof_name, verdict)
            except Exception:
                pass

        except Exception as e:
            logger.error("  [ERROR] Profile %s encountered exception: %s", prof_name, e)
            failures += 1
            verdict = Verdict(result="error", cause=str(e), failed_step=None, trigger_behaviour=None)
            try:
                storage.save_device_result(run_id, prof_name, verdict)
            except Exception:
                pass
        finally:
            logger.info("  Stopping %s and cleaning locks...", serial)
            try:
                stop_device(serial)
            except Exception as ex:
                logger.warning("  stop_device error: %s", ex)

    # Finalize storage run
    try:
        storage.finish_run(run_id, "FINISHED" if failures == 0 else "COMPLETED_WITH_FAILURES")
    except Exception:
        pass

    summary_note = f"Run #{run_id}: {passed}/{len(models)} passed ({failures} failures). Evidence: results/{run_id}"
    logger.info("Sweep finished: %s", summary_note)
    return (failures == 0), failures, summary_note


def run_cleanup():
    """Run evidence storage cleanup keeping last 10 runs."""
    cleanup_script = REPO_ROOT / "scripts" / "cleanup.py"
    if cleanup_script.exists():
        logger.info("Running post-test evidence cleanup...")
        try:
            subprocess.run([sys.executable, str(cleanup_script), "--keep-runs", "10"], cwd=REPO_ROOT, check=False)
        except Exception as e:
            logger.warning("Storage cleanup encountered error: %s", e)


def update_stability_log(date_str: str, finished: bool, failures: int | str, notes: str):
    """Update docs/nightly_stability_log.md table row for the given date or next pending night."""
    STABILITY_LOG.parent.mkdir(parents=True, exist_ok=True)
    if not STABILITY_LOG.exists():
        STABILITY_LOG.write_text(
            "# VMF Nightly Stability Log (5-Night Log)\n\n"
            "**Module**: M12.5 & M15.4  \n"
            "**Owner**: Dhatri  \n"
            "**Required for Acceptance**: 5 unattended nights starting from Wednesday 28 October to Sunday 1 November 2026.\n\n"
            "---\n\n"
            "## Stability Run Table\n\n"
            "| Night | Date | Finished? | Failures | Notes / Fix Applied | Verified By |\n"
            "|---|---|---|---|---|---|\n"
            f"| Night 1 | 2026-10-28 | Pending | — | Night 1 unattended matrix run | Dhatri |\n"
            f"| Night 2 | 2026-10-29 | Pending | — | Night 2 unattended matrix run | Dhatri |\n"
            f"| Night 3 | 2026-10-30 | Pending | — | Night 3 unattended matrix run | Dhatri |\n"
            f"| Night 4 | 2026-10-31 | Pending | — | Night 4 unattended matrix run | Dhatri |\n"
            f"| Night 5 | 2026-11-01 | Pending | — | Night 5 unattended matrix run | Dhatri |\n",
            encoding="utf-8",
        )

    content = STABILITY_LOG.read_text(encoding="utf-8")
    status_str = "Yes" if finished else "No"
    updated = False
    lines = content.splitlines()

    # Look for matching date line or first pending line
    for i, line in enumerate(lines):
        if line.startswith("| Night") and (date_str in line or "Pending" in line):
            # Parse Night identifier
            parts = [p.strip() for p in line.split("|")]
            # parts[0] is empty, parts[1] is 'Night X', parts[2] is Date
            if len(parts) >= 7:
                night_label = parts[1]
                lines[i] = f"| {night_label} | {date_str} | {status_str} | {failures} | {notes} | Dhatri |"
                updated = True
                break

    if not updated:
        lines.append(f"| auto | {date_str} | {status_str} | {failures} | {notes} | Dhatri |")

    STABILITY_LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("Updated stability log in %s", STABILITY_LOG)


def load_target_profiles(catalogue_file: Path, limit: int | None = None, brand: str | None = None) -> list[dict]:
    """Load model definitions from catalogue YAML."""
    if not catalogue_file.exists():
        logger.warning("Catalogue %s not found. Using default Google profile.", catalogue_file)
        return [{"brand": "Google", "tier": "low", "api": 29}]

    with open(catalogue_file, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    models = data.get("models", [])
    if brand:
        models = [m for m in models if m.get("brand", "").lower() == brand.lower()]

    # Ensure api is populated based on tier
    tier_map = {"low": 29, "medium": 31, "high": 35}
    for m in models:
        if "api" not in m:
            m["api"] = tier_map.get(m.get("tier", "low"), 29)

    if limit is not None and limit > 0:
        models = models[:limit]

    return models


def main():
    parser = argparse.ArgumentParser(description="VMF Nightly Test Runner (M12.1, M12.5)")
    parser.add_argument("--api-url", default="http://localhost:8080", help="VMF API URL")
    parser.add_argument("--apk", default="sample_app/build/sample.apk", help="Path to test APK")
    parser.add_argument("--scenario", default="scenarios/login_flow.yaml", help="Path to scenario YAML")
    parser.add_argument("--profiles", default=str(CATALOGUE_PATH), help="Path to reference models YAML")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of profiles to sweep (e.g. 1 for quick test)")
    parser.add_argument("--brand", default=None, help="Filter to specific brand (e.g. Google, Samsung)")
    parser.add_argument("--direct", action="store_true", help="Run the local sweep without API dispatch")
    parser.add_argument("--skip-cleanup", action="store_true", help="Do not run storage cleanup")
    parser.add_argument("--run-now", action="store_true", help="Run immediately and log today's night")
    args = parser.parse_args()

    date_str = datetime.now().strftime("%Y-%m-%d")
    log_file = LOGS_DIR / f"nightly_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    setup_logger(log_file)

    logger.info("========================================")
    logger.info("VMF M12 Nightly Run Started: %s", date_str)
    logger.info("========================================")

    # Load the selected profiles once; use the same selection for API and fallback.
    models = load_target_profiles(Path(args.profiles), limit=args.limit, brand=args.brand)
    logger.info("Loaded %d target profiles.", len(models))

    # 1. First try API run if available.
    api_res = None if args.direct else trigger_api_run(args.api_url, args.apk, args.scenario, models)
    success = False
    failures = 0
    notes = ""

    if api_res is not None:
        # Dispatch acceptance is not the same as matrix completion.
        success = False
        notes = f"Run #{api_res.get('run_id', 'unknown')} accepted by API; completion not yet verified"
        logger.info("%s", notes)
    else:
        # 2. Autonomous fallback sweep.
        success, failures, notes = run_unattended_device_sweep(models)

    # 3. Evidence Retention Cleanup (M12.4)
    if not args.skip_cleanup:
        run_cleanup()

    # 4. Update 5-Night Stability Log (M12.5)
    update_stability_log(
        date_str=date_str,
        finished=success,
        failures=failures if success else "FAIL",
        notes=notes,
    )

    logger.info("========================================")
    logger.info("VMF Nightly Run Finished. Status: %s", "PASS" if success else "FAIL")
    logger.info("========================================")


if __name__ == "__main__":
    main()
