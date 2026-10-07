"""Security checks: bandit (static analysis) + pip-audit (dependency CVEs)."""

from __future__ import annotations

import json
import subprocess

from .project import project_files, relative_to_repo


def run_bandit(repo_path: str) -> tuple[list[dict], str]:
    """Returns (parsed_issues, raw_output).

    Scans only the project's own Python files (see tools/project.py), never a
    virtualenv or other ignored directory inside the repo.
    """
    files = project_files(repo_path, {".py"})
    if not files:
        return [], "no Python files in the project"
    try:
        proc = subprocess.run(
            ["bandit", "-f", "json", "-q", *files],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=180,
        )
        raw = proc.stdout or proc.stderr
        if not raw.strip():
            return [], raw
        data = json.loads(raw)
        results = data.get("results", [])
        for issue in results:
            issue["filename"] = relative_to_repo(issue.get("filename"), repo_path)
        return results, raw
    except FileNotFoundError:
        return [], "bandit not installed"
    except subprocess.TimeoutExpired:
        return [], "bandit timed out"
    except json.JSONDecodeError:
        return [], raw


def run_pip_audit(repo_path: str) -> tuple[list[dict], str]:
    try:
        proc = subprocess.run(
            ["pip-audit", "-r", f"{repo_path}/requirements.txt", "-f", "json"],
            capture_output=True,
            text=True,
            timeout=180,
        )
        raw = proc.stdout or proc.stderr
        if not raw.strip():
            return [], raw
        data = json.loads(raw)
        return data.get("dependencies", []), raw
    except FileNotFoundError:
        return [], "pip-audit not installed or requirements.txt missing"
    except subprocess.TimeoutExpired:
        return [], "pip-audit timed out"
    except json.JSONDecodeError:
        return [], raw