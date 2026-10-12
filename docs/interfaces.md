# VMF public interfaces

Generated from the code (`inspect` on each module). Status is `STUB` when the
function docstring starts with "STUB" or "ARCH stub", otherwise `REAL`.
"Real implementation due" is the milestone ID named in the docstring; the code
states no calendar dates, so none are given. `REAL` means real logic exists, not
that it was verified on a device. `runner.run_scenario` and
`orchestrator.run_matrix.run_single_profile` are REAL but call stubs internally
(dry-run mode / ARCH pipeline).

All of these are importable from the package, e.g. `from vmf import storage;
storage.create_run(...)`, and from the module path shown.

| Module | Function | Signature | Owner | Real implementation due | Status |
|---|---|---|---|---|---|
| `vmf.provision.provision` | `start_device` | `(profile: dict, port: int) -> str` | Dhatri (M3) | M3 | STUB |
| `vmf.provision.provision` | `stop_device` | `(serial: str) -> None` | Dhatri (M3) | M3 | STUB |
| `vmf.oem_packs.oem_packs` | `always_finish_activities` | `(serial: str, pkg: str) -> dict` | Dhatri (M5) | M5 | STUB |
| `vmf.oem_packs.oem_packs` | `apply_profile_settings` | `(serial: str, profile: dict) -> dict` | Dhatri (M5) | M5 | STUB |
| `vmf.oem_packs.oem_packs` | `background_kill` | `(serial: str, pkg: str) -> dict` | Dhatri (M5) | M5 | STUB |
| `vmf.oem_packs.oem_packs` | `doze` | `(serial: str, pkg: str) -> dict` | Dhatri (M5) | M5 | STUB |
| `vmf.oem_packs.oem_packs` | `permission_revoke` | `(serial: str, pkg: str) -> dict` | Dhatri (M5) | M5 | STUB |
| `vmf.oem_packs.oem_packs` | `reset_profile` | `(serial: str, pkg: str) -> None` | Dhatri (M5) | M5 | STUB |
| `vmf.oem_packs.oem_packs` | `restrict_background` | `(serial: str, pkg: str) -> dict` | Dhatri (M5) | M5 | STUB |
| `vmf.oem_packs.oem_packs` | `trim_memory` | `(serial: str, pkg: str) -> dict` | Dhatri (M5) | M5 | STUB |
| `vmf.runner.runner` | `run` | `(serial: str, apk: str, scenario: str, system_port: int, *, run_id: int \| None = None, profile: str \| None = None, record: bool = True, reuse_installed_app: bool = False, mjpeg_port: int \| None = None, chromedriver_port: int \| None = None) -> tuple[int \| None, list[vmf.results.Step]]` | Mathan (M4) | n/a (implemented) | REAL |
| `vmf.runner.runner` | `run_scenario` | `(serial: str, apk: str, scenario: str, system_port: int = 8200, dry_run: bool = False, *, run_id: int \| None = None, profile: str \| None = None, record: bool = True, reuse_installed_app: bool = False, mjpeg_port: int \| None = None, chromedriver_port: int \| None = None) -> tuple[int \| None, list[vmf.results.Step]]` | Mathan (M4) | n/a (implemented) | REAL |
| `vmf.classifier.classifier` | `classify` | `(logcat_path, steps: list[vmf.results.Step] \| None = None, *, package: str \| None = None, pid_alive: Optional[Callable[[], bool]] = None, hang_threshold_s: float = 30.0) -> vmf.results.Verdict` | Mathan (M6) | n/a (implemented) | REAL |
| `vmf.classifier.classifier` | `start_logcat` | `(serial: str, out_path: str) -> subprocess.Popen` | Mathan (M6) | n/a (implemented) | REAL |
| `vmf.classifier.classifier` | `stop_logcat` | `(proc: subprocess.Popen) -> None` | Mathan (M6) | n/a (implemented) | REAL |
| `vmf.geometry.geometry` | `check` | `(hierarchy_xml: str, profile_config: dict) -> list[vmf.results.Finding]` | Mathan (M7) | n/a (implemented) | REAL |
| `vmf.geometry.geometry` | `get_display_zones` | `(serial: str, runner=None) -> dict` | Mathan (M7) | n/a (implemented) | REAL |
| `vmf.geometry.geometry` | `parse_cutout_rects` | `(dumpsys_display_text: str) -> list[tuple[int, int, int, int]]` | Mathan (M7) | n/a (implemented) | REAL |
| `vmf.geometry.geometry` | `parse_gesture_rects` | `(dumpsys_window_text: str, screen: tuple[int, int]) -> list[tuple[int, int, int, int]]` | Mathan (M7) | n/a (implemented) | REAL |
| `vmf.geometry.geometry` | `validate_geometry` | `(hierarchy_xml: str, profile: dict) -> list[vmf.results.Finding]` | Mathan (M7) | n/a (implemented) | REAL |
| `vmf.perf.perf` | `measure` | `(serial: str, profile: dict, runs: int = 5, *, package=None, activity=None, flow=None, baselines=None, thresholds=None, adb=None, sampler_factory=None) -> list[vmf.results.PerfRow]` | Mathan (M8) | n/a (implemented, emulator-relative; tested with fakes only, per-run snapshot restore unreliable on the Pixel_6 AVD, see README) | REAL |
| `vmf.perf.perf` | `run_and_store` | `(run_id: int, profile_name: str, serial: str, profile: dict, **kwargs) -> list[vmf.results.PerfRow]` | Mathan (M8) | n/a (implemented) | REAL |
| `vmf.perf.perf` | `run_performance` | `(serial: str, package: str, profile: dict) -> list[vmf.results.PerfRow]` | Mathan (M8) | kept for old callers; returns a placeholder row | STUB |
| `vmf.storage.storage` | `create_run` | `(apk_name: str, scenario: str, profiles: list[str]) -> int` | Gagan (M9) | M9 | STUB |
| `vmf.storage.storage` | `finish_run` | `(run_id: int) -> None` | Gagan (M9) | M9 | STUB |
| `vmf.storage.storage` | `save_device_result` | `(run_id: int, profile: str, verdict) -> None` | Gagan (M9) | M9 | STUB |
| `vmf.storage.storage` | `save_perf` | `(run_id: int, profile: str, perf_rows: list) -> None` | Gagan (M9) | M9 | STUB |
| `vmf.storage.storage` | `save_profile_config` | `(run_id: int, profile: str, config: dict) -> None` | Gagan (M9) | M9 | STUB |
| `vmf.storage.storage` | `save_step` | `(run_id: int, profile: str, step) -> None` | Gagan (M9) | M9 | STUB |
| `vmf.orchestrator.run_matrix` | `dry_run` | `() -> None` | Mathan (M10 / ARCH) | n/a (implemented) | REAL |
| `vmf.orchestrator.run_matrix` | `run_single_profile` | `(profile: dict, apk: str, scenario: str, port: int = 5554, stub: bool = True) -> dict` | Mathan (M10 / ARCH) | n/a (implemented) | REAL |
| `vmf.orchestrator.run_matrix` | `main` | `(run_id: int \| None, options: dict, deps: Deps \| None = None) -> dict` | Mathan (M10) | n/a (implemented with fakes, not proven on devices) | REAL |
| `vmf.orchestrator.run_matrix` | `run_profile` | `(profile: dict, ctx: RunContext, slot: Slot) -> dict` (the caller owns the slot) | Mathan (M10) | n/a (implemented) | REAL |
| `vmf.orchestrator.run_matrix` | `preflight` | `(deps: Deps, options: dict) -> set` (PIDs seen) | Mathan (M10) | n/a (implemented) | REAL |
| `vmf.orchestrator.run_matrix` | `profile_name` / `load_profiles` / `resolve_max_parallel` / `default_deps` | see module docstrings | Mathan (M10) | n/a (implemented) | REAL |
| `vmf.report.report` | `generate_report` | `(run_id: int, output_dir: str = 'reports') -> str` | Gagan (M11) | M11 | STUB |
| `vmf.api.api` | `create_app` | `()` | Gagan (M12) | M12 | STUB |

Shared data objects live in `vmf/results.py`: `Step`, `Verdict`, `Finding`, `PerfRow`.
