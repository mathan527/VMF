#M2.3


import hashlib
import subprocess
import sys
from pathlib import Path


AVD_ROOT = Path.home() / ".android" / "avd"
PATCH_SCRIPT = Path(__file__).resolve().parent / "patch_avds.py"


def get_config_files():
    return sorted(AVD_ROOT.glob("*.avd/config.ini"))


def get_hash(path):
    sha256 = hashlib.sha256()

    with open(path, "rb") as file:
        while chunk := file.read(8192):
            sha256.update(chunk)

    return sha256.hexdigest()


def capture_hashes():
    files = get_config_files()

    if len(files) != 24:
        print(f"FAIL: Expected 24 config.ini files, found {len(files)}")
        sys.exit(1)

    return {str(path): get_hash(path) for path in files}


def run_patch():
    print("\nRunning patch_avds.py...\n")

    result = subprocess.run(
        [sys.executable, str(PATCH_SCRIPT)],
        text=True,
    )

    if result.returncode != 0:
        print("FAIL: patch_avds.py failed.")
        sys.exit(1)


def main():
    print("========================================")
    print("VMF M2.3 - Idempotence Check")
    print("========================================")

    print("\nCapturing hashes BEFORE second run...")
    before = capture_hashes()

    run_patch()

    print("\nCapturing hashes AFTER second run...")
    after = capture_hashes()

    changed = []

    for path in before:
        if before[path] != after[path]:
            changed.append(path)

    print("\n========================================")
    print("M2.3 SUMMARY")
    print("========================================")
    print(f"Config files checked : {len(before)}")
    print(f"Files changed        : {len(changed)}")

    if not changed:
        print("\nPASS: Second run changed nothing.")
        print("PASS: AVD patching is idempotent.")
    else:
        print("\nFAIL: Some config.ini files changed.")

        for path in changed:
            print(f"CHANGED: {path}")

        sys.exit(1)


if __name__ == "__main__":
    main()