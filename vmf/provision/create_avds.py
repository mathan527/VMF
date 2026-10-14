#M2.1

import re
import subprocess
import sys
from pathlib import Path

import yaml

from android_paths import (
    ANDROID_SDK_ROOT,
    AVDMANAGER,
    AVD_ROOT,
    validate_android_tools,
)

PROJECT_ROOT = Path(__file__).resolve().parent
CATALOG_PATH = PROJECT_ROOT.parent / "config" / "reference-models.yaml"



TIER_API = {
    "low": 29,
    "medium": 31,
    "high": 35,
}

TIER_SYSTEM_IMAGE = {
    "low": "google_apis",
    "medium": "google_apis",
    "high": "google_apis",
}

HUAWEI_DEFAULT_IMAGE = "default"


def load_catalog():
    if not CATALOG_PATH.exists():
        print(f"FAIL: Catalogue not found: {CATALOG_PATH}")
        sys.exit(1)

    with open(CATALOG_PATH, "r", encoding="utf-8") as file:
        data = yaml.safe_load(file)

    if not data or "models" not in data:
        print("FAIL: reference-models.yaml has no 'models' list.")
        sys.exit(1)

    return data["models"]


def get_system_image(brand, tier):
    image_type = (
        HUAWEI_DEFAULT_IMAGE
        if brand == "Huawei"
        else TIER_SYSTEM_IMAGE[tier]
    )

    api = TIER_API[tier]

    return f"system-images;android-{api};{image_type};x86_64"


def run_command(command):
    print("\n>", " ".join(f'"{x}"' if " " in x else x for x in command))

    result = subprocess.run(
        command,
        text=True,
        input="no\n",
        capture_output=True,
    )

    if result.stdout:
        print(result.stdout)

    if result.stderr:
        print(result.stderr)

    return result.returncode


def create_avd(row):
    brand = row["brand"]
    tier = row["tier"]

    api = TIER_API[tier]
    avd_name = f"{brand}_{tier}_{api}"
    system_image = get_system_image(brand, tier)

    command = [
        str(AVDMANAGER),
        "create",
        "avd",
        "--name",
        avd_name,
        "--package",
        system_image,
        "--force",
    ]

    print(f"\nCreating AVD: {avd_name}")
    print(f"Model: {row['model']}")
    print(f"System image: {system_image}")

    return_code = run_command(command)

    if return_code != 0:
        print(f"FAIL: Could not create {avd_name}")
        return False

    print(f"PASS: Created {avd_name}")
    return True



def parse_config(config_path):
    config = {}

    for line in config_path.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            config[key.strip()] = value.strip()

    return config



def parse_resolution(resolution):
    match = re.match(r"^\s*(\d+)\s*x\s*(\d+)\s*$", resolution)

    if not match:
        raise ValueError(f"Invalid resolution: {resolution}")

    return int(match.group(1)), int(match.group(2))


def verify_avd(row):
    brand = row["brand"]
    tier = row["tier"]

    api = TIER_API[tier]
    avd_name = f"{brand}_{tier}_{api}"

    config_path = AVD_ROOT / f"{avd_name}.avd" / "config.ini"

    if not config_path.exists():
        return [
            f"{avd_name}: config.ini not found"
        ]

    config = parse_config(config_path)

    width, height = parse_resolution(row["resolution"])

    expected = {
        "hw.ramSize": f"{row['ram_gb'] * 1024}M",
        "hw.cpu.ncore": str({
            "low": 2,
            "medium": 4,
            "high": 6,
        }[tier]),
        "hw.lcd.width": str(width),
        "hw.lcd.height": str(height),
        "hw.lcd.density": str(row["density_dpi"]),
        "hw.gpu.mode": "swiftshader_indirect",
    }

    mismatches = []

    for key, expected_value in expected.items():
        actual_value = config.get(key)

        if actual_value != expected_value:
            mismatches.append(
                f"{avd_name}: {key} "
                f"expected={expected_value} "
                f"actual={actual_value}"
            )

    return mismatches


def verify_all(rows):
    print("========================================")
    print("VMF M2.4 - AVD Configuration Verification")
    print("========================================")

    all_mismatches = []

    for row in rows:
        mismatches = verify_avd(row)

        if mismatches:
            for mismatch in mismatches:
                print(f"FAIL: {mismatch}")

            all_mismatches.extend(mismatches)
        else:
            brand = row["brand"]
            tier = row["tier"]
            api = TIER_API[tier]
            avd_name = f"{brand}_{tier}_{api}"

            print(f"PASS: Verified {avd_name}")

    print("\n========================================")
    print("M2.4 SUMMARY")
    print("========================================")
    print(f"AVDs checked : {len(rows)}")
    print(f"Mismatches   : {len(all_mismatches)}")

    if not all_mismatches:
        print("\nPASS: All 24 AVD configurations match the catalogue.")
        return True

    print("\nFAIL: AVD configuration mismatches found.")
    return False


def main():
    rows = load_catalog()

    if len(rows) != 24:
        print(f"FAIL: Expected exactly 24 catalogue rows, found {len(rows)}")
        sys.exit(1)

    if "--verify" in sys.argv:
        success = verify_all(rows)

        if not success:
            sys.exit(1)

        return

    print("========================================")
    print("VMF M2.1 - AVD Provisioning")
    print("========================================")

    if not AVDMANAGER.exists():
        print("FAIL: avdmanager not found:")
        print(AVDMANAGER)
        sys.exit(1)

    print(f"\nCatalogue rows found: {len(rows)}")

    success = 0
    failed = 0

    for row in rows:
        if create_avd(row):
            success += 1
        else:
            failed += 1

    print("\n========================================")
    print("M2.1 SUMMARY")
    print("========================================")
    print("Expected AVDs : 24")
    print(f"Created       : {success}")
    print(f"Failed        : {failed}")

    if failed == 0:
        print("\nPASS: All 24 AVDs were created.")
    else:
        print("\nFAIL: Some AVDs were not created.")
        sys.exit(1)


if __name__ == "__main__":
    validate_android_tools()
    main()