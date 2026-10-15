"""Evaluate the M14 locator fallback on the labelled cases in tests/fixtures/locator_drift/.

    python scripts/eval_fallback.py [--model qwen2.5:1.5b] [--only renamed_id]

Runs every case twice with a REAL local Ollama: with the deterministic heuristic
stage and without it (model only). For each case it prints what was resolved and
how long it took, then a summary:

  hit rate            resolved to the expected element (cases that have one)
  wrong-element rate  resolved to a DIFFERENT element, including any resolution of
                      the "element absent" case (the dangerous outcome)
  no-match rate       no resolution although an element was expected
  correct no-match    "element absent" cases that correctly gave no match
  median latency      median time per case, in ms

Needs Ollama on localhost with the model pulled (`ollama pull <model>`).
"""
import argparse
import glob
import json
import logging
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vmf.runner import locator_fallback as lf  # noqa: E402

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "locator_drift"


def load_cases(only: str | None) -> list[dict]:
    cases = [json.loads(Path(p).read_text(encoding="utf-8"))
             for p in sorted(glob.glob(str(FIXTURES / "*.json")))]
    return [c for c in cases if not only or c["name"] == only]


def same_element(resolved: dict, expected: dict) -> bool:
    """True if the resolved locator matches exactly the expected element."""
    nodes = lf.locate(resolved["hierarchy"], resolved["locator"])
    if len(nodes) != 1:
        return False
    node = nodes[0]
    key = {"resource_id": "resource_id", "content-desc": "content_desc", "text": "text"}
    return all(node[key[k]] == v for k, v in expected.items())


def run_mode(cases: list[dict], config, use_heuristic: bool) -> dict:
    rows, latencies = [], []
    hit = wrong = no_match = correct_no_match = expected_cases = 0
    for c in cases:
        outcome = lf.resolve(config=config, action=c["action"], intent=c["intent"],
                             locator=c["locator"], hierarchy_xml=c["hierarchy"],
                             package=c["package"], use_heuristic=use_heuristic)
        latencies.append(outcome.duration_ms)
        expected = c["expected"]
        if expected is not None:
            expected_cases += 1
        if isinstance(outcome, lf.Resolution):
            ok = expected is not None and same_element(
                {"hierarchy": c["hierarchy"], "locator": outcome.locator}, expected)
            if ok:
                hit += 1
            else:
                wrong += 1
            verdict = "HIT" if ok else "WRONG"
            detail = f"{outcome.method} {outcome.locator}"
        else:
            if expected is None:
                correct_no_match += 1
                verdict = "ok (no match)"
            else:
                no_match += 1
                verdict = "no match"
            detail = f"{outcome.code} {outcome.detail}"[:70]
        rows.append((c["name"], verdict, outcome.duration_ms, detail))
    absent = len(cases) - expected_cases
    return {"rows": rows, "hit": hit, "wrong": wrong, "no_match": no_match,
            "correct_no_match": correct_no_match, "expected": expected_cases, "absent": absent,
            "median_ms": statistics.median(latencies) if latencies else 0}


def pct(n: int, d: int) -> str:
    return f"{100 * n / d:.0f}% ({n}/{d})" if d else "n/a"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", help=f"default {lf.DEFAULT_HOST}; must be localhost")
    p.add_argument("--model", help=f"default {lf.DEFAULT_MODEL}")
    p.add_argument("--only", help="run a single case by name")
    args = p.parse_args()
    logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(message)s")

    overrides = {k: v for k, v in (("host", args.host), ("model", args.model)) if v}
    config = lf.config_from_options({"locator_fallback": overrides or True})
    try:
        lf.check_ollama(config)
    except lf.OllamaError as e:
        print(f"ERROR: {e}")
        return 1
    cases = load_cases(args.only)
    if not cases:
        print(f"no cases found in {FIXTURES}")
        return 2
    print(f"model {config.model} at {config.host}, {len(cases)} case(s)\n")

    results = {}
    for label, heuristic in (("WITH heuristic stage", True), ("WITHOUT heuristic (model only)", False)):
        r = results[label] = run_mode(cases, config, heuristic)
        print(f"=== {label} ===")
        for name, verdict, ms, detail in r["rows"]:
            print(f"  {name:26} {verdict:14} {ms:6d} ms  {detail}")
        print()
    print("=== SUMMARY ===")
    print(f"{'':32} {'hit rate':>14} {'wrong-element':>16} {'no-match':>12} {'correct no-match':>18} {'median ms':>10}")
    for label, r in results.items():
        print(f"{label:32} {pct(r['hit'], r['expected']):>14} {pct(r['wrong'], len(cases)):>16} "
              f"{pct(r['no_match'], r['expected']):>12} {pct(r['correct_no_match'], r['absent']):>18} "
              f"{r['median_ms']:>10.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
