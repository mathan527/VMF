"""Try the M14 locator fallback on one saved hierarchy with a REAL local Ollama.

Example:
    python scripts/try_fallback.py --hierarchy results/12/emulator-5554/step_03_hierarchy.xml \
        --package com.example.testapp --intent "tap the Login button" \
        --locator "id=com.example.testapp:id/btn_login"

Prints the candidate list the model would see, the resolution and the reason.
Needs Ollama running on localhost with the model pulled (`ollama pull <model>`).
"""
import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vmf.runner import locator_fallback as lf  # noqa: E402


def parse_locator(text: str) -> dict:
    kind, _, value = text.partition("=")
    keys = {"id": "resource_id", "text": "text", "desc": "content-desc"}
    if kind not in keys or not value:
        raise SystemExit(f'--locator must look like id=..., text=... or desc=...; got {text!r}')
    return {keys[kind]: value}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--hierarchy", required=True, help="UiAutomator hierarchy XML file")
    p.add_argument("--package", required=True)
    p.add_argument("--intent", required=True, help='for example "tap the Login button"')
    p.add_argument("--locator", required=True, help="the scripted locator that failed: id=..., text=... or desc=...")
    p.add_argument("--action", default="tap", help="tap (default) or type")
    p.add_argument("--host", help=f"default {lf.DEFAULT_HOST} (or $VMF_OLLAMA_HOST); must be localhost")
    p.add_argument("--model", help=f"default {lf.DEFAULT_MODEL} (or $VMF_OLLAMA_MODEL)")
    p.add_argument("--no-heuristic", action="store_true", help="skip the deterministic stage and always ask the model")
    args = p.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    overrides = {k: v for k, v in (("host", args.host), ("model", args.model)) if v}
    config = lf.config_from_options({"locator_fallback": overrides or True})
    xml = Path(args.hierarchy).read_text(encoding="utf-8")
    locator = parse_locator(args.locator)

    cands = lf.candidates(xml, args.package, config.max_candidates)
    print(f"{len(cands)} candidate(s) for package {args.package}:")
    for c in cands:
        print("  ", json.dumps(c.as_json(), ensure_ascii=False))
    if not cands:
        print("No candidates: the hierarchy is empty, unparsable, or has no interactive "
              "element of that package.")
        return 2

    try:
        lf.check_ollama(config)
    except lf.OllamaError as e:
        print(f"\nERROR: {e}")
        return 1

    outcome = lf.resolve(config=config, action=args.action, intent=args.intent, locator=locator,
                         hierarchy_xml=xml, package=args.package, use_heuristic=not args.no_heuristic)
    print()
    if isinstance(outcome, lf.Resolution):
        print(f"RESOLVED via {outcome.method} (confidence {outcome.confidence:.2f}, "
              f"{outcome.duration_ms} ms, {outcome.candidate_count} candidates)")
        print(f"  original locator: {locator}")
        print(f"  new locator:      {outcome.locator}")
        print(f"  reason:           {outcome.reason}")
        return 0
    print(f"NO MATCH: {outcome.code} ({outcome.duration_ms} ms) {outcome.detail}")
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
