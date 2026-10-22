"""HTML reports reconstructed solely from persisted VMF records."""
from __future__ import annotations

from datetime import datetime, timezone
from html import escape
import os
from pathlib import Path

from vmf import storage

_ROOT = Path(__file__).resolve().parents[2]
_RESULTS = _ROOT / "results"
FIDELITY_STATEMENT = (
    "The Virtual Mobile Farm detects crashes, hangs/ANRs, layout defects and performance regressions of an APK across 24 controlled virtual Android profiles. "
    "Each profile reproduces the Android version, screen, RAM, CPU tier and known OEM behaviour patterns of a named reference model. "
    "It does not reproduce OEM firmware, physical GPU/chipset performance or hardware sensors. Performance results are relative measurements within the virtual environment."
)


def _text(value) -> str:
    return escape("" if value is None else str(value))


def _evidence_link(value: str | None, report_dir: Path) -> str:
    if not value:
        return "Missing evidence"
    candidate = (_ROOT / value).resolve()
    try:
        candidate.relative_to(_RESULTS.resolve())
    except ValueError:
        return "Unavailable evidence"
    if not candidate.is_file():
        return "Missing evidence"
    href = os.path.relpath(candidate, report_dir).replace(os.sep, "/")
    return f'<a href="{_text(href)}">{_text(candidate.name)}</a>'


def generate_report(run_id: int, output_dir: str = "reports") -> str:
    """Write a real, escaped HTML report for a persisted run and return its path."""
    try:
        run_id = int(run_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("run_id must be an integer") from exc
    record = storage.get_run(run_id)
    if record is None:
        raise LookupError(f"run {run_id} does not exist")
    directory = Path(output_dir).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    results = record["device_results"]
    profiles = sorted({r["profile"] for r in results} | set(record["profile_config"]))
    counts = {name: sum(r["result"] == name for r in results) for name in ("pass", "fail", "crash", "anr", "hang", "error")}
    summary_rows = "".join(
        f"<tr><td>{_text(r['profile'])}</td><td>{_text(r['result'])}</td><td>{_text(r['cause'])}</td></tr>" for r in results
    ) or "<tr><td colspan=3>No device results were persisted.</td></tr>"
    config_sections = "".join(
        f"<h3>{_text(profile)}</h3><pre>{_text(record['profile_config'].get(profile, {}))}</pre>"
        for profile in profiles
    ) or "<p>No profile configuration was persisted.</p>"
    step_rows = "".join(
        f"<tr><td>{_text(s['profile'])}</td><td>{_text(s['step_index'])}</td><td>{_text(s['action'])}</td>"
        f"<td>{_text(s['status'])}</td><td>{_evidence_link(s['screenshot_path'], directory)} / "
        f"{_evidence_link(s['hierarchy_path'], directory)}</td></tr>" for s in record["steps"]
    ) or "<tr><td colspan=5>No steps were persisted.</td></tr>"
    perf_rows = "".join(
        f"<tr><td>{_text(p['profile'])}</td><td>{_text(p['metric'])}</td><td>{_text(p['median'])}</td>"
        f"<td>{_text(p['baseline'])}</td><td>{_text(p['regression_pct'])}</td></tr>" for p in record["perf_results"]
    ) or "<tr><td colspan=5>No performance measurements were persisted.</td></tr>"
    page = f"""<!doctype html><html><head><meta charset=utf-8><title>VMF run {run_id}</title>
<style>body{{font-family:system-ui;margin:2rem;max-width:1100px}}table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #ccc;padding:.45rem;text-align:left}}pre{{white-space:pre-wrap;background:#f5f5f5;padding:.7rem}}</style></head><body>
<h1>Virtual Mobile Farm report: run {run_id}</h1><p>Generated {datetime.now(timezone.utc).isoformat()}</p>
<dl><dt>APK</dt><dd>{_text(record['apk_name'])}</dd><dt>Scenario</dt><dd>{_text(record['scenario'])}</dd><dt>Status</dt><dd>{_text(record['status'])}</dd><dt>Started</dt><dd>{_text(record['started_at'])}</dd><dt>Finished</dt><dd>{_text(record['finished_at'])}</dd></dl>
<p><strong>Profiles:</strong> {len(profiles)}; <strong>Pass:</strong> {counts['pass']}; <strong>Failures/errors:</strong> {sum(counts[k] for k in ('fail','crash','anr','hang','error'))}</p>
<h2>Profile summary</h2><table><tr><th>Profile</th><th>Result</th><th>Cause</th></tr>{summary_rows}</table>
<h2>Exact recorded profile configuration</h2>{config_sections}
<h2>Steps and evidence</h2><table><tr><th>Profile</th><th>Step</th><th>Action</th><th>Status</th><th>Evidence</th></tr>{step_rows}</table>
<h2>Performance (emulator-relative)</h2><table><tr><th>Profile</th><th>Metric</th><th>Median</th><th>Baseline</th><th>Regression</th></tr>{perf_rows}</table>
<h2>Requires physical validation</h2><p>Physical GPU/chipset performance, sensors, OEM firmware behaviour, and device-specific hardware integration require validation on physical devices.</p>
<h2>Fidelity statement</h2><p>{_text(FIDELITY_STATEMENT)}</p></body></html>"""
    path = directory / f"run_{run_id}_report.html"
    path.write_text(page, encoding="utf-8")
    return str(path)
