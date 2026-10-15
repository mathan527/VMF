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
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from vmf import oem_packs, storage
from vmf.config import APPIUM_URL, DEFAULT_TIMEOUT_S, KEYCODE_HOME, RESULTS_DIR
from vmf.results import Step
from vmf.runner.locator_fallback import (FallbackConfig, FallbackSession, Resolution,
                                         validate_fallback_steps)


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
    if "xpath" in locator:  # only produced by the locator fallback (M14)
        return AppiumBy.XPATH, locator["xpath"]
    raise ValueError(f"unsupported locator (need resource_id, text or content-desc): {locator}")


def _wait_visible(driver, locator: dict, timeout_s):
    return WebDriverWait(driver, timeout_s or DEFAULT_TIMEOUT_S).until(
        EC.visibility_of_element_located(_by(locator))
    )


def _find_element(driver, spec: dict, timeout_s, fallback: FallbackSession | None,
                  step_index: int):
    """Find the element of an ACTION step. With ``fallback`` None this is exactly
    ``_wait_visible``. If the lookup times out and the step opted in, the locator
    fallback (M14) may pick the intended element from the current hierarchy; the
    action is then retried once with the new locator. Any failure re-raises the
    original error, so the step fails exactly as it would without the fallback."""
    try:
        return _wait_visible(driver, spec["locator"], timeout_s)
    except TimeoutException:
        if fallback is None or not fallback.eligible(spec):
            raise
        outcome = fallback.heal(driver, spec, step_index)
        if not isinstance(outcome, Resolution):
            raise
        try:
            element = _wait_visible(driver, outcome.locator, timeout_s)
        except Exception as retry_error:
            fallback.retry_failed(step_index, spec, outcome, retry_error)
            raise
        fallback.confirm_healed(step_index, spec, outcome)
        return element


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


def _execute(driver, serial: str, package: str, spec: dict,
             fallback: FallbackSession | None = None, step_index: int = 0) -> str:
    """Run one step; return its status ("pass" | "fail" | "skipped")."""
    action = spec["action"]
    timeout_s = spec.get("timeout_s")

    if action == "tap":
        _find_element(driver, spec, timeout_s, fallback, step_index).click()
    elif action == "type":
        _find_element(driver, spec, timeout_s, fallback, step_index).send_keys(spec["text"])
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

def run(serial: str, apk: str, scenario: str, system_port: int, *,
        run_id: int | None = None, profile: str | None = None,
        record: bool = True, reuse_installed_app: bool = False,
        mjpeg_port: int | None = None,
        chromedriver_port: int | None = None,
        locator_fallback: FallbackConfig | None = None) -> tuple[int | None, list[Step]]:
    """Run ``scenario`` on the already-booted device ``serial``; return (run_id, Steps).

    Does not boot the device (M3 responsibility) and does not judge overall
    pass/fail (M6 responsibility).

    Args:
        serial:      ADB device serial (e.g. ``"emulator-5554"``).
        apk:         Path to the APK to install and test.
        scenario:    Path to the scenario YAML file.
        system_port: UiAutomator2 systemPort (unique per device).
        run_id:      If given, use it instead of calling ``storage.create_run``
                     (the orchestrator creates one run for the whole matrix).
        profile:     Name used for the evidence folder
                     ``results/<run_id>/<profile>/`` and as the profile argument
                     of ``storage.save_step``. Defaults to ``serial``.
        record:      If False, nothing is stored: no ``create_run``, no
                     ``save_step`` and no evidence files. Used when the
                     scenario is replayed as a performance flow. The returned
                     run_id is then None.
        reuse_installed_app:
                     If True the APK is NOT installed again and app data is
                     kept: ``options.app`` is not set, ``noReset`` is True, and
                     ``appPackage`` (and ``appActivity`` if the scenario has an
                     ``activity`` key) come from the scenario. Used by the
                     performance flow, where the app is already installed in
                     the snapshot. Needs ``package`` in the scenario. Default
                     False: unchanged behaviour (install and reset).
        mjpeg_port, chromedriver_port:
                     Optional ``mjpegServerPort`` / ``chromedriverPort``
                     capabilities. Parallel sessions need a distinct value
                     each, as for ``system_port``. None leaves them unset.
        locator_fallback:
                     M14, OFF by default. An enabled FallbackConfig lets steps
                     marked ``fallback: true`` (with an ``intent``) find their
                     element through the local-model fallback when the scripted
                     locator fails. Ignored when ``record`` is False (a
                     performance number must not depend on a model guess). None
                     keeps the behaviour exactly as before.

    Returns:
        Tuple of (run_id, list[Step]).
    """
    with open(scenario, encoding="utf-8") as f:
        spec = yaml.safe_load(f)
    package = spec.get("package")
    fallback_on = locator_fallback is not None and locator_fallback.enabled
    if fallback_on:
        validate_fallback_steps(spec)  # a malformed step is rejected before a session opens

    options = UiAutomator2Options()
    options.platform_name = "Android"
    options.udid = serial
    options.system_port = system_port
    options.automation_name = "UiAutomator2"
    if reuse_installed_app:
        if not package:
            raise ValueError("reuse_installed_app needs 'package' in the scenario")
        options.no_reset = True
        # With noReset Appium does not relaunch an app that is already running; the
        # replayed scenario must start from a foreground app, so force the launch.
        options.set_capability("forceAppLaunch", True)
        options.app_package = package
        if spec.get("activity"):
            options.app_activity = spec["activity"]
    else:
        options.app = apk
        options.no_reset = False
        # Appium force-stops the app when the session ends unless this is set. The
        # classifier asks the device (pidof) whether the app is still alive AFTER the
        # runner returned; without this every clean run looked like a crash ("process gone").
        options.set_capability("dontStopAppOnReset", True)
    if mjpeg_port is not None:
        options.mjpeg_server_port = mjpeg_port
    if chromedriver_port is not None:
        options.chromedriver_port = chromedriver_port

    try:
        driver = webdriver.Remote(APPIUM_URL, options=options)
    except Exception as e:
        raise RuntimeError(
            f"could not connect to Appium at {APPIUM_URL} for serial={serial} "
            f"systemPort={system_port}: {e}"
        ) from e

    steps: list[Step] = []
    try:
        profile_id = profile or serial
        if not record:
            run_id = None
        elif run_id is None:
            run_id = storage.create_run(
                apk_name=os.path.basename(apk), scenario=spec["name"], profiles=[serial]
            )
        out_dir = f"{RESULTS_DIR}/{run_id}/{profile_id}"
        fallback = None
        if fallback_on and record:
            fallback = FallbackSession(locator_fallback, package, out_dir=out_dir,
                                       run_id=run_id, profile=profile_id)
        elif fallback_on:
            logging.info("locator fallback is not used for unrecorded runs")

        for step_index, step_spec in enumerate(spec["steps"]):
            start = time.monotonic()
            try:
                status = _execute(driver, serial, package, step_spec, fallback, step_index)
            except Exception:
                logging.exception("step %d (%s) failed", step_index, step_spec.get("action"))
                status = "fail"
            duration_ms = int((time.monotonic() - start) * 1000)

            if record:
                screenshot, hierarchy = _capture_evidence(driver, out_dir, step_index)
            else:
                screenshot, hierarchy = "", ""
            step = Step(
                step_index=step_index,
                # M14: " [AI-ASSISTED]" / " [UNRESOLVED]" when the locator fallback was used on this step
                action=step_spec["action"] + (fallback.tag(step_index) if fallback else ""),
                status=status,
                duration_ms=duration_ms,
                screenshot_path=screenshot,
                hierarchy_path=hierarchy,
            )
            if record:
                storage.save_step(run_id, profile_id, step)
            steps.append(step)
    finally:
        driver.quit()
    return run_id, steps


# ---------------------------------------------------------------------------
# ARCH entry point — safe for orchestrator / dry-run use
# ---------------------------------------------------------------------------

def run_scenario(serial: str, apk: str, scenario: str,
                 system_port: int = 8200,
                 dry_run: bool = False, *,
                 run_id: int | None = None, profile: str | None = None,
                 record: bool = True, reuse_installed_app: bool = False,
                 mjpeg_port: int | None = None,
                 chromedriver_port: int | None = None,
                 locator_fallback: FallbackConfig | None = None) -> tuple[int | None, list[Step]]:
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
        run_id, profile, record, reuse_installed_app, mjpeg_port,
        chromedriver_port, locator_fallback: see :func:`run`; passed through
        unchanged (the dry run ignores the last four).

    Returns:
        ``(run_id, steps)`` — same contract as :func:`run`.
    """
    if dry_run:
        logging.info("[STUB] runner.run_scenario serial=%s scenario=%s (dry_run)", serial, scenario)
        if not record:
            run_id = None
        elif run_id is None:
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
        if record:
            storage.save_step(run_id, profile or serial, stub_step)
        return run_id, [stub_step]

    logging.info("runner.run_scenario serial=%s scenario=%s", serial, scenario)
    return run(serial, apk, scenario, system_port,
               run_id=run_id, profile=profile, record=record,
               reuse_installed_app=reuse_installed_app,
               mjpeg_port=mjpeg_port, chromedriver_port=chromedriver_port,
               locator_fallback=locator_fallback)
