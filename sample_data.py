"""Generate representative M9 sample storage data for run #142."""
from __future__ import annotations

import json
from pathlib import Path

from vmf.results import PerfRow, Step, Verdict
from vmf.storage import storage


RUN_ID = 142
PROFILE = "emulator-5554-low-notched"
ROOT = Path(__file__).resolve().parent
EVIDENCE_DIR = ROOT / "results" / str(RUN_ID) / PROFILE


def main() -> None:
    storage._initialize_database()
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)

    screenshot_0 = EVIDENCE_DIR / "step_00_screen.png"
    hierarchy_0 = EVIDENCE_DIR / "step_00_hierarchy.xml"
    screenshot_1 = EVIDENCE_DIR / "step_01_screen.png"
    hierarchy_1 = EVIDENCE_DIR / "step_01_hierarchy.xml"
    logcat = EVIDENCE_DIR / "logcat.txt"
    profile_config_path = EVIDENCE_DIR / "profile_config.json"

    screenshot_0.write_text("placeholder screenshot for run 142 step 0\n", encoding="utf-8")
    hierarchy_0.write_text("<hierarchy><node text=\"Login\" /></hierarchy>\n", encoding="utf-8")
    screenshot_1.write_text("placeholder screenshot for run 142 step 1\n", encoding="utf-8")
    hierarchy_1.write_text("<hierarchy><node text=\"Welcome\" /></hierarchy>\n", encoding="utf-8")
    logcat.write_text("10-05 12:00:00.000 I/VMF: sample run 142\n", encoding="utf-8")

    profile_config = {
        "brand": "TEST",
        "tier": "low",
        "cutout": "notch",
        "density": 420,
        "font_scale": 1.3,
        "dark_mode": True,
    }
    profile_config_path.write_text(
        json.dumps(profile_config, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    steps = [
        Step(
            step_index=0,
            action="launch",
            status="pass",
            duration_ms=820,
            screenshot_path=str(screenshot_0.relative_to(ROOT)),
            hierarchy_path=str(hierarchy_0.relative_to(ROOT)),
        ),
        Step(
            step_index=1,
            action="tap",
            status="pass",
            duration_ms=235,
            screenshot_path=str(screenshot_1.relative_to(ROOT)),
            hierarchy_path=str(hierarchy_1.relative_to(ROOT)),
        ),
    ]
    verdict = Verdict(
        result="pass",
        cause="",
        failed_step=None,
        trigger_behaviour=None,
    )
    perf_rows = [
        PerfRow(
            metric="launch_time",
            median=820.0,
            spread_pct=4.2,
            baseline=900.0,
            regression_pct=-8.9,
            noisy=False,
            unstable=False,
        ),
        PerfRow(
            metric="jank_pct",
            median=1.1,
            spread_pct=8.0,
            baseline=1.5,
            regression_pct=-26.7,
            noisy=False,
            unstable=False,
        ),
    ]

    with storage._connection() as conn:
        with conn:
            conn.execute("DELETE FROM perf_baselines WHERE profile = ?", (PROFILE,))
            conn.execute("DELETE FROM runs WHERE run_id = ?", (RUN_ID,))
            conn.execute(
                """
                INSERT INTO runs (
                    run_id, apk_name, scenario, started_at, finished_at, status
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    RUN_ID,
                    "sample.apk",
                    "scenarios/login_flow.yaml",
                    "2026-10-05T12:00:00+00:00",
                    "2026-10-05T12:03:00+00:00",
                    "FINISHED",
                ),
            )
            conn.executemany(
                """
                INSERT INTO perf_baselines (profile, metric, baseline)
                VALUES (?, ?, ?)
                """,
                [
                    (PROFILE, "launch_time", 900.0),
                    (PROFILE, "jank_pct", 1.5),
                ],
            )

    for step in steps:
        storage.save_step(RUN_ID, PROFILE, step)
    storage.save_device_result(RUN_ID, PROFILE, verdict)
    storage.save_profile_config(RUN_ID, PROFILE, profile_config)
    storage.save_perf(RUN_ID, PROFILE, perf_rows)

    print(f"Generated farm.db sample data and evidence under {EVIDENCE_DIR}")


if __name__ == "__main__":
    main()
