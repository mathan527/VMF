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
| `vmf.runner.runner` | `run` | `(serial: str, apk: str, scenario: str, system_port: int, *, run_id: int \| None = None, profile: str \| None = None, record: bool = True, reuse_installed_app: bool = False, mjpeg_port: int \| None = None, chromedriver_port: int \| None = None, locator_fallback: FallbackConfig \| None = None) -> tuple[int \| None, list[vmf.results.Step]]` | Mathan (M4) | n/a (implemented) | REAL |
| `vmf.runner.runner` | `run_scenario` | `(serial: str, apk: str, scenario: str, system_port: int = 8200, dry_run: bool = False, *, run_id: int \| None = None, profile: str \| None = None, record: bool = True, reuse_installed_app: bool = False, mjpeg_port: int \| None = None, chromedriver_port: int \| None = None, locator_fallback: FallbackConfig \| None = None) -> tuple[int \| None, list[vmf.results.Step]]` | Mathan (M4) | n/a (implemented) | REAL |
| `vmf.runner.locator_fallback` | `resolve` | `(*, config, action, intent, locator, hierarchy_xml, package, used_run=0, used_step=0, http_post=None, use_heuristic=True) -> Resolution \| NoMatch` | Mathan (M14) | n/a (implemented with fakes, not run against a real Ollama) | REAL |
| `vmf.runner.locator_fallback` | `candidates` | `(hierarchy_xml: str, package: str \| None, max_candidates: int = 60) -> list[Candidate]` | Mathan (M14) | n/a (implemented) | REAL |
| `vmf.runner.locator_fallback` | `FallbackConfig` / `FallbackSession` / `config_from_options` / `validate_fallback_steps` | see module docstring | Mathan (M14) | n/a (implemented) | REAL |
| `vmf.classifier.classifier` | `classify` | `(logcat_path, steps: list[vmf.results.Step] \| None = None, *, package: str \| None = None, pid_alive: Optional[Callable[[], bool]] = None, hang_threshold_s: float = 30.0) -> vmf.results.Verdict` | Mathan (M6) | n/a (implemented) | REAL |
| `vmf.classifier.classifier` | `start_logcat` | `(serial: str, out_path: str) -> subprocess.Popen` | Mathan (M6) | n/a (implemented) | REAL |
| `vmf.classifier.classifier` | `stop_logcat` | `(proc: subprocess.Popen) -> None` | Mathan (M6) | n/a (implemented) | REAL |
| `vmf.geometry.geometry` | `check` | `(hierarchy_xml: str, profile_config: dict) -> list[vmf.results.Finding]` | Mathan (M7) | n/a (implemented) | REAL |
| `vmf.geometry.geometry` | `get_display_zones` | `(serial: str, runner=None) -> dict` | Mathan (M7) | n/a (implemented) | REAL |
| `vmf.geometry.geometry` | `parse_cutout_rects` | `(dumpsys_display_text: str) -> list[tuple[int, int, int, int]]` | Mathan (M7) | n/a (implemented) | REAL |
| `vmf.geometry.geometry` | `parse_gesture_rects` | `(dumpsys_window_text: str, screen: tuple[int, int]) -> list[tuple[int, int, int, int]]` | Mathan (M7) | n/a (implemented) | REAL |
| `vmf.geometry.geometry` | `validate_geometry` | `(hierarchy_xml: str, profile: dict) -> list[vmf.results.Finding]` | Mathan (M7) | n/a (implemented) | REAL |
| `vmf.perf.perf` | `measure` | `(serial: str, profile: dict, runs: int = 5, *, package=None, activity=None, flow=None, baselines=None, thresholds=None, adb=None, sampler_factory=None, apk=None, host_load_out=None) -> list[vmf.results.PerfRow]`; `host_load_out` (optional dict) is filled with the host-load summary of the measurement window | Mathan (M8) | n/a (implemented, emulator-relative; tested with fakes only, per-run snapshot restore unreliable on the Pixel_6 AVD, see README) | REAL |
| `vmf.perf.perf` | `load_thresholds` / `threshold_status` / `load_approvers` | `(path=None) -> Thresholds` / `dict` / `list[str]`; reads the explicit path, else `$VMF_PERF_CONFIG`, else `perf_config.yaml` at the repo root; rejects unknown keys and non-positive values; `Thresholds.approved/approved_by/approved_on` are set only from the file (not compared, so equal numbers stay equal) | Mathan (M8.1) | n/a (implemented; the values are PROPOSED, not approved by the lead) | REAL |
| `vmf.perf.baselines` | `BaselineStore` / `YamlBaselineStore` / `StorageBaselineStore` / `get_store` | `load(profile)`, `load_all(names)`, `promote(run_id, rows_by_profile, approved_by, *, allow_overwrite=False, ...)`; promotion is never automatic and needs a name in `baseline_approvers` | Mathan (M8.9) | YAML store is the interim default; the storage store needs `storage.load_baselines` / `storage.save_baseline` (requested from Gagan, M9) | REAL |
| `vmf.perf.__main__` | `python -m vmf.perf promote\|baselines` | `promote --run-id N --approved-by NAME [--profile P] [--allow-overwrite]` | Mathan (M8.9) | n/a (implemented) | REAL |
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

## Requests to Gagan (storage, M9) - not implemented, not edited by me

Until these exist, the orchestrator keeps the data in files under `results/<run_id>/<profile>/` and `perf_baselines.yaml`. Nothing here changes `schema.sql` or `storage.py`.

| Request | Interim in the meantime |
|---|---|
| `perf_results` columns `host_load` (JSON: cpu min/mean/max, free RAM min) and `build` (APK name + sha256), plus a way to pass them through `save_perf` | `perf_meta.json` per profile, listed in `summary["perf_meta"]` |
| `storage.load_baselines(profile) -> {metric: float}` | `perf_baselines.yaml` via `YamlBaselineStore` |
| `storage.save_baseline(profile, metric, value, *, build_sha256, promoted_by, run_id)` | `python -m vmf.perf promote ...` writes `perf_baselines.yaml` atomically |
| A third step status or an `ai_assisted` column | the action name gets a suffix: `tap [AI-ASSISTED]` / `tap [UNRESOLVED]` (`strip_tag()` removes it) and `locator_fallbacks.json` lists the details |

`get_store()` switches to the storage store automatically as soon as both baseline functions exist. All performance numbers are emulator-relative.

### Orchestrator summary fields added on `workbook-gaps`

| Key | Content |
|---|---|
| `thresholds` | `{"approved", "approved_by", "approved_on", "source", "values"}`. `approved` is True only when perf_config.yaml has both `approved_by` and `approved_on`; thresholds passed in `options["thresholds"]` are always reported unapproved. A WARNING `perf thresholds are PROPOSED, not approved by the lead` is logged otherwise. |
| `perf_meta` | `{profile: path of results/<run_id>/<profile>/perf_meta.json}` (interim record of build, host load, flags, spreads, medians, tier and thresholds; storage columns requested from Gagan) |
| `baselines`, `no_baseline`, `baseline_notes` | where baselines came from (`options` or the store), profiles without one, and `"no baseline for X"` / `"no baseline for X/metric"` lines |
| `tier_ratio` | low-tier median / high-tier median per metric; present only when a low-tier AND a high-tier profile were measured |
| `locator_fallbacks` | `{profile: {"ai_assisted": n, "unresolved": m}}`, profiles with neither omitted (was `{profile: healed count}`) |

The M14 vision option sends the screenshot of the **previous** step (the screen the step acts on). The runner only captures a step's own screenshot after the step ends, and the spec forbids an extra capture, so step 0 is never sent an image.

## Notes added 14 Oct (device runs)

- `classifier.classify` hang rule: two steps count as unchanged when their hierarchy hash (ignoring `window-id`) is equal and the screenshots differ in at most `SCREEN_CHANGE_TOLERANCE` (0.2%) of the pixels outside the status and navigation bars. Pillow is optional; without it screenshots must match byte for byte.
- `run_matrix` options: `logcat_grace_s` (default `LOGCAT_GRACE_S` = 2.0). New `Deps.host_sampler` (default `vmf.perf.perf.HostSampler`). The stored profile config gains `host_busy` and `host_load`.
- `run_matrix`: if the scenario's last step is in `DELIBERATE_KILL_ACTIONS` (`oem.background_kill`) no `pid_alive` is passed to `classify`.
- `perf.load_snapshot(serial, adb, package=None)`: with a package it also waits until `pm path <package>` answers.
