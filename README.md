# Virtual Mobile Farm (VMF)

A Windows-native automated mobile-compatibility testing system for Android
apps on WHPX-accelerated emulators.

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

## Package Layout

```
vmf/
├── __init__.py
├── __main__.py          # entry: python -m vmf --dry-run
├── results.py           # shared data objects: Step, Verdict, Finding, PerfRow
├── provision/           # M3  emulator lifecycle stubs
├── oem_packs/           # M5  OEM behaviour stubs
├── runner/              # M4  Appium scenario runner (OWNER: Mathan)
├── classifier/          # M6  crash/ANR classifier stub
├── geometry/            # M7  geometry validation stub
├── perf/                # M8  performance measurement stub
├── storage/             # M9  SQLite storage stubs (OWNER: Gagan)
├── orchestrator/        # M10 run_matrix — multi-profile orchestration
├── report/              # M11 report generation stub (OWNER: Gagan)
└── api/                 # M12 FastAPI app stub (OWNER: Gagan)

scenarios/               # YAML scenario definitions
tests/                   # pytest test suite
scripts/                 # helper scripts (run_m4_once.py)
```

## ARCH Dry Run

```powershell
python -m vmf --dry-run
```

Expected output ends with:

```
=== ARCH DRY RUN PASSED ===
--- Orchestrator pipeline OK ---
```

## Running Tests

```powershell
python -m pytest tests/ -v
```

## Syntax Compilation

```powershell
python -m compileall vmf/ tests/ scripts/ -q
```

## Technology Stack

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

## Module Status

| Module       | Status        | Owner  | Target |
|--------------|---------------|--------|--------|
| provision    | ARCH stub     | Dhatri | M3     |
| oem_packs    | ARCH stub     | Dhatri | M5     |
| runner       | M4 IMPL       | Mathan | M4 ✓  |
| classifier   | ARCH stub     | Mathan | M6     |
| geometry     | ARCH stub     | Mathan | M7     |
| perf         | ARCH stub     | Mathan | M8     |
| storage      | ARCH stub     | Gagan  | M9     |
| orchestrator | ARCH dry-run  | Mathan | M10    |
| report       | ARCH stub     | Gagan  | M11    |
| api          | ARCH stub     | Gagan  | M12    |
