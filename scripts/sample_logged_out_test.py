"""sample_logged_out_test.py — Verification test for logged-out / switched background execution.

Module: M12.2 Verification Test
Owner: Dhatri

What this does on each trigger:
1. Logs start time and current Windows user session.
2. Boots one headless emulator (Google_low_29) using swiftshader_indirect.
3. Takes an actual screenshot via ADB screencap.
4. Verifies the screenshot is non-empty and valid.
5. Shuts down the emulator and cleans up locks.
6. Records results in logs/sample_test_log.txt.
"""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import subprocess
import sys
import time

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from vmf.provision.provision import start_device, stop_device
from vmf.provision.android_paths import ADB

LOG_DIR = REPO_ROOT / "logs" / "sample_test_runs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
SUMMARY_LOG = REPO_ROOT / "logs" / "sample_test_log.txt"


def log(msg: str):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    with open(SUMMARY_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def check_screenshot_validity(img_path: Path) -> tuple[bool, str]:
    """Check that the screenshot file exists, has realistic size, and is not zeroed."""
    if not img_path.exists():
        return False, "File does not exist"
    size = img_path.stat().st_size
    if size < 20000:  # Valid Android screenshots are typically > 50KB PNGs
        return False, f"Suspiciously small image ({size} bytes)"
    
    # Check PNG magic bytes
    with open(img_path, "rb") as f:
        header = f.read(8)
        if header != b"\x89PNG\r\n\x1a\n":
            return False, "Not a valid PNG file"
    
    return True, f"Valid PNG ({size / 1024:.1f} KB)"


def run_cycle():
    now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    current_user = os.environ.get("USERNAME", "unknown")
    log("==================================================")
    log(f"Test cycle started | Windows User: {current_user}")

    profile = {
        "brand": "Google",
        "tier": "low",
        "api": 29,
        "avd_name": "Google_low_29",
    }
    port = 5554
    serial = f"emulator-{port}"

    try:
        t0 = time.time()
        log(f"Booting headless emulator {profile['avd_name']} on port {port}...")
        serial = start_device(profile, port)
        boot_time = time.time() - t0
        log(f"Boot completed in {boot_time:.1f}s. Serial: {serial}")

        # Capture screenshot
        screenshot_file = LOG_DIR / f"screen_{now_str}.png"
        log(f"Capturing screenshot via ADB to {screenshot_file.name}...")
        
        with open(screenshot_file, "wb") as f:
            cmd = [str(ADB), "-s", serial, "exec-out", "screencap", "-p"]
            res = subprocess.run(cmd, stdout=f, timeout=30)

        is_valid, reason = check_screenshot_validity(screenshot_file)
        if is_valid:
            log(f"SUCCESS: Screenshot captured successfully! ({reason})")
        else:
            log(f"WARNING: Screenshot check failed: {reason}")

    except Exception as e:
        log(f"ERROR during cycle: {e}")
    finally:
        log(f"Stopping emulator {serial} and cleaning up...")
        try:
            stop_device(serial)
            log("Emulator stopped cleanly.")
        except Exception as e:
            log(f"Error while stopping emulator: {e}")

    log("Test cycle finished.")
    log("==================================================\n")


if __name__ == "__main__":
    run_cycle()
