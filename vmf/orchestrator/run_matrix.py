"""run_matrix.py — ARCH dry-run orchestrator.

Owner: Mathan (M10 / ARCH)

Demonstrates the full VMF execution pipeline by actually calling each
module interface in order.  During ARCH all interfaces are stubs so no
real emulator, ADB, or Appium is invoked.

Real M10 (24-profile orchestration with ThreadPoolExecutor) will extend
this module.  The interface contract (run_single_profile / dry_run) must
be preserved.
"""
import logging

from vmf import api, classifier, geometry, oem_packs, perf, provision, report, runner, storage


# ---------------------------------------------------------------------------
# Public ARCH entry points
# ---------------------------------------------------------------------------

def run_single_profile(profile: dict, apk: str, scenario: str,
                       port: int = 5554, stub: bool = True) -> dict:
    """Execute the full VMF pipeline for one device profile.

    During ARCH (``stub=True``) this calls the stubs of every interface.
    M10 will call with ``stub=False`` against real emulators.

    Args:
        profile:  Device profile dict (brand, tier, cutout, density, ...).
        apk:      Path (real or stub) to the APK under test.
        scenario: Path (real or stub) to the scenario YAML file.
        port:     Base emulator port (default 5554).
        stub:     If True, runner operates in dry-run mode (no Appium).

    Returns:
        Summary dict: serial, run_id, steps, verdict, findings,
        perf_rows, report_path.
    """
    log = logging.getLogger(__name__)

    # 1. Provision --------------------------------------------------------
    log.info("[STUB] provision.start_device()")
    serial = provision.start_device(profile, port)

    try:
        # 2. OEM profile settings -----------------------------------------
        log.info("[STUB] oem_packs.apply_profile_settings()")
        oem_packs.apply_profile_settings(serial, profile)

        # 3. Storage — open run record ------------------------------------
        log.info("[STUB] storage.create_run()")
        # NOTE: run_id is created inside runner.run_scenario() when stub=True,
        # so we obtain it from the return value rather than calling create_run
        # here directly.  This avoids double-counting the run.

        # 4. Runner — actually call runner.run_scenario() -----------------
        log.info("[STUB] runner.run_scenario()")
        run_id, steps = runner.run_scenario(
            serial=serial,
            apk=apk,
            scenario=scenario,
            system_port=8200,
            dry_run=stub,
        )
        log.info("runner.run_scenario() returned run_id=%s steps=%d", run_id, len(steps))

        # 5. Classifier — determine verdict --------------------------------
        log.info("[STUB] classifier.classify()")
        verdict = classifier.classify(steps)

        # 6. Geometry — validate UI layout ---------------------------------
        log.info("[STUB] geometry.validate_geometry()")
        findings = geometry.validate_geometry("<hierarchy/>", profile)

        # 7. Performance — collect metrics ---------------------------------
        log.info("[STUB] perf.run_performance()")
        perf_rows = perf.run_performance(serial, "com.example.app", profile)

        # 8. Storage — persist results -------------------------------------
        log.info("[STUB] storage.save_device_result()")
        storage.save_device_result(run_id, serial, verdict)

        log.info("[STUB] storage.save_perf()")
        storage.save_perf(run_id, serial, perf_rows)

        log.info("[STUB] storage.save_profile_config()")
        storage.save_profile_config(run_id, serial, profile)

        log.info("[STUB] storage.finish_run()")
        storage.finish_run(run_id)

        # 9. Report --------------------------------------------------------
        log.info("[STUB] report.generate_report()")
        report_path = report.generate_report(run_id)

        # 10. API — confirm factory is importable --------------------------
        log.info("[STUB] api.create_app()")
        _ = api.create_app()

        return {
            "serial": serial,
            "run_id": run_id,
            "steps": steps,
            "verdict": verdict,
            "findings": findings,
            "perf_rows": perf_rows,
            "report_path": report_path,
        }

    finally:
        # 11. Provision — stop device -------------------------------------
        log.info("[STUB] provision.stop_device()")
        provision.stop_device(serial)


# ---------------------------------------------------------------------------
# Dry-run convenience wrapper
# ---------------------------------------------------------------------------

def dry_run() -> None:
    """Execute a single-profile ARCH dry-run and print a formatted summary.

    Calls the real interface of every module in sequence.
    No emulator, ADB, or Appium required.
    """
    print()
    print("=== VMF ARCH DRY RUN ===")
    print()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    profile = {
        "brand": "TEST",
        "tier": "low",
        "cutout": "notch",
        "density": 420,
        "font_scale": 1.3,
        "dark_mode": True,
    }

    result = run_single_profile(
        profile=profile,
        apk="app.apk",
        scenario="scenarios/login_flow.yaml",
        port=5554,
        stub=True,
    )

    print()
    print("--- DRY RUN SUMMARY ---")
    print(f"  serial     : {result['serial']}")
    print(f"  run_id     : {result['run_id']}")
    print(f"  steps      : {len(result['steps'])}")
    print(f"  verdict    : {result['verdict'].result}")
    print(f"  findings   : {len(result['findings'])}")
    print(f"  perf rows  : {len(result['perf_rows'])}")
    print(f"  report     : {result['report_path']}")
    print()
    print("=== ARCH DRY RUN PASSED ===")
    print()


if __name__ == "__main__":
    dry_run()
