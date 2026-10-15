"""python -m vmf.perf — promote and list performance baselines.

    python -m vmf.perf promote --run-id 12 --approved-by "Lead Name" [--profile P ...] [--allow-overwrite]
    python -m vmf.perf baselines

``promote`` is the only way to create or change a baseline and is never run
automatically. It needs ``--approved-by`` to be listed under ``baseline_approvers``
in perf_config.yaml (empty list: promotion is refused). The rows come from
``results/<run_id>/<profile>/perf_meta.json``, written by the orchestrator after
each performance run (storage has no public function to read them back yet).
Every value is EMULATOR-RELATIVE.
"""
import argparse
import json
import logging
import sys
from pathlib import Path

from vmf.config import RESULTS_DIR
from vmf.perf import baselines, perf
from vmf.results import PerfRow

META_NAME = "perf_meta.json"


def read_run_rows(results_dir: str | Path, run_id: int, profiles: list[str] | None = None):
    """``({profile: [PerfRow]}, {profile: apk sha256})`` from the perf_meta.json files of a run."""
    run_dir = Path(results_dir) / str(run_id)
    wanted = {baselines.sanitise_profile(p) for p in profiles} if profiles else None
    rows, builds = {}, {}
    for meta_path in sorted(run_dir.glob(f"*/{META_NAME}")):
        name = meta_path.parent.name
        if wanted is not None and name not in wanted:
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        rows[name] = [PerfRow(m["metric"], m["median"], m["spread_pct"], m.get("baseline"),
                              m.get("regression_pct"), bool(m["noisy"]), bool(m["unstable"]))
                      for m in meta["metrics"]]
        builds[name] = (meta.get("build") or {}).get("sha256")
    if wanted:
        missing = sorted(wanted - set(rows))
        if missing:
            raise baselines.BaselineError(f"no {META_NAME} for profile(s) {missing} in run {run_id}")
    return rows, builds


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vmf.perf", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--results-dir", default=RESULTS_DIR)
    parser.add_argument("--baselines-file", help="perf_baselines.yaml to use (default: repo root)")
    parser.add_argument("--config", help="perf_config.yaml to use (default: repo root / $VMF_PERF_CONFIG)")
    sub = parser.add_subparsers(dest="command", required=True)
    promote = sub.add_parser("promote", help="make the medians of a run the baselines")
    promote.add_argument("--run-id", type=int, required=True)
    promote.add_argument("--approved-by", required=True, help="must be listed in baseline_approvers")
    promote.add_argument("--profile", action="append", help="only this profile (repeatable)")
    promote.add_argument("--allow-overwrite", action="store_true",
                         help="replace a baseline that already exists (old and new values are logged)")
    sub.add_parser("baselines", help="list the stored baselines")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    store = baselines.get_store(yaml_path=args.baselines_file)
    try:
        if args.command == "baselines":
            entries = store.all_entries()
            if not entries:
                print(f"no baselines stored in {store.name}")
            for e in entries:
                print(f"{e['profile']:28} {e['metric']:12} {e['value']:12.2f}  promoted by "
                      f"{e.get('promoted_by')} on {e.get('promoted_on')} (run {e.get('run_id')})")
            return 0
        rows, builds = read_run_rows(args.results_dir, args.run_id, args.profile)
        if not rows:
            print(f"REFUSED: no {META_NAME} found for run {args.run_id} under {args.results_dir}")
            return 2
        promoted = store.promote(args.run_id, rows, args.approved_by,
                                 allow_overwrite=args.allow_overwrite, builds=builds,
                                 approvers=perf.load_approvers(args.config))
    except baselines.BaselineError as e:
        logging.error("vmf.perf %s refused", args.command, exc_info=True)
        print(f"REFUSED: {e}")
        return 2
    for profile, metric, old, new in promoted:
        print(f"promoted {profile}/{metric}: {old} -> {new}")
    print(f"{len(promoted)} baseline(s) written to {store.name} (emulator-relative)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
