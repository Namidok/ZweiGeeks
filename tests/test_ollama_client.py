import os
import signal
import subprocess
import threading
import time
from types import SimpleNamespace
from unittest import mock

import pytest

from src import config, ollama_client
from src.ollama_client import OllamaTimeout


class FakeClient:
    """Stands in for ollama.Client. `behavior` decides what chat() does."""

    calls: list[dict] = []
    behavior = staticmethod(lambda **kw: {"message": {"content": "ok"}})

    def __init__(self, host=None, **kwargs):
        self.host = host

    def chat(self, model, messages, options):
        FakeClient.calls.append({"model": model, "messages": messages, "options": options})
        return FakeClient.behavior(model=model, messages=messages, options=options)


@pytest.fixture
def fake_client(monkeypatch):
    FakeClient.calls = []
    FakeClient.behavior = staticmethod(lambda **kw: {"message": {"content": "ok"}})
    monkeypatch.setattr(ollama_client.ollama, "Client", FakeClient)
    return FakeClient


# --- the hard timeout ------------------------------------------------------

def test_hung_call_raises_ollama_timeout_within_roughly_the_timeout(fake_client):
    release = threading.Event()
    fake_client.behavior = staticmethod(lambda **kw: release.wait(10))
    try:
        start = time.monotonic()
        with pytest.raises(OllamaTimeout, match="did not respond within 0.3s"):
            ollama_client.chat("m", [{"role": "user", "content": "hi"}], timeout=0.3)
        elapsed = time.monotonic() - start
    finally:
        release.set()  # let the abandoned worker thread finish
    assert 0.25 <= elapsed < 2.0


def test_default_timeout_comes_from_config(monkeypatch):
    seen = {}

    def fake_once(model, messages, options, timeout):
        seen["timeout"] = timeout
        return "ok"

    monkeypatch.setattr(config, "OLLAMA_TIMEOUT_SECONDS", 17)
    monkeypatch.setattr(ollama_client, "_chat_once", fake_once)
    ollama_client.chat("m", [])
    assert seen["timeout"] == 17


def test_error_from_underlying_call_is_reraised(fake_client):
    def boom(**kw):
        raise ConnectionError("connection refused")

    fake_client.behavior = staticmethod(boom)
    with pytest.raises(ConnectionError, match="connection refused"):
        ollama_client.chat("m", [], timeout=5)


def test_successful_call_returns_response_and_merges_options(fake_client, monkeypatch):
    monkeypatch.setattr(config, "OLLAMA_SEED", 42)
    monkeypatch.setattr(config, "OLLAMA_NUM_PREDICT", 1024)
    monkeypatch.setattr(config, "OLLAMA_TEMPERATURE", None)

    resp = ollama_client.chat("m", [], options={"num_ctx": 8192, "seed": 7}, timeout=5)

    assert resp == {"message": {"content": "ok"}}
    # Caller options win over config defaults; unset temperature isn't sent.
    assert fake_client.calls[0]["options"] == {"seed": 7, "num_predict": 1024, "num_ctx": 8192}


def test_temperature_is_sent_when_configured(fake_client, monkeypatch):
    monkeypatch.setattr(config, "OLLAMA_TEMPERATURE", 0.2)
    ollama_client.chat("m", [], timeout=5)
    assert fake_client.calls[0]["options"]["temperature"] == 0.2


# --- restart-on-timeout gating ---------------------------------------------

@pytest.fixture
def restart_spies(monkeypatch):
    once = mock.Mock(side_effect=[OllamaTimeout("hung"), "second try ok"])
    wedged = mock.Mock(return_value=True)
    restart = mock.Mock(return_value=True)
    monkeypatch.setattr(ollama_client, "_chat_once", once)
    monkeypatch.setattr(ollama_client, "_looks_wedged", wedged)
    monkeypatch.setattr(ollama_client, "_restart_local_ollama", restart)
    return SimpleNamespace(once=once, wedged=wedged, restart=restart)


def test_no_restart_attempt_when_auto_restart_is_off(restart_spies, monkeypatch):
    monkeypatch.setattr(config, "OLLAMA_AUTO_RESTART_ON_TIMEOUT", False)
    with pytest.raises(OllamaTimeout):
        ollama_client.chat("m", [])
    restart_spies.wedged.assert_not_called()
    restart_spies.restart.assert_not_called()
    assert restart_spies.once.call_count == 1


def test_restarts_and_retries_once_when_wedged(restart_spies, monkeypatch):
    monkeypatch.setattr(config, "OLLAMA_AUTO_RESTART_ON_TIMEOUT", True)
    assert ollama_client.chat("m", []) == "second try ok"
    restart_spies.restart.assert_called_once()
    assert restart_spies.once.call_count == 2


def test_slow_but_healthy_server_is_not_killed(restart_spies, monkeypatch):
    monkeypatch.setattr(config, "OLLAMA_AUTO_RESTART_ON_TIMEOUT", True)
    restart_spies.wedged.return_value = False
    with pytest.raises(OllamaTimeout):
        ollama_client.chat("m", [])
    restart_spies.restart.assert_not_called()


def test_no_retry_when_nothing_was_killed(restart_spies, monkeypatch):
    monkeypatch.setattr(config, "OLLAMA_AUTO_RESTART_ON_TIMEOUT", True)
    restart_spies.restart.return_value = False
    with pytest.raises(OllamaTimeout):
        ollama_client.chat("m", [])
    assert restart_spies.once.call_count == 1


# --- the restart itself (subprocess + kill fully mocked) ------------------

def test_restart_never_touches_a_remote_host(monkeypatch):
    monkeypatch.setattr(config, "OLLAMA_HOST", "http://gpu-box.internal:11434")
    run = mock.Mock()
    monkeypatch.setattr(ollama_client.subprocess, "run", run)
    assert ollama_client._restart_local_ollama() is False
    run.assert_not_called()


def test_restart_kills_only_port_listeners_and_skips_own_pid(monkeypatch):
    monkeypatch.setattr(config, "OLLAMA_HOST", "http://localhost:11434")
    run = mock.Mock(return_value=SimpleNamespace(stdout=f"{os.getpid()}\n4242\nnot-a-pid\n"))
    kill = mock.Mock()
    sleep = mock.Mock()
    monkeypatch.setattr(ollama_client.subprocess, "run", run)
    monkeypatch.setattr(ollama_client.os, "kill", kill)
    monkeypatch.setattr(ollama_client.time, "sleep", sleep)

    assert ollama_client._restart_local_ollama() is True

    cmd = run.call_args.args[0]
    assert cmd[:2] == ["lsof", "-ti"] and "tcp:11434" in cmd and "-sTCP:LISTEN" in cmd
    kill.assert_called_once_with(4242, signal.SIGKILL)
    sleep.assert_called_once_with(ollama_client._RESTART_WAIT_SECONDS)


def test_restart_reports_false_when_nothing_is_listening(monkeypatch):
    monkeypatch.setattr(config, "OLLAMA_HOST", "127.0.0.1:11434")
    monkeypatch.setattr(ollama_client.subprocess, "run", mock.Mock(return_value=SimpleNamespace(stdout="")))
    kill = mock.Mock()
    monkeypatch.setattr(ollama_client.os, "kill", kill)
    assert ollama_client._restart_local_ollama() is False
    kill.assert_not_called()


@pytest.mark.parametrize(
    ("result", "wedged"),
    [
        (SimpleNamespace(stdout="NAME  ID  SIZE  PROCESSOR  UNTIL\nqwen  abc  5GB  100% GPU  Stopping...\n"), True),
        (SimpleNamespace(stdout="NAME  ID  SIZE  PROCESSOR  UNTIL\nqwen  abc  5GB  100% GPU  4 minutes\n"), False),
        (subprocess.TimeoutExpired("ollama ps", 10), True),
        (FileNotFoundError("ollama"), False),
    ],
)
def test_looks_wedged(monkeypatch, result, wedged):
    run = mock.Mock(side_effect=result) if isinstance(result, BaseException) else mock.Mock(return_value=result)
    monkeypatch.setattr(ollama_client.subprocess, "run", run)
    assert ollama_client._looks_wedged() is wedged


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("http://localhost:11434", ("localhost", 11434)),
        ("127.0.0.1:9999", ("127.0.0.1", 9999)),
        ("gpu-box", ("gpu-box", 11434)),
    ],
)
def test_parse_host(host, expected):
    assert ollama_client._parse_host(host) == expected


# --- response helpers -------------------------------------------------------

def test_extract_content_handles_dict_and_object_responses():
    as_dict = {"message": {"content": "  hello \n"}}
    as_obj = SimpleNamespace(message=SimpleNamespace(content="  hello \n"))
    assert ollama_client.extract_content(as_dict) == "hello"
    assert ollama_client.extract_content(as_obj) == "hello"


def test_extract_content_handles_empty_content():
    assert ollama_client.extract_content({"message": {"content": None}}) == ""
    assert ollama_client.extract_content({}) == ""
    assert ollama_client.extract_content(SimpleNamespace(message=SimpleNamespace(content=None))) == ""


def test_was_truncated_handles_dict_and_object_responses():
    assert ollama_client.was_truncated({"done_reason": "length"}) is True
    assert ollama_client.was_truncated({"done_reason": "stop"}) is False
    assert ollama_client.was_truncated(SimpleNamespace(done_reason="length")) is True
    assert ollama_client.was_truncated(SimpleNamespace()) is False
