"""Fixes land in the user's working tree as unstaged edits, via a detached
worktree: no branch is created, nothing is staged or committed, a file the
user changed themselves is never overwritten, and the worktree is always
cleaned up.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest import mock

import pytest
from conftest import git, write

from src import git_utils, graph
from src.agents import auditor, fixer
from src.schemas import AuditReport, Category, Finding, FixAttempt, Severity
from src.tools import project


def out(repo, *args) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout


def snapshot(repo) -> dict:
    return {
        "branches": out(repo, "branch", "--format=%(refname:short)").split(),
        "head": out(repo, "rev-parse", "HEAD").strip(),
        "branch": out(repo, "branch", "--show-current").strip(),
        "stash": out(repo, "stash", "list"),
        "index": out(repo, "diff", "--cached", "--name-only"),
        "commits": out(repo, "rev-list", "--count", "--all").strip(),
    }


def worktrees(repo) -> int:
    return out(repo, "worktree", "list", "--porcelain").count("worktree ")


def finding(file, fid="SEC-001"):
    return Finding(id=fid, category=Category.SECURITY, severity=Severity.HIGH, file=file, line=1,
                   title=f"issue in {file}", detail="d", source_tool="llm-review")


@pytest.fixture
def project_repo(git_repo):
    write(git_repo, "app.py", "eval(input())\n")
    write(git_repo, "lib.py", "exec(input())\n")
    write(git_repo, "other.py", "x = 1\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "initial")
    return git_repo


def fake_run(monkeypatch, fixes: dict[str, str]):
    """The first audit reports one blocking finding per file in `fixes`, the
    second is clean. The Fixer 'fixes' a file by rewriting it in whatever
    repo_path it was given. Records every path the agents were pointed at."""
    reports = iter([AuditReport(findings=[finding(f, f"SEC-00{n}") for n, f in enumerate(fixes, 1)]), AuditReport()])
    seen = {"repo_paths": []}

    def audit(repo_path, on_event=None, **_):
        seen["repo_paths"].append(repo_path)
        return next(reports)

    def fix(repo_path, f, on_event=None, **_):
        seen["repo_paths"].append(repo_path)
        (Path(repo_path) / f.file).write_text(fixes[f.file])
        return FixAttempt(finding_id=f.id, file=f.file, patch_applied=True, notes="edited")

    monkeypatch.setattr(auditor, "run_audit", audit)
    monkeypatch.setattr(fixer, "apply_fix", fix)
    return seen


def test_fixes_land_as_unstaged_edits_and_nothing_else_changes(project_repo, monkeypatch):
    before = snapshot(project_repo)
    seen = fake_run(monkeypatch, {"app.py": "print(input())\n", "lib.py": "print(1)\n"})

    final = graph.run_pipeline(str(project_repo), max_iterations=1)

    assert sorted(final["applied_files"]) == ["app.py", "lib.py"]
    assert final["skipped_files"] == []
    # git status shows the fixed files as unstaged modifications, nothing else:
    assert out(project_repo, "status", "--porcelain").splitlines() == [" M app.py", " M lib.py"]
    assert (project_repo / "app.py").read_text() == "print(input())\n"
    assert (project_repo / "other.py").read_text() == "x = 1\n"
    # No branch, commit, stash, staging or checkout happened in the user's repo:
    assert snapshot(project_repo) == before
    # ...and only the main worktree is left.
    assert worktrees(project_repo) == 1
    # The audit and the fixes ran in the private worktree, never in the user's repo:
    assert seen["repo_paths"] and all(os.path.basename(p).startswith("zweigeeks-wt-") for p in seen["repo_paths"])
    assert not any(Path(p).exists() for p in seen["repo_paths"])
    assert "+print(input())" in final["diff"]


def test_a_file_the_user_changed_is_skipped_not_overwritten(project_repo, monkeypatch):
    (project_repo / "app.py").write_text("eval(input())  # my own edit\n")
    fake_run(monkeypatch, {"app.py": "print(input())\n", "lib.py": "print(1)\n"})

    final = graph.run_pipeline(str(project_repo), max_iterations=1)

    assert final["applied_files"] == ["lib.py"]
    assert [s["file"] for s in final["skipped_files"]] == ["app.py"]
    assert "uncommitted changes" in final["skipped_files"][0]["reason"]
    assert (project_repo / "app.py").read_text() == "eval(input())  # my own edit\n"  # untouched
    skipped = Path(final["skipped_patch"])
    assert skipped.name == "skipped.patch" and "+print(input())" in skipped.read_text()
    assert out(project_repo, "status", "--porcelain").splitlines() == [" M app.py", " M lib.py"]


def test_a_staged_change_of_the_users_counts_as_theirs_too(project_repo, monkeypatch):
    (project_repo / "app.py").write_text("eval(input())  # staged\n")
    git(project_repo, "add", "app.py")
    fake_run(monkeypatch, {"app.py": "print(input())\n"})

    final = graph.run_pipeline(str(project_repo), max_iterations=1)

    assert final["applied_files"] == [] and [s["file"] for s in final["skipped_files"]] == ["app.py"]
    assert out(project_repo, "diff", "--cached", "--name-only").split() == ["app.py"]  # still just theirs
    assert (project_repo / "app.py").read_text() == "eval(input())  # staged\n"


def test_no_clean_working_tree_is_required_and_untracked_files_are_left_alone(project_repo, monkeypatch):
    write(project_repo, "notes.txt", "my scratch notes\n")
    (project_repo / "other.py").write_text("x = 2  # unrelated edit in progress\n")
    fake_run(monkeypatch, {"app.py": "print(input())\n"})

    final = graph.run_pipeline(str(project_repo), max_iterations=1)

    assert final["applied_files"] == ["app.py"]
    assert (project_repo / "notes.txt").read_text() == "my scratch notes\n"
    assert (project_repo / "other.py").read_text() == "x = 2  # unrelated edit in progress\n"


def test_works_from_a_feature_branch(project_repo, monkeypatch):
    git(project_repo, "checkout", "-q", "-b", "feature/x")
    before = snapshot(project_repo)
    fake_run(monkeypatch, {"app.py": "print(input())\n"})

    final = graph.run_pipeline(str(project_repo), max_iterations=1)

    assert final["applied_files"] == ["app.py"]
    assert snapshot(project_repo) == before and before["branch"] == "feature/x"
    assert "zweigeeks" not in out(project_repo, "branch", "-a")


def test_works_on_a_detached_head(project_repo, monkeypatch):
    git(project_repo, "checkout", "-q", "--detach")
    fake_run(monkeypatch, {"app.py": "print(input())\n"})

    final = graph.run_pipeline(str(project_repo), max_iterations=1)

    assert final["applied_files"] == ["app.py"]
    still_detached = subprocess.run(["git", "symbolic-ref", "-q", "HEAD"], cwd=project_repo).returncode != 0
    assert still_detached and worktrees(project_repo) == 1


def test_the_worktree_is_removed_when_the_run_crashes(project_repo, monkeypatch):
    monkeypatch.setattr(auditor, "run_audit", mock.Mock(side_effect=RuntimeError("boom")))

    with pytest.raises(RuntimeError, match="boom"):
        graph.run_pipeline(str(project_repo), max_iterations=1)

    assert worktrees(project_repo) == 1
    assert out(project_repo, "status", "--porcelain") == ""


def test_ctrl_c_keeps_the_fixes_made_so_far_and_cleans_up(project_repo, monkeypatch):
    first = AuditReport(findings=[finding("app.py"), finding("lib.py", "SEC-002")])
    monkeypatch.setattr(auditor, "run_audit", lambda *a, **k: first)

    def fix(repo_path, f, on_event=None, **_):
        if f.file == "lib.py":
            raise KeyboardInterrupt
        (Path(repo_path) / f.file).write_text("print(input())\n")
        return FixAttempt(finding_id=f.id, file=f.file, patch_applied=True, notes="edited")

    monkeypatch.setattr(fixer, "apply_fix", fix)

    with pytest.raises(KeyboardInterrupt):
        graph.run_pipeline(str(project_repo), max_iterations=3)

    assert worktrees(project_repo) == 1
    assert out(project_repo, "status", "--porcelain").splitlines() == [" M app.py"]  # what was fixed is kept


def test_a_run_without_fixes_changes_nothing(project_repo, monkeypatch):
    monkeypatch.setattr(auditor, "run_audit", lambda *a, **k: AuditReport())

    final = graph.run_pipeline(str(project_repo), max_iterations=1)

    assert final["applied_files"] == [] and final["diff"] == ""
    assert out(project_repo, "status", "--porcelain") == ""
    assert worktrees(project_repo) == 1


def test_stale_worktrees_from_crashed_runs_are_pruned_on_startup(project_repo, tmp_path):
    repo = git_utils.open_repo(str(project_repo))
    stale = tmp_path / f"{git_utils.WORKTREE_PREFIX}999999999-dead"  # no such process
    live = tmp_path / f"{git_utils.WORKTREE_PREFIX}{os.getpid()}-alive"  # this very process
    other = tmp_path / "someone-elses-worktree"
    for path in (stale, live, other):
        git(project_repo, "worktree", "add", "--detach", str(path), "HEAD")

    removed = git_utils.prune_stale_worktrees(repo)

    assert [os.path.basename(p) for p in removed] == [stale.name]
    listing = out(project_repo, "worktree", "list", "--porcelain")
    assert stale.name not in listing and not stale.exists()
    assert live.name in listing and other.name in listing  # live runs and the user's own worktrees stay


def test_a_repo_with_no_commits_is_refused_clearly(git_repo):
    with pytest.raises(RuntimeError, match="no commits"):
        graph.run_pipeline(str(git_repo))


def test_the_projects_virtualenv_is_found_from_the_worktree(project_repo):
    write(project_repo, "venv/bin/python", "")
    write(project_repo, ".gitignore", "venv/\n")
    git(project_repo, "add", ".gitignore")
    git(project_repo, "commit", "-q", "-m", "ignore venv")
    repo = git_utils.open_repo(str(project_repo))
    worktree = git_utils.create_worktree(repo)
    try:
        assert not (Path(worktree) / "venv").exists()  # gitignored: not in the worktree
        assert project.project_python(worktree) == str(project_repo / "venv/bin/python")
    finally:
        git_utils.remove_worktree(repo, worktree)


def test_binary_and_non_utf8_files_survive_the_round_trip(project_repo, tmp_path):
    (project_repo / "data.bin").write_bytes(bytes(range(256)))
    (project_repo / "latin.py").write_bytes("name = 'caf\xe9'\n".encode("latin-1"))
    git(project_repo, "add", "-A")
    git(project_repo, "commit", "-q", "-m", "binary + latin-1")
    repo = git_utils.open_repo(str(project_repo))
    worktree = git_utils.create_worktree(repo)
    try:
        (Path(worktree) / "data.bin").write_bytes(bytes(reversed(range(256))))
        (Path(worktree) / "latin.py").write_bytes("name = 'caf\xe9'  # fixed\n".encode("latin-1"))
        landed = git_utils.apply_to_working_tree(str(project_repo), worktree, tmp_path / "out")
    finally:
        git_utils.remove_worktree(repo, worktree)

    assert sorted(landed["applied"]) == ["data.bin", "latin.py"] and landed["skipped"] == []
    assert (project_repo / "data.bin").read_bytes() == bytes(reversed(range(256)))
    assert (project_repo / "latin.py").read_bytes().endswith(b"# fixed\n")
