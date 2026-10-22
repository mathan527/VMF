"""Check one of Mathan's modules (M4, M6, M7, M8, M10) on the emulator and print a PASS/FAIL record.

    python scripts/check_modules.py m4      # 10 identical runs of the sample scenario
    python scripts/check_modules.py m6      # clean / crash / ANR / hang verdicts from real logcat
    python scripts/check_modules.py m7      # geometry findings on the broken-layout sample build
    python scripts/check_modules.py m8      # launch-time repeatability and the slowed build
    python scripts/check_modules.py m10     # one profile through the orchestrator on the real emulator
    python scripts/check_modules.py m4 --tests-only      # unit tests only, no emulator

Every check first runs the module's unit tests (fakes, no device), then the device part.
The device part needs: a booted emulator (default emulator-5554), Appium on localhost:4723 and
the sample app builds in sample_app/ (build.ps1, build.ps1 -AnrAfterMs 3000, -BrokenLayout,
-StartDelayMs 500). M14 has its own script: scripts/check_m14.py.

Nothing here changes the code under test. Results (evidence files) go to results/.
"""
import argparse
import collections
import hashlib
import logging
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PKG = "com.vmf.sample"
NORMAL = str(ROOT / "sample_app" / "build" / "sample.apk")
SLOW = str(ROOT / "sample_app" / "build_slow" / "sample.apk")
ANR_APK = str(ROOT / "sample_app" / "build_anr" / "sample.apk")
BROKEN = str(ROOT / "sample_app" / "build_broken" / "sample.apk")
SCENARIO = str(ROOT / "scenarios" / "sample_app.yaml")

TESTS = {
    "m4": ["tests/contract/test_skeleton.py", "tests/unit/test_run_matrix.py", "-k", "runner"],
    "m6": ["tests/unit/test_classifier.py"],
    "m7": ["tests/unit/test_geometry.py"],
    "m8": ["tests/unit/test_perf.py", "tests/unit/test_perf_host_load.py", "tests/unit/test_baselines.py"],
    "m10": ["tests/unit/test_run_matrix.py"],
}


def banner(text: str) -> None:
    print(f"\n{'=' * 78}\n{text}\n{'=' * 78}", flush=True)


def adb(serial: str, *args: str, timeout: int = 90) -> subprocess.CompletedProcess:
    return subprocess.run(["adb", "-s", serial, *args], capture_output=True, text=True, timeout=timeout)


def run_unit_tests(module: str) -> bool:
    banner(f"1. {module.upper()} unit tests (fakes, no device)")
    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *TESTS[module]]
    print("$ " + " ".join(cmd[1:]), flush=True)
    return subprocess.run(cmd, cwd=ROOT).returncode == 0


def verdict_line(ok: bool, text: str) -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {text}", flush=True)
    return ok


# --------------------------------------------------------------------------------------------- M4

def hierarchy_signature(path: str) -> str:
    data = Path(path).read_bytes()
    data = re.sub(rb' window-id="\d+"', b"", data)           # an Android counter, changes every session
    return hashlib.sha256(data).hexdigest()[:12] if data else "EMPTY"


def check_m4(serial: str, runs: int) -> bool:
    from vmf import runner
    banner(f"2. M4: the same scenario {runs} times on {serial} (scenarios/sample_app.yaml)")
    results = []
    for i in range(runs):
        run_id, steps = runner.run(serial, NORMAL, SCENARIO, 8200)
        actions = tuple((s.action, s.status) for s in steps)
        hier = tuple(hierarchy_signature(s.hierarchy_path) for s in steps)
        shots = all(Path(s.screenshot_path).is_file() and Path(s.screenshot_path).stat().st_size > 0
                    for s in steps)
        results.append((actions, hier, shots))
        passed = sum(s.status == "pass" for s in steps)
        print(f"  run {i + 1:2d}: run_id={run_id} steps={len(steps)} pass={passed}/{len(steps)} "
              f"screenshots_ok={shots}", flush=True)
    ok = verdict_line(len({r[0] for r in results}) == 1, "all runs have identical actions and statuses")
    ok &= verdict_line(all(s == "pass" for s in (st for st in (a[1] for a in results[0][0]))),
                       "every step passed")
    ok &= verdict_line(len({r[1] for r in results}) == 1,
                       "hierarchy XML identical in every run (window-id ignored)")
    ok &= verdict_line(all(r[2] for r in results), "every step has a non-empty screenshot")
    print("  steps:", [a for a, _ in results[0][0]])
    return ok


# --------------------------------------------------------------------------------------------- M6

HANG_SCENARIO = (
    'name: "Hang"\npackage: "com.vmf.sample"\nsteps:\n'
    '  - action: wait_for\n    locator: {content-desc: "login_button"}\n    timeout_s: 15\n'
    + '  - action: wait_for\n    locator: {content-desc: "does_not_exist"}\n    timeout_s: 12\n' * 3)


def m6_trial(serial: str, kind: str, out_dir: Path, tmp: Path):
    from vmf import classifier, runner
    from vmf.orchestrator import run_matrix as rm
    path = str(out_dir / f"{kind}_logcat.txt")
    proc = classifier.start_logcat(serial, path)
    steps = []
    try:
        if kind == "clean":
            _, steps = runner.run(serial, NORMAL, SCENARIO, 8200)
        elif kind == "crash":
            _, steps = runner.run(serial, NORMAL, SCENARIO, 8200)
            adb(serial, "shell", "am", "start", "-W", "-n", f"{PKG}/.MainActivity")
            adb(serial, "shell", "am", "crash", PKG)
        elif kind == "hang":
            scenario = tmp / "hang.yaml"
            scenario.write_text(HANG_SCENARIO, encoding="utf-8")
            _, steps = runner.run(serial, NORMAL, str(scenario), 8200)
        elif kind == "anr":
            adb(serial, "install", "-r", "-d", ANR_APK)
            adb(serial, "shell", "am", "force-stop", PKG)
            adb(serial, "shell", "am", "start", "-W", "-n", f"{PKG}/.MainActivity")
            started = time.monotonic()
            while time.monotonic() - started < 40:        # taps while the main thread is blocked
                try:
                    adb(serial, "shell", "input", "tap", "540", "1200", timeout=30)
                except subprocess.TimeoutExpired:
                    pass
                if "ANR in " + PKG in Path(path).read_text(encoding="utf-8", errors="replace"):
                    break
    finally:
        threading.Event().wait(rm.LOGCAT_GRACE_S)         # what the orchestrator does before it stops logcat
        classifier.stop_logcat(proc)
    verdict = classifier.classify(path, steps, package=PKG, pid_alive=rm._pid_alive_factory(serial, PKG))
    if kind == "anr":
        adb(serial, "shell", "am", "force-stop", PKG)
        adb(serial, "install", "-r", "-d", NORMAL)
    return verdict


def check_m6(serial: str, counts: dict) -> bool:
    banner(f"2. M6: verdicts from real logcat on {serial}")
    expected = {"clean": "pass", "crash": "crash", "hang": "hang", "anr": "anr"}
    table = collections.defaultdict(collections.Counter)
    out_dir = ROOT / "results" / "_check_m6"
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        for kind in ("clean", "crash", "hang", "anr"):
            for i in range(counts[kind]):
                v = m6_trial(serial, kind, out_dir, Path(tmp))
                table[kind][v.result] += 1
                print(f"  {kind:5} #{i + 1}: verdict={v.result:5} (expected {expected[kind]}) "
                      f"cause={v.cause[:75]!r}", flush=True)
    ok = True
    print()
    for kind, want in expected.items():
        good, total = table[kind][want], counts[kind]
        ok &= verdict_line(good == total, f"{kind:5}: {good}/{total} classified {want}")
    return ok


# --------------------------------------------------------------------------------------------- M7

def dump_hierarchy(serial: str, dest: Path) -> str:
    adb(serial, "shell", "uiautomator", "dump", "/sdcard/vmf_check.xml")
    adb(serial, "pull", "/sdcard/vmf_check.xml", str(dest))
    return dest.read_text(encoding="utf-8")


def check_m7(serial: str) -> bool:
    from vmf import geometry
    banner(f"2. M7: geometry findings on {serial}")
    zones = geometry.get_display_zones(serial)
    size = adb(serial, "shell", "wm", "size").stdout.strip()
    density = adb(serial, "shell", "wm", "density").stdout.strip()
    print(f"  screen: {size}, {density}")
    print(f"  zones read from the device: {zones}")
    config = {"width_px": 1080, "height_px": 2400, "density_dpi": 420, "package": PKG, **zones}
    out_dir = ROOT / "results" / "_check_m7"
    out_dir.mkdir(parents=True, exist_ok=True)
    findings = {}
    for label, apk in (("normal screen", NORMAL), ("broken-layout build", BROKEN)):
        adb(serial, "install", "-r", "-d", apk)
        adb(serial, "shell", "am", "force-stop", PKG)
        adb(serial, "shell", "am", "start", "-W", "-n", f"{PKG}/.MainActivity")
        xml = dump_hierarchy(serial, out_dir / f"{label.split()[0]}_hierarchy.xml")
        found = geometry.check(xml, config)
        findings[label] = found
        print(f"\n  {label}: {len(found)} finding(s)")
        for f in found:
            print(f"    {f.kind:15} {f.element_id} {tuple(f.bounds)}  {f.detail[:70]}")
    adb(serial, "shell", "am", "force-stop", PKG)
    adb(serial, "install", "-r", "-d", NORMAL)
    kinds = {f.kind for f in findings["broken-layout build"]}
    print()
    ok = verdict_line("cutout_overlap" in kinds, "a button drawn under the display cutout is flagged")
    ok &= verdict_line("overlap" in kinds, "two overlapping buttons are flagged")
    ok &= verdict_line("tap_target" in kinds, "a 15 dp tap target is flagged")
    ok &= verdict_line("cutout_overlap" not in {f.kind for f in findings["normal screen"]},
                       "the normal screen has nothing under the cutout")
    print("  note: the normal screen still reports its 45 dp email field (minimum is 48 dp); that is a real finding.")
    return ok


# --------------------------------------------------------------------------------------------- M8

def check_m8(serial: str) -> bool:
    from vmf import perf
    banner(f"2. M8: launch time on {serial} (reset=app mode: stop and clear the app before every run)")

    def flow():
        for _ in range(4):
            for args in (("input", "tap", "540", "420"), ("input", "swipe", "540", "1500", "540", "900", "250"),
                         ("input", "tap", "540", "620")):
                adb(serial, "shell", *args, timeout=30)

    def measure(label: str, apk: str) -> dict:
        started = time.monotonic()
        rows = perf.measure(serial, {"package": PKG, "activity": ".MainActivity"}, runs=5, apk=apk,
                            flow=flow, reset="app")
        out = {r.metric: r for r in rows}
        print(f"  {label:26} {time.monotonic() - started:4.0f}s  "
              + "  ".join(f"{m}={r.median:g} (spread {r.spread_pct:.0f}%{' UNSTABLE' if r.unstable else ''})"
                          for m, r in out.items()), flush=True)
        return {m: r.median for m, r in out.items()}

    a1 = measure("normal build, run 1", NORMAL)
    a2 = measure("normal build, run 2", NORMAL)
    slow = measure("build with +500 ms start", SLOW)
    adb(serial, "install", "-r", "-d", NORMAL)
    again = 100 * (a2["launch_time"] - a1["launch_time"]) / a1["launch_time"]
    slower = 100 * (slow["launch_time"] - a1["launch_time"]) / a1["launch_time"]
    print("\n  All numbers are emulator-relative.")
    ok = verdict_line(abs(again) <= 10, f"same build twice: launch time differs by {again:+.1f}% (limit 10%)")
    ok &= verdict_line(slower > 20, f"+500 ms build: launch time {slower:+.1f}% (flag threshold 20%)")
    print("  note: jank (adb-driven taps) and PSS can vary more between runs; see the README.")
    return ok


# --------------------------------------------------------------------------------------------- M10

def check_m10(serial: str) -> bool:
    from dataclasses import replace
    from vmf.orchestrator import run_matrix as rm
    banner(f"2. M10: one profile through run_matrix.main on {serial}")
    print("  real: runner (Appium), logcat, classifier, geometry, storage (farm.db)")
    print("  stubs: provision and oem_packs (Dhatri's M3/M5); the emulator is already running, so the")
    print("         orphan-process check is switched off for this demonstration")
    profile = {"name": "pixel6_demo", "brand": "Google", "tier": "low", "density_dpi": 420,
               "resolution": "1080 x 2400"}
    deps = replace(rm.default_deps(), list_emulator_processes=lambda: [])
    options = {"apk": NORMAL, "scenario": SCENARIO, "profiles": [profile], "performance_pass": False,
               "max_parallel": 1}
    summary = rm.main(None, options, deps=deps)
    row = summary["profiles"][0]
    print(f"\n  run_id={summary['run_id']} status={summary['status']} counts={summary['counts']}")
    print(f"  profile {row['profile']}: result={row['result']} serial={row['serial']} "
          f"findings={row['findings_count']} duration={row['duration_s']}s")
    print(f"  cause: {row['cause'][:110]}")
    print(f"  leftover emulator check: {summary['leftover_check']}, warnings: {row['warnings']}")
    print(f"  evidence: results/{summary['run_id']}/pixel6_demo/ (logcat.txt, findings.json, step files)")
    print()
    ok = verdict_line(not row["error"], "the profile ran through every stage without an error")
    ok &= verdict_line(row["result"] in ("pass", "layout"),
                       f"the profile result was recorded ({row['result']})")
    ok &= verdict_line(summary["leftover_check"] == "ok", "the leftover check ran")
    print("  note: a full 24-profile run needs Dhatri's real provision and oem_packs.")
    return ok


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("module", choices=sorted(TESTS))
    p.add_argument("--serial", default="emulator-5554")
    p.add_argument("--tests-only", action="store_true", help="unit tests only (no emulator)")
    p.add_argument("--runs", type=int, default=10, help="M4: how many identical runs")
    p.add_argument("--clean", type=int, default=2, help="M6: clean trials")
    p.add_argument("--crash", type=int, default=2, help="M6: crash trials")
    p.add_argument("--hang", type=int, default=1, help="M6: hang trials")
    p.add_argument("--anr", type=int, default=1, help="M6: ANR trials")
    args = p.parse_args()
    logging.basicConfig(level=logging.ERROR)

    print(f"VMF module check: {args.module.upper()}   {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("commit: " + subprocess.run(["git", "log", "--oneline", "-1"], cwd=ROOT, capture_output=True,
                                      text=True).stdout.strip())
    ok = run_unit_tests(args.module)
    if not ok:
        print("\nUNIT TESTS FAILED")
        return 1
    if args.tests_only:
        return 0
    if args.module == "m4":
        ok = check_m4(args.serial, args.runs)
    elif args.module == "m6":
        ok = check_m6(args.serial, {"clean": args.clean, "crash": args.crash, "hang": args.hang, "anr": args.anr})
    elif args.module == "m7":
        ok = check_m7(args.serial)
    elif args.module == "m8":
        ok = check_m8(args.serial)
    else:
        ok = check_m10(args.serial)
    banner(f"{args.module.upper()} CHECK {'PASSED' if ok else 'FAILED'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
