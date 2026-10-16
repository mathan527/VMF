#M3.6

import csv
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    from vmf.provision.device_lifecycle import start_with_retry, stop_device
    from vmf.provision.android_paths import ADB
except ImportError:
    from device_lifecycle import start_with_retry, stop_device
    from android_paths import ADB

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "soak_test_50cycle_results.csv"

PORT = 5554
CYCLES = 50

AVDS = [
    "Google_high_35", "Google_low_29", "Google_medium_31",
    "Honor_high_35", "Honor_low_29", "Honor_medium_31",
    "Huawei_high_35", "Huawei_low_29", "Huawei_medium_31",
    "Motorola_high_35", "Motorola_low_29", "Motorola_medium_31",
    "OnePlus_high_35", "OnePlus_low_29", "OnePlus_medium_31",
    "Samsung_high_35", "Samsung_low_29", "Samsung_medium_31",
    "Vivo_high_35", "Vivo_low_29", "Vivo_medium_31",
    "Xiaomi_high_35", "Xiaomi_low_29", "Xiaomi_medium_31",
]

# Two extra cycles, after each AVD has been tested twice.
SCHEDULE = AVDS + AVDS + ["Samsung_low_29", "Samsung_high_35"]

FIELDS = [
    "cycle", "round", "avd_name", "port", "boot", "shutdown",
    "orphan_check", "result", "duration_seconds", "timestamp", "error",
]


def emulator_processes():
    result = subprocess.run(
        ["tasklist", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, timeout=15, check=True,
    )
    found = []
    for line in result.stdout.splitlines():
        fields = next(csv.reader([line]), [])
        if len(fields) >= 2 and fields[0].strip().lower() in {
            "emulator.exe", "qemu-system-x86_64.exe"
        }:
            found.append(fields[1].strip())
    return found


def adb_devices():
    result = subprocess.run(
        [str(ADB), "devices"],
        capture_output=True, text=True, timeout=15, check=True,
    )
    return [
        line.strip()
        for line in result.stdout.splitlines()[1:]
        if line.strip() and "\tdevice" in line
    ]


def check_orphans_after_settle():
    deadline = time.monotonic() + 15
    while True:
        processes = emulator_processes()
        devices = adb_devices()
        if not processes and not devices:
            return [], []
        if time.monotonic() >= deadline:
            return processes, devices
        time.sleep(3)


def save_results(results):
    with REPORT.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(results)


def main():
    if not ADB.is_file():
        print(f"ERROR: ADB not found: {ADB}")
        return 2

    if len(AVDS) != 24 or len(SCHEDULE) != CYCLES:
        print("ERROR: The test schedule does not contain 50 cycles.")
        return 2

    if emulator_processes() or adb_devices():
        print("ERROR: Emulator process/device detected before test.")
        print("Inspect and stop it before running the soak test.")
        return 2

    results = []
    interrupted = False
    print("M3.6: 50-cycle sequential soak test")
    print("Cycles 1-24: first pass; 25-48: second pass.")
    print("Cycles 49-50: Samsung Android 29 and Android 35.")
    print(f"Report: {REPORT}")

    try:
        for cycle, avd in enumerate(SCHEDULE, start=1):
            started = time.monotonic()
            process = None
            serial = f"emulator-{PORT}"
            boot = "FAIL"
            shutdown = "NOT_RUN"
            orphan_check = "NOT_CHECKED"
            error = ""
            round_name = (
                "Round 1" if cycle <= 24 else
                "Round 2" if cycle <= 48 else "Extra"
            )

            print(f"\n[{cycle}/50] {round_name}: starting {avd}")

            try:
                process, serial = start_with_retry(avd, PORT)
                if process is None:
                    raise RuntimeError("No process handle returned.")
                boot = "PASS"
                print(f"Boot PASS: {serial}")
            except Exception as exc:
                error = f"boot: {type(exc).__name__}: {exc}"
                print(f"Boot FAIL: {error}")

            if process is not None:
                try:
                    stop_device(serial, process, avd)
                    shutdown = "PASS"
                except Exception as exc:
                    shutdown = "FAIL"
                    msg = f"shutdown: {type(exc).__name__}: {exc}"
                    error = f"{error}; {msg}".strip("; ")
                    print(f"Shutdown FAIL: {msg}")

            # On failed startup, do not guess which process to kill.
            # Check state and stop the run if cleanup is uncertain.
            try:
                processes, devices = check_orphans_after_settle()
                orphan_check = (
                    "PASS" if not processes and not devices else "FAIL"
                )
            except Exception as exc:
                processes, devices = ["CHECK_ERROR"], ["CHECK_ERROR"]
                orphan_check = "UNKNOWN"
                error = f"{error}; orphan check: {exc}".strip("; ")

            passed = (
                boot == "PASS"
                and shutdown == "PASS"
                and orphan_check == "PASS"
            )

            results.append({
                "cycle": cycle,
                "round": round_name,
                "avd_name": avd,
                "port": PORT,
                "boot": boot,
                "shutdown": shutdown,
                "orphan_check": orphan_check,
                "result": "PASS" if passed else "FAIL",
                "duration_seconds": round(time.monotonic() - started, 1),
                "timestamp": datetime.now().astimezone().isoformat(
                    timespec="seconds"
                ),
                "error": error,
            })
            save_results(results)

            print(
                f"[{cycle}/50] "
                f"{'PASS' if passed else 'FAIL'}; "
                f"shutdown={shutdown}; orphan_check={orphan_check}"
            )

            if not passed:
                print("Stopping early; inspect the process state and CSV.")
                if processes:
                    print(f"Remaining emulator process IDs: {processes}")
                if devices:
                    print(f"Remaining ADB devices: {devices}")
                break

    except KeyboardInterrupt:
        interrupted = True
        print("\nInterrupted by user. Saved completed cycles remain in CSV.")

    try:
        processes = emulator_processes()
        devices = adb_devices()
        final_check = "PASS" if not processes and not devices else "FAIL"
    except Exception as exc:
        processes, devices = ["CHECK_ERROR"], ["CHECK_ERROR"]
        final_check = "UNKNOWN"
        print(f"Final check error: {exc}")

    completed = len(results)
    passed_count = sum(r["result"] == "PASS" for r in results)

    print("\n" + "=" * 50)
    print("M3.6 SOAK TEST SUMMARY")
    print(f"Completed: {completed}/50")
    print(f"Passed: {passed_count}")
    print(f"Failed: {completed - passed_count}")
    print(f"Final orphan check: {final_check}")
    print(f"Remaining process IDs: {processes}")
    print(f"Remaining ADB devices: {devices}")
    print(f"CSV report: {REPORT}")

    if (
        not interrupted
        and completed == 50
        and passed_count == 50
        and final_check == "PASS"
    ):
        print("M3.6 RESULT: PASS")
        return 0

    print("M3.6 RESULT: NOT PASSED - investigate the report.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
