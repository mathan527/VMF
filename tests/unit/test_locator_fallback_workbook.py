# ruff: noqa: F811  (fixtures imported from test_locator_fallback are used as test arguments)
"""Workbook M14.2 additions: action tags, unresolved records, per-profile counts, and the
optional vision input (OFF by default). Helpers and the runner fixture are shared with
test_locator_fallback.py. No device, no model, no network.
"""
import base64
import json
import logging
from types import SimpleNamespace

import pytest
from test_locator_fallback import (FIXTURES, CFG, HEAL_STEP, PKG, STEP, FakeDriver, FakeOllama, answer, case, n, ui,  # noqa: F401
                                   env, resolve, run_it, session)

from vmf.runner import locator_fallback as lf

VISION_CFG = lf.FallbackConfig(enabled=True, vision_model="llava:7b")


def sidecar(env):
    return json.loads((env.results / "31" / "p1" / "locator_fallbacks.json").read_text(encoding="utf-8"))


def png(tmp_path, size=1000, name="shot.png"):
    p = tmp_path / name
    p.write_bytes(b"\x89PNG" + b"x" * (size - 4))
    return str(p)


# --- action tags -------------------------------------------------------------------------------------------

def test_the_tags_are_the_workbook_ones_and_strip_tag_removes_them():
    assert lf.TAG_AI_ASSISTED == " [AI-ASSISTED]" and lf.TAG_UNRESOLVED == " [UNRESOLVED]"
    assert lf.strip_tag("tap [AI-ASSISTED]") == "tap" and lf.strip_tag("tap [UNRESOLVED]") == "tap"
    assert lf.strip_tag("tap") == "tap" and lf.strip_tag("oem.dnd_on") == "oem.dnd_on"
    assert lf.strip_tag("tap [AI-ASSISTED] later") == "tap [AI-ASSISTED] later"       # only a trailing tag


def test_a_healed_step_is_stored_with_the_ai_assisted_tag_and_stays_a_pass(env, monkeypatch):
    monkeypatch.setattr(lf, "ollama_post", FakeOllama(answer(0)))
    _, steps = run_it(env, [HEAL_STEP], locator_fallback=CFG)
    assert [(s.action, s.status) for s in steps] == [("tap [AI-ASSISTED]", "pass")]
    assert [s.action for s in env.saved] == ["tap [AI-ASSISTED]"]
    assert lf.strip_tag(steps[0].action) == "tap"


def test_an_unresolved_step_is_stored_with_the_unresolved_tag_and_stays_a_fail(env, monkeypatch):
    monkeypatch.setattr(lf, "ollama_post", FakeOllama(answer(-1)))
    _, steps = run_it(env, [HEAL_STEP], locator_fallback=CFG)
    assert [(s.action, s.status) for s in steps] == [("tap [UNRESOLVED]", "fail")]
    data = sidecar(env)
    assert data["healed"] == [] and [u["code"] for u in data["unresolved"]] == ["none_matched"]
    assert lf.read_fallback_counts(str(env.results / "31" / "p1")) == {"ai_assisted": 0, "unresolved": 1}


def test_a_failed_retry_is_unresolved_not_ai_assisted(env, monkeypatch):
    monkeypatch.setattr(lf, "ollama_post", FakeOllama(answer(0)))
    env.fail_new_locator = True
    _, steps = run_it(env, [HEAL_STEP], locator_fallback=CFG)
    assert [(s.action, s.status) for s in steps] == [("tap [UNRESOLVED]", "fail")]
    data = sidecar(env)
    assert data["healed"] == [] and data["unresolved"][0]["code"] == "retry_failed"


def test_an_ollama_outage_on_an_opted_in_step_is_unresolved(env, monkeypatch):
    monkeypatch.setattr(lf, "ollama_post", FakeOllama(raises=lf.OllamaError("ollama_unreachable", "down")))
    _, steps = run_it(env, [HEAL_STEP], locator_fallback=CFG)
    assert [s.action for s in steps] == ["tap [UNRESOLVED]"]
    assert lf.read_fallback_counts(str(env.results / "31" / "p1")) == {"ai_assisted": 0, "unresolved": 1}


def test_steps_that_did_not_use_the_fallback_keep_their_plain_action_name(env, monkeypatch):
    monkeypatch.setattr(lf, "ollama_post", FakeOllama(answer(0)))
    plain = {"action": "tap", "locator": {"resource_id": "com.shop.app:id/btn_sign_in"}}
    assert_step = {"action": "assert_visible", "locator": {"resource_id": "com.shop.app:id/nothing"},
                   "fallback": True, "intent": "x"}
    _, steps = run_it(env, [plain, assert_step], locator_fallback=CFG)
    assert [s.action for s in steps] == ["tap", "assert_visible"] and steps[1].status == "fail"
    assert not (env.results / "31" / "p1" / "locator_fallbacks.json").exists()


def test_with_the_fallback_off_the_action_names_are_untouched(env, monkeypatch):
    monkeypatch.setattr(lf, "ollama_post", FakeOllama(answer(0)))
    _, steps = run_it(env, [HEAL_STEP])
    assert [s.action for s in steps] == ["tap"]


def test_only_the_step_that_used_the_fallback_is_tagged(env, monkeypatch):
    monkeypatch.setattr(lf, "ollama_post", FakeOllama(answer(0)))
    ok = {"action": "tap", "locator": {"resource_id": "com.shop.app:id/btn_sign_in"}}
    _, steps = run_it(env, [ok, HEAL_STEP, ok], locator_fallback=CFG)
    assert [s.action for s in steps] == ["tap", "tap [AI-ASSISTED]", "tap"]


def test_session_tag_reports_the_state_per_step(tmp_path):
    s = session(tmp_path, http=FakeOllama(answer(0)))
    driver = SimpleNamespace(page_source=case("renamed_id")["hierarchy"])
    assert s.tag(0) == ""
    outcome = s.heal(driver, STEP, 0)
    s.confirm_healed(0, STEP, outcome)
    assert s.tag(0) == lf.TAG_AI_ASSISTED and s.tag(1) == ""


def test_the_budget_running_out_on_an_opted_in_step_is_recorded_unresolved(tmp_path):
    cfg = lf.FallbackConfig(enabled=True, max_fallbacks_per_run=1)
    s = session(tmp_path, config=cfg, http=FakeOllama(answer(-1)))
    driver = SimpleNamespace(page_source=case("renamed_id")["hierarchy"])
    s.heal(driver, STEP, 0)
    s.heal(driver, STEP, 1)
    assert [u["code"] for u in s.unresolved] == ["none_matched", "budget_exhausted"]
    assert s.tag(1) == lf.TAG_UNRESOLVED


def test_ineligible_steps_are_never_recorded_as_unresolved(tmp_path):
    s = session(tmp_path, http=FakeOllama(answer(0)))
    driver = SimpleNamespace(page_source=case("renamed_id")["hierarchy"])
    s.heal(driver, {**STEP, "action": "assert_visible"}, 0)
    s.heal(driver, {k: v for k, v in STEP.items() if k != "fallback"}, 1)
    assert s.unresolved == [] and s.tag(0) == "" and s.tag(1) == ""


def test_read_fallback_counts_handles_missing_and_corrupt_files(tmp_path, caplog):
    zero = {"ai_assisted": 0, "unresolved": 0}
    assert lf.read_fallback_counts(str(tmp_path / "nothing")) == zero
    (tmp_path / "locator_fallbacks.json").write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.ERROR):
        assert lf.read_fallback_counts(str(tmp_path)) == zero
    assert any(r.exc_info for r in caplog.records)
    (tmp_path / "locator_fallbacks.json").write_text(json.dumps({"healed": [1, 2], "unresolved": [3]}),
                                                     encoding="utf-8")
    assert lf.read_fallback_counts(str(tmp_path)) == {"ai_assisted": 2, "unresolved": 1}


# --- optional vision input (OFF by default) ---------------------------------------------------------------------

def test_vision_is_off_by_default_and_nothing_enables_it_implicitly():
    assert lf.FallbackConfig().vision_model is None and CFG.vision_model is None
    assert lf.config_from_options({"locator_fallback": True}, environ={}).vision_model is None
    assert lf.config_from_options({}, environ={"VMF_LOCATOR_FALLBACK": "1"}).vision_model is None


def test_the_vision_model_comes_from_its_environment_variable():
    c = lf.config_from_options({"locator_fallback": True}, environ={"VMF_OLLAMA_VISION_MODEL": "llava:7b"})
    assert c.vision_model == "llava:7b" and lf.VISION_MODEL_ENV == "VMF_OLLAMA_VISION_MODEL"


def test_a_blank_vision_model_is_rejected():
    with pytest.raises(ValueError, match="vision_model"):
        lf.FallbackConfig(enabled=True, vision_model="  ")


def test_an_image_is_sent_only_when_a_vision_model_is_configured():
    cands = lf.candidates(case("renamed_id")["hierarchy"], PKG)
    plain = lf.build_request(CFG, "tap Login", {"resource_id": "x"}, cands)
    assert all("images" not in m for m in plain["messages"]) and plain["model"] == CFG.model
    with_vision = lf.build_request(VISION_CFG, "tap Login", {"resource_id": "x"}, cands, image_b64="QUJD")
    assert with_vision["messages"][-1]["images"] == ["QUJD"] and with_vision["model"] == "llava:7b"
    ignored = lf.build_request(CFG, "tap Login", {"resource_id": "x"}, cands, image_b64="QUJD")
    assert all("images" not in m for m in ignored["messages"]) and ignored["model"] == CFG.model
    no_image = lf.build_request(VISION_CFG, "tap Login", {"resource_id": "x"}, cands)
    assert all("images" not in m for m in no_image["messages"]) and no_image["model"] == CFG.model


def test_resolve_sends_the_screenshot_as_base64_and_still_validates_the_index(tmp_path):
    path = png(tmp_path)
    spy = FakeOllama(answer(0))
    out = resolve(case("renamed_id")["hierarchy"], http=spy, config=VISION_CFG, image_path=path)
    assert isinstance(out, lf.Resolution)
    call = spy.calls[0]
    with open(path, "rb") as f:
        assert call["body"]["messages"][-1]["images"] == [base64.b64encode(f.read()).decode("ascii")]
    assert call["body"]["model"] == "llava:7b"
    bad = resolve(case("renamed_id")["hierarchy"], http=FakeOllama(answer(99)), config=VISION_CFG, image_path=path)
    assert isinstance(bad, lf.NoMatch)           # an index outside the list is rejected as for a text model


def test_no_image_is_sent_without_a_vision_model_even_if_a_path_is_given(tmp_path):
    spy = FakeOllama(answer(0))
    resolve(case("renamed_id")["hierarchy"], http=spy, config=CFG, image_path=png(tmp_path))
    assert all("images" not in m for m in spy.calls[0]["body"]["messages"])


def test_a_screenshot_over_2_mb_is_skipped_with_a_warning(tmp_path, caplog):
    big = png(tmp_path, size=lf.MAX_SCREENSHOT_BYTES + 1, name="big.png")
    spy = FakeOllama(answer(0))
    with caplog.at_level(logging.WARNING):
        out = resolve(case("renamed_id")["hierarchy"], http=spy, config=VISION_CFG, image_path=big)
    assert isinstance(out, lf.Resolution)
    assert all("images" not in m for m in spy.calls[0]["body"]["messages"])
    assert any("limit" in r.getMessage() for r in caplog.records)
    assert lf._load_image(png(tmp_path, size=lf.MAX_SCREENSHOT_BYTES, name="exact.png")) is not None


def test_a_missing_or_unreadable_screenshot_never_fails_the_heal(tmp_path, caplog, monkeypatch):
    spy = FakeOllama(answer(0))
    with caplog.at_level(logging.WARNING):
        out = resolve(case("renamed_id")["hierarchy"], http=spy, config=VISION_CFG,
                      image_path=str(tmp_path / "gone.png"))
    assert isinstance(out, lf.Resolution) and all("images" not in m for m in spy.calls[0]["body"]["messages"])
    path = png(tmp_path)

    def denied(_):
        raise OSError("denied")
    monkeypatch.setattr(lf.os.path, "getsize", denied)
    caplog.clear()
    with caplog.at_level(logging.ERROR):
        assert lf._load_image(path) is None
    assert any(r.exc_info for r in caplog.records)
    assert lf._load_image(None) is None


def test_the_session_sends_the_previous_steps_screenshot_only_with_a_vision_model(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "step_00_screen.png").write_bytes(b"\x89PNGdata")
    driver = SimpleNamespace(page_source=case("renamed_id")["hierarchy"])
    spy = FakeOllama(answer(0))
    s = session(tmp_path, config=VISION_CFG, http=spy)
    assert s._image_for(0) is None and s._image_for(1).endswith("step_00_screen.png")
    s.heal(driver, STEP, 1)
    assert "images" in spy.calls[0]["body"]["messages"][-1]
    spy2 = FakeOllama(answer(0))
    plain = session(tmp_path, config=CFG, http=spy2)
    assert plain._image_for(1) is None
    plain.heal(driver, STEP, 1)
    assert all("images" not in m for m in spy2.calls[0]["body"]["messages"])


# --- added: heuristic heal, non-opted steps, sidecar fields, determinism ---------------------------------------

def test_a_heuristic_heal_is_also_tagged_ai_assisted_without_calling_the_model(env, monkeypatch):
    c = case("two_similar_buttons")
    env.driver = FakeDriver(c["hierarchy"])
    spy = FakeOllama(answer(0))
    monkeypatch.setattr(lf, "ollama_post", spy)
    step = {"action": c["action"], "locator": c["locator"], "intent": c["intent"], "fallback": True}
    _, steps = run_it(env, [step], locator_fallback=CFG)
    assert [(s.action, s.status) for s in steps] == [(c["action"] + " [AI-ASSISTED]", "pass")]
    assert spy.calls == [] and sidecar(env)["healed"][0]["method"] == "heuristic"


def test_a_failing_step_that_did_not_opt_in_is_not_tagged(env, monkeypatch):
    spy = FakeOllama(answer(0))
    monkeypatch.setattr(lf, "ollama_post", spy)
    not_opted = {k: v for k, v in HEAL_STEP.items() if k != "fallback"}
    _, steps = run_it(env, [not_opted], locator_fallback=CFG)
    assert [(s.action, s.status) for s in steps] == [("tap", "fail")] and spy.calls == []
    assert not (env.results / "31" / "p1" / "locator_fallbacks.json").exists()


def test_the_sidecar_records_each_unresolved_attempt_with_step_intent_code_and_candidates(env, monkeypatch):
    monkeypatch.setattr(lf, "ollama_post", FakeOllama(answer(-1)))
    run_it(env, [{"action": "back"}, HEAL_STEP], locator_fallback=CFG)
    (u,) = sidecar(env)["unresolved"]
    assert u["step"] == 1 and u["intent"] == HEAL_STEP["intent"] and u["code"] == "none_matched"
    assert isinstance(u["candidate_count"], int) and u["candidate_count"] > 0


def test_the_same_scenario_gives_the_same_actions_in_the_same_order(env, monkeypatch):
    monkeypatch.setattr(lf, "ollama_post", FakeOllama(answer(0)))
    ok = {"action": "tap", "locator": {"resource_id": "com.shop.app:id/btn_sign_in"}}
    scenario_steps = [ok, HEAL_STEP, {"action": "back"}, ok]
    _, first = run_it(env, scenario_steps, locator_fallback=CFG)
    _, second = run_it(env, scenario_steps, locator_fallback=CFG)
    assert [s.action for s in first] == [s.action for s in second]
    assert [lf.strip_tag(s.action) for s in first] == [s["action"] for s in scenario_steps]
    _, without = run_it(env, scenario_steps)      # fallback off: same names once the tag is stripped
    assert [lf.strip_tag(s.action) for s in first] == [s.action for s in without]


# --- the model is asked twice, with the candidates in opposite orders (14 Oct accuracy run) ---------------

class SeqOllama:
    """Answers from a fixed list, one per call, and records the requests."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, url, body, timeout_s):
        self.calls.append({"url": url, "body": body, "timeout_s": timeout_s})
        return self.answers[len(self.calls) - 1]


def test_the_second_question_lists_the_candidates_in_reverse_with_new_indexes():
    c = case("renamed_id")                                   # 3 candidates
    spy = FakeOllama(answer(0))
    out = resolve(c["hierarchy"], http=spy, locator=c["locator"], intent=c["intent"])
    assert isinstance(out, lf.Resolution) and len(spy.calls) == 2
    first, second = (lf_candidates(call["body"]) for call in spy.calls)
    assert [x["index"] for x in first] == [0, 1, 2] and [x["index"] for x in second] == [0, 1, 2]
    key = lambda x: (x["resource_id"], x["text"], x["content_desc"])          # noqa: E731
    assert [key(x) for x in second] == [key(x) for x in reversed(first)]


def lf_candidates(body):
    text = body["messages"][-1]["content"]
    return json.loads(text.split("<<<DATA" + chr(10))[1].split(chr(10) + "DATA>>>")[0])


def test_a_model_that_follows_list_position_instead_of_the_element_is_refused():
    c = case("renamed_id")
    spy = FakeOllama(answer(0), consistent=False)               # says "0" for both orders
    out = resolve(c["hierarchy"], http=spy, locator=c["locator"], intent=c["intent"])
    assert isinstance(out, lf.NoMatch) and out.code == "inconsistent" and len(spy.calls) == 2


def test_a_second_answer_of_minus_one_is_refused():
    c = case("renamed_id")
    out = resolve(c["hierarchy"], http=SeqOllama(answer(0), answer(-1)), locator=c["locator"], intent=c["intent"])
    assert isinstance(out, lf.NoMatch) and out.code == "inconsistent"


def test_a_first_answer_of_minus_one_is_final_and_asks_nothing_more():
    c = case("renamed_id")
    spy = SeqOllama(answer(-1))
    out = resolve(c["hierarchy"], http=spy, locator=c["locator"], intent=c["intent"])
    assert isinstance(out, lf.NoMatch) and out.code == "none_matched" and len(spy.calls) == 1


def test_the_lower_of_the_two_confidences_is_used_and_checked():
    c = case("renamed_id")                                   # candidates: 3, so the mirror of 0 is 2
    low = resolve(c["hierarchy"], http=SeqOllama(answer(0, 0.95), answer(2, 0.30)),
                  locator=c["locator"], intent=c["intent"])
    assert isinstance(low, lf.NoMatch) and low.code == "low_confidence"
    ok = resolve(c["hierarchy"], http=SeqOllama(answer(0, 0.95), answer(2, 0.70)),
                 locator=c["locator"], intent=c["intent"])
    assert isinstance(ok, lf.Resolution) and ok.confidence == 0.70


def test_a_single_candidate_cannot_be_cross_checked_and_is_refused():
    xml = ui(n(rid="a:id/only", text="Continue", bounds=(0, 100, 500, 200)))
    spy = FakeOllama(answer(0))
    out = resolve(xml, http=spy, locator={"resource_id": "a:id/gone"}, intent="tap the Login button")
    assert isinstance(out, lf.NoMatch) and out.code == "inconsistent" and len(spy.calls) == 1


def test_an_error_on_the_second_question_is_reported_like_any_other_ollama_error():
    c = case("renamed_id")

    class SecondFails(SeqOllama):
        def __call__(self, url, body, timeout_s):
            if self.calls:
                self.calls.append(body)
                raise lf.OllamaError("ollama_timeout", "no answer")
            return super().__call__(url, body, timeout_s)
    out = resolve(c["hierarchy"], http=SecondFails(answer(0)), locator=c["locator"], intent=c["intent"])
    assert isinstance(out, lf.NoMatch) and out.code == "ollama_timeout"


def test_every_absent_fixture_is_refused_by_a_model_that_always_picks_the_first_candidate():
    """The 14 Oct run: qwen2.5:1.5b healed 10 of 10 absent-element screens to a wrong element
    (confidence 1.0). A position-following model must now give no match on all of them."""
    absent = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(FIXTURES.glob("*.json"))]
    absent = [c for c in absent if c["expected"] is None]
    assert len(absent) >= 10
    for c in absent:
        out = lf.resolve(config=CFG, action=c["action"], intent=c["intent"], locator=c["locator"],
                         hierarchy_xml=c["hierarchy"], package=c["package"],
                         http_post=FakeOllama(answer(0, 1.0), consistent=False), use_heuristic=False)
        assert isinstance(out, lf.NoMatch), c["name"]
