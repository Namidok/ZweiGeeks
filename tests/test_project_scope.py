"""What the agents treat as "the project", and what ends up on the branches.

Regression tests for a real run on a small repo with a gitignored `venv/`:
bandit scanned site-packages and reported 1,193 findings, the test suite ran
in the wrong interpreter, and the sandbox branch picked up a stray
`.coverage` file and the user's untracked PDF.
"""

from __future__ import annotations

import stat

import pytest
from conftest import git, write

from src import git_utils
from src.agents import auditor
from src.tools import project, quality


@pytest.fixture
def project_repo(git_repo):
    write(git_repo, ".gitignore", "venv/\n")
    write(git_repo, "src/app.py", "print('app')\n")
    write(git_repo, "venv/lib/site-packages/dep.py", "import pickle\n")
    git(git_repo, "add", ".")
    git(git_repo, "commit", "-q", "-m", "init")
    return git_repo


def test_project_files_skip_gitignored_dirs_but_keep_untracked_source(project_repo):
    write(project_repo, "src/new_module.py", "x = 1\n")  # untracked, not ignored

    files = project.project_files(str(project_repo), {".py"})

    assert files == ["src/app.py", "src/new_module.py"]


def test_committed_virtualenvs_and_dependency_dirs_are_skipped_even_when_tracked(git_repo):
    # A project that commits its venv (or doesn't gitignore it) under any name.
    write(git_repo, "app.py", "x = 1\n")
    write(git_repo, "pyenv311/pyvenv.cfg", "home = /usr/bin\n")
    write(git_repo, "pyenv311/lib/python3.11/site-packages/dep.py", "import pickle\n")
    write(git_repo, "pyenv311/bin/tool.py", "x = 2\n")
    write(git_repo, "web/node_modules/pkg/index.js", "eval(x)\n")
    write(git_repo, "web/vendor/lib.php", "<?php eval($x);\n")
    write(git_repo, "web/app.js", "let x = 1;\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "everything committed")

    assert project.project_files(str(git_repo)) == ["app.py", "web/app.js"]


def test_model_review_only_sees_project_files(project_repo):
    found = auditor._discover_source_files(str(project_repo), max_files=30, max_chars=12000)
    assert [p.relative_to(project_repo).as_posix() for p in found] == ["src/app.py"]


def test_project_python_prefers_the_repos_own_venv(tmp_path):
    assert project.project_python(str(tmp_path)) is None
    write(tmp_path, ".venv/bin/python", "")
    assert project.project_python(str(tmp_path)).endswith(".venv/bin/python")


def test_tests_are_skipped_not_failed_when_the_project_env_has_no_pytest(tmp_path):
    fake = write(tmp_path, "venv/bin/python", "#!/bin/sh\nexit 1\n")  # every import fails
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    summary, _ = quality.run_tests_with_coverage(str(tmp_path))

    assert summary["passed"] is None
    assert "pytest" in summary["skipped"]


def test_pytest_run_leaves_no_coverage_or_cache_files(git_repo):
    write(git_repo, "test_ok.py", "def test_ok():\n    assert True\n")

    summary, _ = quality.run_tests_with_coverage(str(git_repo))

    assert summary["passed"] is True
    leftovers = {p.name for p in git_repo.iterdir()} - {".git", "test_ok.py"}
    assert leftovers == set()


# --- what gets committed ---------------------------------------------------------

def test_fix_pass_commits_only_the_files_the_fixer_edited(project_repo):
    write(project_repo, "src/app.py", "print('fixed')\n")
    write(project_repo, ".coverage", "junk")
    write(project_repo, "notes.pdf", "user's own file")
    repo = git_utils.open_repo(str(project_repo))

    assert git_utils.commit_files(repo, ["src/app.py"], "zweigeeks: pass 1 fixes") is True

    committed = repo.git.show("--name-only", "--format=", "HEAD").split()
    assert committed == ["src/app.py"]
    assert {".coverage", "notes.pdf"} <= set(repo.untracked_files)


def test_commit_files_with_nothing_changed_is_a_no_op(project_repo):
    repo = git_utils.open_repo(str(project_repo))
    head = repo.head.commit
    assert git_utils.commit_files(repo, [], "empty") is False
    assert git_utils.commit_files(repo, ["src/app.py"], "unchanged") is False
    assert repo.head.commit == head


def test_interrupted_run_commits_tracked_edits_but_no_untracked_files(project_repo):
    write(project_repo, "src/app.py", "print('half fixed')\n")
    write(project_repo, ".coverage", "junk")
    repo = git_utils.open_repo(str(project_repo))

    assert git_utils.commit_tracked_changes(repo, "partial") is True
    assert repo.git.show("--name-only", "--format=", "HEAD").split() == ["src/app.py"]
    assert ".coverage" in repo.untracked_files
