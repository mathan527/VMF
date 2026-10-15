# Virtual Mobile Farm (VMF)

A Windows-native automated mobile-compatibility testing system for Android
apps on WHPX-accelerated emulators. It boots emulators for a matrix of device
profiles, runs a scenario on each through Appium, classifies the outcome
(crash / ANR / hang / pass), checks UI geometry and performance, stores the
evidence, and produces reports.

## Documentation

- [docs/architecture/VMF_Architecture_v2_SIGNOFF.pdf](docs/architecture/VMF_Architecture_v2_SIGNOFF.pdf) — signed-off architecture.
- [docs/interfaces.md](docs/interfaces.md) — every public function: module, signature, owner, STUB or REAL.

## Architecture

```
Clients         FastAPI (api/) + Windows Task Scheduler
Control         orchestrator/run_matrix.py  (ThreadPoolExecutor, M10)
Devices         provision/  — WHPX emulator.exe lifecycle
                oem_packs/  — per-OEM behaviour stubs (M5)
Execution       runner/     — Appium 2 + UiAutomator2 scenarios (M4)
                classifier/ — crash/ANR/hang detection (M6)
                geometry/   — UI-bounds validation (M7)
                perf/       — launch-time / jank / PSS (M8)
Data & Output   storage/    — SQLite + evidence folders (M9)
                report/     — HTML/Excel reports (M11)
```

## Folder map

```
vmf/                     # the package (import path: vmf.<package>)
├── __main__.py          # python -m vmf --dry-run
├── config.py            # shared constants (APPIUM_URL, DEFAULT_TIMEOUT_S, ...)
├── results.py           # shared data objects: Step, Verdict, Finding, PerfRow
├── provision/           # emulator lifecycle
├── oem_packs/           # OEM behaviour packs
├── runner/              # Appium scenario runner
├── classifier/          # crash / ANR / hang classifier, logcat capture helpers
├── geometry/            # UI geometry validation
├── perf/                # performance measurement
├── storage/             # run storage
├── orchestrator/        # run_matrix: multi-profile orchestration
├── report/              # report generation
└── api/                 # HTTP API
scenarios/               # YAML scenario definitions (see scenarios/README.md)
scripts/                 # helper scripts (see scripts/README.md)
tests/
├── contract/            # import and signature contracts (test_skeleton.py)
├── unit/                # per-module tests (test_classifier.py)
└── fixtures/logcat/     # sample logcat files used by the classifier tests
docs/                    # architecture PDF and interfaces.md
pyproject.toml           # project metadata and pytest settings
requirements.txt         # runtime and test dependencies
requirements-dev.txt     # pytest
```

Each package's `__init__.py` re-exports its public functions, so both
`from vmf import storage; storage.create_run(...)` and
`from vmf.storage.storage import create_run` work.

## ARCH dry run

No emulator or Appium needed; every module interface is called in order, most
of them as stubs.

```powershell
python -m vmf --dry-run
```

The output ends with `=== ARCH DRY RUN PASSED ===`.

## Tests

```powershell
python -m pytest -q
```

Test settings (`testpaths`, `pythonpath`) are in `pyproject.toml`. The tests
need no device. Nothing in the suite exercises a real emulator or Appium.

## Ownership

| Owner | Modules |
|---|---|
| Mathan | ARCH skeleton, `runner` (M4), `classifier` (M6), `geometry` (M7), `perf` (M8), `orchestrator` (M10), M14 (optional) |
| Dhatri | `provision` (M3), `oem_packs` (M5) and, outside this repo's `vmf/`, M0, M1, M2, M12, M15 |
| Gagan | `storage` (M9), `report` (M11), `api` (code labels it M12) |

## Module status

See [docs/interfaces.md](docs/interfaces.md) for the per-function STUB or REAL
status. In short: `runner`, `classifier`, `geometry` and `perf` (`perf.measure`) contain real logic that has not
been proven on a device; `provision`, `oem_packs`,
`report` and `api` are still ARCH stubs; `storage` (M9) is implemented; `orchestrator` has the M10 matrix
(`run_matrix.main`, tested with fakes only, never run on devices) plus the single-profile ARCH dry run.

### Verification status (what has and has not been run on a device)

| Module | Device evidence | Still open |
|---|---|---|
| M4 runner | Sample app, Appium 3.8, Pixel_6 AVD, 14 Oct: 10 consecutive runs of `scenarios/sample_app.yaml`, each 6/6 steps pass, identical actions and statuses, all screenshots non-empty, hierarchy XML identical apart from Android's `window-id` counter | Default install mode needs a working `JAVA_HOME` for Appium. Fixed 14 Oct: Appium used to force-stop the app when the session closed, so the classifier saw "process gone" after every run (now `dontStopAppOnReset`) |
| M6 classifier | 14 Oct, Pixel_6 AVD, real logcat, after the fixes below: clean 10/10 `pass`, `am crash` 8/8 `crash` (read from the log, `CrashedByAdbException`), ANR build tapped 4/4 `anr` (3 + 1), unchanged screen for 36 s 2/2 `hang`. Before the fixes: crash 3/5, hang 0/2 | The hang threshold (30 s) is not agreed with the lead. An emulator left running for hours became sluggish and produced genuine ANRs in clean runs (3 of 13): restart the emulator between batches |
| M7 geometry | 14 Oct: real cutout `(492,0,610,128)` and gesture zone read from the device; `build.ps1 -BrokenLayout` flagged a button under the cutout (`cutout_overlap`), two overlapping buttons and a 15 dp tap target | The normal sample screen is not finding-free: its email field is 45 dp high (< 48 dp) and is reported |
| M8 perf | One full `measure()` run completed on the Pixel_6 AVD (install, snapshot save, warm-up, 3 measured runs): launch_time median 1111 ms, jank_pct median 20.00 %, pss median 21044 KB, all emulator-relative; the host-load and spread checks fired (`noisy host`, `unstable`) | **Per-run snapshot restore is unreliable** (see below). 14 Oct, normal build twice and the +500 ms build, 5 runs each, 1 profile: PSS repeatable (26,341 / 26,406 / 26,268 KB, spread about 1%), but launch time and jank are not: launch median 411 ms then 792 ms for the SAME build (+93%), spread 22-35% each time, so Test A (within 10%) FAILS on this emulator and Test B is inconclusive (slow build 1003 ms is +144% against run 1 but only +27% against run 2). Fixed on the way: `load_snapshot` now waits for the activity/package services and the app to be visible (before, `am force-stop` failed with "Can't find service: activity"). Later the same day, on a freshly restarted emulator, a snapshot restore left the guest unresponsive for more than 10 minutes, which no wait in the code can fix. New opt-in mode `measure(..., reset="app")` (stop the app and `pm clear` it before every run, no snapshot): launch time of the same build twice 363 ms then 373 ms (+2.8%, passes the 10% test), the +500 ms build 783 ms (+110% to +116%, clearly flagged), 43 s per 5 runs instead of 3 minutes. Jank is still not repeatable (47-60%, spread 27-78%: it comes from adb-driven taps) and PSS differed by 10.7% between the two normal runs. The default stays "snapshot" until the lead agrees to the change, because the workbook specifies a snapshot load before every run |
| M10 orchestrator | Fakes only | Everything on real devices |

**Workbook gaps (branch `workbook-gaps`).** Nothing below has been run on a device.
- `perf_config.yaml` holds the workbook M8.1 thresholds. The values are PROPOSED: `approved_by` / `approved_on` are empty and a run says so in the log and in `summary["thresholds"]`. Only the lead approves them, by filling those two fields.
- After each performance run `results/<run_id>/<profile>/perf_meta.json` records the build (APK name + sha256), host load, noisy/unstable flags, spreads, tier and thresholds. All numbers are emulator-relative.
- Baselines: `python -m vmf.perf baselines` lists them, `python -m vmf.perf promote --run-id N --approved-by NAME` creates them. Promotion is never automatic; the name must be in `baseline_approvers` in `perf_config.yaml` (empty today, so promotion is refused until the lead is added). The interim store is `perf_baselines.yaml`; the storage functions are requested from Gagan (see [docs/interfaces.md](docs/interfaces.md)).
- **Fixes from the 14 Oct device runs** (each has unit tests; the numbers are in the status table above):
  - `runner.run` sets `dontStopAppOnReset`: Appium used to force-stop the app when the session closed, so the classifier saw "process gone" after every run.
  - `run_matrix` keeps logcat running `LOGCAT_GRACE_S` (2 s) after the last step before stopping it and classifying; a crash in the last moments used to be missed (2 of 5 `am crash` trials).
  - A scenario whose LAST step is `oem.background_kill` skips the "process gone" crash rule (`DELIBERATE_KILL_ACTIONS`).
  - The M6 hang rule compares screenshots with a pixel tolerance (needs Pillow, optional) and ignores `window-id` in the hierarchy; a blinking cursor or the status-bar clock used to hide every hang.
  - `load_snapshot` waits for the `activity` and `package` services and for the app to be visible after a restore.
  - An ANR, hang or crash that happens while the host CPU outside the emulator is above 30% is annotated "host busy ... re-run to confirm" and the host load is stored in the profile config.
  - M14: the model is asked twice with the candidates in opposite orders and a heal needs both answers to agree; elements whose text looks like an instruction are not candidates. With `qwen2.5:1.5b` this cut wrong heals on the 10 "element removed" screens from 10 to 1; it is still not safe to enable on real runs.
  - `sample_app/build.ps1` has `-AnrAfterMs` (an ANR build) and `-BrokenLayout` (button under the cutout, overlapping buttons, tiny target) for the M6/M7 checks.
  - `perf.measure(reset="app")` / `perf.reset_app`: the no-snapshot alternative described in the M8 row. Snapshot mode is unreliable on this laptop: a restore worked on the emulator started on 14 Oct at 21:15 and froze the guest on the three emulators started afterwards (with and without `-no-snapshot-load`); the cause is not known.
- M14: fallback steps are tagged `[AI-ASSISTED]` / `[UNRESOLVED]` in the stored action name; the vision input is off by default and untested against a real model.

**M8 snapshot restore.** After `adb emu avd snapshot load`, the Pixel_6 AVD often shows the device
`offline`: usually for a few seconds, sometimes for minutes or indefinitely (four repeated loads of one
snapshot came back in 9 s, never, 6 s and 48 s; a live `measure()` run then stayed offline for all 3 load
attempts). `load_snapshot` waits for `sys.boot_completed` and repeats the load up to 3 times, which is
tested with fakes; it did not fix one earlier live run, but a later run on a healthy emulator completed with no retries needed. M8's Done-when (slow build flagged on every tier; same
build twice within 10%) is therefore OPEN. Planned alternative once `provision` (M3) is real: restart the
emulator with `-snapshot perf_base` for each run instead of loading in place.

## Technology stack

| Layer         | Technology                          |
|---------------|-------------------------------------|
| Emulator      | Android emulator.exe + WHPX         |
| Automation    | Appium 2 + UiAutomator2             |
| ADB           | Used directly (separate from Appium)|
| Concurrency   | Python ThreadPoolExecutor           |
| Storage       | SQLite + local evidence folders     |
| API           | FastAPI                             |
| Scheduling    | Windows Task Scheduler              |
| Reports       | HTML / Excel                        |
