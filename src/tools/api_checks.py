"""Lightweight static checks for API hygiene.

v1 deliberately does NOT spin up the target app or fuzz a live server
(that needs real sandboxing we haven't built yet). Instead we grep route
definitions for the two things that most commonly bite in production:
missing request validation and missing auth on non-trivial routes.

This is intentionally shallow, and the README says so.
"""

from __future__ import annotations

import re
from pathlib import Path

ROUTE_DECORATOR_RE = re.compile(
    r"@\w+\.(get|post|put|patch|delete)\(", re.IGNORECASE
)
AUTH_HINT_RE = re.compile(r"depends\(|current_user|require_auth|login_required", re.IGNORECASE)
PYDANTIC_HINT_RE = re.compile(r":\s*\w+\s*=\s*(Body|Depends)|BaseModel")


def scan_routes(repo_path: str) -> list[dict]:
    findings = []
    for py_file in Path(repo_path).rglob("*.py"):
        if any(part.startswith(".") or part in ("venv", ".venv", "node_modules") for part in py_file.parts):
            continue
        try:
            text = py_file.read_text(errors="ignore")
        except OSError:
            continue
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if not ROUTE_DECORATOR_RE.search(line):
                continue
            method = ROUTE_DECORATOR_RE.search(line).group(1).lower()
            # Look at the decorator + following function signature for hints.
            window = "\n".join(lines[i : min(i + 4, len(lines))])
            has_auth_hint = bool(AUTH_HINT_RE.search(window))
            has_validation_hint = bool(PYDANTIC_HINT_RE.search(window))
            if method in ("post", "put", "patch") and not has_validation_hint:
                findings.append(
                    {
                        "file": str(py_file.relative_to(repo_path)),
                        "line": i + 1,
                        "issue": "mutating_route_no_validation",
                        "detail": f"{method.upper()} route has no obvious pydantic/body validation nearby.",
                    }
                )
            if method in ("post", "put", "patch", "delete") and not has_auth_hint:
                findings.append(
                    {
                        "file": str(py_file.relative_to(repo_path)),
                        "line": i + 1,
                        "issue": "mutating_route_no_auth_hint",
                        "detail": f"{method.upper()} route has no obvious auth/dependency check nearby.",
                    }
                )
    return findings