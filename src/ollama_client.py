"""Centralized, timeout-safe wrapper around the ollama client.

A local Ollama runner can wedge (server process alive, generation stuck) -
`ollama ps` shows it as stuck on "Stopping..." with no progress. When that
happens, a plain client.chat() call never raises and never returns: it
hangs forever with no exception on our side. Every LLM call in this app
goes through here so a wedged runner turns into a fast, per-call failure
instead of an indefinite silent hang.
"""

from __future__ import annotations

import contextlib
import contextvars
import os
import queue
import signal
import subprocess
import threading
import time
from typing import Callable, Iterator, Optional
from urllib.parse import urlparse

import ollama

from . import config


class OllamaTimeout(Exception):
    """Raised when a call to Ollama doesn't return within the configured timeout."""


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", ""}
_DEFAULT_PORT = 11434
# Seconds to wait after killing Ollama for the Ollama app/launchd to bring it back.
_RESTART_WAIT_SECONDS = 3


def _parse_host(host: str) -> tuple[str, int]:
    """OLLAMA_HOST may be a full URL or a bare 'host[:port]' - handle both."""
    parsed = urlparse(host if "://" in host else f"http://{host}")
    return parsed.hostname or "", parsed.port or _DEFAULT_PORT


def _looks_wedged() -> bool:
    """Only a wedged runner justifies killing Ollama - a slow but healthy one
    just needs more time. Wedged means `ollama ps` shows a model stuck on
    'Stopping...', or `ollama ps` itself can't get an answer from the server.
    If we can't tell (no ollama CLI on PATH), assume healthy and leave it be.
    """
    try:
        out = subprocess.run(
            ["ollama", "ps"], capture_output=True, text=True, timeout=10,
        ).stdout
    except subprocess.TimeoutExpired:
        return True
    except (OSError, subprocess.SubprocessError):
        return False
    return "Stopping" in out


def _restart_local_ollama() -> bool:
    """Kill -9 whatever is LISTENING on the Ollama port and give it a moment
    to be auto-restarted. Returns True if anything was killed.

    Only listeners are targeted: a bare `lsof -ti :<port>` also returns
    clients connected to that port - including this process, whose
    abandoned worker thread still holds a socket to the wedged server.
    Never touches a remote OLLAMA_HOST.
    """
    hostname, port = _parse_host(config.OLLAMA_HOST)
    if hostname not in _LOCAL_HOSTS:
        return False
    try:
        out = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False

    killed = False
    for pid_str in out.split():
        try:
            pid = int(pid_str)
        except ValueError:
            continue
        if pid == os.getpid():
            continue
        try:
            os.kill(pid, signal.SIGKILL)
            killed = True
        except OSError:
            continue
    if killed:
        time.sleep(_RESTART_WAIT_SECONDS)
    return killed


# Per-run accounting of model calls (count, time, role). Context variables,
# so concurrent jobs on different threads don't mix their numbers.
CallListener = Callable[..., None]
_role: contextvars.ContextVar[str] = contextvars.ContextVar("zweigeeks_llm_role", default="other")
_listener: contextvars.ContextVar[Optional[CallListener]] = contextvars.ContextVar(
    "zweigeeks_llm_listener", default=None
)


@contextlib.contextmanager
def llm_role(name: str) -> Iterator[None]:
    """Tags the model calls made inside the block (auditor, fixer, verifier)."""
    token = _role.set(name)
    try:
        yield
    finally:
        _role.reset(token)


@contextlib.contextmanager
def call_listener(listener: CallListener) -> Iterator[None]:
    """`listener(role=, model=, seconds=, ok=)` is called after every model
    call made inside the block, on the calling thread."""
    token = _listener.set(listener)
    try:
        yield
    finally:
        _listener.reset(token)


def chat(model: str, messages: list[dict], options: Optional[dict] = None, timeout: Optional[float] = None):
    """Same shape as ollama.Client(...).chat(...), but enforces a hard
    wall-clock timeout regardless of what the underlying HTTP client does.

    On a timeout, if OLLAMA_AUTO_RESTART_ON_TIMEOUT is on and Ollama actually
    looks wedged (not just slow), kills the local Ollama server once, waits
    for it to come back, and retries the call a single time before raising
    OllamaTimeout.
    """
    started = time.monotonic()
    ok = False
    try:
        response = _chat_with_restart(model, messages, options, timeout or config.OLLAMA_TIMEOUT_SECONDS)
        ok = True
        return response
    finally:
        listener = _listener.get()
        if listener:
            listener(role=_role.get(), model=model, seconds=time.monotonic() - started, ok=ok)


def _chat_with_restart(model: str, messages: list[dict], options: Optional[dict], timeout: float):
    try:
        return _chat_once(model, messages, options, timeout)
    except OllamaTimeout:
        if (
            not config.OLLAMA_AUTO_RESTART_ON_TIMEOUT
            or not _looks_wedged()
            or not _restart_local_ollama()
        ):
            raise
    return _chat_once(model, messages, options, timeout)


def _chat_once(model: str, messages: list[dict], options: Optional[dict], timeout: float):
    """Runs the real call on a background daemon thread and gives up after
    `timeout` seconds, raising OllamaTimeout. The abandoned thread is never
    joined - daemon threads don't block process exit - so it's safe to just
    move on rather than wait for a wedged server to come back.
    """
    result_q: queue.Queue = queue.Queue(maxsize=1)

    def _call():
        try:
            client = ollama.Client(host=config.OLLAMA_HOST)
            # Caller-supplied options win over the config defaults.
            defaults: dict = {"seed": config.OLLAMA_SEED, "num_predict": config.OLLAMA_NUM_PREDICT}
            if config.OLLAMA_TEMPERATURE is not None:
                defaults["temperature"] = config.OLLAMA_TEMPERATURE
            opts = {**defaults, **(options or {})}
            resp = client.chat(model=model, messages=messages, options=opts)
            result_q.put(("ok", resp))
        except Exception as exc:  # noqa: BLE001 - forward any error to the waiting side
            result_q.put(("error", exc))

    t = threading.Thread(target=_call, daemon=True)
    t.start()
    try:
        status, payload = result_q.get(timeout=timeout)
    except queue.Empty:
        raise OllamaTimeout(
            f"Ollama call to model '{model}' did not respond within {timeout}s - "
            f"the local runner is likely wedged. Check `ollama ps` (look for "
            f"'Stopping...' with no progress), kill that ollama process, let it "
            f"restart, and re-run."
        )
    if status == "error":
        raise payload
    return payload


def was_truncated(response) -> bool:
    """True if generation stopped at the num_predict cap rather than finishing."""
    if isinstance(response, dict):
        return response.get("done_reason") == "length"
    return getattr(response, "done_reason", None) == "length"


def extract_content(response) -> str:
    """ollama's client returns a dict or an object depending on version - every
    call site needs both handled the same way, so it lives here once.
    """
    if isinstance(response, dict):
        return (response.get("message", {}).get("content") or "").strip()
    return (response.message.content or "").strip()