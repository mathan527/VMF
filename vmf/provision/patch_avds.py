#M2.2

import re
import sys
from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parent
CATALOG_PATH = PROJECT_ROOT / "reference-models.yaml"
AVD_ROOT = Path.home() / ".android" / "avd"


TIER_CPU = {
    "low": 2,
    "medium": 4,
    "high": 6,
}


TIER_RAM = {
    "low": 4096,
    "medium": 6144,
    "high": 8192,
}


def load_catalog():
    with open(CATALOG_PATH, "r", encoding="utf-8") as file:
        data = yaml.safe_load(file)

    return data["models"]


def parse_resolution(resolution):
    match = re.match(r"^\s*(\d+)\s*x\s*(\d+)\s*$", resolution)

    if not match:
        raise ValueError(f"Invalid resolution: {resolution}")

    width = int(match.group(1))
    height = int(match.group(2))

    return width, height


def update_config(config_path, row):
    lines = config_path.read_text(encoding="utf-8").splitlines()

    width, height = parse_resolution(row["resolution"])
    density = row["density_dpi"]
    ram = row["ram_gb"] * 1024
    cpu = TIER_CPU[row["tier"]]

    updates = {
        "hw.ramSize": f"{ram}M",
        "hw.cpu.ncore": str(cpu),
        "hw.lcd.width": str(width),
        "hw.lcd.height": str(height),
        "hw.lcd.density": str(density),
        "hw.gpu.mode": "swiftshader_indirect",
    }

    updated_keys = set()
    new_lines = []

    for line in lines:
        if "=" not in line:
            new_lines.append(line)
            continue

        key = line.split("=", 1)[0]

        if key in updates:
            new_lines.append(f"{key}={updates[key]}")
            updated_keys.add(key)
        else:
            new_lines.append(line)

    for key, value in updates.items():
        if key not in updated_keys:
            new_lines.append(f"{key}={value}")

    config_path.write_text(
        "\n".join(new_lines) + "\n",
        encoding="utf-8",
    )


def patch_avd(row):
    brand = row["brand"]
    tier = row["tier"]
    api = {"low": 29, "medium": 31, "high": 35}[tier]

    avd_name = f"{brand}_{tier}_{api}"
    config_path = AVD_ROOT / f"{avd_name}.avd" / "config.ini"

    if not config_path.exists():
        print(f"FAIL: config.ini not found for {avd_name}")
        return False

    update_config(config_path, row)

    print(f"PASS: Patched {avd_name}")
    return True


def main():
    print("========================================")
    print("VMF M2.2 - AVD Configuration Patching")
    print("========================================")

    rows = load_catalog()

    if len(rows) != 24:
        print(f"FAIL: Expected 24 catalogue rows, found {len(rows)}")
        sys.exit(1)

    success = 0
    failed = 0

    for row in rows:
        if patch_avd(row):
            success += 1
        else:
            failed += 1

    print("\n========================================")
    print("M2.2 SUMMARY")
    print("========================================")
    print(f"Expected AVDs : 24")
    print(f"Patched       : {success}")
    print(f"Failed        : {failed}")

    if failed == 0:
        print("\nPASS: All 24 AVDs were patched.")
    else:
        print("\nFAIL: Some AVDs were not patched.")
        sys.exit(1)


if __name__ == "__main__":
    main()
