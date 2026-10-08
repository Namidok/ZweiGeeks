"""The default: fixes are edited straight into the user's working tree and
left uncommitted, so Source Control shows them. Files the user has already
changed are never touched."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from conftest import git, write

from src import graph
from src.agents import auditor, fixer
from src.schemas import AuditReport, Category, Finding, FixAttempt, Severity


def out(repo, *args) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout


def finding(file, fid="SEC-001"):
    return Finding(id=fid, category=Category.SECURITY, severity=Severity.HIGH, file=file, line=1,
                   title=f"issue in {file}", detail="d", source_tool="llm-review")


@pytest.fixture
def project_repo(git_repo):
    write(git_repo, "app.py", "eval(input())\n")
    write(git_repo, "lib.py", "exec(input())\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "initial")
    return git_repo


def fake_run(monkeypatch, fixes):
    reports = iter([AuditReport(findings=[finding(f, f"SEC-00{n}") for n, f in enumerate(fixes, 1)]), AuditReport()])
    seen = []

    def audit(repo_path, on_event=None, **_):
        seen.append(repo_path)
        return next(reports)

    def fix(repo_path, f, on_event=None, **_):
        (Path(repo_path) / f.file).write_text(fixes[f.file])
        return FixAttempt(finding_id=f.id, file=f.file, patch_applied=True, notes="edited")

    monkeypatch.setattr(auditor, "run_audit", audit)
    monkeypatch.setattr(fixer, "apply_fix", fix)
    return seen


def test_fixes_are_edited_in_place_and_left_uncommitted(project_repo, monkeypatch):
    head = out(project_repo, "rev-parse", "HEAD")
    seen = fake_run(monkeypatch, {"app.py": "print(input())\n", "lib.py": "print(1)\n"})

    final = graph.run_pipeline(str(project_repo), max_iterations=1)

    assert seen[0] == str(project_repo)  # worked on the real folder, no copy
    assert sorted(final["applied_files"]) == ["app.py", "lib.py"]
    assert out(project_repo, "status", "--porcelain").splitlines() == [" M app.py", " M lib.py"]  # unstaged
    assert out(project_repo, "rev-parse", "HEAD") == head  # nothing committed
    assert out(project_repo, "branch", "--format=%(refname:short)").split() == [out(project_repo, "branch", "--show-current").strip()]
    assert out(project_repo, "worktree", "list", "--porcelain").count("worktree ") == 1
    assert "+print(input())" in final["diff"]
    assert (Path(final["summary"]["log_path"]).parent / "fixes.patch").exists()


def test_a_file_you_already_changed_is_left_alone(project_repo, monkeypatch):
    (project_repo / "app.py").write_text("eval(input())  # my edit\n")
    findings = [finding("app.py"), finding("lib.py", "SEC-002")]
    reports = iter([AuditReport(findings=findings), AuditReport()])
    monkeypatch.setattr(auditor, "run_audit", lambda *a, **k: next(reports))
    edited = []

    def fix(repo_path, f, on_event=None, **_):
        edited.append(f.file)
        (Path(repo_path) / f.file).write_text("print(1)\n")
        return FixAttempt(finding_id=f.id, file=f.file, patch_applied=True, notes="edited")

    monkeypatch.setattr(fixer, "apply_fix", fix)

    final = graph.run_pipeline(str(project_repo), max_iterations=1)

    assert edited == ["lib.py"]  # the Fixer never even tried app.py
    assert (project_repo / "app.py").read_text() == "eval(input())  # my edit\n"
    assert final["applied_files"] == ["lib.py"]
    assert [s["file"] for s in final["skipped_files"]] == ["app.py"]
    assert findings[0].needs_manual_fix and "uncommitted changes" in findings[0].fix_notes


def test_a_crash_keeps_the_fixes_made_so_far(project_repo, monkeypatch):
    monkeypatch.setattr(auditor, "run_audit", lambda *a, **k: AuditReport(findings=[finding("app.py"), finding("lib.py", "SEC-002")]))

    def fix(repo_path, f, on_event=None, **_):
        if f.file == "lib.py":
            raise KeyboardInterrupt
        (Path(repo_path) / f.file).write_text("print(input())\n")
        return FixAttempt(finding_id=f.id, file=f.file, patch_applied=True, notes="edited")

    monkeypatch.setattr(fixer, "apply_fix", fix)

    with pytest.raises(KeyboardInterrupt):
        graph.run_pipeline(str(project_repo), max_iterations=3)

    assert out(project_repo, "status", "--porcelain").splitlines() == [" M app.py"]


def test_deterministic_fixes_skip_protected_files(project_repo):
    from src.agents import deterministic

    f = finding("requirements.txt")
    f.autofix = {"tool": "pin", "package": "x", "version": "1", "fix_versions": ["2"]}
    report = AuditReport(findings=[f])
    assert deterministic.pending(report) == [f]
    assert deterministic.pending(report, frozenset({"requirements.txt"})) == []
