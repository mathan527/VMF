"""Tests for Drawback Verification tests V1 to V10 (Module M15).

Owner: Dhatri
Traceability: M15.3, Drawbacks D1–D10, DoD R1–R8.
"""
from __future__ import annotations

from pathlib import Path
import yaml

from scripts.verify_drawbacks import (
    check_v1, check_v2, check_v3, check_v4, check_v5,
    check_v6, check_v7, check_v8, check_v9, check_v10,
    run_all_verification_tests,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = REPO_ROOT / "vmf" / "provision" / "reference-models.yaml"


def test_v1_to_v10_drawback_verification_suite_all_pass():
    results = run_all_verification_tests()
    assert len(results) == 10
    for v_id, res in results.items():
        assert res["passed"] is True, f"Test {v_id} failed: {res['detail']}"


def test_v3_detects_forbidden_whole_brand_claims():
    catalog_data = yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8"))
    models = catalog_data["models"]
    ok, err = check_v3("This report claims all models of Samsung are covered.", models)
    assert ok is False
    assert "whole-brand claim" in err


def test_v4_detects_missing_physical_validation_disclaimer():
    ok, err = check_v4("Report without disclaimers")
    assert ok is False
    assert "missing" in err.lower()


def test_v6_detects_missing_emulator_relative_label():
    ok, err = check_v6("Report without relative labels")
    assert ok is False
    assert "emulator-relative" in err.lower()


def test_v8_detects_chipset_attribution():
    html = "<h2>Performance (emulator-relative)</h2><p>Tested on Qualcomm Snapdragon 8 Gen 3</p>"
    ok, err = check_v8(html)
    assert ok is False
    assert "snapdragon" in err.lower()
