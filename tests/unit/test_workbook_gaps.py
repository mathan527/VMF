"""Tests for the workbook gaps in the orchestrator and perf modules: perf_config.yaml and its
approval status, perf_meta.json, baselines loaded from a store, the tier ratio, and the rule
that nothing promotes a baseline automatically. No device, no network.
"""
import datetime
import hashlib
import inspect
import json
import logging
from pathlib import Path

import pytest
import yaml
from test_run_matrix import Fakes, go, make_profiles

from vmf.orchestrator import run_matrix as rm
from vmf.perf import baselines, perf
from vmf.perf.perf import Thresholds, load_approvers, load_thresholds, threshold_status
from vmf.results import PerfRow

REPO = Path(rm.__file__).resolve().parents[2]
PROPOSED_WARNING = "perf thresholds are PROPOSED, not approved by the lead"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(rm, "RESULTS_DIR", str(tmp_path / "results"))
    monkeypatch.delenv("VMF_PERF_CONFIG", raising=False)
    monkeypatch.setattr(rm, "get_store", lambda: baselines.YamlBaselineStore(tmp_path / "perf_baselines.yaml"))


@pytest.fixture
def scenario(tmp_path):
    p = tmp_path / "scenario.yaml"
    p.write_text(yaml.safe_dump({"name": "Login", "package": "com.example.testapp", "steps": []}), encoding="utf-8")
    return str(p)


def write_cfg(tmp_path, name="cfg.yaml", **values):
    base = {"launch_regression_pct": 20, "jank_increase_points": 5, "pss_increase_pct": 15,
            "max_spread_pct": 10, "noisy_host_cpu_pct": 30, "approved_by": None, "approved_on": None,
            "baseline_approvers": []}
    base.update(values)
    path = tmp_path / name
    path.write_text(yaml.safe_dump(base), encoding="utf-8")
    return str(path)


def warned(caplog, text):
    return any(text in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)


# --- GAP 1: perf_config.yaml ----------------------------------------------------------------------

def test_the_repo_config_has_exactly_the_workbook_keys_and_no_approval():
    data = yaml.safe_load((REPO / "perf_config.yaml").read_text(encoding="utf-8"))
    assert data == {"launch_regression_pct": 20, "jank_increase_points": 5, "pss_increase_pct": 15,
                    "max_spread_pct": 10, "noisy_host_cpu_pct": 30, "approved_by": None,
                    "approved_on": None, "baseline_approvers": []}


def test_the_repo_config_loads_to_the_proposed_values_and_is_not_approved():
    assert load_thresholds() == Thresholds(20.0, 5.0, 15.0, 10.0, 30.0, 5)
    status = threshold_status()
    assert status["approved"] is False and status["approved_by"] is None and status["approved_on"] is None
    assert load_approvers() == []


def test_workbook_key_names_map_to_the_threshold_fields(tmp_path):
    path = write_cfg(tmp_path, launch_regression_pct=30, jank_increase_points=3, pss_increase_pct=25,
                     max_spread_pct=12, noisy_host_cpu_pct=40)
    assert load_thresholds(path) == Thresholds(30.0, 3.0, 25.0, 12.0, 40.0, 5)


def test_the_threshold_field_names_are_accepted_as_aliases(tmp_path):
    path = tmp_path / "old.yaml"
    path.write_text("jank_increase_pts: 4\npss_regression_pct: 35\nruns: 7\n", encoding="utf-8")
    t = load_thresholds(str(path))
    assert (t.jank_increase_pts, t.pss_regression_pct, t.runs) == (4.0, 35.0, 7)


def test_one_setting_in_two_spellings_is_rejected(tmp_path):
    path = tmp_path / "dup.yaml"
    path.write_text("jank_increase_points: 3\njank_increase_pts: 4\n", encoding="utf-8")
    with pytest.raises(ValueError, match="repeats"):
        load_thresholds(str(path))


def test_a_missing_file_gives_the_defaults_a_warning_and_never_approval(tmp_path, caplog):
    missing = str(tmp_path / "absent.yaml")
    with caplog.at_level(logging.WARNING):
        assert load_thresholds(missing) == Thresholds()
    assert warned(caplog, "NOT YET APPROVED")
    status = threshold_status(missing)
    assert status["approved"] is False and "no perf config" in status["source"]
    assert load_approvers(missing) == []


@pytest.mark.parametrize("key, text", [
    ("launch_regression_pct", "-5"), ("launch_regression_pct", "0"), ("max_spread_pct", "abc"),
    ("jank_increase_points", "true"), ("pss_increase_pct", "[1, 2]"), ("noisy_host_cpu_pct", "101"),
    ("noisy_host_cpu_pct", "'30%'"), ("jank_increase_points", "null"),
])
def test_a_negative_zero_or_non_numeric_value_is_rejected_naming_the_key(tmp_path, key, text):
    path = tmp_path / "bad.yaml"
    path.write_text(f"{key}: {text}\n", encoding="utf-8")
    with pytest.raises(ValueError, match=key):
        load_thresholds(str(path))


@pytest.mark.parametrize("key", ["launch_regression_pct", "pss_increase_pct"])
def test_a_threshold_not_larger_than_the_allowed_spread_is_rejected(tmp_path, key):
    path = write_cfg(tmp_path, **{key: 10})                 # equal to max_spread_pct 10
    with pytest.raises(ValueError, match="larger than max_spread_pct"):
        load_thresholds(path)
    path = write_cfg(tmp_path, **{key: 8})
    with pytest.raises(ValueError, match="larger than max_spread_pct"):
        load_thresholds(path)


def test_an_unknown_key_is_rejected_naming_it(tmp_path):
    path = write_cfg(tmp_path, mystery_setting=1)
    with pytest.raises(ValueError, match="'mystery_setting'"):
        load_thresholds(path)
    with pytest.raises(ValueError, match="'mystery_setting'"):
        threshold_status(path)


def test_a_config_that_is_not_a_mapping_is_rejected(tmp_path):
    path = tmp_path / "list.yaml"
    path.write_text("- 1\n- 2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="mapping"):
        load_thresholds(str(path))


def test_approval_needs_both_a_name_and_a_date(tmp_path):
    both = threshold_status(write_cfg(tmp_path, "both.yaml", approved_by="Lead Name", approved_on="2026-10-13"))
    assert both["approved"] is True and both["approved_by"] == "Lead Name" and both["approved_on"] == "2026-10-13"
    assert threshold_status(write_cfg(tmp_path, "who.yaml", approved_by="Lead Name"))["approved"] is False
    assert threshold_status(write_cfg(tmp_path, "when.yaml", approved_on="2026-10-13"))["approved"] is False
    with pytest.raises(ValueError, match="approved_by"):      # a blank name is a mistake, not "unapproved"
        threshold_status(write_cfg(tmp_path, "blank.yaml", approved_by="  ", approved_on=""))


def test_a_yaml_date_counts_as_a_date(tmp_path):
    path = tmp_path / "date.yaml"
    path.write_text("approved_by: Lead\napproved_on: 2026-10-13\n", encoding="utf-8")
    status = threshold_status(str(path))
    assert status["approved"] is True and status["approved_on"] == str(datetime.date(2026, 10, 13))


@pytest.mark.parametrize("extra", [{"approved_by": ["x"]}, {"approved_on": 5},
                                   {"baseline_approvers": "lead"}, {"baseline_approvers": [""]},
                                   {"baseline_approvers": [1]}])
def test_invalid_approval_fields_are_rejected(tmp_path, extra):
    path = write_cfg(tmp_path, **extra)
    with pytest.raises(ValueError, match=next(iter(extra))):
        load_thresholds(path)


def test_the_environment_variable_selects_the_file_and_an_explicit_path_wins(tmp_path, monkeypatch):
    env_file = write_cfg(tmp_path, "env.yaml", launch_regression_pct=44)
    explicit = write_cfg(tmp_path, "explicit.yaml", launch_regression_pct=55)
    monkeypatch.setenv("VMF_PERF_CONFIG", env_file)
    assert load_thresholds().launch_regression_pct == 44.0
    assert load_thresholds(explicit).launch_regression_pct == 55.0
    assert perf.resolve_config_path() == Path(env_file)
    monkeypatch.delenv("VMF_PERF_CONFIG")
    assert perf.resolve_config_path() == REPO / "perf_config.yaml"


def test_approvers_are_read_from_the_config(tmp_path):
    assert load_approvers(write_cfg(tmp_path, baseline_approvers=["Lead Name", " Deputy "])) == ["Lead Name", "Deputy"]


# --- the orchestrator and the approval status -----------------------------------------------------------

def test_unapproved_thresholds_warn_and_the_summary_never_claims_approval(tmp_path, scenario, caplog):
    fakes = Fakes(tmp_path)
    with caplog.at_level(logging.WARNING):
        summary = go(fakes, scenario, make_profiles(1), performance_pass=True)
    assert warned(caplog, PROPOSED_WARNING)
    th = summary["thresholds"]
    assert th["approved"] is False and th["approved_by"] is None and th["approved_on"] is None
    assert th["values"] == rm.threshold_values(Thresholds()) and "approved" not in th["values"]
    assert th["source"].endswith("perf_config.yaml")


def test_an_approved_config_is_reported_with_who_and_when_and_does_not_warn(tmp_path, scenario, caplog, monkeypatch):
    monkeypatch.setenv("VMF_PERF_CONFIG", write_cfg(tmp_path, approved_by="Lead Name", approved_on="2026-10-13",
                                                    launch_regression_pct=25))
    fakes = Fakes(tmp_path)
    with caplog.at_level(logging.WARNING):
        summary = go(fakes, scenario, make_profiles(1), performance_pass=True)
    assert not warned(caplog, PROPOSED_WARNING)
    th = summary["thresholds"]
    assert th["approved"] is True and th["approved_by"] == "Lead Name" and th["approved_on"] == "2026-10-13"
    assert th["values"]["launch_regression_pct"] == 25.0
    (_, e), = fakes.extras("perf")
    assert e["kw"]["thresholds"].launch_regression_pct == 25.0       # the file's value is what perf used


def test_a_name_without_a_date_is_still_reported_unapproved(tmp_path, scenario, caplog, monkeypatch):
    monkeypatch.setenv("VMF_PERF_CONFIG", write_cfg(tmp_path, approved_by="Lead Name"))
    with caplog.at_level(logging.WARNING):
        summary = go(Fakes(tmp_path), scenario, make_profiles(1), performance_pass=True)
    assert summary["thresholds"]["approved"] is False and warned(caplog, PROPOSED_WARNING)


def test_options_thresholds_win_and_carry_no_approval(tmp_path, scenario, caplog, monkeypatch):
    monkeypatch.setenv("VMF_PERF_CONFIG", write_cfg(tmp_path, approved_by="Lead Name", approved_on="2026-10-13"))
    mine = Thresholds(launch_regression_pct=33.0)
    fakes = Fakes(tmp_path)
    with caplog.at_level(logging.WARNING):
        summary = go(fakes, scenario, make_profiles(1), performance_pass=True, thresholds=mine)
    (_, e), = fakes.extras("perf")
    assert e["kw"]["thresholds"] is mine
    assert summary["thresholds"]["approved"] is False and summary["thresholds"]["source"] == "options['thresholds']"
    assert warned(caplog, PROPOSED_WARNING)               # an object passed in code is never "approved"


def test_without_the_performance_pass_the_config_is_not_read_and_nothing_warns(tmp_path, scenario, caplog, monkeypatch):
    monkeypatch.setenv("VMF_PERF_CONFIG", write_cfg(tmp_path, mystery=1))   # would be rejected if it were read
    with caplog.at_level(logging.WARNING):
        summary = go(Fakes(tmp_path), scenario, make_profiles(1), performance_pass=False)
    assert not warned(caplog, PROPOSED_WARNING)
    assert summary["thresholds"]["approved"] is False and "off" in summary["thresholds"]["source"]


def test_an_invalid_config_is_a_validation_error_before_anything_starts(tmp_path, scenario, monkeypatch):
    monkeypatch.setenv("VMF_PERF_CONFIG", write_cfg(tmp_path, mystery=1))
    fakes = Fakes(tmp_path, runs=())
    with pytest.raises(ValueError, match="'mystery'"):
        go(fakes, scenario, make_profiles(1), run_id=None, performance_pass=True)
    assert fakes.kinds(include_global=True) == []


# --- GAP 2: perf_meta.json -------------------------------------------------------------------------------

HOST_LOAD = {"samples": 12, "cpu_pct": {"min": 5.0, "mean": 20.0, "max": 55.0},
             "cpu_outside_emulator_pct": {"min": 1.0, "mean": 8.0, "max": 20.0},
             "free_ram_min_bytes": 4_000_000_000, "noisy_threshold_pct": 30.0}


def test_perf_meta_json_is_written_after_each_perf_run_and_listed_in_the_summary(tmp_path, scenario):
    apk = tmp_path / "app.apk"
    apk.write_bytes(b"not really an apk" * 1000)
    fakes = Fakes(tmp_path)
    fakes.host_load = HOST_LOAD
    fakes.perf_flags = {"p1": (True, True)}
    summary = go(fakes, scenario, make_profiles(2), performance_pass=True, apk=str(apk))
    paths = summary["perf_meta"]
    assert sorted(paths) == ["p0", "p1"]
    meta = json.loads(Path(paths["p0"]).read_text(encoding="utf-8"))
    assert meta["label"] == "emulator-relative" and meta["profile"] == "p0" and meta["run_id"] == 77
    assert meta["build"] == {"apk": "app.apk", "sha256": hashlib.sha256(apk.read_bytes()).hexdigest()}
    assert meta["host_load"] == HOST_LOAD
    assert meta["noisy"] is False and meta["unstable"] is False and meta["runs"] == 5
    assert meta["tier"] == "low" and meta["thresholds"] == rm.threshold_values(Thresholds())
    assert meta["thresholds_approved"] is False
    (m,) = meta["metrics"]
    assert (m["metric"], m["median"]) == ("launch_time", 1000.0)
    assert meta["spread_pct"] == {"launch_time": 1.0}
    assert meta["baseline_compared"] == {"launch_time": True}
    other = json.loads(Path(paths["p1"]).read_text(encoding="utf-8"))
    assert other["noisy"] is True and other["unstable"] is True and other["tier"] == "medium"
    assert [p["perf_meta"] for p in summary["performance"]] == [paths["p0"], paths["p1"]]


def test_perf_meta_records_the_baseline_comparison(tmp_path, scenario):
    fakes = Fakes(tmp_path)
    summary = go(fakes, scenario, make_profiles(1), performance_pass=True,
                 baselines={"p0": {"launch_time": 800.0}})
    meta = json.loads(Path(summary["perf_meta"]["p0"]).read_text(encoding="utf-8"))
    assert meta["metrics"][0]["baseline"] == 800.0 and meta["baseline_compared"] == {"launch_time": True}


def test_a_failure_writing_perf_meta_is_logged_and_does_not_affect_the_run(tmp_path, scenario, caplog, monkeypatch):
    real = rm._write_json

    def failing(run_id, name, filename, payload):
        if filename == "perf_meta.json":
            raise OSError("disk full")
        return real(run_id, name, filename, payload)
    monkeypatch.setattr(rm, "_write_json", failing)
    with caplog.at_level(logging.ERROR):
        summary = go(Fakes(tmp_path), scenario, make_profiles(2), performance_pass=True)
    assert summary["counts"] == {"pass": 2} and summary["perf_meta"] == {}
    assert [p["status"] for p in summary["performance"]] == ["ok", "ok"]      # the perf results are intact
    assert any(r.exc_info and "disk full" in str(r.exc_info[1]) for r in caplog.records)


def test_an_unreadable_apk_gives_a_null_hash_and_a_logged_traceback(tmp_path, scenario, caplog):
    with caplog.at_level(logging.ERROR):
        summary = go(Fakes(tmp_path), scenario, make_profiles(1), performance_pass=True,
                     apk=str(tmp_path / "missing.apk"))
    meta = json.loads(Path(summary["perf_meta"]["p0"]).read_text(encoding="utf-8"))
    assert meta["build"] == {"apk": "missing.apk", "sha256": None}
    assert any(r.exc_info for r in caplog.records)


def test_a_failed_perf_run_writes_no_perf_meta(tmp_path, scenario):
    fakes = Fakes(tmp_path, fail={("perf", "p0"): RuntimeError("perf broke")})
    summary = go(fakes, scenario, make_profiles(1), performance_pass=True)
    assert summary["perf_meta"] == {} and summary["performance"][0]["status"] == "error"


def test_perf_gets_the_run_count_and_a_host_load_out_dict(tmp_path, scenario):
    fakes = Fakes(tmp_path)
    go(fakes, scenario, make_profiles(1), performance_pass=True, thresholds=Thresholds(runs=7))
    (_, e), = fakes.extras("perf")
    assert e["kw"]["runs"] == 7 and isinstance(e["kw"]["host_load_out"], dict)


# --- GAP 3: baselines in the orchestrator -----------------------------------------------------------------------

def seeded_store(tmp_path, profile="p0", value=777.0):
    store = baselines.YamlBaselineStore(tmp_path / "bl.yaml")
    store.promote(1, {profile: [PerfRow("launch_time", value, 1.0, None, None, False, False)]}, "Lead",
                  approvers=["Lead"])
    return store


def test_baselines_come_from_the_store_when_the_option_is_missing(tmp_path, scenario, monkeypatch, caplog):
    store = seeded_store(tmp_path)
    monkeypatch.setattr(rm, "get_store", lambda: store)
    fakes = Fakes(tmp_path)
    with caplog.at_level(logging.WARNING):
        summary = go(fakes, scenario, make_profiles(2), performance_pass=True)
    kw = {n: e["kw"] for n, e in fakes.extras("perf")}
    assert kw["p0"]["baselines"] == {"launch_time": 777.0} and kw["p1"]["baselines"] is None
    assert summary["no_baseline"] == ["p1"]
    assert summary["baselines"] == {"source": "perf_baselines.yaml", "error": None}
    assert warned(caplog, "no baseline for p1")
    assert not warned(caplog, "no baseline for p0")


def test_the_baselines_option_wins_over_the_store(tmp_path, scenario, monkeypatch):
    def no_store():
        raise AssertionError("the store must not be consulted when baselines are given")
    monkeypatch.setattr(rm, "get_store", no_store)
    fakes = Fakes(tmp_path)
    summary = go(fakes, scenario, make_profiles(2), performance_pass=True,
                 baselines={"p0": {"launch_time": 5.0}})
    kw = {n: e["kw"] for n, e in fakes.extras("perf")}
    assert kw["p0"]["baselines"] == {"launch_time": 5.0} and kw["p1"]["baselines"] is None
    assert summary["baselines"]["source"] == "options" and summary["no_baseline"] == ["p1"]


def test_an_explicit_empty_baselines_option_also_wins(tmp_path, scenario, monkeypatch):
    monkeypatch.setattr(rm, "get_store", lambda: (_ for _ in ()).throw(AssertionError("store consulted")))
    summary = go(Fakes(tmp_path), scenario, make_profiles(2), performance_pass=True, baselines={})
    assert summary["no_baseline"] == ["p0", "p1"] and summary["baselines"]["source"] == "options"


def test_a_missing_baseline_is_still_measured_and_reported(tmp_path, scenario):
    fakes = Fakes(tmp_path)
    summary = go(fakes, scenario, make_profiles(1), performance_pass=True)
    (_, e), = fakes.extras("perf")
    assert e["kw"]["baselines"] is None and summary["no_baseline"] == ["p0"]
    assert summary["counts"] == {"pass": 1} and summary["performance"][0]["status"] == "ok"


def test_a_broken_baseline_file_is_logged_and_the_profiles_are_still_measured(tmp_path, scenario, monkeypatch, caplog):
    bad = tmp_path / "bl.yaml"
    bad.write_text("profiles: [not, a, mapping]\n", encoding="utf-8")
    monkeypatch.setattr(rm, "get_store", lambda: baselines.YamlBaselineStore(bad))
    with caplog.at_level(logging.ERROR):
        summary = go(Fakes(tmp_path), scenario, make_profiles(1), performance_pass=True)
    assert summary["baselines"]["error"].startswith("BaselineError") and summary["no_baseline"] == ["p0"]
    assert summary["performance"][0]["status"] == "ok"
    assert any(r.exc_info for r in caplog.records)


def test_the_store_is_not_consulted_when_the_performance_pass_is_off(tmp_path, scenario, monkeypatch):
    monkeypatch.setattr(rm, "get_store", lambda: (_ for _ in ()).throw(AssertionError("store consulted")))
    summary = go(Fakes(tmp_path), scenario, make_profiles(1), performance_pass=False)
    assert summary["no_baseline"] == [] and "off" in summary["baselines"]["source"]


def test_main_never_promotes_a_baseline(tmp_path, scenario, monkeypatch):
    calls = []

    def spy(self, *a, **k):
        calls.append((a, k))
        raise AssertionError("promote() must never be called by a run")
    monkeypatch.setattr(baselines.BaselineStore, "promote", spy)
    summary = go(Fakes(tmp_path), scenario, make_profiles(3), performance_pass=True)
    assert calls == [] and summary["counts"] == {"pass": 3}
    assert ".promote(" not in inspect.getsource(rm) and "promote(" not in inspect.getsource(rm).replace("# ", "")


def test_a_run_never_writes_the_baseline_file(tmp_path, scenario):
    go(Fakes(tmp_path), scenario, make_profiles(2), performance_pass=True)
    assert not (tmp_path / "perf_baselines.yaml").exists()


# --- the tier ratio ----------------------------------------------------------------------------------------------

def tiers(*pairs):
    return [{"name": n, "tier": t} for n, t in pairs]


def test_the_tier_ratio_appears_when_a_low_and_a_high_tier_profile_were_measured(tmp_path, scenario):
    fakes = Fakes(tmp_path)
    fakes.perf_median = {"lowphone": 2000.0, "highphone": 1000.0}
    summary = go(fakes, scenario, tiers(("lowphone", "low"), ("highphone", "high")), performance_pass=True)
    assert summary["tier_ratio"] == {"launch_time": 2.0}


def test_several_profiles_in_a_tier_use_the_median_of_their_medians(tmp_path, scenario):
    fakes = Fakes(tmp_path)
    fakes.perf_median = {"l1": 2000.0, "l2": 4000.0, "h1": 1000.0}
    summary = go(fakes, scenario, tiers(("l1", "low"), ("l2", "low"), ("h1", "high")), performance_pass=True)
    assert summary["tier_ratio"] == {"launch_time": 3.0}


def test_no_tier_ratio_without_both_tiers(tmp_path, scenario):
    only_low_and_medium = go(Fakes(tmp_path), scenario, tiers(("a", "low"), ("b", "medium")), performance_pass=True)
    assert "tier_ratio" not in only_low_and_medium
    only_high = go(Fakes(tmp_path), scenario, tiers(("a", "high"), ("b", "high")), performance_pass=True)
    assert "tier_ratio" not in only_high


def test_no_tier_ratio_when_one_tier_failed_or_the_perf_pass_is_off(tmp_path, scenario):
    fakes = Fakes(tmp_path, fail={("perf", "highphone"): RuntimeError("perf broke")})
    summary = go(fakes, scenario, tiers(("lowphone", "low"), ("highphone", "high")), performance_pass=True)
    assert "tier_ratio" not in summary
    off = go(Fakes(tmp_path), scenario, tiers(("lowphone", "low"), ("highphone", "high")), performance_pass=False)
    assert "tier_ratio" not in off


def test_a_zero_high_tier_median_is_skipped_not_divided(tmp_path, scenario):
    fakes = Fakes(tmp_path)
    fakes.perf_median = {"lowphone": 2000.0, "highphone": 0.0}
    summary = go(fakes, scenario, tiers(("lowphone", "low"), ("highphone", "high")), performance_pass=True)
    assert "tier_ratio" not in summary


def test_the_profile_key_rule_has_one_source_of_truth():
    assert rm.profile_name({"name": "Samsung A07"}) == baselines.sanitise_profile("Samsung A07") == "Samsung_A07"


# --- added: approval travels with the loaded thresholds; notes for missing baselines ----------------------------

def test_load_thresholds_carries_the_approval_of_the_file(tmp_path, monkeypatch):
    unapproved = load_thresholds(write_cfg(tmp_path, "u.yaml"))
    assert unapproved.approved is False and unapproved.approved_by is None and unapproved.approved_on is None
    approved = load_thresholds(write_cfg(tmp_path, "a.yaml", approved_by="Lead Name", approved_on="2026-10-13"))
    assert approved.approved is True and approved.approved_by == "Lead Name" and approved.approved_on == "2026-10-13"
    assert approved == unapproved                       # approval is metadata, the numbers are the same
    half = load_thresholds(write_cfg(tmp_path, "h.yaml", approved_by="Lead Name"))
    assert half.approved is False
    monkeypatch.setenv("VMF_PERF_CONFIG", str(tmp_path / "absent.yaml"))
    assert load_thresholds().approved is False and Thresholds().approved is False
    assert load_thresholds().approved == threshold_status()["approved"]


def test_the_repo_config_is_loaded_as_not_approved():
    th = load_thresholds()
    assert th.approved is False and th.approved_by is None and th.approved_on is None


def test_a_thresholds_object_in_options_cannot_approve_itself(tmp_path, scenario, caplog):
    self_approved = Thresholds(approved=True, approved_by="me", approved_on="today")
    with caplog.at_level(logging.WARNING):
        summary = go(Fakes(tmp_path), scenario, make_profiles(1), performance_pass=True, thresholds=self_approved)
    assert summary["thresholds"]["approved"] is False and summary["thresholds"]["approved_by"] is None
    assert not any(k.startswith("approved") for k in summary["thresholds"]["values"])
    meta = json.loads(Path(summary["perf_meta"]["p0"]).read_text(encoding="utf-8"))
    assert meta["thresholds_approved"] is False and "approved" not in meta["thresholds"]
    assert warned(caplog, PROPOSED_WARNING)


def test_the_summary_says_no_baseline_for_each_profile_without_one(tmp_path, scenario, monkeypatch):
    monkeypatch.setattr(rm, "get_store", lambda: seeded_store(tmp_path))
    summary = go(Fakes(tmp_path), scenario, make_profiles(3), performance_pass=True)
    assert summary["baseline_notes"] == ["no baseline for p1", "no baseline for p2"]


def test_a_metric_without_a_baseline_is_noted_even_when_the_profile_has_others():
    plan = type("P", (), {"no_baseline": ["p1"], "names": ["p0", "p1"]})()
    perf_results = {"p0": {"status": "ok", "metrics": [{"metric": "launch_time", "baseline": 900.0},
                                                       {"metric": "pss", "baseline": None}]},
                    "p1": {"status": "ok", "metrics": [{"metric": "launch_time", "baseline": None}]}}
    assert rm._baseline_notes(plan, perf_results) == ["no baseline for p1", "no baseline for p0/pss"]
