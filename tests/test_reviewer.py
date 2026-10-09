"""JAX's closing review: the verdict, the bugs/changes/improvements lists,
and JAX implementing the file-level improvements through the Fixer."""

from __future__ import annotations

import json

from src import config, graph
from src.agents import auditor, fixer, reviewer
from src.report import generate_pdf_report
from src.schemas import AuditReport, Category, Finding, FixAttempt, Severity
from tests.conftest import git, ollama_reply, write


def _repo(git_repo, files: dict[str, str]):
    for rel, content in files.items():
        write(git_repo, rel, content)
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "init")
    return git_repo


def _model_says(monkeypatch, data: dict) -> list:
    calls = []

    def fake_chat(model, messages, options=None, timeout=None):
        calls.append(messages)
        return ollama_reply(json.dumps(data))

    monkeypatch.setattr(reviewer, "chat", fake_chat)
    return calls


def _finding(**overrides) -> Finding:
    data = dict(
        id="SEC-001", category=Category.SECURITY, severity=Severity.HIGH, file="app.py", line=1,
        title="SQL injection", detail="raw query", source_tool="llm-review",
    )
    data.update(overrides)
    return Finding(**data)


CLEAN = {
    "README.md": "# Demo\n", "LICENSE": "MIT\n", ".gitignore": ".venv/\n",
    "app.py": "def main():\n    return 1\n", "tests/test_app.py": "def test_x():\n    assert True\n",
    ".github/workflows/ci.yml": "on: push\n", "requirements.txt": "flask\n",
}


def test_clean_repo_is_called_great_but_still_gets_improvements(git_repo, monkeypatch):
    repo = _repo(git_repo, CLEAN)
    calls = _model_says(monkeypatch, {
        "strengths": ["Small, readable app.py."],
        "improvements": [
            {"title": "Add a docstring to main", "detail": "Say what it returns.", "file": "app.py"},
            {"title": "Publish a changelog", "detail": "Track releases.", "file": "CHANGELOG.md"},
        ],
    })

    review = reviewer.write_review(str(repo), AuditReport(), stack=["python"])

    assert review.verdict == "perfect"
    assert "great shape" in review.headline and "even better" in review.headline
    assert review.bugs == [] and review.changes == []
    assert review.strengths == ["Small, readable app.py."]
    titles = {i.title: i for i in review.improvements}
    assert titles["Add a docstring to main"].file == "app.py"
    # A file that doesn't exist can't be edited: it stays a repo-level suggestion.
    assert titles["Publish a changelog"].file is None
    # The repo has a LICENSE, CI, tests and a .gitignore, so none of those are suggested.
    assert not {"Add a LICENSE", "Run lint and tests in CI", "Add automated tests", "Add a .gitignore"} & set(titles)
    assert "app.py" in calls[0][1]["content"]  # the model saw the source


def test_missing_basics_and_windows_unsafe_names_are_reported(git_repo, monkeypatch):
    repo = _repo(git_repo, {"report:final.sql": "select 1;\n", "main.py": "print(1)\n"})
    _model_says(monkeypatch, {})

    review = reviewer.write_review(str(repo), AuditReport(), stack=["python"])

    assert review.verdict == "good"  # a real bug is open, so not "perfect"
    assert review.bugs[0].title.startswith("File names that can't be checked out on Windows")
    titles = {i.title for i in review.improvements}
    assert {"Add a README", "Add a LICENSE", "Add a .gitignore", "Add automated tests",
            "Run lint and tests in CI", "Declare the Python dependencies"} <= titles


def test_open_high_finding_means_needs_work_and_fixed_ones_are_changes(git_repo, monkeypatch):
    repo = _repo(git_repo, CLEAN)
    _model_says(monkeypatch, {})
    report = AuditReport(findings=[
        _finding(),
        _finding(id="QUA-001", severity=Severity.LOW, title="Unused import", fixed=True, fix_notes="removed"),
        _finding(id="QUA-002", severity=Severity.LOW, title="False alarm", dismissed=True),
    ])

    review = reviewer.write_review(str(repo), report, stack=["python"])

    assert review.verdict == "needs_work"
    assert [b.title for b in review.bugs] == ["SQL injection"]
    assert [c.title for c in review.changes] == ["Unused import"]
    assert "I fixed 1 thing" in review.headline


def test_incomplete_audit_is_never_called_clean(git_repo, monkeypatch):
    repo = _repo(git_repo, CLEAN)
    calls = _model_says(monkeypatch, {"strengths": ["x"]})

    review = reviewer.write_review(str(repo), AuditReport(audit_incomplete=True), stack=[])

    assert review.verdict == "incomplete"
    assert calls == []


def test_model_failure_still_gives_the_deterministic_review(git_repo, monkeypatch):
    repo = _repo(git_repo, {"main.py": "print(1)\n"})

    def broken(*a, **k):
        raise RuntimeError("ollama down")

    monkeypatch.setattr(reviewer, "chat", broken)
    review = reviewer.write_review(str(repo), AuditReport(), stack=[])

    assert review.verdict == "perfect"
    assert any(i.title == "Add a README" for i in review.improvements)


def test_jax_applies_file_improvements_through_the_fixer(git_repo, monkeypatch):
    repo = _repo(git_repo, {**CLEAN, "analysis.ipynb": "{}\n"})
    _model_says(monkeypatch, {"improvements": [
        {"title": "Add a docstring to main", "detail": "Say what it returns.", "file": "app.py"},
        {"title": "Add markdown cells", "detail": "Explain each step.", "file": "analysis.ipynb"},
        {"title": "Add a Setup section", "detail": "How to install.", "file": "README.md"},
    ]})
    seen = []

    def fake_fix(repo_path, finding, on_event=None, **_):
        seen.append(finding)
        ok = finding.file == "app.py"
        return FixAttempt(finding_id=finding.id, file=finding.file, patch_applied=ok,
                          notes="done" if ok else "verifier said no", reason=None if ok else "not_verified")

    monkeypatch.setattr(fixer, "apply_fix", fake_fix)
    review = reviewer.write_review(str(repo), AuditReport(), stack=["python"])
    results = reviewer.apply_improvements(str(repo), review)

    assert [f.file for f in seen] == ["app.py", "README.md"]  # notebooks are left alone
    assert all(f.source_tool == "jax-review" and f.severity == Severity.LOW for f in seen)
    assert all(a.kind == "improvement" for _, a in results)
    status = {i.title: i.status for i in review.improvements}
    assert status["Add a docstring to main"] == "applied"
    assert status["Add a Setup section"] == "not_applied"
    assert status["Add markdown cells"] == "suggested"


def test_in_place_run_on_a_clean_repo_ends_with_the_review_and_improvements(git_repo, monkeypatch):
    repo = _repo(git_repo, CLEAN)
    _model_says(monkeypatch, {"improvements": [
        {"title": "Add a docstring to main", "detail": "Say what it returns.", "file": "app.py"},
    ]})
    monkeypatch.setattr(auditor, "run_audit", lambda *a, **k: AuditReport())

    def fake_fix(repo_path, finding, on_event=None, **_):
        target = repo / finding.file
        target.write_text('def main():\n    """Returns 1."""\n    return 1\n')
        return FixAttempt(finding_id=finding.id, file=finding.file, patch_applied=True, notes="done")

    monkeypatch.setattr(fixer, "apply_fix", fake_fix)
    monkeypatch.setattr(config, "ISOLATE_RUNS", False)
    events = []
    final = graph.run_pipeline(str(repo), 2, on_event=lambda e, p: events.append(e))

    assert final["stop_reason"] == "no_blocking_findings"
    assert final["review"].verdict == "perfect"
    assert final["applied_files"] == ["app.py"]
    assert '"""Returns 1."""' in final["diff"]
    assert [a.kind for a in final["fix_attempts"]] == ["improvement"]
    assert final["summary"]["fixes_applied"]["improvement"] == 1
    assert events.index("review_done") < events.index("improve_start") < events.index("pdf_report")


def test_improvements_can_be_switched_off(git_repo, monkeypatch):
    repo = _repo(git_repo, CLEAN)
    _model_says(monkeypatch, {"improvements": [{"title": "Docstring", "detail": "x", "file": "app.py"}]})
    monkeypatch.setattr(auditor, "run_audit", lambda *a, **k: AuditReport())

    def no_edits(*a, **k):
        raise AssertionError("improvements are switched off")

    monkeypatch.setattr(fixer, "apply_fix", no_edits)
    monkeypatch.setattr(config, "ISOLATE_RUNS", False)
    monkeypatch.setattr(config, "JAX_APPLY_IMPROVEMENTS", False)

    final = graph.run_pipeline(str(repo), 2)

    assert final["review"].improvements[0].status == "suggested"
    assert final["applied_files"] == []


def test_pdf_includes_the_review(git_repo, monkeypatch, tmp_path):
    repo = _repo(git_repo, CLEAN)
    _model_says(monkeypatch, {"strengths": ["Tidy <code> & tests"], "improvements": [
        {"title": "Add <b>docs</b>", "detail": "x < y", "file": "app.py"},
    ]})
    review = reviewer.write_review(str(repo), AuditReport(), stack=[])

    out = generate_pdf_report(AuditReport(), str(repo), str(tmp_path / "r.pdf"), review=review)

    assert (tmp_path / "r.pdf").read_bytes().startswith(b"%PDF")
    assert out.endswith("r.pdf")
