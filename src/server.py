"""FastAPI backend: kicks off an Auditor<->Fixer job against a local repo
path and streams progress over a websocket to the retro UI.

Run with: uvicorn src.server:app --reload
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from pathlib import Path
from typing import Optional

import ollama
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config
from .graph import run_pipeline

app = FastAPI(title="ZweiGeeks")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class JobRequest(BaseModel):
    repo_path: str
    max_iterations: int = config.MAX_ITERATIONS


class Job:
    def __init__(self, job_id: str, repo_path: str, max_iterations: int, loop: asyncio.AbstractEventLoop):
        self.id = job_id
        self.repo_path = repo_path
        self.max_iterations = max_iterations
        self.loop = loop
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
        final_state = run_pipeline(job.repo_path, job.max_iterations, on_event=job.emit)
        job.status = "done"
        job.result = {
            "stop_reason": final_state["stop_reason"],
            "branch": final_state["branch_name"],
            "findings": [f.model_dump() for f in final_state["report"].findings],
            "fix_attempts": [a.model_dump() for a in final_state["fix_attempts"]],
            "diff": final_state.get("diff", ""),
        }
        job.emit("job_complete", job.result)
    except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
        job.status = "error"
        job.error = str(exc)
        job.emit("job_error", {"error": str(exc)})


@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.post("/api/jobs")
async def create_job(req: JobRequest) -> dict:
    try:
        # Short timeout: a wedged Ollama shouldn't hang the request itself.
        ollama.Client(host=config.OLLAMA_HOST, timeout=5).list()
    except Exception:  # noqa: BLE001
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
        # Two jobs branching and committing in the same repo would trample
        # each other.
        return {"error": f"A job is already running on this repo (job {running.id})."}

    job_id = uuid.uuid4().hex[:12]
    loop = asyncio.get_running_loop()
    job = Job(job_id, repo_path, req.max_iterations, loop)
    JOBS[job_id] = job

    thread = threading.Thread(target=_run_job, args=(job,), daemon=True)
    thread.start()
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    job = JOBS.get(job_id)
    if not job:
        return {"error": "not found"}
    return {"status": job.status, "result": job.result, "error": job.error}


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