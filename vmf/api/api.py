"""FastAPI read API and small operational console for persisted VMF runs."""
from __future__ import annotations

from html import escape
import logging
from pathlib import Path
from typing import Callable

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from vmf import storage
from vmf.orchestrator import run_matrix
from vmf.report.report import generate_report

ROOT = Path(__file__).resolve().parents[2]
RESULTS_ROOT = ROOT / "results"
REPORTS_ROOT = ROOT / "reports"
log = logging.getLogger(__name__)


class RunRequest(BaseModel):
    apk: str = Field(min_length=1, max_length=500)
    scenario: str = Field(min_length=1, max_length=500)
    profiles: list[dict] = Field(min_length=1, max_length=24)
    max_parallel: int | None = Field(default=None, ge=1, le=16)


def _inside_root(value: str, root: Path) -> Path:
    candidate = Path(value).expanduser().resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise HTTPException(400, "path must be inside the VMF workspace") from exc
    return candidate


def _run_options(request: RunRequest) -> dict:
    apk = _inside_root(request.apk, ROOT)
    scenario = _inside_root(request.scenario, ROOT)
    if not apk.is_file() or not scenario.is_file():
        raise HTTPException(422, "apk and scenario must name existing workspace files")
    options = {"apk": str(apk), "scenario": str(scenario), "profiles": request.profiles}
    if request.max_parallel is not None:
        options["max_parallel"] = request.max_parallel
    return options


def _console(rows: list[dict]) -> str:
    items = "".join(
        f'<li><a href="/console/runs/{r["run_id"]}">Run {r["run_id"]}</a> — '
        f'{escape(str(r["status"]))} — {escape(str(r["apk_name"]))}</li>'
        for r in rows) or "<li>No persisted runs yet.</li>"
    return f"""<!doctype html><html><head><title>VMF console</title><style>body{{font-family:system-ui;max-width:900px;margin:2rem}}input,textarea{{width:100%;margin:.3rem 0}}</style></head><body>
<h1>Virtual Mobile Farm</h1><p><a href='/docs'>API docs</a> · <a href='/health'>System status</a></p><h2>Results / history</h2><ul>{items}</ul>
<h2>Start run</h2><p>Use the API docs or POST <code>/runs</code> with workspace-relative APK/scenario paths and profile mappings. Runs are dispatched in the background.</p></body></html>"""


def _dispatch_run(orchestrator: Callable, run_id: int, options: dict) -> None:
    """Run M10 after the response and record an unhandled dispatch failure once."""
    try:
        orchestrator(run_id, options)
    except Exception:
        try:
            storage.finish_run_if_running(run_id, "DISPATCH_FAILED")
        except Exception:
            log.exception("could not record dispatch failure for run %s", run_id)
        log.exception("background dispatch failed for run %s", run_id)


def create_app(*, orchestrator: Callable | None = None) -> FastAPI:
    """Create the persisted-results API; ``orchestrator`` is injectable for safe tests."""
    app = FastAPI(title="Virtual Mobile Farm", version="0.1.0")
    runner = orchestrator or run_matrix.main

    @app.get("/health")
    def health():
        try:
            storage.list_runs(limit=1)
        except Exception as exc:
            raise HTTPException(503, "storage unavailable") from exc
        return {"status": "ok"}

    @app.get("/runs")
    def runs(limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0), status: str | None = None):
        return {"items": storage.list_runs(limit=limit, offset=offset, status=status), "limit": limit, "offset": offset}

    @app.get("/runs/{run_id}")
    def run_detail(run_id: int):
        record = storage.get_run(run_id)
        if record is None:
            raise HTTPException(404, "run not found")
        return record

    @app.post("/runs", status_code=202)
    def start_run(request: RunRequest, background: BackgroundTasks):
        options = _run_options(request)
        try:
            profile_ids = [run_matrix.profile_name(p) for p in request.profiles]
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, f"invalid profile selection: {exc}") from exc
        run_id = storage.create_run(Path(request.apk).name, Path(request.scenario).name, profile_ids)
        try:
            background.add_task(_dispatch_run, runner, run_id, options)
        except Exception as exc:
            storage.finish_run(run_id, "START_FAILED")
            raise HTTPException(503, "could not dispatch run") from exc
        return {"run_id": run_id, "status": "accepted"}

    @app.get("/runs/{run_id}/report")
    def report(run_id: int):
        try:
            path = Path(generate_report(run_id, str(REPORTS_ROOT)))
        except LookupError as exc:
            raise HTTPException(404, "run not found") from exc
        return FileResponse(path, media_type="text/html", filename=path.name)

    @app.get("/evidence/{run_id}/{relative_path:path}")
    def evidence(run_id: int, relative_path: str):
        base = (RESULTS_ROOT / str(run_id)).resolve()
        candidate = (base / relative_path).resolve()
        try:
            candidate.relative_to(base)
        except ValueError as exc:
            raise HTTPException(400, "invalid evidence path") from exc
        if not candidate.is_file():
            raise HTTPException(404, "evidence not found")
        return FileResponse(candidate)

    @app.get("/", response_class=HTMLResponse)
    def console():
        return _console(storage.list_runs(limit=30))

    @app.get("/console/runs/{run_id}", response_class=HTMLResponse)
    def console_run(run_id: int):
        record = storage.get_run(run_id)
        if record is None:
            raise HTTPException(404, "run not found")
        rows = "".join(
            f"<li>{escape(str(r['profile']))}: {escape(str(r['result']))} — {escape(str(r['cause']))}</li>"
            for r in record["device_results"])
        return f"<h1>Run {run_id}</h1><p><a href='/runs/{run_id}/report'>Open report</a></p><ul>{rows or '<li>No results yet.</li>'}</ul>"

    return app
