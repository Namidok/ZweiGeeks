"""What counts as the audited project: its own files and its own Python.

Static tools pointed at a directory scan everything under it, including a
virtualenv checked out next to the code - one small repo with a `venv/`
produced 1,193 bandit findings, all in site-packages. Asking git for the
file list (tracked files plus untracked ones that aren't ignored) keeps
dependencies, build output and caches out without a hand-kept exclude list.
"""

from __future__ import annotations

import functools
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Optional

# Fallback when git can't list files (not a repo, git missing).
_FALLBACK_EXCLUDE_DIRS = {
    ".git", "node_modules", "vendor", "venv", ".venv", "env", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".tox", "dist", "build", "site-packages",
}

_VENV_DIRS = ("venv", ".venv", "env")


def project_files(repo_path: str, suffixes: Optional[Iterable[str]] = None) -> list[str]:
    """Repo-relative paths of the project's own files, sorted.

    `suffixes` filters by extension, e.g. {".py"}.
    """
    wanted = {s.lower() for s in suffixes} if suffixes else None
    root = Path(repo_path)
    try:
        proc = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=repo_path, capture_output=True, text=True, timeout=60, check=True,
        )
        rels = [r for r in proc.stdout.split("\0") if r]
    except (OSError, subprocess.SubprocessError):
        rels = [
            str(p.relative_to(root)) for p in root.rglob("*")
            if p.is_file() and not any(part in _FALLBACK_EXCLUDE_DIRS for part in p.relative_to(root).parts)
        ]
    files = []
    venv_dirs: dict[Path, bool] = {}
    for rel in rels:
        if wanted is not None and Path(rel).suffix.lower() not in wanted:
            continue
        if _is_dependency(root, Path(rel), venv_dirs):
            continue
        if (root / rel).is_file():  # tracked-but-deleted files are listed too
            files.append(rel)
    return sorted(set(files))


# Third-party code, skipped even when a project commits it or forgets to
# gitignore it: the audit is about the project's own code.
_DEPENDENCY_DIRS = {"node_modules", "site-packages", "vendor", "__pycache__"}


def _is_dependency(root: Path, rel: Path, venv_dirs: dict[Path, bool]) -> bool:
    if any(part in _DEPENDENCY_DIRS for part in rel.parts[:-1]):
        return True
    # Any folder holding a pyvenv.cfg is a virtualenv, whatever it's called.
    for parent in list(rel.parents)[:-1]:
        if parent not in venv_dirs:
            venv_dirs[parent] = (root / parent / "pyvenv.cfg").is_file()
        if venv_dirs[parent]:
            return True
    return False


@functools.lru_cache(maxsize=64)
def origin_repo(path: str) -> str:
    """The main working tree for a path that may be a linked worktree. A run
    audits a private worktree of the user's repo, but the user's virtualenv
    (gitignored, so absent from the worktree) lives in the original."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"], cwd=path, capture_output=True, text=True, timeout=30,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return path
    if not out:
        return path
    common = (Path(path) / out).resolve()
    return str(common.parent) if common.name == ".git" else path


def project_python(repo_path: str) -> Optional[str]:
    """The interpreter of a virtualenv in the repo (or, for a worktree, in
    the repo it was made from), if there is one."""
    for root in dict.fromkeys([Path(repo_path), Path(origin_repo(repo_path))]):
        for name in _VENV_DIRS:
            for candidate in (root / name / "bin" / "python", root / name / "Scripts" / "python.exe"):
                if candidate.is_file():
                    return str(candidate)
    return None


def relative_to_repo(path: Optional[str], repo_path: str) -> Optional[str]:
    """Tools report absolute paths; findings and the Fixer use repo-relative ones."""
    if not path:
        return path
    p = Path(path)
    if not p.is_absolute():
        return Path(path).as_posix()  # "./app.py" (bandit) -> "app.py"
    try:
        return str(p.resolve().relative_to(Path(repo_path).resolve()))
    except ValueError:
        return path


def tool_path(name: str) -> str:
    """A CLI tool (bandit, ruff, pip-audit) from ZweiGeeks' own environment
    first, so it works without an activated venv; then from PATH."""
    candidate = Path(sys.executable).with_name(name)
    if candidate.is_file():
        return str(candidate)
    return shutil.which(name) or name
