"""Tests for vmf.perf.baselines and the `python -m vmf.perf` command. No device, no sqlite."""
import datetime
import json
import types

import pytest
import yaml

from vmf.perf import __main__ as cli
from vmf.perf import baselines
from vmf.perf.baselines import BaselineError, PromotionRefused, StorageBaselineStore, YamlBaselineStore
from vmf.results import PerfRow
from vmf.storage import storage as storage_module

LEAD = "Lead Name"
NOW = datetime.datetime(2026, 10, 13, 9, 0, tzinfo=datetime.timezone.utc)


def row(metric="launch_time", median=1000.0, noisy=False, unstable=False, spread=2.0):
    return PerfRow(metric, median, spread, None, None, noisy, unstable)


@pytest.fixture
def store(tmp_path):
    return YamlBaselineStore(tmp_path / "perf_baselines.yaml")


def promote(store, rows=None, **kw):
    kw.setdefault("approvers", [LEAD])
    kw.setdefault("now", NOW)
    rows = {"Samsung A07": [row()]} if rows is None else rows
    return store.promote(7, rows, kw.pop("approved_by", LEAD), **kw)


# --- sanitising ----------------------------------------------------------------------------------

def test_sanitise_profile():
    assert baselines.sanitise_profile("Samsung A07") == "Samsung_A07"
    assert baselines.sanitise_profile("  a/b\\c:d  ") == "a_b_c_d"
    assert baselines.sanitise_profile("ok-name_1.2") == "ok-name_1.2"


# --- YAML store: load / promote ----------------------------------------------------------------------

def test_an_empty_store_has_no_baselines(store):
    assert store.load("anything") == {} and store.load_all(["a", "b"]) == {} and store.all_entries() == []


def test_promote_writes_the_value_with_its_provenance(store):
    result = promote(store, builds={"Samsung A07": "abc123"})
    assert result == [("Samsung_A07", "launch_time", None, 1000.0)]
    assert store.load("Samsung A07") == {"launch_time": 1000.0}
    assert store.load("Samsung_A07") == {"launch_time": 1000.0}          # same key either way
    (entry,) = store.all_entries()
    assert (entry["profile"], entry["metric"], entry["value"]) == ("Samsung_A07", "launch_time", 1000.0)
    assert entry["promoted_by"] == LEAD and entry["run_id"] == 7 and entry["build_sha256"] == "abc123"
    assert entry["promoted_on"].startswith("2026-10-13T09:00:00")


def test_load_all_returns_only_profiles_with_a_baseline(store):
    promote(store, {"a": [row(median=1.0)], "b": [row(median=2.0)]})
    assert store.load_all(["a", "b", "c"]) == {"a": {"launch_time": 1.0}, "b": {"launch_time": 2.0}}


def test_promoting_other_profiles_keeps_the_existing_ones(store):
    promote(store, {"a": [row(median=1.0)]})
    promote(store, {"b": [row(median=2.0)]})
    assert store.load("a") == {"launch_time": 1.0} and store.load("b") == {"launch_time": 2.0}


# --- refusals ----------------------------------------------------------------------------------------------

def refused(store, match, **kw):
    with pytest.raises(BaselineError, match=match):
        promote(store, **kw)
    assert not store.path.exists()


def test_an_empty_approver_list_refuses_promotion(store):
    refused(store, "no baseline approvers", approvers=[])
    assert not store.path.exists()


def test_a_name_that_is_not_an_approver_is_refused(store):
    refused(store, "not in baseline_approvers", approved_by="Somebody Else")
    assert not store.path.exists()


@pytest.mark.parametrize("who", ["", "   ", None])
def test_a_blank_approver_is_refused(store, who):
    with pytest.raises((BaselineError, AttributeError, TypeError)):
        promote(store, approved_by=who)
    assert not store.path.exists()


def test_approver_names_match_ignoring_case_and_spaces(store):
    promote(store, approved_by="  lead NAME ")
    assert store.load("Samsung A07") == {"launch_time": 1000.0}


def test_nothing_to_promote_is_refused(store):
    with pytest.raises(BaselineError, match="nothing to promote"):
        promote(store, rows={})


def test_noisy_and_unstable_rows_are_refused_and_every_reason_is_listed(store):
    rows = {"a": [row(noisy=True)], "b": [row(unstable=True, spread=40.0)]}
    with pytest.raises(PromotionRefused) as exc:
        promote(store, rows=rows)
    assert len(exc.value.reasons) == 2
    assert "a/launch_time was measured on a noisy host" in exc.value.reasons
    assert any("b/launch_time was unstable" in r for r in exc.value.reasons)
    assert not store.path.exists()


def test_one_bad_row_blocks_the_good_ones_too(store):
    with pytest.raises(PromotionRefused):
        promote(store, rows={"good": [row()], "bad": [row(noisy=True)]})
    assert store.load("good") == {} and not store.path.exists()


def test_an_existing_baseline_is_not_overwritten_without_permission(store):
    promote(store, rows={"a": [row(median=1.0)]})
    with pytest.raises(PromotionRefused, match="allow_overwrite"):
        promote(store, rows={"a": [row(median=9.0)]})
    assert store.load("a") == {"launch_time": 1.0}


def test_allow_overwrite_replaces_the_value_and_reports_old_and_new(store, caplog):
    promote(store, rows={"a": [row(median=1.0)]})
    with caplog.at_level("WARNING"):
        result = promote(store, rows={"a": [row(median=9.0)]}, allow_overwrite=True)
    assert result == [("a", "launch_time", 1.0, 9.0)]
    assert store.load("a") == {"launch_time": 9.0}
    assert any("1.0 -> 9.0" in r.getMessage() for r in caplog.records)


def test_allow_overwrite_does_not_waive_the_noisy_rule(store):
    promote(store, rows={"a": [row(median=1.0)]})
    with pytest.raises(PromotionRefused):
        promote(store, rows={"a": [row(median=9.0, noisy=True)]}, allow_overwrite=True)
    assert store.load("a") == {"launch_time": 1.0}


def test_promote_reads_the_approvers_from_the_config_by_default(store, tmp_path, monkeypatch):
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(yaml.safe_dump({"baseline_approvers": [LEAD]}), encoding="utf-8")
    monkeypatch.setenv("VMF_PERF_CONFIG", str(cfg))
    store.promote(1, {"a": [row()]}, LEAD)
    assert store.load("a") == {"launch_time": 1000.0}
    cfg.write_text(yaml.safe_dump({"baseline_approvers": []}), encoding="utf-8")
    with pytest.raises(BaselineError, match="no baseline approvers"):
        store.promote(2, {"b": [row()]}, LEAD)


def test_the_repo_config_has_no_approvers_so_nobody_can_promote(store, monkeypatch):
    monkeypatch.delenv("VMF_PERF_CONFIG", raising=False)
    with pytest.raises(BaselineError, match="no baseline approvers"):
        store.promote(1, {"a": [row()]}, "Anyone")


# --- the file ---------------------------------------------------------------------------------------------------------

def test_the_file_is_valid_yaml_with_a_header_and_no_temp_files_left(store):
    promote(store)
    text = store.path.read_text(encoding="utf-8")
    assert text.startswith("# perf_baselines.yaml") and "EMULATOR-RELATIVE" in text
    assert yaml.safe_load(text)["profiles"]["Samsung_A07"]["launch_time"]["value"] == 1000.0
    assert [p.name for p in store.path.parent.iterdir()] == [store.path.name]


def test_a_failed_write_keeps_the_old_file_and_removes_the_temp_file(store, monkeypatch, caplog):
    promote(store, rows={"a": [row(median=1.0)]})
    before = store.path.read_bytes()

    def boom(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(baselines.os, "replace", boom)
    with caplog.at_level("ERROR"), pytest.raises(OSError, match="disk full"):
        promote(store, rows={"b": [row(median=2.0)]})
    assert store.path.read_bytes() == before
    assert [p.name for p in store.path.parent.iterdir()] == [store.path.name]
    assert any(r.exc_info for r in caplog.records)


@pytest.mark.parametrize("text", ["profiles: [1, 2]\n", "just a string\n", "{not: [valid\n"])
def test_a_corrupt_file_is_a_baseline_error_not_a_crash(store, text):
    store.path.write_text(text, encoding="utf-8")
    with pytest.raises(BaselineError):
        store.load("a")
    with pytest.raises(BaselineError):
        promote(store)
    assert store.path.read_text(encoding="utf-8") == text            # untouched


def test_an_empty_file_is_an_empty_store(store):
    store.path.write_text("", encoding="utf-8")
    with pytest.raises(BaselineError):          # no 'profiles' mapping: refuse rather than guess
        store.load("a")


# --- storage-backed store --------------------------------------------------------------------------------------------

class FakeStorageModule:
    def __init__(self):
        self.saved, self.data = [], {}

    def load_baselines(self, profile):
        return self.data.get(profile, {})

    def save_baseline(self, profile, metric, value, *, build_sha256, promoted_by, run_id):
        self.saved.append((profile, metric, value, build_sha256, promoted_by, run_id))
        self.data.setdefault(profile, {})[metric] = value


def test_get_store_uses_storage_only_when_both_functions_exist(tmp_path):
    full = FakeStorageModule()
    assert isinstance(baselines.get_store(full), StorageBaselineStore)
    only_load = types.SimpleNamespace(load_baselines=lambda p: {})
    only_save = types.SimpleNamespace(save_baseline=lambda *a, **k: None)
    neither = types.SimpleNamespace()
    for mod in (only_load, only_save, neither):
        assert isinstance(baselines.get_store(mod, tmp_path / "b.yaml"), YamlBaselineStore)


def test_the_real_storage_module_uses_sqlite_baselines():
    assert isinstance(baselines.get_store(), StorageBaselineStore)


def test_storage_store_loads_through_load_baselines_with_the_sanitised_name():
    mod = FakeStorageModule()
    mod.data["Samsung_A07"] = {"launch_time": 5}
    store = StorageBaselineStore(mod)
    assert store.load("Samsung A07") == {"launch_time": 5.0}
    assert store.load_all(["Samsung A07", "other"]) == {"Samsung_A07".replace("_", " "): {"launch_time": 5.0}} or \
        store.load_all(["Samsung A07", "other"]) == {"Samsung A07": {"launch_time": 5.0}}


def test_storage_store_promotes_through_save_baseline_with_provenance():
    mod = FakeStorageModule()
    store = StorageBaselineStore(mod)
    promote(store, builds={"Samsung A07": "abc"})
    assert mod.saved == [("Samsung_A07", "launch_time", 1000.0, "abc", LEAD, 7)]
    with pytest.raises(PromotionRefused):          # same rules as the YAML store
        promote(store)
    assert len(mod.saved) == 1


def test_storage_store_applies_the_approver_rules_before_saving():
    mod = FakeStorageModule()
    with pytest.raises(BaselineError):
        promote(StorageBaselineStore(mod), approvers=[])
    assert mod.saved == []


# --- the command line --------------------------------------------------------------------------------------------------------

def write_run(results, run_id, profile, *, median=1000.0, noisy=False, unstable=False, sha="sha1"):
    d = results / str(run_id) / profile
    d.mkdir(parents=True)
    meta = {"profile": profile, "build": {"apk": "a.apk", "sha256": sha},
            "metrics": [{"metric": "launch_time", "median": median, "spread_pct": 2.0, "baseline": None,
                         "regression_pct": None, "noisy": noisy, "unstable": unstable}]}
    (d / "perf_meta.json").write_text(json.dumps(meta), encoding="utf-8")


@pytest.fixture
def env(tmp_path, monkeypatch):
    results = tmp_path / "results"
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(yaml.safe_dump({"baseline_approvers": [LEAD]}), encoding="utf-8")
    base = ["--results-dir", str(results), "--baselines-file", str(tmp_path / "bl.yaml"), "--config", str(cfg)]
    monkeypatch.setattr(storage_module, "_DB_PATH", tmp_path / "farm.db")
    storage_module._INITIALIZED_PATHS.clear()
    return types.SimpleNamespace(results=results, base=base, bl=tmp_path / "bl.yaml", cfg=cfg)


def test_cli_promote_then_list(env, capsys):
    write_run(env.results, 5, "Samsung_A07", median=1234.0)
    assert cli.main(env.base + ["promote", "--run-id", "5", "--approved-by", LEAD]) == 0
    out = capsys.readouterr().out
    assert "promoted Samsung_A07/launch_time: None -> 1234.0" in out and "emulator-relative" in out
    assert cli.main(env.base + ["baselines"]) == 0
    listing = capsys.readouterr().out
    assert "Samsung_A07" in listing and "1234.00" in listing and LEAD in listing
    entry = baselines.get_store().all_entries()[0]
    assert entry["build_sha256"] == "sha1" and entry["run_id"] == 5


def test_cli_refuses_a_non_approver_with_exit_2_and_writes_nothing(env, capsys):
    write_run(env.results, 5, "p")
    assert cli.main(env.base + ["promote", "--run-id", "5", "--approved-by", "Intruder"]) == 2
    assert "REFUSED" in capsys.readouterr().out and not env.bl.exists()


def test_cli_refuses_when_no_approvers_are_configured(env, capsys):
    env.cfg.write_text(yaml.safe_dump({"baseline_approvers": []}), encoding="utf-8")
    write_run(env.results, 5, "p")
    assert cli.main(env.base + ["promote", "--run-id", "5", "--approved-by", LEAD]) == 2
    assert "no baseline approvers" in capsys.readouterr().out and not env.bl.exists()


def test_cli_refuses_noisy_runs(env, capsys):
    write_run(env.results, 5, "p", noisy=True)
    assert cli.main(env.base + ["promote", "--run-id", "5", "--approved-by", LEAD]) == 2
    assert "noisy" in capsys.readouterr().out and not env.bl.exists()


def test_cli_overwrite_needs_the_flag(env, capsys):
    write_run(env.results, 5, "p", median=1.0)
    write_run(env.results, 6, "p", median=2.0)
    assert cli.main(env.base + ["promote", "--run-id", "5", "--approved-by", LEAD]) == 0
    assert cli.main(env.base + ["promote", "--run-id", "6", "--approved-by", LEAD]) == 2
    assert baselines.get_store().load("p") == {"launch_time": 1.0}
    assert cli.main(env.base + ["promote", "--run-id", "6", "--approved-by", LEAD, "--allow-overwrite"]) == 0
    assert baselines.get_store().load("p") == {"launch_time": 2.0}
    assert "1.0 -> 2.0" in capsys.readouterr().out


def test_cli_profile_filter_and_missing_profile(env, capsys):
    write_run(env.results, 5, "a", median=1.0)
    write_run(env.results, 5, "b", median=2.0)
    assert cli.main(env.base + ["promote", "--run-id", "5", "--approved-by", LEAD, "--profile", "a"]) == 0
    store = baselines.get_store()
    assert store.load("a") == {"launch_time": 1.0} and store.load("b") == {}
    assert cli.main(env.base + ["promote", "--run-id", "5", "--approved-by", LEAD, "--profile", "zzz"]) == 2
    assert "zzz" in capsys.readouterr().out


def test_cli_run_without_perf_meta_is_refused(env, capsys):
    assert cli.main(env.base + ["promote", "--run-id", "99", "--approved-by", LEAD]) == 2
    assert "no perf_meta.json" in capsys.readouterr().out


def test_cli_lists_an_empty_store(env, capsys):
    assert cli.main(env.base + ["baselines"]) == 0
    assert "no baselines stored" in capsys.readouterr().out


def test_cli_requires_a_command_and_an_approver_name(env):
    with pytest.raises(SystemExit):
        cli.main(env.base)
    with pytest.raises(SystemExit):
        cli.main(env.base + ["promote", "--run-id", "5"])


def test_read_run_rows_builds_perf_rows_and_build_hashes(env):
    write_run(env.results, 5, "a", median=3.0, sha="s")
    rows, builds = cli.read_run_rows(env.results, 5)
    assert rows["a"][0] == PerfRow("launch_time", 3.0, 2.0, None, None, False, False) and builds == {"a": "s"}


# --- added: profile-level overwrite rule and a write that fails half way -------------------------------------------

def test_a_profile_with_any_baseline_needs_allow_overwrite_even_for_a_new_metric(store):
    promote(store, rows={"a": [row("launch_time", 1.0)]})
    with pytest.raises(PromotionRefused, match="a already has a baseline"):
        promote(store, rows={"a": [row("pss", 5.0)]})
    assert store.load("a") == {"launch_time": 1.0}
    result = promote(store, rows={"a": [row("pss", 5.0)]}, allow_overwrite=True)
    assert result == [("a", "pss", None, 5.0)]
    assert store.load("a") == {"launch_time": 1.0, "pss": 5.0}


def test_a_write_that_fails_half_way_leaves_the_old_file_intact(store, monkeypatch):
    promote(store, rows={"a": [row(median=1.0)]})
    before = store.path.read_bytes()

    def half(data, stream, **kw):
        stream.write("profiles:\n  b:\n    launch_ti")      # a truncated document
        raise OSError("disk full")
    monkeypatch.setattr(baselines.yaml, "safe_dump", half)
    with pytest.raises(OSError, match="disk full"):
        promote(store, rows={"b": [row(median=2.0)]})
    assert store.path.read_bytes() == before
    assert store.load("a") == {"launch_time": 1.0}
    assert [p.name for p in store.path.parent.iterdir()] == [store.path.name]


def test_promoted_entries_record_who_when_run_and_build(store):
    promote(store, rows={"Samsung A07": [row(median=1234.0)]}, builds={"Samsung A07": "ab" * 32})
    (entry,) = store.all_entries()
    assert entry == {"profile": "Samsung_A07", "metric": "launch_time", "value": 1234.0,
                     "build_sha256": "ab" * 32, "promoted_by": LEAD,
                     "promoted_on": NOW.isoformat(timespec="seconds"), "run_id": 7}
