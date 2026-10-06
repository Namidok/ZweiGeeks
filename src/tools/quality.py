"""Code quality + test coverage checks: ruff and pytest --cov."""

from __future__ import annotations

import json
import subprocess


def run_ruff(repo_path: str) -> tuple[list[dict], str]:
    try:
        proc = subprocess.run(
            ["ruff", "check", repo_path, "--output-format", "json"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        raw = proc.stdout or proc.stderr
        if not raw.strip():
            return [], raw
        return json.loads(raw), raw
    except FileNotFoundError:
        return [], "ruff not installed"
    except subprocess.TimeoutExpired:
        return [], "ruff timed out"
    except json.JSONDecodeError:
        return [], raw


def run_tests_with_coverage(repo_path: str) -> tuple[dict, str]:
    """Runs pytest with coverage. Returns a small summary dict + raw output.

    Failures here are informational for the Auditor, not fatal for us.
    """
    try:
        proc = subprocess.run(
            ["pytest", "--cov", "--cov-report=term-missing", "-q"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=300,
        )
        raw = proc.stdout + "\n" + proc.stderr
        summary = {
            "returncode": proc.returncode,
            "passed": "failed" not in raw.lower() and proc.returncode == 0,
        }
        return summary, raw
    except FileNotFoundError:
        return {"returncode": -1, "passed": False}, "pytest not installed"
    except subprocess.TimeoutExpired:
        return {"returncode": -1, "passed": False}, "pytest timed out"