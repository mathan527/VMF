"""oem_packs.py — OEM behaviour pack interface.

Owner: Dhatri (M5)
ARCH stubs provided by Mathan.

Real ADB-backed OEM behaviour implementations are due in M5.
No ADB commands are executed here.
"""
import logging


def apply_profile_settings(serial: str, profile: dict) -> dict:
    """STUB — real implementation: Dhatri, M5.

    Push display/font/dark-mode settings to the emulator matching the
    given device profile.  Returns an echo of the applied settings.

    Args:
        serial:  ADB device serial.
        profile: Device profile dict (cutout, density, font_scale, dark_mode).

    Returns:
        Dict of the applied profile keys (echo for verification).
    """
    logging.info("[STUB] oem_packs.apply_profile_settings serial=%s profile=%s", serial, profile)
    return {k: profile.get(k) for k in ("cutout", "density", "font_scale", "dark_mode")}


def reset_profile(serial: str, pkg: str) -> None:
    """STUB — real implementation: Dhatri, M5.

    Reset all OEM profile settings applied to ``pkg`` on ``serial``.

    Args:
        serial: ADB device serial.
        pkg:    App package name (e.g. ``"com.example.app"``).
    """
    logging.info("[STUB] oem_packs.reset_profile serial=%s pkg=%s", serial, pkg)


def background_kill(serial: str, pkg: str) -> dict:
    """STUB — background kill OEM behaviour: M5.

    Args:
        serial: ADB device serial.
        pkg:    App package name.

    Returns:
        ``{"ok": True, "verify": "stubbed"}``
    """
    logging.info("[STUB] oem_packs.background_kill serial=%s pkg=%s", serial, pkg)
    return {"ok": True, "verify": "stubbed"}


def restrict_background(serial: str, pkg: str) -> dict:
    """STUB — restrict background data OEM behaviour: M5."""
    logging.info("[STUB] oem_packs.restrict_background serial=%s pkg=%s", serial, pkg)
    return {"ok": True, "verify": "stubbed"}


def doze(serial: str, pkg: str) -> dict:
    """STUB — force doze mode OEM behaviour: M5."""
    logging.info("[STUB] oem_packs.doze serial=%s pkg=%s", serial, pkg)
    return {"ok": True, "verify": "stubbed"}


def always_finish_activities(serial: str, pkg: str) -> dict:
    """STUB — always-finish-activities OEM developer option: M5."""
    logging.info("[STUB] oem_packs.always_finish_activities serial=%s pkg=%s", serial, pkg)
    return {"ok": True, "verify": "stubbed"}


def trim_memory(serial: str, pkg: str) -> dict:
    """STUB — TRIM_MEMORY signal OEM behaviour: M5."""
    logging.info("[STUB] oem_packs.trim_memory serial=%s pkg=%s", serial, pkg)
    return {"ok": True, "verify": "stubbed"}


def permission_revoke(serial: str, pkg: str) -> dict:
    """STUB — revoke dangerous permissions OEM behaviour: M5."""
    logging.info("[STUB] oem_packs.permission_revoke serial=%s pkg=%s", serial, pkg)
    return {"ok": True, "verify": "stubbed"}
