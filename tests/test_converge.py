"""The audit -> fix loop converges instead of spinning.

Regressions from a measured run (5 passes, 115 fix attempts, 4 applied):
file-less pip-audit findings were "attempted" every pass, a rejected fix was
retried with a byte-identical prompt and seed, a pass that fixed nothing
still led to another audit, and every audit re-reviewed unchanged files.
"""

from __future__ import annotations

from unittest import mock

import pytest
from conftest import ollama_reply, write

from src import config, git_utils, graph
from src.agents import auditor, fixer
from src.schemas import AuditReport, Category, Finding, FixAttempt, Severity

HANDLER = (
    "import ast\n"
    "\n"
    "def handler(request):\n"
    "    data = request.args['q']\n"
    "    result = eval(data)\n"
    "    return result\n"
)


def state(repo_path, max_iterations=5):
    return {
        "repo_path": str(repo_path), "branch_name": "zweigeeks/test", "iteration": 0,
        "max_iterations": max_iterations, "report": AuditReport(), "fix_attempts": [],
        "done": False, "stop_reason": None, "consecutive_fix_timeouts": 0,
        "history": {}, "improvements": [],
    }


@pytest.fixture
def no_git(monkeypatch):
    monkeypatch.setattr(git_utils, "open_repo", mock.Mock())
    monkeypatch.setattr(git_utils, "commit_files", mock.Mock(return_value=False))


def eval_finding(**kw):
    data = dict(id="SEC-001", category=Category.SECURITY, severity=Severity.HIGH, file="handler.py", line=5,
                title="eval() on request input", detail="Arbitrary code execution.",
                resolution="Use ast.literal_eval.", source_tool="llm-review")
    data.update(kw)
    return Finding(**data)


def run_graph(repo_path, reports, max_iterations=5):
    run_audit = mock.Mock(side_effect=[r() for r in reports])
    events = []
    with mock.patch.object(auditor, "run_audit", run_audit):
        final = graph.build_graph(on_event=lambda e, p: events.append((e, p))).invoke(
            state(repo_path, max_iterations), config={"recursion_limit": 100},
        )
    return final, events, run_audit


def test_a_file_less_high_finding_never_keeps_the_loop_going(tmp_path, no_git, monkeypatch):
    apply_fix = mock.Mock()
    monkeypatch.setattr(fixer, "apply_fix", apply_fix)
    dependency = Finding(id="SEC-002", category=Category.SECURITY, severity=Severity.HIGH,
                         title="flask 1.0: PYSEC-1", detail="d", source_tool="pip-audit")
    suite = Finding(id="TEST-001", category=Category.TESTING, severity=Severity.HIGH,
                    title="Test suite not passing", detail="d", source_tool="pytest")

    final, _, run_audit = run_graph(tmp_path, [lambda: AuditReport(findings=[dependency, suite])])

    assert final["stop_reason"] == "no_blocking_findings"
    assert run_audit.call_count == 1
    apply_fix.assert_not_called()


def test_a_pass_that_applies_nothing_stops_with_no_progress(tmp_path, no_git, monkeypatch):
    rejected = mock.Mock(side_effect=lambda repo, f, **_: FixAttempt(
        finding_id=f.id, file=f.file, patch_applied=False, notes="nope", reason="not_verified"))
    monkeypatch.setattr(fixer, "apply_fix", rejected)

    final, events, run_audit = run_graph(tmp_path, [lambda: AuditReport(findings=[eval_finding()])] * 3)

    assert final["stop_reason"] == "no_progress"
    assert run_audit.call_count == 1  # no second audit of an unchanged repo
    assert rejected.call_count == config.FIXER_MAX_ATTEMPTS_PER_FINDING
    assert ("loop_end", {"reason": "no_progress", "remaining": 1}) in events
    finding = final["report"].findings[0]
    assert finding.needs_manual_fix and "gave up after 3 attempts" in finding.fix_notes


def test_rejected_fix_is_retried_with_the_reason_and_a_new_seed_at_most_n_times(tmp_path, no_git, monkeypatch):
    write(tmp_path, "handler.py", HANDLER)
    seeds, prompts = [], []
    # The model keeps using a name it never imports - the undefined_name gate
    # rejects it every time.
    bad_edit = (
        "<<<<<<< SEARCH\n    result = eval(data)\n=======\n    result = safe.literal_eval(data)\n>>>>>>> REPLACE"
    )

    def fake_chat(model, messages, options=None, timeout=None):
        seeds.append(options["seed"])
        prompts.append(messages[-1]["content"])
        return ollama_reply(bad_edit)

    monkeypatch.setattr(fixer, "chat", fake_chat)

    final, _, _ = run_graph(tmp_path, [lambda: AuditReport(findings=[eval_finding()])])

    n = config.FIXER_MAX_ATTEMPTS_PER_FINDING
    assert len(seeds) == n  # never more than the cap
    assert seeds == [config.OLLAMA_SEED + i for i in range(n)]  # a different seed each time
    assert "previous edit" not in prompts[0]
    for prompt in prompts[1:]:
        assert "Your previous edit for this issue was rejected because" in prompt
        assert "safe" in prompt  # the actual reason, not a generic "try again"
    assert [a.reason for a in final["fix_attempts"]] == ["undefined_name"] * n
    assert (tmp_path / "handler.py").read_text() == HANDLER
    assert final["stop_reason"] == "no_progress"


def test_a_retry_that_succeeds_ends_the_retries(tmp_path, no_git, monkeypatch):
    calls = iter([
        FixAttempt(finding_id="SEC-001", file="handler.py", patch_applied=False, notes="n", reason="edit_mismatch"),
        FixAttempt(finding_id="SEC-001", file="handler.py", patch_applied=True, notes="ok"),
    ])
    apply_fix = mock.Mock(side_effect=lambda *a, **k: next(calls))
    monkeypatch.setattr(fixer, "apply_fix", apply_fix)

    final, _, _ = run_graph(
        tmp_path, [lambda: AuditReport(findings=[eval_finding()]), lambda: AuditReport(findings=[])],
    )

    assert apply_fix.call_count == 2
    assert apply_fix.call_args_list[1].kwargs["previous"] == {"reason": "edit_mismatch", "notes": "n"}
    assert apply_fix.call_args_list[1].kwargs["attempt_number"] == 1
    assert final["stop_reason"] == "no_blocking_findings"


def test_file_level_rejections_are_not_retried(tmp_path, no_git, monkeypatch):
    apply_fix = mock.Mock(side_effect=lambda repo, f, **_: FixAttempt(
        finding_id=f.id, file=f.file, patch_applied=False, notes="gone", reason="file_missing"))
    monkeypatch.setattr(fixer, "apply_fix", apply_fix)

    run_graph(tmp_path, [lambda: AuditReport(findings=[eval_finding()])])

    assert apply_fix.call_count == 1


# --- review cache ------------------------------------------------------------------

def test_unchanged_files_are_not_sent_to_the_model_again(vuln_repo, monkeypatch):
    reviewed = []

    def fake_review(model, file_path, repo_root, max_chars, num_ctx):
        reviewed.append(file_path.name)
        return [{"category": "security", "severity": "high", "line": 1, "title": f"issue in {file_path.name}",
                 "detail": "d", "file": file_path.name}]

    monkeypatch.setattr(auditor, "_review_file_with_llm", fake_review)
    monkeypatch.setattr(auditor, "_generate_improvements", lambda *a, **k: [])
    monkeypatch.setattr(auditor.security, "run_pip_audit", lambda repo: ([], "offline"))

    first = auditor.run_audit(str(vuln_repo))
    assert sorted(reviewed) == ["app.py", "helpers.py"]

    reviewed.clear()
    second = auditor.run_audit(str(vuln_repo))
    assert reviewed == []  # nothing changed, nothing re-reviewed
    assert second.review_cache_hits == 2
    llm = lambda r: sorted(f.title for f in r.findings if f.source_tool == "llm-review")  # noqa: E731
    assert llm(second) == llm(first)  # same findings, straight from the cache

    (vuln_repo / "helpers.py").write_text((vuln_repo / "helpers.py").read_text() + "\n# edited\n")
    reviewed.clear()
    auditor.run_audit(str(vuln_repo))
    assert reviewed == ["helpers.py"]  # only the edited file goes back to the model


def test_review_cache_can_be_switched_off(vuln_repo, monkeypatch):
    calls = mock.Mock(return_value=[])
    monkeypatch.setattr(auditor, "_review_file_with_llm", calls)
    monkeypatch.setattr(auditor, "_generate_improvements", lambda *a, **k: [])
    monkeypatch.setattr(auditor.security, "run_pip_audit", lambda repo: ([], "offline"))
    monkeypatch.setattr(config, "REVIEW_CACHE", False)

    auditor.run_audit(str(vuln_repo))
    auditor.run_audit(str(vuln_repo))

    assert calls.call_count == 4  # 2 files x 2 audits


def test_improvements_are_generated_once_per_run(tmp_path, no_git, monkeypatch):
    seen = []

    def audit(repo, on_event=None, improvements=True):
        seen.append(improvements)
        return AuditReport(findings=[eval_finding()] if len(seen) == 1 else [],
                           improvements=["add CI"] if improvements else [])

    monkeypatch.setattr(auditor, "run_audit", audit)
    monkeypatch.setattr(fixer, "apply_fix", lambda repo, f, **_: FixAttempt(
        finding_id=f.id, file=f.file, patch_applied=True, notes="ok"))

    final = graph.build_graph().invoke(state(tmp_path), config={"recursion_limit": 100})

    assert seen == [True, False]
    assert final["report"].improvements == ["add CI"]  # carried over to the final report
