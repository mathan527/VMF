#M3.1,.2,.3,.4,.5




import subprocess
import time
from pathlib import Path

from android_paths import EMULATOR, ADB

BOOT_TIMEOUT = 180

def start_device(avd_name, port):
    command = [
        str(EMULATOR),
        "-avd",
        avd_name,
        "-port",
        str(port),
        "-no-window",
        "-no-snapshot-save",
        "-accel",
        "on",
        "-gpu",
        "swiftshader_indirect",
    ]

    process = subprocess.Popen(command)

    print(f"Started {avd_name} on port {port}")
    print(f"Process ID: {process.pid}")

    return process


def wait_for_boot(port):
    serial = f"emulator-{port}"

    subprocess.run(
        [str(ADB), "-s", serial, "wait-for-device"],
        check=True,
    )

    start_time = time.time()

    while time.time() - start_time < BOOT_TIMEOUT:
        result = subprocess.run(
            [str(ADB), "-s", serial, "shell", "getprop", "sys.boot_completed"],
            capture_output=True,
            text=True,
        )

        if result.stdout.strip() == "1":
            print(f"{serial} boot completed.")
            return serial

        print(f"Waiting for {serial} to boot...")
        time.sleep(2)

    return None

def start_with_retry(avd_name, port):
    for attempt in range(2):
        process = start_device(avd_name, port)

        serial = wait_for_boot(port)

        if serial:
            return process, serial

        print(f"Boot timeout for {avd_name}.")

        process.kill()
        process.wait()

        if attempt == 0:
            print("Retrying once...")
        else:
            raise RuntimeError(
                f"Emulator {avd_name} failed to boot within 3 minutes."
            )

def stop_device(serial, process, avd_name):
    subprocess.run(
        [str(ADB), "-s", serial, "emu", "kill"],
        check=False
    )

    time.sleep(10)

    if process.poll() is None:
        process.kill()
        process.wait()

    avd_folder = Path.home() / ".android" / "avd" / f"{avd_name}.avd"

    for lock_file in avd_folder.glob("*.lock"):
        lock_file.unlink()

    print(f"Stopped {serial} and cleaned up.")

def cleanup_orphans():
    import psutil

    found = False

    for process in psutil.process_iter(["pid", "name"]):
        try:
            name = (process.info["name"] or "").lower()

            if name in {"emulator.exe", "qemu-system-x86_64.exe"}:
                print(f"Found orphan process: {name} (PID {process.info['pid']})")
                process.kill()
                found = True

        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if found:
        print("Orphan emulator processes cleaned up.")
    else:
        print("No orphan emulator processes found.")




if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="VMF AVD lifecycle smoke test")
    parser.add_argument("avd_name", help="Name of the AVD to test")
    parser.add_argument(
        "--port",
        type=int,
        default=5554,
        help="Emulator console port (default: 5554; use an even port)",
    )
    args = parser.parse_args()

    if args.port < 5554 or args.port > 5682 or args.port % 2 != 0:
        parser.error("Port must be an even number between 5554 and 5682.")

    process, serial = start_with_retry(args.avd_name, args.port)
    print(f"Ready: {serial}")

    stop_device(serial, process, args.avd_name)
