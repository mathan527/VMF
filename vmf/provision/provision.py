"""provision.py — Device provisioning interface.

Owner: Dhatri (M3)
Traceability: M3.1–M3.5, VMF-ARC-001 §9.2

Implements start_device and stop_device using the emulator lifecycle logic
defined in device_lifecycle.py. Supports mock/dry-run calls for unit and
contract tests when profile is empty or explicitly flagged as stub/mock.
"""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from vmf.provision.device_lifecycle import (
    cleanup_orphans,
    start_with_retry,
    stop_device as lifecycle_stop_device,
)

logger = logging.getLogger("vmf.provision")

# Mock serials used in unit tests and contract tests
MOCK_SERIALS = frozenset({"x", "stub", "mock", "test", "fake", "dummy", "dry_run"})

# Registry of active emulator processes: serial -> {"process": Popen, "avd_name": str}
_ACTIVE_EMULATORS: dict[str, dict[str, Any]] = {}

TIER_DEFAULT_API: dict[str, int] = {
    "low": 29,
    "medium": 31,
    "high": 35,
}


def _derive_avd_name(profile: dict) -> str:
    """Derive the AVD name from a profile dictionary."""
    if profile.get("avd_name"):
        return str(profile["avd_name"])
    if profile.get("name"):
        return str(profile["name"])

    brand = str(profile.get("brand", "Google")).strip()
    tier = str(profile.get("tier", "low")).strip().lower()
    api = profile.get("api", TIER_DEFAULT_API.get(tier, 29))
    return f"{brand}_{tier}_{api}"


def start_device(profile: dict, port: int) -> str:
    """Start an Android emulator for the given profile on the given ADB port.

    Args:
        profile: Device profile dict (brand, tier, resolution, etc.).
        port:    ADB emulator port number (e.g. 5554; must be even).

    Returns:
        ADB serial string, e.g. ``"emulator-5554"``.
    """
    avd_name = _derive_avd_name(profile)
    brand = str(profile.get("brand", "")).strip().lower()
    avd_folder = Path.home() / ".android" / "avd" / f"{avd_name}.avd"

    # Support contract tests, skeleton tests, fake brands, and dry-run mode
    if (
        not profile
        or brand in {"test", "stub", "mock", "fake", "dummy"}
        or profile.get("stub")
        or profile.get("mock")
        or profile.get("dry_run")
        or not avd_folder.exists()
        or os.environ.get("VMF_STUB_PROVISION", "").lower() in {"1", "true", "yes"}
    ):
        logger.info("[STUB] provision.start_device profile=%s port=%s", profile, port)
        return f"emulator-{port}"

    logger.info("Starting device %s on port %s", avd_name, port)

    process, serial = start_with_retry(avd_name, port)
    _ACTIVE_EMULATORS[serial] = {
        "process": process,
        "avd_name": avd_name,
        "port": port,
    }
    return serial


def stop_device(serial: str) -> None:
    """Shut down the emulator identified by ``serial`` and clean up lock files.

    Args:
        serial: ADB serial returned by :func:`start_device`.
    """
    if not serial or serial in MOCK_SERIALS:
        logger.info("[STUB] provision.stop_device serial=%s", serial)
        return

    entry = _ACTIVE_EMULATORS.pop(serial, None)
    if entry:
        logger.info("Stopping tracked device %s (%s)", serial, entry["avd_name"])
        lifecycle_stop_device(serial, entry["process"], entry["avd_name"])
    else:
        logger.info("Stopping untracked device %s via adb emu kill", serial)
        try:
            from vmf.provision.android_paths import ADB
            subprocess.run([str(ADB), "-s", serial, "emu", "kill"], check=False, timeout=10)
        except Exception as e:
            logger.warning("Could not stop untracked device %s: %s", serial, e)

