"""All git operations are scoped to a throwaway local branch.

We never push, never touch origin, and never write to the branch the user
had checked out. Worst case, the user deletes the branch we made.
"""

from __future__ import annotations

import time

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
    if repo.is_dirty(untracked_files=False):
        raise RuntimeError(
            "Working tree has uncommitted changes. Commit or stash them "
            "before running the auditor, so we can safely branch."
        )
    branch_name = f"{prefix}/{int(time.time())}"
    new_branch = repo.create_head(branch_name)
    new_branch.checkout()
    return branch_name


def commit_all(repo: Repo, message: str) -> bool:
    """Stage and commit everything. Returns False if nothing changed."""
    repo.git.add(A=True)
    if not repo.is_dirty(untracked_files=True) and not repo.index.diff("HEAD"):
        return False
    diff = repo.index.diff("HEAD")
    if not diff and not repo.untracked_files:
        return False
    repo.index.commit(message)
    return True


def diff_against(repo: Repo, base_ref: str) -> str:
    return repo.git.diff(base_ref, "HEAD")


def return_to_original_branch(repo: Repo, original_branch_name: str) -> None:
    repo.git.checkout(original_branch_name)