from unittest import mock

import pytest
from conftest import git, write

from src import config, git_utils, graph
from src.agents import auditor, fixer
from src.schemas import AuditReport, Category, Finding, FixAttempt, Severity


def blocking(fid="SEC-001", file="app.py"):
    return Finding(id=fid, category=Category.SECURITY, severity=Severity.HIGH,
                   title="t", detail="d", source_tool="llm-review", file=file)


def minor(fid="QA-001"):
    return Finding(id=fid, category=Category.QUALITY, severity=Severity.LOW,
                   title="t", detail="d", source_tool="ruff")


def initial_state(max_iterations=5):
    return {
        "repo_path": "/nonexistent", "branch_name": "zweigeeks/test", "iteration": 0,
        "max_iterations": max_iterations, "report": AuditReport(), "fix_attempts": [],
        "done": False, "stop_reason": None, "consecutive_fix_timeouts": 0,
    }


@pytest.fixture
def no_git(monkeypatch):
    """fix_node commits each pass; these tests only care about routing."""
    monkeypatch.setattr(git_utils, "open_repo", mock.Mock())
    monkeypatch.setattr(git_utils, "commit_all", mock.Mock(return_value=False))


def run(monkeypatch, reports, attempts=None, max_iterations=5):
    """Drives the compiled graph with scripted auditor reports (a fresh one
    per audit pass) and scripted fixer attempts. Returns (final_state,
    events, run_audit mock, apply_fix mock).
    """
    run_audit = mock.Mock(side_effect=[r() for r in reports])
    apply_fix = mock.Mock(side_effect=attempts or (lambda repo, f, on_event=None: FixAttempt(
        finding_id=f.id, file=f.file, patch_applied=False, notes="could not fix")))
    monkeypatch.setattr(auditor, "run_audit", run_audit)
    monkeypatch.setattr(fixer, "apply_fix", apply_fix)
    events = []
    final = graph.build_graph(on_event=lambda e, p: events.append((e, p))).invoke(
        initial_state(max_iterations), config={"recursion_limit": 100},
    )
    return final, events, run_audit, apply_fix


# --- stop reasons -------------------------------------------------------------

def test_incomplete_audit_never_reports_clean_even_with_zero_findings(monkeypatch, no_git):
    final, events, _, apply_fix = run(monkeypatch, [
        lambda: AuditReport(findings=[], audit_incomplete=True, incomplete_reason="ollama_wedged"),
    ])

    assert final["done"] is True
    assert final["stop_reason"] == "ollama_wedged_incomplete"
    assert not any(p.get("reason") == "clean" for e, p in events if e == "loop_end")
    apply_fix.assert_not_called()


def test_incomplete_audit_takes_precedence_over_blocking_findings(monkeypatch, no_git):
    final, _, _, apply_fix = run(monkeypatch, [
        lambda: AuditReport(findings=[blocking()], audit_incomplete=True, incomplete_reason="ollama_error"),
    ])

    assert final["stop_reason"] == "ollama_error_incomplete"
    apply_fix.assert_not_called()  # don't "fix" on the back of a half-finished audit


def test_no_blocking_findings_ends_the_loop(monkeypatch, no_git):
    final, events, _, apply_fix = run(monkeypatch, [lambda: AuditReport(findings=[minor()])])

    assert final["stop_reason"] == "no_blocking_findings"
    assert final["iteration"] == 1
    assert ("loop_end", {"reason": "clean"}) in events
    apply_fix.assert_not_called()


def test_iteration_budget_stops_an_unfixable_loop(monkeypatch, no_git):
    final, events, run_audit, apply_fix = run(
        monkeypatch, [lambda: AuditReport(findings=[blocking()])] * 3, max_iterations=3,
    )

    assert final["stop_reason"] == "max_iterations_reached"
    assert final["iteration"] == 3
    assert run_audit.call_count == 3
    assert apply_fix.call_count == 2  # fix runs between audits, not after the last one
    assert ("loop_end", {"reason": "budget_exhausted", "remaining": 1}) in events


def test_fix_then_reaudit_clean(monkeypatch, no_git):
    def fixed(repo, f, on_event=None):
        return FixAttempt(finding_id=f.id, file=f.file, patch_applied=True, notes="edited")

    final, _, _, _ = run(
        monkeypatch,
        [lambda: AuditReport(findings=[blocking()]), lambda: AuditReport(findings=[])],
        attempts=fixed,
    )

    assert final["stop_reason"] == "no_blocking_findings"
    assert final["iteration"] == 2
    assert [a.patch_applied for a in final["fix_attempts"]] == [True]


# --- fixer circuit breaker -----------------------------------------------------

def timed_out(repo, f, on_event=None):
    return FixAttempt(finding_id=f.id, file=f.file, patch_applied=False, notes="timeout", timed_out=True)


def test_fix_node_stops_after_n_consecutive_timeouts(monkeypatch, no_git):
    monkeypatch.setattr(config, "OLLAMA_MAX_CONSECUTIVE_TIMEOUTS", 3)
    five = [blocking(f"SEC-00{i}") for i in range(1, 6)]

    final, events, run_audit, apply_fix = run(
        monkeypatch, [lambda: AuditReport(findings=five)], attempts=timed_out,
    )

    assert apply_fix.call_count == 3  # didn't wait out the remaining two
    assert final["stop_reason"] == "fixer_ollama_timeouts"
    assert run_audit.call_count == 1  # no re-audit after a wedged fix pass
    assert ("fix_pass_aborted", {"reason": "ollama_wedged"}) in events
    git_utils.commit_all.assert_called_once()  # whatever did get fixed is still committed


def test_fix_timeout_count_carries_across_passes(monkeypatch, no_git):
    # One blocking finding per pass is fewer than the threshold, so the
    # counter has to survive between passes for the breaker to ever trip.
    monkeypatch.setattr(config, "OLLAMA_MAX_CONSECUTIVE_TIMEOUTS", 3)

    final, _, run_audit, apply_fix = run(
        monkeypatch, [lambda: AuditReport(findings=[blocking()])] * 3,
        attempts=timed_out, max_iterations=10,
    )

    assert final["stop_reason"] == "fixer_ollama_timeouts"
    assert apply_fix.call_count == 3
    assert run_audit.call_count == 3


def test_a_non_timeout_attempt_resets_the_timeout_count(monkeypatch, no_git):
    monkeypatch.setattr(config, "OLLAMA_MAX_CONSECUTIVE_TIMEOUTS", 2)
    outcomes = iter([True, False, True, False])

    def alternate(repo, f, on_event=None):
        return FixAttempt(finding_id=f.id, file=f.file, patch_applied=False,
                          notes="n", timed_out=next(outcomes))

    findings = [blocking(f"SEC-00{i}") for i in range(1, 5)]
    final, _, _, apply_fix = run(
        monkeypatch, [lambda: AuditReport(findings=findings)] * 2,
        attempts=alternate, max_iterations=2,
    )

    assert apply_fix.call_count == 4
    assert final["stop_reason"] == "max_iterations_reached"


# --- full pipeline on a real throwaway repo -----------------------------------

def _committed_repo(git_repo, content="x = 1\n"):
    write(git_repo, "app.py", content)
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "initial")
    return git_repo


def test_run_pipeline_fixes_on_sandbox_branch_and_restores_original(git_repo, tmp_path, monkeypatch):
    _committed_repo(git_repo, "eval(input())\n")
    original = git_utils.open_repo(str(git_repo)).active_branch.name
    monkeypatch.setattr(config, "REPORTS_DIR", str(tmp_path / "reports"))

    reports = iter([AuditReport(findings=[blocking(file="app.py")]), AuditReport(findings=[minor()])])
    monkeypatch.setattr(auditor, "run_audit", lambda repo, on_event=None: next(reports))

    def fix(repo, f, on_event=None):
        (git_repo / f.file).write_text("print(input())\n")
        return FixAttempt(finding_id=f.id, file=f.file, patch_applied=True, notes="edited")

    monkeypatch.setattr(fixer, "apply_fix", fix)

    final = graph.run_pipeline(str(git_repo), max_iterations=3)

    repo = git_utils.open_repo(str(git_repo))
    assert final["stop_reason"] == "no_blocking_findings"
    assert repo.active_branch.name == original  # user's branch is checked out again
    assert (git_repo / "app.py").read_text() == "eval(input())\n"  # and untouched
    assert final["branch_name"] in [h.name for h in repo.heads]
    assert "+print(input())" in final["diff"]
    assert final["pdf_path"] and (tmp_path / "reports" / "audit_report_repo.pdf").exists()


def test_run_pipeline_refuses_a_dirty_tree(git_repo, monkeypatch):
    _committed_repo(git_repo)
    (git_repo / "app.py").write_text("x = 2\n")
    run_audit = mock.Mock()
    monkeypatch.setattr(auditor, "run_audit", run_audit)

    with pytest.raises(RuntimeError, match="uncommitted changes"):
        graph.run_pipeline(str(git_repo))
    run_audit.assert_not_called()


def test_run_audit_only_never_writes_to_the_repo(git_repo, tmp_path, monkeypatch):
    _committed_repo(git_repo)
    monkeypatch.setattr(config, "REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setattr(auditor, "run_audit", lambda repo, on_event=None: AuditReport(findings=[blocking()]))
    apply_fix = mock.Mock()
    monkeypatch.setattr(fixer, "apply_fix", apply_fix)

    result = graph.run_audit_only(str(git_repo))

    apply_fix.assert_not_called()
    assert len(git_utils.open_repo(str(git_repo)).heads) == 1
    assert result["pdf_path"].endswith("audit_report_repo.pdf")
