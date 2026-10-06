"""Security checks: bandit (static analysis) + pip-audit (dependency CVEs)."""

from __future__ import annotations

import json
import subprocess


def run_bandit(repo_path: str) -> tuple[list[dict], str]:
    """Returns (parsed_issues, raw_output)."""
    try:
        proc = subprocess.run(
            ["bandit", "-r", repo_path, "-f", "json", "-q"],
            capture_output=True,
            text=True,
            timeout=180,
        )
        raw = proc.stdout or proc.stderr
        if not raw.strip():
            return [], raw
        data = json.loads(raw)
        return data.get("results", []), raw
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