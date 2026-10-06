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
status. In short: `runner` and `classifier` contain real logic that has not
been proven on a device; `geometry`, `perf`, `provision`, `oem_packs`,
`storage`, `report` and `api` are still ARCH stubs; `orchestrator` runs the
single-profile ARCH pipeline only (`run_matrix` is not written).

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
