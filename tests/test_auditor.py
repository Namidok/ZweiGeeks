import json
from pathlib import Path
from unittest import mock

import pytest
from conftest import git, ollama_reply, write

from src import config
from src.agents import auditor
from src.ollama_client import OllamaTimeout
from src.schemas import Category, Finding, Severity

ONE_FINDING = [{
    "category": "security", "severity": "high", "line": 3,
    "title": "SQL built from request input", "detail": "d", "resolution": "r",
}]


def review(tmp_path, monkeypatch, llm_text: str):
    src_file = write(tmp_path, "app/handler.js", "const q = 'SELECT ' + req.query.id;\n")
    monkeypatch.setattr(auditor, "chat", mock.Mock(return_value=ollama_reply(llm_text)))
    return auditor._review_file_with_llm("m", src_file, tmp_path, 12000, 8192)


# --- parsing the per-file review -------------------------------------------

@pytest.mark.parametrize(
    "llm_text",
    [
        json.dumps(ONE_FINDING),
        "```json\n" + json.dumps(ONE_FINDING, indent=2) + "\n```",
        "```\n" + json.dumps(ONE_FINDING) + "\n```",
        "Sure! Here is my review:\n" + json.dumps(ONE_FINDING) + "\nLet me know if you need more.",
    ],
    ids=["bare", "fenced-json", "fenced-plain", "junk-around"],
)
def test_review_parses_array_out_of_llm_output(tmp_path, monkeypatch, llm_text):
    items = review(tmp_path, monkeypatch, llm_text)
    assert len(items) == 1
    assert items[0]["title"] == "SQL built from request input"
    assert items[0]["file"] == str(Path("app/handler.js"))  # attributed to the reviewed file


def test_review_empty_array_means_reviewed_and_clean(tmp_path, monkeypatch):
    assert review(tmp_path, monkeypatch, "[]") == []


@pytest.mark.parametrize(
    "llm_text",
    [
        json.dumps(ONE_FINDING[0]),           # a bare object, not a list
        '"no issues"',                         # valid JSON, wrong type
        "I could not find any problems.",      # no JSON at all
        "[{'category': 'security',]",          # bracketed but not JSON
    ],
    ids=["object", "string", "prose", "broken"],
)
def test_review_returns_none_for_unusable_output(tmp_path, monkeypatch, llm_text):
    # None (not []) so an unparseable reply is never mistaken for "nothing found".
    assert review(tmp_path, monkeypatch, llm_text) is None


def test_review_skips_empty_files_without_calling_the_model(tmp_path, monkeypatch):
    f = write(tmp_path, "empty.js", "   \n")
    chat = mock.Mock()
    monkeypatch.setattr(auditor, "chat", chat)
    assert auditor._review_file_with_llm("m", f, tmp_path, 12000, 8192) == []
    chat.assert_not_called()


def test_review_lets_ollama_timeouts_propagate(tmp_path, monkeypatch):
    f = write(tmp_path, "a.js", "x()\n")
    monkeypatch.setattr(auditor, "chat", mock.Mock(side_effect=OllamaTimeout("hung")))
    with pytest.raises(OllamaTimeout):
        auditor._review_file_with_llm("m", f, tmp_path, 12000, 8192)


def test_review_truncates_file_to_max_chars(tmp_path, monkeypatch):
    f = write(tmp_path, "big.js", "a" * 500)
    chat = mock.Mock(return_value=ollama_reply("[]"))
    monkeypatch.setattr(auditor, "chat", chat)
    auditor._review_file_with_llm("m", f, tmp_path, 100, 8192)
    user_msg = chat.call_args.kwargs["messages"][1]["content"]
    assert "a" * 100 in user_msg and "a" * 101 not in user_msg


# --- run_audit: circuit breaker and incomplete-audit reporting ------------

@pytest.fixture
def js_repo(tmp_path):
    """Five plain JS files: a node stack, so none of the Python tools
    (bandit/pip-audit/ruff/pytest) run - only the per-file LLM review.
    """
    for name in "abcde":
        write(tmp_path, f"{name}.js", f"function {name}() {{ return 1; }}\n")
    return tmp_path


def fake_chat(per_file):
    """Routes the improvements call separately so per_file only sees file reviews."""
    def _chat(model, messages, options=None, timeout=None):
        if messages[0]["content"] == auditor.IMPROVEMENT_SYSTEM_PROMPT:
            return ollama_reply('["Add CI"]')
        return per_file(messages)
    return mock.Mock(side_effect=_chat)


def file_review_calls(chat_mock) -> int:
    return sum(
        1 for c in chat_mock.call_args_list
        if c.kwargs["messages"][0]["content"] == auditor.FILE_REVIEW_SYSTEM_PROMPT
    )


def test_breaker_marks_audit_incomplete_after_n_consecutive_timeouts(js_repo, monkeypatch):
    monkeypatch.setattr(config, "OLLAMA_MAX_CONSECUTIVE_TIMEOUTS", 2)

    def hang(messages):
        raise OllamaTimeout("hung")

    chat = fake_chat(hang)
    monkeypatch.setattr(auditor, "chat", chat)
    events = []

    report = auditor.run_audit(str(js_repo), on_event=lambda e, p: events.append((e, p)))

    assert file_review_calls(chat) == 2  # stopped; didn't grind through all 5 files
    assert report.audit_incomplete is True
    assert report.incomplete_reason == "ollama_wedged"
    assert ("audit_aborted", {"reason": "ollama_wedged", "files_reviewed": 0, "detail": "hung"}) in events


def test_breaker_reports_ollama_error_for_non_timeout_failures(js_repo, monkeypatch):
    monkeypatch.setattr(config, "OLLAMA_MAX_CONSECUTIVE_TIMEOUTS", 3)

    def refused(messages):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(auditor, "chat", fake_chat(refused))

    report = auditor.run_audit(str(js_repo))

    assert report.audit_incomplete is True
    assert report.incomplete_reason == "ollama_error"


def test_a_successful_review_resets_the_breaker(js_repo, monkeypatch):
    monkeypatch.setattr(config, "OLLAMA_MAX_CONSECUTIVE_TIMEOUTS", 2)
    replies = iter([OllamaTimeout("x"), ONE_FINDING, OllamaTimeout("x"), [], OllamaTimeout("x")])

    def flaky(messages):
        r = next(replies)
        if isinstance(r, Exception):
            raise r
        return ollama_reply(json.dumps(r))

    chat = fake_chat(flaky)
    monkeypatch.setattr(auditor, "chat", chat)

    report = auditor.run_audit(str(js_repo))

    assert file_review_calls(chat) == 5
    assert report.audit_incomplete is False
    llm = [f for f in report.findings if f.source_tool == "llm-review"]
    assert [(f.id, f.severity) for f in llm] == [("SEC-001", Severity.HIGH)]
    assert report.improvements == ["Add CI"]


def test_fewer_files_than_the_threshold_all_timing_out_is_still_incomplete(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OLLAMA_MAX_CONSECUTIVE_TIMEOUTS", 3)
    write(tmp_path, "only.js", "x()\n")

    def hang(messages):
        raise OllamaTimeout("hung")

    monkeypatch.setattr(auditor, "chat", fake_chat(hang))

    report = auditor.run_audit(str(tmp_path))

    assert report.audit_incomplete is True
    assert report.incomplete_reason == "ollama_wedged"


def test_all_unparseable_replies_mark_audit_incomplete(js_repo, monkeypatch):
    monkeypatch.setattr(auditor, "chat", fake_chat(lambda messages: ollama_reply("no idea")))

    report = auditor.run_audit(str(js_repo))

    assert report.audit_incomplete is True
    assert report.incomplete_reason == "no_files_reviewed"


def test_invalid_llm_items_are_dropped_not_fatal(js_repo, monkeypatch):
    items = [
        {"category": "made-up", "severity": "high", "title": "bad category", "detail": "d"},
        {"category": "ui", "severity": "medium", "title": "ok", "detail": "d"},
    ]
    monkeypatch.setattr(auditor, "chat", fake_chat(lambda messages: ollama_reply(json.dumps(items))))

    report = auditor.run_audit(str(js_repo))

    titles = {f.title for f in report.findings}
    assert "bad category" not in titles
    assert sum(f.title == "ok" for f in report.findings) == 5
    assert report.audit_incomplete is False


def test_debug_scripts_are_reported_but_not_sent_to_llm_review(git_repo, monkeypatch):
    write(git_repo, "debug.php", "<?php var_dump($_SESSION);")
    write(git_repo, "index.php", "<?php echo 'hi';")
    write(git_repo, "composer.json", "{}")
    git(git_repo, "add", "-A")
    chat = fake_chat(lambda messages: ollama_reply("[]"))
    monkeypatch.setattr(auditor, "chat", chat)

    report = auditor.run_audit(str(git_repo))

    reviewed = [
        c.kwargs["messages"][1]["content"].splitlines()[0] for c in chat.call_args_list
        if c.kwargs["messages"][0]["content"] == auditor.FILE_REVIEW_SYSTEM_PROMPT
    ]
    assert reviewed == ["File: index.php"]
    hygiene = [f for f in report.findings if f.source_tool == "repo-hygiene"]
    assert [f.file for f in hygiene] == ["debug.php"]
    assert hygiene[0].severity == Severity.HIGH


# --- improvements pass -------------------------------------------------------

def _finding():
    return Finding(id="QA-001", category=Category.QUALITY, severity=Severity.LOW,
                   title="t", detail="d", source_tool="ruff")


def test_improvements_skip_the_llm_when_there_are_no_findings(monkeypatch):
    chat = mock.Mock()
    monkeypatch.setattr(auditor, "chat", chat)
    assert auditor._generate_improvements([], ["python"]) == []
    chat.assert_not_called()


def test_improvements_parse_fenced_list_and_drop_non_strings(monkeypatch):
    reply = '```json\n["Add CI", 3, "Add type hints"]\n```'
    monkeypatch.setattr(auditor, "chat", mock.Mock(return_value=ollama_reply(reply)))
    assert auditor._generate_improvements([_finding()], ["python"]) == ["Add CI", "Add type hints"]


def test_improvements_never_fail_the_audit(monkeypatch):
    monkeypatch.setattr(auditor, "chat", mock.Mock(side_effect=OllamaTimeout("hung")))
    assert auditor._generate_improvements([_finding()], ["python"]) == []
