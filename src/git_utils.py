"""All git operations are scoped to a throwaway local branch.

We never push, never touch origin, and never write to the branch the user
had checked out. Worst case, the user deletes the branch we made.
"""

from __future__ import annotations

import re
import subprocess
import time
from typing import Optional

from git import Repo


def open_repo(repo_path: str) -> Repo:
    repo = Repo(repo_path)
    if repo.bare:
        raise ValueError("Repo is bare, nothing to check out.")
    return repo


def create_sandbox_branch(repo: Repo, prefix: str = "zweigeeks") -> str:
    """Create and check out a new branch off the current HEAD.

    Refuses to run on a dirty working tree so we never clobber uncommitted
    work the user cares about.
    """
    leftover = sandbox_branch_checked_out(repo, prefix)
    if leftover:
        raise RuntimeError(
            f"The repo is still on {leftover}, a ZweiGeeks sandbox branch from an "
            f"earlier run that didn't finish. Switch back to your own branch first "
            f"(e.g. `git checkout main`)."
        )
    if repo.is_dirty(untracked_files=False):
        raise RuntimeError(
            "Working tree has uncommitted changes. Commit or stash them "
            "before running the auditor, so we can safely branch."
        )
    branch_name = f"{prefix}/{int(time.time())}"
    new_branch = repo.create_head(branch_name)
    new_branch.checkout()
    return branch_name


def sandbox_branch_checked_out(repo: Repo, prefix: str = "zweigeeks") -> Optional[str]:
    """The checked-out branch if it's one of our sandbox branches (not a
    `-review` branch, which is the user's to keep), else None."""
    if repo.head.is_detached:
        return None
    name = repo.active_branch.name
    return name if re.fullmatch(rf"{re.escape(prefix)}/\d+", name) else None


def commit_files(repo: Repo, paths: list[str], message: str) -> bool:
    """Commit exactly these repo-relative paths. Returns False if none of
    them changed. Never stages anything else, so stray files in the working
    tree (coverage data, the user's untracked notes) stay out of the branch.
    """
    if not paths:
        return False
    repo.git.add("--", *paths)
    if not repo.index.diff("HEAD"):
        return False
    repo.index.commit(message)
    return True


def commit_tracked_changes(repo: Repo, message: str) -> bool:
    """Commit edits to already-tracked files only (`git add -u`). Used when a
    run is interrupted mid-pass, so partial fixes are kept for review
    without sweeping untracked files into the sandbox branch."""
    repo.git.add(u=True)
    if not repo.index.diff("HEAD"):
        return False
    repo.index.commit(message)
    return True


def diff_against(repo: Repo, base_ref: str) -> str:
    return repo.git.diff(base_ref, "HEAD")


def return_to_original_branch(repo: Repo, original_branch_name: str) -> None:
    repo.git.checkout(original_branch_name)


def open_for_review(repo: Repo, base_branch: str, fixes_branch: str) -> tuple[str, list[str]]:
    """Check out a new `<fixes_branch>-review` branch off `base_branch` with
    the fixes applied as uncommitted changes, so an editor's source-control
    view shows every changed file with a diff. Returns (review branch, files).

    Nothing is committed, and `base_branch` itself is never modified.
    """
    review = f"{fixes_branch}-review"
    if repo.active_branch.name == review:
        raise RuntimeError(f"The fixes are already open for review: the repo is on {review}.")
    if repo.is_dirty(untracked_files=False):
        raise RuntimeError(
            "The repo has uncommitted changes. Commit or stash them first, so the "
            "fixes don't get mixed in with your own work."
        )
    if review in [head.name for head in repo.heads]:
        raise RuntimeError(
            f"Branch {review} already exists. Check it out, or delete it with "
            f"`git branch -D {review}` and try again."
        )
    # Three dots: only what the sandbox branch changed since it forked.
    span = f"{base_branch}...{fixes_branch}"
    files = [f for f in repo.git.diff("--name-only", span).splitlines() if f]
    if not files:
        raise RuntimeError("The sandbox branch has no changes to review.")
    patch = repo.git.diff("--binary", span) + "\n"

    repo.git.checkout("-b", review, base_branch)
    proc = subprocess.run(
        ["git", "apply", "--whitespace=nowarn"], input=patch, text=True,
        cwd=repo.working_tree_dir, capture_output=True,
    )
    if proc.returncode != 0:
        repo.git.checkout(base_branch)
        repo.git.branch("-D", review)
        raise RuntimeError(f"Couldn't apply the fixes: {proc.stderr.strip()}")
    return review, files
