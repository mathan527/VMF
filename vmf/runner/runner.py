"""runner.py — M4 Scenario Runner.

Owner: Mathan (M4)

Drives one already-booted Android emulator through a scenario YAML via
Appium 2 / UiAutomator2.  Evidence (screenshot + hierarchy XML) is
captured after every step.

The ``run()`` function requires a live Appium server and a running emulator.
The ``run_scenario()`` function is the ARCH entry point: in dry-run mode
(``dry_run=True``) it returns a single deterministic ``Step`` without
connecting to Appium or requiring any device to be running.
"""
import logging
import os
import time

import yaml
from appium import webdriver
from appium.options.android import UiAutomator2Options
from appium.webdriver.common.appiumby import AppiumBy
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from vmf import oem_packs, storage
from vmf.results import Step

APPIUM_URL = "http://localhost:4723"
DEFAULT_TIMEOUT_S = 10
KEYCODE_HOME = 3


# ---------------------------------------------------------------------------
# Internal helpers (M4 implementation — do not modify for ARCH)
# ---------------------------------------------------------------------------

def _by(locator: dict):
    """Translate a scenario locator dict into an Appium (by, value) tuple."""
    if "resource_id" in locator:
        return AppiumBy.ID, locator["resource_id"]
    if "text" in locator:
        return AppiumBy.XPATH, f'//*[@text="{locator["text"]}"]'
    if "content-desc" in locator:
        return AppiumBy.ACCESSIBILITY_ID, locator["content-desc"]
    raise ValueError(f"unsupported locator (need resource_id, text or content-desc): {locator}")


def _wait_visible(driver, locator: dict, timeout_s):
    return WebDriverWait(driver, timeout_s or DEFAULT_TIMEOUT_S).until(
        EC.visibility_of_element_located(_by(locator))
    )


def _swipe(driver, direction: str) -> None:
    size = driver.get_window_size()
    w, h = size["width"], size["height"]
    cx, cy = w // 2, h // 2
    coords = {
        "up":    (cx, int(h * 0.7), cx, int(h * 0.3)),
        "down":  (cx, int(h * 0.3), cx, int(h * 0.7)),
        "left":  (int(w * 0.8), cy, int(w * 0.2), cy),
        "right": (int(w * 0.2), cy, int(w * 0.8), cy),
    }
    if direction not in coords:
        raise ValueError(f"unsupported swipe direction: {direction!r}")
    driver.swipe(*coords[direction], 500)


def _execute(driver, serial: str, package: str, spec: dict) -> str:
    """Run one step; return its status ("pass" | "fail" | "skipped")."""
    action = spec["action"]
    timeout_s = spec.get("timeout_s")

    if action == "tap":
        _wait_visible(driver, spec["locator"], timeout_s).click()
    elif action == "type":
        _wait_visible(driver, spec["locator"], timeout_s).send_keys(spec["text"])
    elif action == "swipe":
        _swipe(driver, spec["direction"])
    elif action in ("wait_for", "assert_visible"):
        _wait_visible(driver, spec["locator"], timeout_s)
    elif action == "back":
        driver.back()
    elif action == "home":
        driver.press_keycode(KEYCODE_HOME)
    elif action.startswith("oem."):
        behaviour = action[len("oem."):]
        fn = getattr(oem_packs, behaviour, None)
        if not callable(fn):
            logging.warning("oem_packs has no %r; skipping step", behaviour)
            return "skipped"
        result = fn(serial, package)
        if isinstance(result, dict) and result.get("ok") is False:
            return "fail"
    else:
        raise ValueError(f"unknown action: {action!r}")
    return "pass"


def _capture_evidence(driver, out_dir: str, step_index: int) -> tuple[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    screenshot = f"{out_dir}/step_{step_index:02d}_screen.png"
    hierarchy  = f"{out_dir}/step_{step_index:02d}_hierarchy.xml"
    try:
        driver.get_screenshot_as_file(screenshot)
        with open(hierarchy, "w", encoding="utf-8") as f:
            f.write(driver.page_source)
    except Exception:
        logging.exception("evidence capture failed at step %d", step_index)
    return screenshot, hierarchy


# ---------------------------------------------------------------------------
# M4 core — requires live Appium + running emulator
# ---------------------------------------------------------------------------

def run(serial: str, apk: str, scenario: str, system_port: int) -> tuple[int, list[Step]]:
    """Run ``scenario`` on the already-booted device ``serial``; return (run_id, Steps).

    Does not boot the device (M3 responsibility) and does not judge overall
    pass/fail (M6 responsibility).

    Args:
        serial:      ADB device serial (e.g. ``"emulator-5554"``).
        apk:         Path to the APK to install and test.
        scenario:    Path to the scenario YAML file.
        system_port: UiAutomator2 systemPort (unique per device).

    Returns:
        Tuple of (run_id, list[Step]).
    """
    options = UiAutomator2Options()
    options.platform_name = "Android"
    options.udid = serial
    options.system_port = system_port
    options.app = apk
    options.automation_name = "UiAutomator2"
    options.no_reset = False

    try:
        driver = webdriver.Remote(APPIUM_URL, options=options)
    except Exception as e:
        raise RuntimeError(
            f"could not connect to Appium at {APPIUM_URL} for serial={serial} "
            f"systemPort={system_port}: {e}"
        ) from e

    steps: list[Step] = []
    try:
        with open(scenario, encoding="utf-8") as f:
            spec = yaml.safe_load(f)
        package = spec.get("package")

        run_id = storage.create_run(
            apk_name=os.path.basename(apk), scenario=spec["name"], profiles=[serial]
        )
        out_dir = f"results/{run_id}/{serial}"

        for step_index, step_spec in enumerate(spec["steps"]):
            start = time.monotonic()
            try:
                status = _execute(driver, serial, package, step_spec)
            except Exception:
                logging.exception("step %d (%s) failed", step_index, step_spec.get("action"))
                status = "fail"
            duration_ms = int((time.monotonic() - start) * 1000)

            screenshot, hierarchy = _capture_evidence(driver, out_dir, step_index)
            step = Step(
                step_index=step_index,
                action=step_spec["action"],
                status=status,
                duration_ms=duration_ms,
                screenshot_path=screenshot,
                hierarchy_path=hierarchy,
            )
            storage.save_step(run_id, serial, step)
            steps.append(step)
    finally:
        driver.quit()
    return run_id, steps


# ---------------------------------------------------------------------------
# ARCH entry point — safe for orchestrator / dry-run use
# ---------------------------------------------------------------------------

def run_scenario(serial: str, apk: str, scenario: str,
                 system_port: int = 8200,
                 dry_run: bool = False) -> tuple[int, list[Step]]:
    """ARCH entry point for the scenario runner.

    When ``dry_run=True`` this function does NOT connect to Appium or require
    any device to be running.  It returns a single deterministic stub Step so
    the orchestrator pipeline can exercise the full call chain.

    When ``dry_run=False`` (production / M4 path) it delegates to :func:`run`.

    Args:
        serial:      ADB device serial.
        apk:         Path to APK under test.
        scenario:    Path to scenario YAML.
        system_port: UiAutomator2 systemPort (default 8200).
        dry_run:     If ``True``, return a stub result without Appium.

    Returns:
        ``(run_id, steps)`` — same contract as :func:`run`.
    """
    if dry_run:
        logging.info("[STUB] runner.run_scenario serial=%s scenario=%s (dry_run)", serial, scenario)
        run_id = storage.create_run(
            apk_name=os.path.basename(apk) if apk else "stub.apk",
            scenario=os.path.basename(scenario) if scenario else "stub_scenario",
            profiles=[serial],
        )
        stub_step = Step(
            step_index=0,
            action="launch",
            status="pass",
            duration_ms=0,
            screenshot_path="",
            hierarchy_path="",
        )
        storage.save_step(run_id, serial, stub_step)
        return run_id, [stub_step]

    logging.info("runner.run_scenario serial=%s scenario=%s", serial, scenario)
    return run(serial, apk, scenario, system_port)
