"""vmf.__main__ — CLI entry point.

Usage:
    python -m vmf --dry-run
"""
import argparse
import logging

# Import all packages to prove they load at startup (ARCH import check)
from vmf import (api, classifier, geometry, oem_packs, orchestrator, perf,  # noqa: F401
                 provision, report, results, runner, storage)
from vmf.orchestrator import run_matrix
from vmf.results import Finding, PerfRow, Step, Verdict  # noqa: F401


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(prog="vmf")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run the ARCH architecture dry-run (no emulator required).")
    args = parser.parse_args()
    if args.dry_run:
        run_matrix.dry_run()
    else:
        parser.error("only --dry-run is supported at ARCH stage")


if __name__ == "__main__":
    main()
