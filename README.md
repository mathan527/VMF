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
| M4 runner | Sample app, Appium 3.8, Pixel_6 AVD: all 6 steps of `scenarios/sample_app.yaml` pass | Default install mode needs a working `JAVA_HOME` for Appium |
| M6 classifier | A deliberate `am crash` was classified `crash` from real logcat | ANR, hang and "process gone" paths |
| M7 geometry | `get_display_zones` returned real cutout and gesture zones | A deliberately broken layout on a device |
| M8 perf | One full `measure()` run completed on the Pixel_6 AVD (install, snapshot save, warm-up, 3 measured runs): launch_time median 1111 ms, jank_pct median 20.00 %, pss median 21044 KB, all emulator-relative; the host-load and spread checks fired (`noisy host`, `unstable`) | **Per-run snapshot restore is unreliable** (see below); repeatability within 10% and the slowed-build test are not done |
| M10 orchestrator | Fakes only | Everything on real devices |

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
