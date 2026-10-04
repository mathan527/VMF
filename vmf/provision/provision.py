"""provision.py — Device provisioning interface.

Owner: Dhatri (M3)
ARCH stubs provided by Mathan.

Real implementation (emulator.exe + WHPX lifecycle) is due in M3.
"""
import logging


def start_device(profile: dict, port: int) -> str:
    """STUB — real implementation: Dhatri, M3.

    Start an Android emulator for the given profile on the given ADB port.
    During ARCH this is a no-op stub that returns a deterministic fake serial.

    Args:
        profile: Device profile dict (cutout, density, font_scale, ...).
        port:    ADB emulator port number (e.g. 5554).

    Returns:
        ADB serial string, e.g. ``"emulator-5554"``.
    """
    logging.info("[STUB] provision.start_device profile=%s port=%s", profile, port)
    return f"emulator-{port}"


def stop_device(serial: str) -> None:
    """STUB — real implementation: Dhatri, M3.

    Shut down the emulator identified by ``serial``.
    During ARCH this is a no-op.

    Args:
        serial: ADB serial returned by :func:`start_device`.
    """
    logging.info("[STUB] provision.stop_device serial=%s", serial)
