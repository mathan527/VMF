
import os
from pathlib import Path


def find_android_sdk():
    sdk_path = (
        os.environ.get("ANDROID_HOME")
        or os.environ.get("ANDROID_SDK_ROOT")
    )

    if not sdk_path:
        raise RuntimeError(
            "Android SDK not configured. Set ANDROID_HOME "
            "or ANDROID_SDK_ROOT to your Android SDK folder."
        )

    sdk = Path(sdk_path).expanduser()

    if not sdk.is_dir():
        raise FileNotFoundError(
            f"Android SDK directory not found: {sdk}"
        )

    return sdk


ANDROID_SDK_ROOT = find_android_sdk()

AVDMANAGER = (
    ANDROID_SDK_ROOT
    / "cmdline-tools"
    / "latest"
    / "bin"
    / "avdmanager.bat"
)

EMULATOR = ANDROID_SDK_ROOT / "emulator" / "emulator.exe"
ADB = ANDROID_SDK_ROOT / "platform-tools" / "adb.exe"

AVD_ROOT = Path.home() / ".android" / "avd"


def validate_android_tools():
    for tool in (AVDMANAGER, EMULATOR, ADB):
        if not tool.is_file():
            raise FileNotFoundError(
                f"Required Android tool not found: {tool}"
            )
