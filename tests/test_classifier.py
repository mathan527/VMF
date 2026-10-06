"""Tests for vmf.classifier (M6). No device required."""
from pathlib import Path
from unittest import mock

from vmf.classifier import classifier
from vmf.classifier.classifier import classify, start_logcat, stop_logcat
from vmf.results import Step

FIX = Path(__file__).parent / "fixtures" / "logcat"
PKG = "com.example.testapp"


def _step(i, action="tap", status="pass", ms=10, shot="", hier=""):
    return Step(step_index=i, action=action, status=status, duration_ms=ms,
                screenshot_path=str(shot), hierarchy_path=str(hier))


def _healthy():
    return [_step(0), _step(1)]


def _evidence_steps(tmp_path, n, ms, hier_bytes=lambda i: b"<h/>"):
    steps = []
    for i in range(n):
        shot = tmp_path / f"s{i}.png"
        hier = tmp_path / f"h{i}.xml"
        shot.write_bytes(b"png")
        hier.write_bytes(hier_bytes(i))
        steps.append(_step(i, ms=ms, shot=shot, hier=hier))
    return steps


def test_clean_pass():
    v = classify(str(FIX / "clean.txt"), _healthy(), package=PKG)
    assert (v.result, v.cause, v.failed_step, v.trigger_behaviour) == ("pass", "", None, None)


def test_crash():
    v = classify(str(FIX / "crash.txt"), _healthy(), package=PKG)
    assert v.result == "crash"
    assert "java.lang.NullPointerException" in v.cause


def test_crash_other_process_is_pass():
    assert classify(str(FIX / "crash_other_process.txt"), _healthy(), package=PKG).result == "pass"


def test_crash_other_process_counts_without_package():
    assert classify(str(FIX / "crash_other_process.txt"), _healthy()).result == "crash"


def test_anr():
    v = classify(str(FIX / "anr.txt"), _healthy(), package=PKG)
    assert v.result == "anr"
    assert f"ANR in {PKG}" in v.cause


def test_crash_beats_anr():
    assert classify(str(FIX / "crash_and_anr.txt"), _healthy(), package=PKG).result == "crash"


def test_process_gone():
    v = classify(str(FIX / "clean.txt"), _healthy(), package=PKG, pid_alive=lambda: False)
    assert (v.result, v.cause) == ("crash", "process gone")


def test_hang(tmp_path):
    steps = _evidence_steps(tmp_path, 4, ms=12_000)
    v = classify(str(FIX / "clean.txt"), steps, package=PKG, pid_alive=lambda: True)
    assert v.result == "hang"


def test_hang_with_dead_process_is_crash(tmp_path):
    steps = _evidence_steps(tmp_path, 4, ms=12_000)
    v = classify(str(FIX / "clean.txt"), steps, package=PKG, pid_alive=lambda: False)
    assert v.result == "crash"


def test_identical_but_under_threshold(tmp_path):
    steps = _evidence_steps(tmp_path, 4, ms=1_000)
    assert classify(str(FIX / "clean.txt"), steps, package=PKG).result == "pass"


def test_changing_hierarchy_is_pass(tmp_path):
    steps = _evidence_steps(tmp_path, 4, ms=12_000, hier_bytes=lambda i: b"<h%d/>" % i)
    assert classify(str(FIX / "clean.txt"), steps, package=PKG).result == "pass"


def test_missing_evidence_files(tmp_path):
    steps = [_step(i, ms=60_000, shot=tmp_path / "no.png", hier=tmp_path / "no.xml")
             for i in range(3)]
    assert classify(str(FIX / "clean.txt"), steps, package=PKG).result == "pass"


def test_missing_logcat_file(tmp_path):
    v = classify(str(tmp_path / "nope.txt"), _healthy(), package=PKG)
    assert v.result == "pass"


def test_failed_step_is_first_fail():
    steps = [_step(0), _step(1, status="fail"), _step(2, status="fail"), _step(3)]
    v = classify(str(FIX / "crash.txt"), steps, package=PKG)
    assert v.failed_step == 1


def test_failed_step_defaults_to_last():
    v = classify(str(FIX / "crash.txt"), _healthy(), package=PKG)
    assert v.failed_step == 1


def test_failed_step_none_for_empty_steps():
    assert classify(str(FIX / "crash.txt"), [], package=PKG).failed_step is None


def test_trigger_behaviour():
    steps = [_step(0), _step(1, action="oem.background_kill"), _step(2, status="fail")]
    assert classify(str(FIX / "crash.txt"), steps, package=PKG).trigger_behaviour == "background_kill"


def test_trigger_behaviour_none_without_oem_step():
    steps = [_step(0), _step(1, status="fail")]
    assert classify(str(FIX / "crash.txt"), steps, package=PKG).trigger_behaviour is None


def test_trigger_ignores_oem_after_failure():
    steps = [_step(0, status="fail"), _step(1, action="oem.doze")]
    assert classify(str(FIX / "crash.txt"), steps, package=PKG).trigger_behaviour is None


def test_legacy_call_still_works():
    assert classify(_healthy()).result == "pass"


def test_start_logcat_argv_and_stop(tmp_path):
    out = tmp_path / "sub" / "logcat.txt"
    with mock.patch.object(classifier.subprocess, "run"), \
            mock.patch.object(classifier.subprocess, "Popen") as popen:
        proc = start_logcat("emulator-5554", str(out))
    assert popen.call_args.args[0] == ["adb", "-s", "emulator-5554", "logcat", "-v", "threadtime"]
    assert out.parent.is_dir()
    stop_logcat(proc)
    proc.terminate.assert_called_once()
    proc.wait.assert_called()


def test_start_logcat_clears_buffer_before_capture(tmp_path):
    calls = mock.Mock()
    with mock.patch.object(classifier.subprocess, "run", calls.run), \
            mock.patch.object(classifier.subprocess, "Popen", calls.Popen):
        start_logcat("emulator-5554", str(tmp_path / "l.txt"))
    names = [c[0] for c in calls.mock_calls]
    assert names == ["run", "Popen"]
    assert calls.run.call_args.args[0] == ["adb", "-s", "emulator-5554", "logcat", "-c"]
    assert calls.run.call_args.kwargs["check"] is False


def test_start_logcat_ignores_clear_failure(tmp_path):
    with mock.patch.object(classifier.subprocess, "run", side_effect=OSError("no adb")), \
            mock.patch.object(classifier.subprocess, "Popen") as popen:
        start_logcat("emulator-5554", str(tmp_path / "l.txt"))
    popen.assert_called_once()


def test_stop_logcat_kills_after_timeout():
    proc = mock.Mock()
    proc.wait.side_effect = [classifier.subprocess.TimeoutExpired("adb", 5), 0]
    stop_logcat(proc)
    proc.terminate.assert_called_once()
    proc.kill.assert_called_once()
    assert proc.wait.call_args_list[0].kwargs["timeout"] == 5


def test_stop_logcat_no_kill_when_terminate_works():
    proc = mock.Mock()
    stop_logcat(proc)
    proc.kill.assert_not_called()


def test_package_exports():
    from vmf import classifier as pkg
    assert pkg.start_logcat is start_logcat and pkg.stop_logcat is stop_logcat


# --- hang: empty evidence -------------------------------------------------

def test_zero_byte_hierarchy_is_not_a_hang(tmp_path):
    steps = _evidence_steps(tmp_path, 4, ms=12_000, hier_bytes=lambda i: b"")
    assert classify(str(FIX / "clean.txt"), steps, package=PKG).result == "pass"


def test_anr_beats_hang(tmp_path):
    steps = _evidence_steps(tmp_path, 4, ms=12_000)
    assert classify(str(FIX / "anr.txt"), steps, package=PKG).result == "anr"


# --- ANR package matching ---------------------------------------------------

def _log(tmp_path, *lines):
    p = tmp_path / "log.txt"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(p)


def _tt(pid, msg, tag="AndroidRuntime", ts="12:00:02.000"):
    return f"10-06 {ts}  {pid}  {pid} E {tag}: {msg}"


def test_anr_other_package_with_same_prefix_does_not_match(tmp_path):
    log = _log(tmp_path, _tt(1000, f"ANR in {PKG}2 (x/.Main)", "ActivityManager"))
    assert classify(log, _healthy(), package=PKG).result == "pass"


def test_anr_exact_package_at_end_of_line_matches(tmp_path):
    log = _log(tmp_path, _tt(1000, f"ANR in {PKG}", "ActivityManager"))
    assert classify(log, _healthy(), package=PKG).result == "anr"


def test_anr_package_followed_by_space_matches(tmp_path):
    log = _log(tmp_path, _tt(1000, f"ANR in {PKG} (x/.Main)", "ActivityManager"))
    assert classify(log, _healthy(), package=PKG).result == "anr"


# --- crash package matching ---------------------------------------------------

def test_crash_in_service_process_is_detected(tmp_path):
    log = _log(tmp_path,
               _tt(5, "FATAL EXCEPTION: main"),
               _tt(5, f"Process: {PKG}:service, PID: 5"),
               _tt(5, "java.lang.RuntimeException: svc"))
    v = classify(log, _healthy(), package=PKG)
    assert v.result == "crash"
    assert "svc" in v.cause


def test_crash_in_other_package_with_same_prefix_ignored(tmp_path):
    log = _log(tmp_path,
               _tt(5, "FATAL EXCEPTION: main"),
               _tt(5, f"Process: {PKG}2, PID: 5"),
               _tt(5, "java.lang.RuntimeException: other"))
    assert classify(log, _healthy(), package=PKG).result == "pass"


# --- crash cause with interleaved lines -----------------------------------

def test_crash_cause_uses_lines_from_the_crashing_pid():
    v = classify(str(FIX / "crash_interleaved.txt"), _healthy(), package=PKG)
    assert v.result == "crash"
    assert v.cause.startswith("java.lang.ArrayIndexOutOfBoundsException")


def test_crash_cause_falls_back_when_no_line_has_the_pid(tmp_path):
    log = _log(tmp_path,
               _tt(5, "FATAL EXCEPTION: main"),
               _tt(5, f"Process: {PKG}, PID: 777"),
               _tt(5, "java.lang.Error: no pid match"))
    assert "no pid match" in classify(log, _healthy(), package=PKG).cause


# --- 5-line window boundary -------------------------------------------------

def _window_log(tmp_path, filler):
    return _log(tmp_path,
                _tt(5, "FATAL EXCEPTION: main"),
                *[_tt(6, f"filler {i}", "Other") for i in range(filler)],
                _tt(5, f"Process: {PKG}, PID: 5"))


def test_process_line_on_fifth_line_counts(tmp_path):
    assert classify(_window_log(tmp_path, 4), _healthy(), package=PKG).result == "crash"


def test_process_line_on_sixth_line_does_not_count(tmp_path):
    assert classify(_window_log(tmp_path, 5), _healthy(), package=PKG).result == "pass"


# --- missing logcat warning -------------------------------------------------

def test_missing_logcat_logs_warning(tmp_path, caplog):
    with caplog.at_level("WARNING"):
        classify(str(tmp_path / "nope.txt"), _healthy(), package=PKG)
    assert any("cannot read logcat" in r.message for r in caplog.records)


# --- pid_alive failure ----------------------------------------------------

def _raises():
    raise RuntimeError("adb gone")


def test_pid_alive_raising_is_unknown_not_crash(caplog):
    with caplog.at_level("WARNING"):
        v = classify(str(FIX / "clean.txt"), _healthy(), package=PKG, pid_alive=_raises)
    assert v.result == "pass"
    assert any("pid_alive raised" in r.message for r in caplog.records)


def test_pid_alive_raising_still_reports_logcat_crash():
    v = classify(str(FIX / "crash.txt"), _healthy(), package=PKG, pid_alive=_raises)
    assert v.result == "crash" and "NullPointerException" in v.cause


# --- trigger behaviour ----------------------------------------------------

def test_last_of_several_oem_steps_is_the_trigger():
    steps = [_step(0, action="oem.doze"), _step(1, action="oem.trim_memory"),
             _step(2, status="fail")]
    assert classify(str(FIX / "crash.txt"), steps, package=PKG).trigger_behaviour == "trim_memory"
