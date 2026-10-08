"""All git operations. ZweiGeeks never creates a branch, never stages,
commits, stashes or switches branches in the user's repo, and never pushes.

A run works in a *detached git worktree*: a second checkout of the user's
HEAD in a temp directory. Auditing and fixing happen there, so the user's
working tree, index and branch aren't touched while it runs and they can keep
working. When the run ends, the fixes are applied to the user's working tree
as unstaged changes (`git apply`), file by file: a file the user has changed
themselves, or whose patch doesn't apply cleanly, is skipped and its patch is
saved instead of being overwritten.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from git import GitCommandError, Repo

WORKTREE_PREFIX = "zweigeeks-wt-"


def open_repo(repo_path: str) -> Repo:
    repo = Repo(repo_path)
    if repo.bare:
        raise ValueError("Repo is bare, nothing to check out.")
    return repo


# --- the private worktree ------------------------------------------------------------

def create_worktree(repo: Repo) -> str:
    """`git worktree add --detach <tmpdir> HEAD`: a private checkout of the
    commit the user is on, on a detached HEAD, so no branch is ever created.
    The directory name carries our pid, so a crashed run's leftover can be
    told from a live one."""
    if not repo.head.is_valid():
        raise RuntimeError("The repository has no commits yet. ZweiGeeks audits what's committed.")
    path = tempfile.mkdtemp(prefix=f"{WORKTREE_PREFIX}{os.getpid()}-")
    try:
        repo.git.worktree("add", "--detach", path, "HEAD")
    except GitCommandError:
        shutil.rmtree(path, ignore_errors=True)
        raise
    return path


def remove_worktree(repo: Repo, path: str) -> None:
    """Always safe to call: removes the worktree and prunes git's records of
    it, and never raises, so it can sit in a `finally`."""
    try:
        repo.git.worktree("remove", "--force", path)
    except GitCommandError:
        pass
    # Only ever delete a directory we created.
    if os.path.basename(path).startswith(WORKTREE_PREFIX) and os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    try:
        repo.git.worktree("prune")
    except GitCommandError:
        pass


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def prune_stale_worktrees(repo: Repo) -> list[str]:
    """Removes worktrees left by ZweiGeeks runs that crashed (or were killed
    before they could clean up). A worktree whose owning process is still
    running is left alone."""
    try:
        listing = repo.git.worktree("list", "--porcelain")
    except GitCommandError:
        return []
    removed = []
    for line in listing.splitlines():
        if not line.startswith("worktree "):
            continue
        path = line[len("worktree "):]
        m = re.fullmatch(rf"{re.escape(WORKTREE_PREFIX)}(\d+)-.*", os.path.basename(path))
        if m and not _pid_alive(int(m.group(1))):
            remove_worktree(repo, path)
            removed.append(path)
    try:
        repo.git.worktree("prune")
    except GitCommandError:
        pass
    return removed


# --- getting the fixes into the user's working tree --------------------------------------

def _run(cmd: list[str], cwd: str, data: Optional[bytes] = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, input=data, capture_output=True, timeout=120)


def worktree_changed_files(worktree: str) -> list[str]:
    """Files the run changed, relative to the commit the worktree started from."""
    out = _run(["git", "diff", "HEAD", "--name-only", "--no-renames", "-z"], worktree).stdout
    return [p.decode("utf-8", "surrogateescape") for p in out.split(b"\0") if p]


def file_patch(worktree: str, rel: str) -> bytes:
    return _run(["git", "diff", "HEAD", "--binary", "--no-ext-diff", "--no-color", "--no-renames", "--", rel],
                worktree).stdout


def worktree_diff(worktree: str) -> str:
    """The run's whole diff as text (for the UI's Changes tab)."""
    out = _run(["git", "diff", "HEAD", "--no-ext-diff", "--no-color", "--no-renames"], worktree).stdout
    return out.decode("utf-8", "replace")


def user_modified_files(repo_path: str) -> set[str]:
    """Tracked files with changes the user hasn't committed (staged or not).
    Untracked files don't count: the worktree never contains them."""
    out = _run(["git", "status", "--porcelain=v1", "-z", "--untracked-files=no"], repo_path).stdout
    parts = out.split(b"\0")
    files: set[str] = set()
    i = 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:].decode("utf-8", "surrogateescape")
        files.add(path)
        if b"R" in status or b"C" in status:  # a rename/copy lists its source next
            if i < len(parts):
                files.add(parts[i].decode("utf-8", "surrogateescape"))
            i += 1
    return files


def apply_to_working_tree(repo_path: str, worktree: str, out_dir: Path) -> dict:
    """Applies the worktree's changes to the user's working tree, file by file,
    as plain unstaged modifications (`git apply` without --index: nothing is
    staged, committed or stashed).

    A file is skipped, and its patch saved to `out_dir/skipped.patch`, when
    the user has uncommitted changes in it or `git apply --check` says its
    patch doesn't apply cleanly. The whole diff is also saved to
    `out_dir/fixes.patch` first, so nothing is lost if applying fails.

    Returns {"applied": [files], "skipped": [{"file", "reason"}],
    "skipped_patch": path or None, "fixes_patch": path or None}.
    """
    files = worktree_changed_files(worktree)
    result: dict = {"applied": [], "skipped": [], "skipped_patch": None, "fixes_patch": None}
    if not files:
        return result
    out_dir.mkdir(parents=True, exist_ok=True)
    patches = {rel: file_patch(worktree, rel) for rel in files}
    fixes = out_dir / "fixes.patch"
    fixes.write_bytes(b"".join(p if p.endswith(b"\n") else p + b"\n" for p in patches.values()))
    result["fixes_patch"] = str(fixes)

    busy = user_modified_files(repo_path)
    skipped_patches = []
    for rel, patch in patches.items():
        reason = None
        if rel in busy:
            reason = "you have uncommitted changes in this file"
        else:
            check = _run(["git", "apply", "--check", "-"], repo_path, patch)
            if check.returncode != 0:
                reason = "the fix doesn't apply cleanly to your current file: " + \
                    check.stderr.decode("utf-8", "replace").strip().splitlines()[-1][:160]
            else:
                applied = _run(["git", "apply", "-"], repo_path, patch)
                if applied.returncode != 0:
                    reason = "git apply failed: " + applied.stderr.decode("utf-8", "replace").strip()[:160]
        if reason:
            result["skipped"].append({"file": rel, "reason": reason})
            skipped_patches.append(patch if patch.endswith(b"\n") else patch + b"\n")
        else:
            result["applied"].append(rel)
    if skipped_patches:
        skipped = out_dir / "skipped.patch"
        skipped.write_bytes(b"".join(skipped_patches))
        result["skipped_patch"] = str(skipped)
    return result


# --- helpers kept from the earlier sandbox-branch flow --------------------------------------
# Not used by the worktree flow; kept until it's decided whether to remove them.

def sandbox_branch_checked_out(repo: Repo, prefix: str = "zweigeeks") -> Optional[str]:
    """The checked-out branch if it's one of the old sandbox branches
    (`zweigeeks/<unix-time>`), else None."""
    if repo.head.is_detached:
        return None
    name = repo.active_branch.name
    return name if re.fullmatch(rf"{re.escape(prefix)}/\d+", name) else None


def commit_files(repo: Repo, paths: list[str], message: str) -> bool:
    """Commit exactly these repo-relative paths. Returns False if none of
    them changed."""
    if not paths:
        return False
    repo.git.add("--", *paths)
    if not repo.index.diff("HEAD"):
        return False
    repo.index.commit(message)
    return True


def commit_tracked_changes(repo: Repo, message: str) -> bool:
    """Commit edits to already-tracked files only (`git add -u`)."""
    repo.git.add(u=True)
    if not repo.index.diff("HEAD"):
        return False
    repo.index.commit(message)
    return True


def diff_against(repo: Repo, base_ref: str) -> str:
    return repo.git.diff(base_ref, "HEAD")
