"""Verifies the vmf/ skeleton against its specification."""
import importlib

import pytest

PACKAGES = [
    "provision", "oem_packs", "runner", "classifier", "geometry",
    "perf", "storage", "orchestrator", "report", "api",
]
# Modules that had no public callables before ARCH stubs were added.
# Now that all ARCH stubs are filled, this list is empty.
# Future placeholder modules should be added here until they are implemented.
PLACEHOLDERS: list[str] = []

# Companion .py modules that must be importable alongside __init__.py
COMPANION_MODULES = [
    "vmf.provision.provision",
    "vmf.oem_packs.oem_packs",
    "vmf.runner.runner",
    "vmf.classifier.classifier",
    "vmf.geometry.geometry",
    "vmf.perf.perf",
    "vmf.storage.storage",
    "vmf.orchestrator.run_matrix",
    "vmf.report.report",
    "vmf.api.api",
]


# 1. package __init__ imports
@pytest.mark.parametrize("name", [f"vmf.{p}" for p in PACKAGES] + ["vmf.results"])
def test_imports(name):
    assert importlib.import_module(name) is not None


# 1b. companion .py module imports
@pytest.mark.parametrize("name", COMPANION_MODULES)
def test_companion_module_imports(name):
    assert importlib.import_module(name) is not None


# 2-3. provision
def test_start_device_returns_str():
    from vmf import provision
    assert isinstance(provision.start_device(profile={}, port=5554), str)


def test_stop_device_returns_none():
    from vmf import provision
    assert provision.stop_device(serial="x") is None


# provision.provision module-level import
def test_provision_module_start_device():
    from vmf.provision.provision import start_device
    assert isinstance(start_device(profile={}, port=5554), str)


# 4-6. oem_packs
def test_apply_profile_settings_returns_dict():
    from vmf import oem_packs
    assert isinstance(oem_packs.apply_profile_settings(serial="x", profile={"cutout": "notch"}), dict)


def test_background_kill_shape():
    from vmf import oem_packs
    r = oem_packs.background_kill(serial="x", pkg="com.test")
    assert isinstance(r, dict)
    assert isinstance(r["ok"], bool)
    assert isinstance(r["verify"], str)


def test_reset_profile_returns_none():
    from vmf import oem_packs
    assert oem_packs.reset_profile(serial="x", pkg="com.test") is None


# 7-8. storage
def test_create_run_increments():
    from vmf import storage
    a = storage.create_run(apk_name="a", scenario="s", profiles=["p1"])
    b = storage.create_run(apk_name="a", scenario="s", profiles=["p1"])
    assert isinstance(a, int) and isinstance(b, int)
    assert a != b


def test_storage_save_functions_return_none(tmp_path, monkeypatch):
    from vmf import storage
    from vmf.storage import storage as storage_module
    from vmf.results import Step, Verdict, PerfRow
    monkeypatch.setattr(storage_module, "_DB_PATH", tmp_path / "farm.db")
    storage_module._INITIALIZED_PATHS.clear()
    run_id = storage.create_run(apk_name="a", scenario="s", profiles=["p1"])
    step = Step(step_index=0, action="tap", status="pass", duration_ms=1,
                screenshot_path="s.png", hierarchy_path="h.xml")
    verdict = Verdict(result="pass", cause="", failed_step=None, trigger_behaviour=None)
    perf = PerfRow(metric="pss", median=1.0, spread_pct=0.0, baseline=None,
                   regression_pct=None, noisy=False, unstable=False)
    assert storage.save_device_result(run_id=run_id, profile="p1", verdict=verdict) is None
    assert storage.save_step(run_id=run_id, profile="p1", step=step) is None
    assert storage.save_profile_config(run_id=run_id, profile="p1", config={"a": 1}) is None
    assert storage.save_perf(run_id=run_id, profile="p1", perf_rows=[perf]) is None
    assert storage.finish_run(run_id=run_id) is None
    storage_module._INITIALIZED_PATHS.clear()


# 9. results dataclasses
VALID = {
    "Step": dict(step_index=0, action="tap", status="pass", duration_ms=5,
                 screenshot_path="s.png", hierarchy_path="h.xml"),
    "Verdict": dict(result="pass", cause="", failed_step=None, trigger_behaviour=None),
    "Finding": dict(kind="overlap", element_id="e1", bounds=(0, 0, 1, 1), detail="d"),
    "PerfRow": dict(metric="pss", median=1.0, spread_pct=0.5, baseline=None,
                    regression_pct=None, noisy=False, unstable=False),
}


@pytest.mark.parametrize("cls_name", list(VALID))
def test_dataclass_instantiates_and_stores_fields(cls_name):
    from vmf import results
    obj = getattr(results, cls_name)(**VALID[cls_name])
    for k, v in VALID[cls_name].items():
        assert getattr(obj, k) == v


@pytest.mark.parametrize("cls_name", list(VALID))
def test_dataclass_missing_field_raises(cls_name):
    from vmf import results
    cls = getattr(results, cls_name)
    for field in VALID[cls_name]:
        kwargs = {k: v for k, v in VALID[cls_name].items() if k != field}
        with pytest.raises(TypeError):
            cls(**kwargs)


@pytest.mark.parametrize("cls_name", list(VALID))
def test_dataclass_misspelled_field_raises(cls_name):
    from vmf import results
    cls = getattr(results, cls_name)
    for field in VALID[cls_name]:
        kwargs = dict(VALID[cls_name])
        kwargs[field + "_x"] = kwargs.pop(field)
        with pytest.raises(TypeError):
            cls(**kwargs)


@pytest.mark.parametrize("cls_name", list(VALID))
def test_dataclass_extra_field_raises(cls_name):
    from vmf import results
    with pytest.raises(TypeError):
        getattr(results, cls_name)(**VALID[cls_name], bogus=1)


# 10. placeholder packages (empty now that ARCH stubs are filled)
@pytest.mark.parametrize("pkg", PLACEHOLDERS)
def test_placeholder_has_docstring_and_no_public_callables(pkg):
    mod = importlib.import_module(f"vmf.{pkg}")
    assert mod.__doc__ and mod.__doc__.strip()
    callables = [n for n in dir(mod)
                 if not (n.startswith("__") and n.endswith("__")) and callable(getattr(mod, n))]
    assert callables == []


# 11. ARCH stub callable contracts
def test_classifier_classify_returns_verdict():
    from vmf import classifier
    from vmf.results import Step, Verdict
    step = Step(step_index=0, action="tap", status="pass", duration_ms=5,
                screenshot_path="s.png", hierarchy_path="h.xml")
    result = classifier.classify([step])
    assert isinstance(result, Verdict)


def test_geometry_validate_geometry_returns_list():
    from vmf import geometry
    result = geometry.validate_geometry("<hierarchy/>", {"cutout": "notch"})
    assert isinstance(result, list)


def test_perf_run_performance_returns_list_of_perfrow():
    from vmf import perf
    from vmf.results import PerfRow
    rows = perf.run_performance("emulator-5554", "com.example", {"density": 420})
    assert isinstance(rows, list)
    assert all(isinstance(r, PerfRow) for r in rows)


def test_report_generate_report_returns_str():
    from vmf import report
    path = report.generate_report(run_id=1)
    assert isinstance(path, str)


def test_api_create_app_returns_something():
    from vmf import api
    app = api.create_app()
    assert app is not None


def test_orchestrator_run_matrix_importable():
    from vmf.orchestrator import run_matrix
    assert callable(run_matrix.dry_run)
    assert callable(run_matrix.run_single_profile)


def test_runner_run_scenario_callable():
    from vmf import runner
    assert callable(runner.run_scenario)


def test_oem_packs_additional_stubs():
    from vmf import oem_packs
    for fn_name in ("restrict_background", "doze", "always_finish_activities",
                    "trim_memory", "permission_revoke"):
        fn = getattr(oem_packs, fn_name)
        result = fn("emulator-5554", "com.example.app")
        assert isinstance(result, dict)
        assert result["ok"] is True


# 12. runner.run_scenario dry_run mode — actually calls the interface chain
def test_runner_run_scenario_dry_run_returns_steps():
    """runner.run_scenario(dry_run=True) must return a real Step list
    without requiring Appium or an emulator."""
    from vmf import runner
    from vmf.results import Step
    run_id, steps = runner.run_scenario(
        serial="emulator-5554",
        apk="app.apk",
        scenario="stub",
        dry_run=True,
    )
    assert isinstance(run_id, int)
    assert isinstance(steps, list)
    assert len(steps) >= 1
    assert all(isinstance(s, Step) for s in steps)


# 12b. companion module-level run_scenario
def test_runner_runner_module_run_scenario():
    from vmf.runner.runner import run_scenario
    from vmf.results import Step
    run_id, steps = run_scenario(
        serial="emulator-5554",
        apk="app.apk",
        scenario="stub",
        dry_run=True,
    )
    assert isinstance(run_id, int)
    assert len(steps) >= 1


# 13. full orchestrator pipeline (no emulator required)
def test_orchestrator_run_single_profile_dry():
    from vmf.orchestrator import run_matrix
    from vmf.results import Verdict, PerfRow
    profile = {"brand": "TEST", "tier": "low"}
    result = run_matrix.run_single_profile(
        profile=profile,
        apk="app.apk",
        scenario="stub",
        port=5554,
        stub=True,
    )
    assert isinstance(result["serial"], str)
    assert isinstance(result["run_id"], int)
    assert isinstance(result["verdict"], Verdict)
    assert isinstance(result["findings"], list)
    assert isinstance(result["perf_rows"], list)
    assert all(isinstance(r, PerfRow) for r in result["perf_rows"])
    assert isinstance(result["report_path"], str)
