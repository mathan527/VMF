"""Tests for vmf.geometry (M7). No device required.

Fixtures assume a 720x1600 screen at 320 dpi, so 48 dp = 96 px.
"""
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from vmf import geometry
from vmf.geometry import geometry as geo
from vmf.geometry import (check, get_display_zones, parse_cutout_rects,
                          parse_gesture_rects, validate_geometry)
from vmf.results import Finding

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "hierarchy"
PROFILE = {"width_px": 720, "height_px": 1600, "density_dpi": 320}
NOTCH = (300, 0, 420, 60)
GESTURE = (0, 1500, 720, 1600)


def _xml(name):
    return (FIX / name).read_text(encoding="utf-8")


def _kinds(findings):
    return [f.kind for f in findings]


# --- correct and broken layouts ---------------------------------------------

def test_clean_layout_has_no_findings():
    profile = dict(PROFILE, cutout_rects=[NOTCH], gesture_rects=[GESTURE])
    assert check(_xml("clean.xml"), profile) == []


def test_off_screen_button():
    findings = check(_xml("offscreen.xml"), PROFILE)
    assert _kinds(findings) == ["off_screen"]
    assert findings[0].element_id == "app:id/wide"
    assert findings[0].bounds == (500, 700, 760, 820)
    assert "right by 40px" in findings[0].detail


def test_off_screen_inside_scrollable_is_ignored():
    assert check(_xml("offscreen_in_scrollable.xml"), PROFILE) == []


def test_overlapping_siblings():
    findings = check(_xml("overlap.xml"), PROFILE)
    assert _kinds(findings) == ["overlap"]
    assert "app:id/left" in findings[0].detail and "app:id/right" in findings[0].detail


def test_nested_clickable_is_not_an_overlap():
    assert check(_xml("nested_clickable.xml"), PROFILE) == []


def test_small_tap_target_reports_measured_dp():
    findings = check(_xml("small_tap_target.xml"), PROFILE)
    assert _kinds(findings) == ["tap_target"]
    assert "30.0 dp" in findings[0].detail


def test_tap_target_exactly_48dp_is_fine():
    xml = _xml("small_tap_target.xml").replace("[100,400][160,460]", "[100,400][196,496]")
    assert check(xml, PROFILE) == []


def test_button_under_notch():
    findings = check(_xml("under_notch.xml"), dict(PROFILE, cutout_rects=[NOTCH]))
    assert _kinds(findings) == ["cutout_overlap"]
    assert "display cutout" in findings[0].detail


def test_button_under_gesture_bar():
    findings = check(_xml("under_gesture_bar.xml"), dict(PROFILE, gesture_rects=[GESTURE]))
    assert _kinds(findings) == ["cutout_overlap"]
    assert "gesture bar zone" in findings[0].detail


def test_touching_edges_is_not_a_cutout_overlap():
    # button spans x 260..460; the rectangle starts exactly at x=460
    profile = dict(PROFILE, cutout_rects=[(460, 0, 560, 60)])
    assert check(_xml("under_notch.xml"), profile) == []


def test_disabled_element_is_not_checked():
    xml = _xml("small_tap_target.xml").replace('clickable="true" enabled="true"',
                                              'clickable="true" enabled="false"')
    assert check(xml, PROFILE) == []


# --- profile handling ---------------------------------------------------------

def test_missing_density_skips_tap_target_but_other_checks_run(caplog):
    profile = {"width_px": 720, "height_px": 1600}
    with caplog.at_level("WARNING"):
        assert check(_xml("small_tap_target.xml"), profile) == []
        findings = check(_xml("overlap.xml"), profile)
    assert _kinds(findings) == ["overlap"]
    assert any("skipping tap-target" in r.message for r in caplog.records)


def test_density_alias_is_accepted():
    profile = {"width_px": 720, "height_px": 1600, "density": 320}
    assert _kinds(check(_xml("small_tap_target.xml"), profile)) == ["tap_target"]


def test_resolution_string_is_accepted():
    profile = {"resolution": "720x1600", "density_dpi": 320}
    assert _kinds(check(_xml("offscreen.xml"), profile)) == ["off_screen"]


def test_size_is_inferred_from_the_root_when_absent(caplog):
    with caplog.at_level("INFO"):
        findings = check(_xml("offscreen.xml"), {"density_dpi": 320})
    assert _kinds(findings) == ["off_screen"]
    assert any("using root node bounds 720x1600" in r.message for r in caplog.records)


def test_invalid_zone_rect_is_ignored(caplog):
    profile = dict(PROFILE, cutout_rects=[("a", 0, 1, 1), NOTCH])
    with caplog.at_level("WARNING"):
        findings = check(_xml("under_notch.xml"), profile)
    assert _kinds(findings) == ["cutout_overlap"]
    assert any("invalid cutout_rects" in r.message for r in caplog.records)


# --- parsing robustness ---------------------------------------------------------

def test_empty_xml_returns_empty_list_with_warning(caplog):
    with caplog.at_level("WARNING"):
        assert check("", PROFILE) == []
        assert check("   \n ", PROFILE) == []
    assert sum("empty hierarchy" in r.message for r in caplog.records) == 2


def test_malformed_xml_raises_value_error():
    with pytest.raises(ValueError, match="malformed hierarchy XML"):
        check("<hierarchy><node>", PROFILE)


def test_nodes_without_usable_bounds_are_skipped():
    xml = """<hierarchy>
      <node class="android.widget.FrameLayout" bounds="[0,0][720,1600]">
        <node class="android.widget.Button" clickable="true" enabled="true"/>
        <node class="android.widget.Button" clickable="true" enabled="true" bounds="garbage"/>
        <node class="android.widget.Button" clickable="true" enabled="true" bounds="[10,10][10,50]"/>
        <node class="android.widget.Button" clickable="true" enabled="true" bounds="[10,10][50,10]"/>
      </node>
    </hierarchy>"""
    assert check(xml, PROFILE) == []


def test_hierarchy_without_nodes_returns_empty_list():
    assert check("<hierarchy/>", PROFILE) == []


def test_element_id_falls_back_to_class_and_document_index():
    assert check(_xml("small_tap_target.xml"), PROFILE)[0].element_id == "app:id/close"
    xml = _xml("small_tap_target.xml").replace('resource-id="app:id/close"', 'resource-id=""')
    assert check(xml, PROFILE)[0].element_id == "android.widget.ImageButton[2]"


def test_displayed_false_is_ignored_for_off_screen():
    xml = _xml("offscreen.xml").replace('resource-id="app:id/wide"',
                                        'resource-id="app:id/wide" displayed="false"')
    assert check(xml, PROFILE) == []


def test_real_hierarchy_sample_parses():
    findings = check(_xml("real_sample.xml"), {})
    assert isinstance(findings, list)
    assert all(isinstance(f, Finding) for f in findings)


# --- determinism and wrapper ------------------------------------------------------

def test_output_is_deterministic_and_sorted():
    xml = """<hierarchy>
      <node class="android.widget.FrameLayout" bounds="[0,0][720,1600]">
        <node class="android.widget.Button" resource-id="b" clickable="true" enabled="true" bounds="[600,10][700,200]"/>
        <node class="android.widget.Button" resource-id="a" clickable="true" enabled="true" bounds="[100,10][200,200]"/>
        <node class="android.widget.Button" resource-id="z" clickable="true" enabled="true" bounds="[650,700][790,820]"/>
      </node>
    </hierarchy>"""
    profile = dict(PROFILE, cutout_rects=[(0, 0, 720, 100), (0, 0, 720, 100)])
    first = check(xml, profile)
    assert first == check(xml, profile)
    keys = [(f.kind, f.element_id, f.bounds) for f in first]
    assert keys == sorted(keys)
    assert len(first) == len({(f.kind, f.element_id, f.bounds, f.detail) for f in first})


def test_validate_geometry_wrapper_keeps_old_call_shape():
    assert validate_geometry("<hierarchy/>", {"cutout": "notch"}) == []
    assert validate_geometry(_xml("overlap.xml"), PROFILE) == check(_xml("overlap.xml"), PROFILE)


def test_package_exports():
    for name in ("check", "validate_geometry", "parse_cutout_rects",
                 "parse_gesture_rects", "get_display_zones"):
        assert name in geometry.__all__
        assert callable(getattr(geometry, name))


# --- dumpsys parsers -----------------------------------------------------------------

DISPLAY_NEW = """Display Devices: size=1
  DisplayDeviceInfo{"Built-in Screen": uniqueId="local:0", 1080 x 2400, modeId 1}
    mCutout=DisplayCutout{insets=Rect(0, 80 - 0, 0) boundingRect={Bounds=[Rect(0, 0 - 0, 0), Rect(448, 0 - 632, 80), Rect(0, 0 - 0, 0), Rect(0, 0 - 0, 0)]} waterfall=Insets{left=0, top=0, right=0, bottom=0}}
    mCutout=DisplayCutout{insets=Rect(0, 80 - 0, 0) boundingRect={Bounds=[Rect(0, 0 - 0, 0), Rect(448, 0 - 632, 80), Rect(0, 0 - 0, 0), Rect(0, 0 - 0, 0)]} waterfall=Insets{left=0, top=0, right=0, bottom=0}}
"""
DISPLAY_OLD = "    cutout=DisplayCutout{insets=Rect(0, 63 - 0, 0) boundingRect=Rect(465, 0 - 615, 63)}\n"
DISPLAY_NONE = "    mCutout=null\n    mBaseDisplayInfo=DisplayInfo{\"Built-in Screen\"}\n"
DISPLAY_ALL_ZERO = ("  mCutout=DisplayCutout{insets=Rect(0, 0 - 0, 0) boundingRect={Bounds="
                    "[Rect(0, 0 - 0, 0), Rect(0, 0 - 0, 0), Rect(0, 0 - 0, 0), Rect(0, 0 - 0, 0)]}}\n")

WINDOW_NEW = """WINDOW MANAGER DISPLAY CONTENTS (dumpsys window displays)
  Display: mDisplayId=0
    init=1080x2400 420dpi cur=1080x2400 app=1080x2400
      InsetsSource type=statusBars frame=[0,0][1080,128] visible=true
      InsetsSource type=navigationBars frame=[0,2274][1080,2400] visible=true
"""
WINDOW_OLD = "      InsetsSource: mType=ITYPE_NAVIGATION_BAR mFrame=[0,2274][1080,2400] mVisible=true\n"


def test_parse_cutout_rects_new_format_dedupes_and_drops_zero_rects():
    assert parse_cutout_rects(DISPLAY_NEW) == [(448, 0, 632, 80)]


def test_parse_cutout_rects_old_format_ignores_insets():
    assert parse_cutout_rects(DISPLAY_OLD) == [(465, 0, 615, 63)]


def test_parse_cutout_rects_no_match_and_all_zero():
    assert parse_cutout_rects(DISPLAY_NONE) == []
    assert parse_cutout_rects(DISPLAY_ALL_ZERO) == []
    assert parse_cutout_rects("") == []


def test_parse_gesture_rects_finds_navigation_bar_frame():
    assert parse_gesture_rects(WINDOW_NEW, (1080, 2400)) == [(0, 2274, 1080, 2400)]
    assert parse_gesture_rects(WINDOW_OLD, (1080, 2400)) == [(0, 2274, 1080, 2400)]


def test_parse_gesture_rects_no_match():
    assert parse_gesture_rects("      InsetsSource type=statusBars frame=[0,0][1080,128]\n",
                               (1080, 2400)) == []
    assert parse_gesture_rects("", (1080, 2400)) == []


def test_parse_gesture_rects_drops_frames_off_screen_or_empty():
    off = "type=navigationBars frame=[0,3000][1080,3100]\n"
    empty = "type=navigationBars frame=[0,2274][0,2274]\n"
    assert parse_gesture_rects(off, (1080, 2400)) == []
    assert parse_gesture_rects(empty, (1080, 2400)) == []


# --- get_display_zones (fake runner, no adb) -------------------------------------------

def _runner(display_out="", window_out="", returncode=0, calls=None):
    def run(argv):
        if calls is not None:
            calls.append(argv)
        out = display_out if argv[-1] == "display" else window_out
        return SimpleNamespace(returncode=returncode, stdout=out)
    return run


def test_get_display_zones_success_and_argv():
    calls = []
    zones = get_display_zones("emulator-5554",
                              runner=_runner(DISPLAY_NEW, WINDOW_NEW, calls=calls))
    assert zones == {"cutout_rects": [(448, 0, 632, 80)],
                     "gesture_rects": [(0, 2274, 1080, 2400)]}
    assert calls == [["adb", "-s", "emulator-5554", "shell", "dumpsys", "display"],
                     ["adb", "-s", "emulator-5554", "shell", "dumpsys", "window"]]


def test_get_display_zones_zones_feed_check():
    zones = get_display_zones("s", runner=_runner(DISPLAY_NEW, WINDOW_NEW))
    xml = _xml("under_notch.xml").replace("[260,0][460,120]", "[500,0][700,120]")
    profile = {"width_px": 1080, "height_px": 2400, "density_dpi": 320, **zones}
    findings = check(xml, profile)
    assert _kinds(findings) == ["cutout_overlap"]
    assert "display cutout" in findings[0].detail


def test_get_display_zones_adb_failure_returns_empty_lists(caplog):
    with caplog.at_level("WARNING"):
        zones = get_display_zones("s", runner=_runner(DISPLAY_NEW, WINDOW_NEW, returncode=1))
    assert zones == {"cutout_rects": [], "gesture_rects": []}
    assert any("exited with 1" in r.message for r in caplog.records)


def test_get_display_zones_timeout_returns_empty_lists(caplog):
    def run(argv):
        raise subprocess.TimeoutExpired(argv, 10)
    with caplog.at_level("WARNING"):
        zones = get_display_zones("s", runner=run)
    assert zones == {"cutout_rects": [], "gesture_rects": []}
    assert any("failed" in r.message for r in caplog.records)


def test_get_display_zones_missing_adb_returns_empty_lists():
    def run(argv):
        raise FileNotFoundError("adb")
    assert get_display_zones("s", runner=run) == {"cutout_rects": [], "gesture_rects": []}


def test_default_runner_uses_subprocess_run_with_timeout():
    from unittest import mock
    with mock.patch.object(geo.subprocess, "run") as run:
        geo._default_runner(["adb", "devices"])
    assert run.call_args.kwargs["timeout"] == geo.ADB_TIMEOUT_S
