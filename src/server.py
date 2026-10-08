"""FastAPI backend: kicks off an Auditor<->Fixer job against a local repo
path and streams progress over a websocket to the web UI in static/.

Run with: uvicorn src.server:app --reload
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from pathlib import Path
from typing import Literal, Optional

import ollama
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, folder_picker
from .graph import run_audit_only, run_pipeline

app = FastAPI(title="ZweiGeeks")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class JobRequest(BaseModel):
    repo_path: str
    max_iterations: int = Field(default=config.MAX_ITERATIONS, ge=1, le=20)
    # "fix" runs the audit -> fix -> re-audit loop in a private worktree and
    # applies the fixes to the working tree as unstaged edits; "audit" only
    # reports and never touches the repo.
    mode: Literal["fix", "audit"] = "fix"


class Job:
    def __init__(
        self, job_id: str, repo_path: str, max_iterations: int, mode: str,
        loop: asyncio.AbstractEventLoop,
    ):
        self.id = job_id
        self.repo_path = repo_path
        self.max_iterations = max_iterations
        self.mode = mode
        self.loop = loop
        self.pdf_path: Optional[str] = None
        self.events: list[dict] = []
        self.subscribers: list[asyncio.Queue] = []
        self.status = "running"
        self.result: Optional[dict] = None
        self.error: Optional[str] = None

    def emit(self, event: str, payload: dict) -> None:
        item = {"event": event, "payload": payload}
        self.events.append(item)
        for q in list(self.subscribers):
            self.loop.call_soon_threadsafe(q.put_nowait, item)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        for item in self.events:
            q.put_nowait(item)
        self.subscribers.append(q)
        return q


JOBS: dict[str, Job] = {}


def _run_job(job: Job) -> None:
    try:
        if job.mode == "audit":
            final_state = run_audit_only(job.repo_path, on_event=job.emit)
            report = final_state["report"]
            stop_reason = (
                f"{report.incomplete_reason}_incomplete" if report.audit_incomplete else "audit_only"
            )
            fix_attempts, diff, applied_files, skipped_files, skipped_patch = [], "", [], [], None
        else:
            final_state = run_pipeline(job.repo_path, job.max_iterations, on_event=job.emit)
            report = final_state["report"]
            stop_reason = final_state["stop_reason"]
            applied_files = final_state.get("applied_files", [])
            skipped_files = final_state.get("skipped_files", [])
            skipped_patch = final_state.get("skipped_patch")
            fix_attempts = [a.model_dump() for a in final_state["fix_attempts"]]
            diff = final_state.get("diff", "")
        job.pdf_path = final_state.get("pdf_path")
        job.status = "done"
        job.result = {
            "mode": job.mode,
            "repo_path": job.repo_path,
            "stop_reason": stop_reason,
            "applied_files": applied_files,
            "skipped_files": skipped_files,
            "skipped_patch": skipped_patch,
            "findings": [f.model_dump() for f in report.findings],
            "improvements": report.improvements,
            "audit_incomplete": report.audit_incomplete,
            "fix_attempts": fix_attempts,
            "diff": diff,
            "has_pdf": job.pdf_path is not None,
            "summary": final_state.get("summary"),
        }
        job.emit("job_complete", job.result)
    except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
        job.status = "error"
        job.error = str(exc)
        job.emit("job_error", {"error": str(exc)})


@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


def _ollama_reachable() -> bool:
    try:
        # Short timeout: a wedged Ollama shouldn't hang the request itself.
        ollama.Client(host=config.OLLAMA_HOST, timeout=5).list()
    except Exception:  # noqa: BLE001
        return False
    return True


@app.get("/api/config")
def get_config() -> dict:
    """What the UI shows in its header and uses as form defaults."""
    return {
        "ollama_host": config.OLLAMA_HOST,
        "auditor_model": config.AUDITOR_MODEL,
        "fixer_model": config.FIXER_MODEL,
        "verifier_model": config.VERIFIER_MODEL,
        "max_iterations": config.MAX_ITERATIONS,
    }


@app.post("/api/jobs")
async def create_job(req: JobRequest) -> dict:
    if not _ollama_reachable():
        return {
            "error": (
                f"Can't reach Ollama at {config.OLLAMA_HOST}. "
                f"Run `ollama serve` (and `ollama pull {config.AUDITOR_MODEL}`) first."
            )
        }

    repo_path = str(Path(req.repo_path).expanduser().resolve())
    if not Path(repo_path).is_dir():
        return {"error": f"'{repo_path}' is not a directory."}
    if not (Path(repo_path) / ".git").exists():
        return {"error": f"'{repo_path}' is not a git repo (no .git found)."}
    running = next(
        (j for j in JOBS.values() if j.repo_path == repo_path and j.status == "running"), None
    )
    if running:
        # Two jobs applying fixes to the same working tree would trample
        # each other.
        return {"error": f"A job is already running on this repo (job {running.id})."}

    job_id = uuid.uuid4().hex[:12]
    loop = asyncio.get_running_loop()
    job = Job(job_id, repo_path, req.max_iterations, req.mode, loop)
    JOBS[job_id] = job

    thread = threading.Thread(target=_run_job, args=(job,), daemon=True)
    thread.start()
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    job = JOBS.get(job_id)
    if not job:
        return {"error": "not found"}
    return {
        "status": job.status, "mode": job.mode, "repo_path": job.repo_path,
        "max_iterations": job.max_iterations, "result": job.result, "error": job.error,
    }


@app.get("/api/jobs/{job_id}/report")
def get_job_report(job_id: str) -> FileResponse:
    job = JOBS.get(job_id)
    if not job or not job.pdf_path or not Path(job.pdf_path).is_file():
        raise HTTPException(status_code=404, detail="No PDF report for this job.")
    return FileResponse(job.pdf_path, media_type="application/pdf", filename=Path(job.pdf_path).name)


@app.websocket("/ws/{job_id}")
async def ws_job(websocket: WebSocket, job_id: str) -> None:
    await websocket.accept()
    job = JOBS.get(job_id)
    if not job:
        await websocket.send_json({"event": "job_error", "payload": {"error": "unknown job id"}})
        await websocket.close()
        return

    queue = job.subscribe()
    try:
        while True:
            item = await queue.get()
            await websocket.send_json(item)
            if item["event"] in ("job_complete", "job_error"):
                # Close explicitly - returning alone leaves the socket open
                # and clients waiting for the stream to end hang forever.
                await websocket.close()
                break
    except WebSocketDisconnect:
        pass
    finally:
        job.subscribers.remove(queue)


@app.post("/api/pick-folder")
def pick_folder() -> dict:
    """Opens the native folder dialog on this machine and returns the choice."""
    try:
        path = folder_picker.pick_folder()
    except folder_picker.PickerUnavailable as exc:
        return {"error": str(exc)}
    if not path:
        return {"cancelled": True}
    is_repo = (Path(path).expanduser() / ".git").exists()
    return {"path": path, "is_git_repo": is_repo}
