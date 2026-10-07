"""Code quality + test coverage checks: ruff and pytest --cov."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

from .project import project_files, project_python, relative_to_repo, tool_path


def run_ruff(repo_path: str) -> tuple[list[dict], str]:
    files = project_files(repo_path, {".py"})
    if not files:
        return [], "no Python files in the project"
    try:
        proc = subprocess.run(
            [tool_path("ruff"), "check", "--output-format", "json", *files],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=120,
        )
        raw = proc.stdout or proc.stderr
        if not raw.strip():
            return [], raw
        issues = json.loads(raw)
        for issue in issues:
            issue["filename"] = relative_to_repo(issue.get("filename"), repo_path)
        return issues, raw
    except FileNotFoundError:
        return [], "ruff not installed"
    except subprocess.TimeoutExpired:
        return [], "ruff timed out"
    except json.JSONDecodeError:
        return [], raw


def _has_modules(python: str, *modules: str) -> bool:
    try:
        proc = subprocess.run(
            [python, "-c", "import " + ", ".join(modules)],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def run_tests_with_coverage(repo_path: str) -> tuple[dict, str]:
    """Runs the project's tests with the project's own interpreter when it has
    a virtualenv in the repo, so its dependencies are importable. Returns a
    small summary dict + raw output.

    summary["passed"] is True/False, or None when the tests couldn't be run
    at all (e.g. the project's environment has no pytest) - that's reported
    as skipped, not as a failing suite.

    Leaves nothing behind in the repo: no .coverage file, no .pytest_cache.
    """
    python = project_python(repo_path)
    if python and not _has_modules(python, "pytest"):
        return (
            {"returncode": -1, "passed": None, "skipped": "pytest isn't installed in the project's environment"},
            f"skipped: {python} has no pytest",
        )
    python = python or sys.executable
    cmd = [python, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    if _has_modules(python, "pytest_cov"):
        cmd += ["--cov", "--cov-report=term-missing"]
    try:
        with tempfile.TemporaryDirectory() as scratch:
            env = {
                **os.environ,
                "COVERAGE_FILE": os.path.join(scratch, ".coverage"),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            proc = subprocess.run(cmd, cwd=repo_path, capture_output=True, text=True, timeout=300, env=env)
        raw = proc.stdout + "\n" + proc.stderr
        summary = {
            "returncode": proc.returncode,
            "passed": "failed" not in raw.lower() and proc.returncode == 0,
        }
        return summary, raw
    except FileNotFoundError:
        return {"returncode": -1, "passed": None, "skipped": "no Python interpreter found"}, "pytest not runnable"
    except subprocess.TimeoutExpired:
        return {"returncode": -1, "passed": False}, "pytest timed out"
