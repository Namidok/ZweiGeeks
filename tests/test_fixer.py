from unittest import mock

import pytest
from conftest import ollama_reply, write

from src import config
from src.agents import fixer
from src.ollama_client import OllamaTimeout
from src.schemas import Category, Finding, Severity

HANDLER = (
    "import ast\n"
    "\n"
    "def handler(request):\n"
    "    data = request.args['q']\n"
    "    result = eval(data)\n"
    "    return result\n"
)
EDIT = (
    "<<<<<<< SEARCH\n"
    "    result = eval(data)\n"
    "=======\n"
    "    result = ast.literal_eval(data)\n"
    ">>>>>>> REPLACE"
)
APPROVE = '{"resolved": true, "preserves_behavior": true, "reason": "eval replaced"}'


def eval_finding(file="handler.py", line=5):
    return Finding(id="SEC-001", category=Category.SECURITY, severity=Severity.HIGH, file=file, line=line,
                   title="eval() on request input", detail="Arbitrary code execution.",
                   resolution="Use ast.literal_eval.", source_tool="llm-review")


def scripted_chat(fix_replies, verify_reply=APPROVE):
    """Fixer and verifier share chat(); route by system prompt."""
    fixes = iter(fix_replies)

    def _chat(model, messages, options=None, timeout=None):
        if messages[0]["content"] == fixer.VERIFY_SYSTEM_PROMPT:
            if isinstance(verify_reply, Exception):
                raise verify_reply
            return ollama_reply(verify_reply)
        r = next(fixes)
        if isinstance(r, Exception):
            raise r
        return r if isinstance(r, dict) else ollama_reply(r)

    return mock.Mock(side_effect=_chat)


def fix_calls(chat_mock):
    return [c for c in chat_mock.call_args_list if c.kwargs["messages"][0]["content"] == fixer.FIX_SYSTEM_PROMPT]


@pytest.fixture
def repo(tmp_path):
    write(tmp_path, "handler.py", HANDLER)
    return tmp_path


# --- the happy path ---------------------------------------------------------

def test_applies_a_verified_edit(repo, monkeypatch):
    monkeypatch.setattr(fixer, "chat", scripted_chat([EDIT]))

    attempt = fixer.apply_fix(str(repo), eval_finding())

    assert attempt.patch_applied is True and attempt.timed_out is False
    # Everything outside the edit block is preserved byte-for-byte.
    assert (repo / "handler.py").read_text() == HANDLER.replace("eval(data)", "ast.literal_eval(data)")


def test_edit_blocks_inside_markdown_fences_still_apply(repo, monkeypatch):
    monkeypatch.setattr(fixer, "chat", scripted_chat(["Here you go:\n```python\n" + EDIT + "\n```\n"]))

    attempt = fixer.apply_fix(str(repo), eval_finding())

    assert attempt.patch_applied is True
    content = (repo / "handler.py").read_text()
    assert "```" not in content
    assert "ast.literal_eval(data)" in content


def test_line_number_prefixes_copied_from_the_prompt_are_stripped(repo, monkeypatch):
    numbered = EDIT.replace("    result = eval", "5|     result = eval").replace(
        "    result = ast", "5|     result = ast")
    monkeypatch.setattr(fixer, "chat", scripted_chat([numbered]))

    assert fixer.apply_fix(str(repo), eval_finding()).patch_applied is True
    assert "5|" not in (repo / "handler.py").read_text()


def test_crlf_line_endings_are_preserved(tmp_path, monkeypatch):
    (tmp_path / "handler.py").write_bytes(HANDLER.replace("\n", "\r\n").encode())
    monkeypatch.setattr(fixer, "chat", scripted_chat([EDIT]))

    assert fixer.apply_fix(str(tmp_path), eval_finding()).patch_applied is True
    raw = (tmp_path / "handler.py").read_bytes()
    assert raw.count(b"\r\n") == HANDLER.count("\n")
    assert b"ast.literal_eval(data)\r\n" in raw


# --- truncation & size guards -------------------------------------------------

def test_rejects_a_reply_truncated_at_the_token_cap(repo, monkeypatch):
    chat = scripted_chat([ollama_reply(EDIT, done_reason="length")])
    monkeypatch.setattr(fixer, "chat", chat)

    attempt = fixer.apply_fix(str(repo), eval_finding())

    assert attempt.patch_applied is False
    assert "token cap" in attempt.notes
    assert (repo / "handler.py").read_text() == HANDLER
    assert len(chat.call_args_list) == 1  # never reached the verifier


def test_skips_oversized_file_when_there_is_no_line_to_focus_on(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "FIXER_MAX_FILE_CHARS", 200)
    write(tmp_path, "big.py", "x = 1\n" * 100)
    chat = mock.Mock()
    monkeypatch.setattr(fixer, "chat", chat)

    attempt = fixer.apply_fix(str(tmp_path), eval_finding(file="big.py", line=None))

    assert attempt.patch_applied is False
    assert "FIXER_MAX_FILE_CHARS" in attempt.notes
    chat.assert_not_called()


def test_oversized_file_with_a_line_gets_a_window_not_the_whole_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "FIXER_MAX_FILE_CHARS", 400)
    write(tmp_path, "big.py", "\n".join(f"v{i} = {i}" for i in range(1, 301)) + "\n")
    chat = scripted_chat(["no edits here"] * 2)
    monkeypatch.setattr(fixer, "chat", chat)

    fixer.apply_fix(str(tmp_path), eval_finding(file="big.py", line=150))

    prompt = fix_calls(chat)[0].kwargs["messages"][1]["content"]
    assert "(showing lines" in prompt
    assert "v150 = 150" in prompt
    assert "v1 = 1\n" not in prompt and "v300 = 300" not in prompt


# --- Ollama failures --------------------------------------------------------------

def test_fixer_timeout_is_marked_timed_out(repo, monkeypatch):
    monkeypatch.setattr(fixer, "chat", scripted_chat([OllamaTimeout("hung")]))

    attempt = fixer.apply_fix(str(repo), eval_finding())

    assert attempt.patch_applied is False
    assert attempt.timed_out is True
    assert (repo / "handler.py").read_text() == HANDLER


def test_other_ollama_errors_are_not_counted_as_timeouts(repo, monkeypatch):
    monkeypatch.setattr(fixer, "chat", scripted_chat([ConnectionError("refused")]))

    attempt = fixer.apply_fix(str(repo), eval_finding())

    assert attempt.patch_applied is False
    assert attempt.timed_out is False
    assert "Could not reach Ollama" in attempt.notes


def test_verifier_timeout_is_marked_timed_out_and_nothing_is_written(repo, monkeypatch):
    monkeypatch.setattr(fixer, "chat", scripted_chat([EDIT], verify_reply=OllamaTimeout("hung")))

    attempt = fixer.apply_fix(str(repo), eval_finding())

    assert attempt.timed_out is True
    assert (repo / "handler.py").read_text() == HANDLER


# --- rejection paths -----------------------------------------------------------

def test_verifier_rejection_leaves_the_file_untouched(repo, monkeypatch):
    verdict = '{"resolved": false, "preserves_behavior": true, "reason": "still evals"}'
    monkeypatch.setattr(fixer, "chat", scripted_chat([EDIT], verify_reply=verdict))

    attempt = fixer.apply_fix(str(repo), eval_finding())

    assert attempt.patch_applied is False
    assert "still evals" in attempt.notes
    assert (repo / "handler.py").read_text() == HANDLER


def test_unparseable_verifier_output_is_a_rejection(repo, monkeypatch):
    monkeypatch.setattr(fixer, "chat", scripted_chat([EDIT], verify_reply="looks fine to me"))
    assert fixer.apply_fix(str(repo), eval_finding()).patch_applied is False
    assert (repo / "handler.py").read_text() == HANDLER


def test_unmatched_edit_is_retried_once_with_feedback(repo, monkeypatch):
    wrong = EDIT.replace("result = eval(data)\n=", "result = evaluate(data)\n=")
    chat = scripted_chat([wrong, EDIT])
    monkeypatch.setattr(fixer, "chat", chat)

    attempt = fixer.apply_fix(str(repo), eval_finding())

    assert attempt.patch_applied is True
    retry_messages = fix_calls(chat)[1].kwargs["messages"]
    assert "couldn't be applied" in retry_messages[-1]["content"]


def test_gives_up_after_one_retry(repo, monkeypatch):
    chat = scripted_chat(["I would change eval to literal_eval.", "Still no blocks."])
    monkeypatch.setattr(fixer, "chat", chat)

    attempt = fixer.apply_fix(str(repo), eval_finding())

    assert attempt.patch_applied is False
    assert "after a retry" in attempt.notes
    assert len(fix_calls(chat)) == 2


def test_no_op_edit_is_rejected(repo, monkeypatch):
    noop = "<<<<<<< SEARCH\n    return result\n=======\n    return result\n>>>>>>> REPLACE"
    monkeypatch.setattr(fixer, "chat", scripted_chat([noop]))
    attempt = fixer.apply_fix(str(repo), eval_finding())
    assert attempt.patch_applied is False and "don't change the file" in attempt.notes


def test_finding_without_file_or_missing_file_is_skipped(tmp_path, monkeypatch):
    chat = mock.Mock()
    monkeypatch.setattr(fixer, "chat", chat)
    assert "no associated file" in fixer.apply_fix(str(tmp_path), eval_finding(file=None)).notes
    assert "not found" in fixer.apply_fix(str(tmp_path), eval_finding(file="gone.py")).notes
    chat.assert_not_called()


# --- edit application ------------------------------------------------------------

def test_apply_edits_reindents_when_the_model_drops_indentation():
    text = "def f():\n    if x:\n        y = 1\n"
    new, why = fixer._apply_edits(text, [("y = 1", "y = 2")])
    assert why == "" and new == "def f():\n    if x:\n        y = 2\n"


def test_apply_edits_rejects_ambiguous_and_missing_matches():
    text = "a = 1\nb = 2\na = 1\n"
    assert fixer._apply_edits(text, [("a = 1", "a = 3")]) == (None, "edit 1's SEARCH text matches 2 places")
    assert fixer._apply_edits(text, [("c = 1", "c = 3")]) == (None, "edit 1's SEARCH text isn't in the file")
    assert fixer._apply_edits(text, [("  ", "x")]) == (None, "edit 1 has an empty SEARCH")


def test_apply_edits_matches_whole_lines_only():
    # `echo $x;` must not match inside a longer line.
    new, _ = fixer._apply_edits("    echo $x; // keep\necho $x;\n", [("echo $x;", "echo h($x);")])
    assert new == "    echo $x; // keep\necho h($x);\n"


# --- deterministic regression checks (PHP) ---------------------------------------

def test_dropped_data_flow_is_caught():
    original = "<?php\n$messages = [];\nforeach ($q as $m) {\n  $messages[] = $m;\n}\nsend($messages);\n"
    edited = "<?php\n$messages = [];\nsend($messages);\n"
    assert "$messages is still used" in fixer._dropped_data_flow(original, edited)
    assert fixer._dropped_data_flow(original, original.replace("= $m;", "= trim($m);")) is None


def test_dropped_placeholder_check_is_caught():
    original = "<?php\nif ($id === 'YOUR_GOOGLE_CLIENT_ID_HERE') { die('configure me'); }\nrun();\n"
    assert "YOUR_GOOGLE_CLIENT_ID_HERE" in fixer._dropped_placeholder_check(original, "<?php\nrun();\n")


def test_exposure_fix_must_remove_the_secret_output():
    finding = Finding(id="SEC-002", category=Category.SECURITY, severity=Severity.CRITICAL,
                      file="debug.php", line=2, title="API key exposed", detail="Echoes the secret.",
                      source_tool="llm-review")
    original = "<?php\necho $config['api_key'];\necho 'ok';\n"
    still_leaks = "<?php\n// redacted\necho $config['api_key'];\necho 'ok';\n"
    assert "unchanged" in fixer._exposure_still_present(finding, original, still_leaks)
    assert fixer._exposure_still_present(finding, original, "<?php\necho 'ok';\n") is None


def test_invented_php_helpers_are_flagged(tmp_path, monkeypatch):
    monkeypatch.setattr(fixer, "_php_builtins", lambda: frozenset({"htmlspecialchars"}))
    write(tmp_path, "lib.php", "<?php function render_page($x) { return $x; }")
    original = "<?php\necho render_page($name);\n"
    edited = "<?php\nvalidate_api_key();\necho htmlspecialchars(render_page($name));\n"
    assert fixer._undefined_functions(str(tmp_path), "index.php", original, edited) == ["validate_api_key"]
