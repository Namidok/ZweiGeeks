"""Cheap stack detection so we don't run Python-only tools against a PHP
(or Node, or anything-else) repo and silently report "0 findings" because
the tool never applied in the first place.
"""

from __future__ import annotations

from pathlib import Path

PYTHON_MARKERS = {"requirements.txt", "pyproject.toml", "setup.py", "Pipfile"}
PHP_MARKERS = {"composer.json", "composer.lock"}
NODE_MARKERS = {"package.json"}

EXCLUDE_DIRS = {
    ".git", "node_modules", "vendor", "venv", ".venv", "__pycache__",
    ".pytest_cache", "dist", "build", ".idea", ".vscode",
}


def detect_stack(repo_path: str) -> set[str]:
    root = Path(repo_path)
    if not root.is_dir():
        return set()

    stack: set[str] = set()
    top_level = {p.name for p in root.iterdir()}
    if PYTHON_MARKERS & top_level:
        stack.add("python")
    if PHP_MARKERS & top_level:
        stack.add("php")
    if NODE_MARKERS & top_level:
        stack.add("node")

    if stack:
        return stack

    # No marker files at the top level - fall back to a quick extension scan.
    for p in root.rglob("*"):
        if any(part in EXCLUDE_DIRS for part in p.parts):
            continue
        if p.suffix == ".py":
            stack.add("python")
        elif p.suffix == ".php":
            stack.add("php")
        elif p.suffix in (".js", ".ts", ".jsx", ".tsx"):
            stack.add("node")
        if len(stack) >= 3:
            break
    return stack