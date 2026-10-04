"""Run one scenario once against a live device and print a summary."""
import argparse
import collections
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vmf import runner  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--serial", required=True, help="ADB serial, e.g. emulator-5554")
    p.add_argument("--apk", required=True)
    p.add_argument("--scenario", required=True)
    p.add_argument("--port", required=True, type=int, help="UiAutomator2 systemPort, e.g. 8200")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO)
    run_id, steps = runner.run(args.serial, args.apk, args.scenario, args.port)
    counts = collections.Counter(s.status for s in steps)
    print(f"run_id={run_id} steps={len(steps)} "
          f"pass={counts['pass']} fail={counts['fail']} skipped={counts['skipped']}")


if __name__ == "__main__":
    main()
