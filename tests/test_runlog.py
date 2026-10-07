"""Step 1 of making the loop converge: measure it. Every fix attempt is
logged with its fingerprint, prompt, raw reply, rejection reason and time,
and each run ends with a summary of where fixes went."""

from __future__ import annotations

import json

import pytest
from conftest import ollama_reply

from src import config, graph, ollama_client
from src.agents import auditor
from src.fingerprint import fingerprint_of
from src.runlog import RunLog, format_summary
from src.schemas import AuditReport, Category, Finding, FixAttempt, Severity
from src.tools import security


def finding(**kw) -> Finding:
    data = dict(id="SEC-001", category=Category.SECURITY, severity=Severity.HIGH, file="app.py", line=3,
                title="subprocess call with shell=True", detail="d", source_tool="bandit", rule="B602",
                line_text="subprocess.call(cmd, shell=True)")
    data.update(kw)
    f = Finding(**data)
    f.fingerprint = fingerprint_of(f)
    return f


def test_fingerprint_ignores_ids_and_line_numbers_but_not_the_flagged_code():
    base = fingerprint_of(finding())
    assert fingerprint_of(finding(id="SEC-009", line=40)) == base  # moved, renumbered: same issue
    assert fingerprint_of(finding(line_text="subprocess.call(cmd)")) != base  # the code changed
    assert fingerprint_of(finding(rule="B603")) != base
    # LLM findings have no rule id: the normalised title stands in for it.
    llm = finding(source_tool="llm-review", rule=None, title="SQL Injection!")
    assert fingerprint_of(llm) == fingerprint_of(finding(source_tool="llm-review", rule=None, title="sql injection"))


def test_model_calls_are_counted_by_role(monkeypatch):
    class Client:
        def __init__(self, *a, **k):
            pass

        def chat(self, **kw):
            return ollama_reply("ok")

    monkeypatch.setattr(ollama_client.ollama, "Client", Client)
    calls = []
    with ollama_client.call_listener(lambda **kw: calls.append(kw)):
        with ollama_client.llm_role("fixer"):
            ollama_client.chat("m", [])
        ollama_client.chat("m", [])
    assert [c["role"] for c in calls] == ["fixer", "other"]
    assert all(c["ok"] for c in calls)


def test_run_log_records_attempts_and_summarises_rejections(tmp_path):
    log = RunLog("/repo/demo", "fix", reports_dir=str(tmp_path))
    log.audit(1, AuditReport(findings=[finding(), finding(source_tool="ruff", rule="F401")]))
    rejected = FixAttempt(finding_id="SEC-001", file="app.py", patch_applied=False, notes="no match",
                          reason="edit_mismatch", duration_s=1.5,
                          transcript=[{"role": "user", "content": "PROMPT"}, {"role": "assistant", "content": "REPLY"}])
    log.fix_attempt(1, finding(), rejected)
    log.fix_attempt(1, finding(), FixAttempt(finding_id="SEC-001", file="app.py", patch_applied=True, notes="ok"))
    log.on_llm_call(role="fixer", model="m", seconds=2.0, ok=True)

    summary = log.summary(AuditReport(findings=[finding()]), "no_blocking_findings")

    assert summary["fixes_applied"] == {"llm": 1, "deterministic": 0}
    assert summary["rejections_by_reason"] == {"edit_mismatch": 1}
    assert summary["findings_by_source"] == {"bandit": {"first": 1, "last": 1}, "ruff": {"first": 1, "last": 1}}
    assert summary["llm_calls"]["fixer"]["calls"] == 1
    records = [json.loads(line) for line in log.path.read_text().splitlines()]
    attempt = next(r for r in records if r["type"] == "fix_attempt")
    assert attempt["fingerprint"] and attempt["source_tool"] == "bandit" and attempt["pass_no"] == 1
    assert attempt["transcript"][0]["content"] == "PROMPT" and attempt["transcript"][1]["content"] == "REPLY"
    assert attempt["reason"] == "edit_mismatch" and attempt["duration_s"] == 1.5
    assert "edit_mismatch" in format_summary(summary)


def test_transcript_stays_out_of_api_payloads():
    attempt = FixAttempt(finding_id="x", patch_applied=False, notes="n", transcript=[{"role": "user", "content": "p"}])
    assert "transcript" not in attempt.model_dump()


@pytest.fixture
def no_llm_review(monkeypatch):
    monkeypatch.setattr(auditor, "_review_file_with_llm", lambda *a, **k: [])
    monkeypatch.setattr(auditor, "_generate_improvements", lambda *a, **k: [])
    monkeypatch.setattr(security, "run_pip_audit", lambda repo: ([], "offline"))


def test_fixture_repo_has_the_planted_issues(vuln_repo, no_llm_review):
    report = auditor.run_audit(str(vuln_repo))

    bandit = {f.rule for f in report.findings if f.source_tool == "bandit"}
    assert {"B602", "B105", "B506", "B501", "B608"} <= bandit
    ruff = sorted(f.rule for f in report.findings if f.source_tool == "ruff")
    assert ruff == ["E711", "F401", "F401", "F541"]
    assert all(f.fingerprint for f in report.findings)
    shell = next(f for f in report.findings if f.rule == "B602")
    assert shell.line_text == 'return subprocess.call("tar czf logs.tgz " + directory, shell=True)'


def test_bandit_scan_errors_are_reported_not_hidden(vuln_repo, no_llm_review, monkeypatch):
    raw = json.dumps({"results": [], "errors": [{"filename": "app.py", "reason": "exception while scanning file"}]})
    monkeypatch.setattr(security, "run_bandit", lambda repo: ([], raw))

    report = auditor.run_audit(str(vuln_repo))

    scan_error = [f for f in report.findings if f.rule == "scan-error"]
    assert len(scan_error) == 1 and "app.py" in scan_error[0].detail


def test_audit_only_run_writes_a_log_and_a_summary(vuln_repo, no_llm_review):
    events = []
    result = graph.run_audit_only(str(vuln_repo), on_event=lambda e, p: events.append((e, p)))

    summary = result["summary"]
    assert summary["mode"] == "audit" and summary["audits"] == 1
    assert summary["findings_by_source"]["bandit"]["first"] >= 5
    assert ("run_summary", summary) in events
    assert summary["log_path"].startswith(config.REPORTS_DIR)
