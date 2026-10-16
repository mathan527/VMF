#M3 Checking for 24 devices

import argparse
import csv
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

from device_lifecycle import start_with_retry, stop_device

ROOT = Path(__file__).resolve().parent.parent
CATALOG = Path(__file__).resolve().parent / "reference-models.yaml"
REPORT = ROOT / "batch_lifecycle_results.csv"

API_BY_TIER = {
    "low": 29,
    "medium": 31,
    "high": 35,
}


def load_avds():
    data = yaml.safe_load(CATALOG.read_text(encoding="utf-8"))
    models = data.get("models", [])
    avds = []

    for model in models:
        brand = str(model["brand"]).strip()
        tier = str(model["tier"]).strip().lower()

        if tier not in API_BY_TIER:
            raise ValueError(f"Unknown tier {tier!r} in catalogue")

        avd_name = f"{brand}_{tier}_{API_BY_TIER[tier]}"
        avds.append(avd_name)

    if len(avds) != 24:
        raise ValueError(
            f"Expected 24 catalogue entries, found {len(avds)}. "
            "No emulators were started."
        )

    if len(set(avds)) != len(avds):
        raise ValueError(
            "Duplicate AVD names generated from catalogue. "
            "No emulators were started."
        )

    return avds


def main():
    parser = argparse.ArgumentParser(
        description="Run VMF AVD lifecycle smoke tests sequentially"
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="List the planned AVD names without starting emulators",
    )
    args = parser.parse_args()

    avds = load_avds()

    print(f"Catalogue: {CATALOG}")
    print(f"AVDs found: {len(avds)}")

    for index, name in enumerate(avds, start=1):
        print(f"{index:02d}. {name}")

    if args.list_only:
        print("\nList-only mode: no emulators were started.")
        return 0

    print("\nStarting sequential lifecycle tests.")
    print("Only one emulator will be tested at a time.")
    print(f"Results will be written to: {REPORT}")

    results = []

    for index, avd_name in enumerate(avds, start=1):
        started_at = datetime.now().astimezone().isoformat(
            timespec="seconds"
        )
        start_time = time.monotonic()
        process = None
        serial = None
        boot_result = "FAIL"
        shutdown_result = "NOT_RUN"
        error = ""

        print(f"\n{'=' * 60}")
        print(f"[{index}/{len(avds)}] Testing {avd_name}")
        print("=" * 60)

        try:
            process, serial = start_with_retry(avd_name, 5554)
            boot_result = "PASS"
            print(f"Ready: {serial}")

        except Exception as exc:
            error = f"Boot: {type(exc).__name__}: {exc}"
            print(f"FAILED to boot {avd_name}: {error}")

        finally:
            if process is not None:
                if serial is not None:
                    try:
                        stop_device(serial, process, avd_name)
                        shutdown_result = "PASS"
                    except Exception as exc:
                        shutdown_result = "FAIL"
                        message = (
                            f"Shutdown: {type(exc).__name__}: {exc}"
                        )
                        error = f"{error}; {message}" if error else message
                        print(message)
                        if process.poll() is None:
                            process.kill()
                            process.wait()
                else:
                    try:
                        if process.poll() is None:
                            process.kill()
                            process.wait()
                    except Exception as exc:
                        shutdown_result = "FAIL"
                        message = f"Process cleanup: {exc}"
                        error = f"{error}; {message}" if error else message
                        print(message)

        duration = round(time.monotonic() - start_time, 1)
        overall = (
            "PASS"
            if boot_result == "PASS" and shutdown_result == "PASS"
            else "FAIL"
        )

        results.append({
            "avd_name": avd_name,
            "boot": boot_result,
            "shutdown": shutdown_result,
            "overall": overall,
            "duration_seconds": duration,
            "started_at": started_at,
            "error": error,
        })

        # Save progress after each AVD so results survive an interruption.
        with REPORT.open("w", newline="", encoding="utf-8-sig") as file:
            writer = csv.DictWriter(file, fieldnames=results[0].keys())
            writer.writeheader()
            writer.writerows(results)

        print(
            f"RESULT: {avd_name}: {overall} "
            f"(boot={boot_result}, shutdown={shutdown_result}, "
            f"{duration}s)"
        )

    passed = sum(row["overall"] == "PASS" for row in results)
    failed = len(results) - passed

    print(f"\n{'=' * 60}")
    print("BATCH LIFECYCLE TEST SUMMARY")
    print(f"Total:  {len(results)}")
    print(f"Passed: {passed}")
    print(f"Failed: {failed}")
    print(f"CSV report: {REPORT}")
    print("=" * 60)

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"Batch test could not start: {type(exc).__name__}: {exc}")
        sys.exit(2)
