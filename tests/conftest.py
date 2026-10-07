"""Shared fixtures. Nothing in this suite talks to a real Ollama server or
kills a real process: the client is replaced with a stub that fails loudly,
and auto-restart is switched off unless a test opts back in with mocks.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from src import config, ollama_client


class _NoRealOllama:
    def __init__(self, *args, **kwargs):
        pass

    def chat(self, *args, **kwargs):
        raise RuntimeError("test tried to call a real Ollama server - mock chat() instead")


@pytest.fixture(autouse=True)
def _isolate_from_ollama(monkeypatch):
    monkeypatch.setattr(ollama_client.ollama, "Client", _NoRealOllama)
    monkeypatch.setattr(config, "OLLAMA_AUTO_RESTART_ON_TIMEOUT", False)


@pytest.fixture(autouse=True)
def _isolate_reports(monkeypatch, tmp_path_factory):
    """Run logs and PDFs go to a temp dir, never the real audit_results/."""
    monkeypatch.setattr(config, "REPORTS_DIR", str(tmp_path_factory.mktemp("reports")))


def ollama_reply(content: str, done_reason: str = "stop") -> dict:
    """The dict shape ollama.Client.chat() returns."""
    return {"message": {"role": "assistant", "content": content}, "done_reason": done_reason}


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def git_repo(tmp_path):
    """An empty git repo with a local identity, so commits work on CI runners
    that have no global git config.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "ZweiGeeks Tests")
    git(repo, "config", "user.email", "tests@example.invalid")
    git(repo, "config", "commit.gpgsign", "false")
    return repo


def write(repo: Path, rel: str, content: str = "") -> Path:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def vuln_repo(git_repo):
    """tests/fixtures/vuln_py committed into a throwaway git repo (the fixture
    is plain files in ZweiGeeks itself - no nested .git)."""
    shutil.copytree(FIXTURES / "vuln_py", git_repo, dirs_exist_ok=True)
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "fixture")
    return git_repo
