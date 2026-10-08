"""The job API the web UI talks to. The pipelines themselves are faked:
these tests cover request validation, both job modes, the report download
and the websocket replay, not the agents.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from src import server
from src.schemas import AuditReport, Category, Finding, FixAttempt, Severity
from tests.conftest import git


def _finding(**overrides) -> Finding:
    data = dict(
        id="SEC-001", category=Category.SECURITY, severity=Severity.HIGH,
        file="app.py", line=3, title="SQL injection", detail="raw query", source_tool="llm-review",
    )
    data.update(overrides)
    return Finding(**data)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(server, "_ollama_reachable", lambda: True)
    server.JOBS.clear()
    with TestClient(server.app) as c:
        yield c
    server.JOBS.clear()


@pytest.fixture
def repo(git_repo):
    (git_repo / "app.py").write_text("print('hi')\n")
    git(git_repo, "add", ".")
    git(git_repo, "commit", "-q", "-m", "init")
    return git_repo


@pytest.fixture
def pdf(tmp_path):
    path = tmp_path / "audit_report_repo.pdf"
    path.write_bytes(b"%PDF-1.4 fake")
    return str(path)


def _wait_for(client: TestClient, job_id: str) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        body = client.get(f"/api/jobs/{job_id}").json()
        if body["status"] != "running":
            return body
        time.sleep(0.02)
    raise AssertionError("job never finished")


def test_index_and_static_assets_are_served(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "ZweiGeeks" in page.text
    for asset in ("app.js", "office.js", "style.css"):
        res = client.get(f"/static/{asset}")
        assert res.status_code == 200, asset
        assert res.content.strip(), f"static/{asset} is empty"


def test_config_exposes_models_and_defaults(client):
    body = client.get("/api/config").json()
    assert set(body) == {"ollama_host", "auditor_model", "fixer_model", "verifier_model", "max_iterations"}


def test_rejects_unreachable_ollama(client, monkeypatch, repo):
    monkeypatch.setattr(server, "_ollama_reachable", lambda: False)
    body = client.post("/api/jobs", json={"repo_path": str(repo)}).json()
    assert "Can't reach Ollama" in body["error"]


@pytest.mark.parametrize("payload", [{"mode": "deploy"}, {"max_iterations": 0}, {"max_iterations": 21}])
def test_rejects_invalid_job_options(client, repo, payload):
    res = client.post("/api/jobs", json={"repo_path": str(repo), **payload})
    assert res.status_code == 422


def test_rejects_non_git_directory(client, tmp_path):
    body = client.post("/api/jobs", json={"repo_path": str(tmp_path)}).json()
    assert "not a git repo" in body["error"]


def test_audit_only_job_never_runs_the_pipeline(client, monkeypatch, repo, pdf):
    def fake_audit(repo_path, on_event=None):
        on_event("audit_only_done", {"finding_count": 1, "blocking": 1})
        return {"report": AuditReport(findings=[_finding()], improvements=["add CI"]), "pdf_path": pdf}

    def no_pipeline(*args, **kwargs):
        raise AssertionError("audit-only mode must not start the fix loop")

    monkeypatch.setattr(server, "run_audit_only", fake_audit)
    monkeypatch.setattr(server, "run_pipeline", no_pipeline)

    job_id = client.post("/api/jobs", json={"repo_path": str(repo), "mode": "audit"}).json()["job_id"]
    body = _wait_for(client, job_id)

    assert body["status"] == "done", body["error"]
    assert body["mode"] == "audit"
    result = body["result"]
    assert result["stop_reason"] == "audit_only"
    assert result["applied_files"] == [] and result["skipped_files"] == []
    assert result["fix_attempts"] == [] and result["diff"] == ""
    assert result["improvements"] == ["add CI"]
    assert result["has_pdf"] is True

    report = client.get(f"/api/jobs/{job_id}/report")
    assert report.status_code == 200
    assert report.headers["content-type"] == "application/pdf"
    assert report.content.startswith(b"%PDF")


def test_audit_only_incomplete_review_is_not_reported_as_done_clean(client, monkeypatch, repo):
    report = AuditReport(audit_incomplete=True, incomplete_reason="ollama_wedged")
    monkeypatch.setattr(server, "run_audit_only", lambda repo_path, on_event=None: {"report": report, "pdf_path": None})

    job_id = client.post("/api/jobs", json={"repo_path": str(repo), "mode": "audit"}).json()["job_id"]
    result = _wait_for(client, job_id)["result"]

    assert result["stop_reason"] == "ollama_wedged_incomplete"
    assert result["audit_incomplete"] is True
    assert client.get(f"/api/jobs/{job_id}/report").status_code == 404


def test_fix_job_reports_applied_and_skipped_files_and_the_diff(client, monkeypatch, repo, pdf):
    def fake_pipeline(repo_path, max_iterations, on_event=None):
        assert max_iterations == 2
        on_event("worktree_created", {})
        return {
            "report": AuditReport(findings=[_finding(fixed=True)]),
            "stop_reason": "no_blocking_findings",
            "fix_attempts": [FixAttempt(finding_id="SEC-001", file="app.py", patch_applied=True, notes="ok")],
            "diff": "diff --git a/app.py b/app.py\n",
            "pdf_path": pdf,
            "applied_files": ["app.py"],
            "skipped_files": [{"file": "b.py", "reason": "you have uncommitted changes in this file"}],
            "skipped_patch": "audit_results/run/skipped.patch",
        }

    monkeypatch.setattr(server, "run_pipeline", fake_pipeline)
    job_id = client.post("/api/jobs", json={"repo_path": str(repo), "max_iterations": 2}).json()["job_id"]
    body = _wait_for(client, job_id)

    assert body["max_iterations"] == 2
    result = body["result"]
    assert result["mode"] == "fix"
    assert result["applied_files"] == ["app.py"]
    assert result["skipped_files"][0]["file"] == "b.py"
    assert result["skipped_patch"].endswith("skipped.patch")
    assert "branch" not in result and "review_branch" not in result
    assert result["fix_attempts"][0]["patch_applied"] is True
    assert result["diff"].startswith("diff --git")


def test_websocket_replays_events_then_closes(client, monkeypatch, repo):
    def fake_audit(repo_path, on_event=None):
        on_event("tool_start", {"tool": "repo-hygiene"})
        on_event("tool_done", {"tool": "repo-hygiene", "count": 0})
        return {"report": AuditReport(), "pdf_path": None}

    monkeypatch.setattr(server, "run_audit_only", fake_audit)
    job_id = client.post("/api/jobs", json={"repo_path": str(repo), "mode": "audit"}).json()["job_id"]
    _wait_for(client, job_id)

    # Connecting after the job finished still gets the whole run, which is
    # what lets the UI pick a run back up after a page reload.
    with client.websocket_connect(f"/ws/{job_id}") as ws:
        events = []
        while True:
            msg = ws.receive_json()
            events.append(msg["event"])
            if msg["event"] in ("job_complete", "job_error"):
                break
    assert events == ["tool_start", "tool_done", "job_complete"]


def test_pipeline_crash_surfaces_as_job_error(client, monkeypatch, repo):
    def boom(*args, **kwargs):
        raise RuntimeError("dirty working tree")

    monkeypatch.setattr(server, "run_pipeline", boom)
    job_id = client.post("/api/jobs", json={"repo_path": str(repo)}).json()["job_id"]
    body = _wait_for(client, job_id)
    assert body["status"] == "error"
    assert body["error"] == "dirty working tree"


def test_unknown_job_report_is_404(client):
    assert client.get("/api/jobs/nope/report").status_code == 404
