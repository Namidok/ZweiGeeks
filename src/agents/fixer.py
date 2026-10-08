"""The Fixer: takes one finding at a time and asks the LLM for a TARGETED
edit - search/replace blocks for just the lines that need to change - not
a rewrite of the whole file.

v1 asked for the full new file. On a local 7B model that went wrong in the
ways that matter most: the model re-typed hundreds of lines it wasn't asked
to touch and silently dropped or "simplified" some of them (deleting the
loop that sent the user's question to the AI, weakening a placeholder
check), and files over the context budget couldn't be fixed at all. With
edit blocks, every line outside a block is preserved byte-for-byte by
construction, and a large file can be fixed from a window around the
finding. Every edit is still validated (syntax, exposure, data-flow,
placeholder checks, then an LLM verifier) before it touches disk.
"""

from __future__ import annotations

import ast
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

from .. import config
from ..ollama_client import OllamaTimeout, chat, extract_content, llm_role, was_truncated
from ..schemas import Finding, FixAttempt
from ..tools.project import project_python, tool_path

EventCallback = Optional[Callable[[str, dict], None]]

FIX_SYSTEM_PROMPT = """You are a careful senior engineer fixing ONE specific issue \
in ONE file. You will be given the issue and the file content with line numbers.

Rules:
- Fix the ROOT CAUSE of the described issue, not a symptom. If the file exposes \
secrets, credentials, or debug information, remove that output entirely - escaping \
or reformatting it is not a fix.
- Change as few lines as possible. Do not refactor, rename, reformat, or "improve" \
anything else.
- Never remove or weaken existing checks, validation, or functionality. Code that \
builds data used later in the file must keep doing so.

Output ONLY one or more edit blocks in exactly this format - no explanation, no \
markdown fence:
<<<<<<< SEARCH
existing lines to replace, copied exactly from the file WITHOUT the line numbers
=======
the new lines
>>>>>>> REPLACE

Each SEARCH must be consecutive lines copied exactly from the file - including any \
comment or blank lines between them - and must match exactly one place. Include a \
couple of surrounding lines if needed to make it unique. To delete lines, leave the \
part after ======= empty.
"""


VERIFY_SYSTEM_PROMPT = """You are a strict code reviewer checking a proposed fix \
for ONE specific issue. You will be given the issue and a unified diff of the change \
(and the full file after the change, when it fits).

Answer two questions:
1. resolved: is the issue described genuinely gone after this change? A change that \
only reformats, renames, or touches unrelated lines is NOT a fix.
2. preserves_behavior: apart from fixing the issue, does the file still do everything \
it did before? Look at every removed or changed line: if it built data that is still \
used, performed a check or validation, or was part of a feature, and the new code no \
longer does that, answer false. Removing or redacting the sensitive output, logging, \
or data that the issue itself is about IS the fix - that alone never makes this false.

Output ONLY a JSON object, no markdown fence, no commentary:
{"resolved": true or false, "preserves_behavior": true or false, "reason": "one short sentence"}
"""


def _emit(cb: EventCallback, event: str, payload: dict) -> None:
    if cb:
        cb(event, payload)


def _match_formatting(original: str, new: str) -> str:
    """Models emit LF and drop the trailing newline (extract_content strips
    it too). Put back the original file's line endings and final newline so
    the diff shows only the real change, not every line of a CRLF file.
    """
    new = new.replace("\r\n", "\n")
    if "\r\n" in original:
        new = new.replace("\n", "\r\n")
    newline = "\r\n" if "\r\n" in original else "\n"
    if original.endswith(("\n", "\r\n")) and not new.endswith(newline):
        new += newline
    return new


def _php_lint_error(content: str) -> Optional[str]:
    """`php -l` on content; None if it passes."""
    result = subprocess.run(
        ["php", "-l"], input=content, capture_output=True, text=True, timeout=30,
    )
    if result.returncode == 0:
        return None
    return (result.stdout + result.stderr).strip() or "php -l failed"


def _tree_sitter_php_errors(content: str) -> Optional[int]:
    """Count of syntax error nodes per tree-sitter's PHP grammar, or None if
    tree-sitter isn't installed.
    """
    try:
        import tree_sitter_php
        from tree_sitter import Language, Parser
    except ImportError:
        return None
    parser = Parser(Language(tree_sitter_php.language_php()))
    count, stack = 0, [parser.parse(content.encode("utf-8")).root_node]
    while stack:
        node = stack.pop()
        if node.is_error or node.is_missing:
            count += 1
        stack.extend(node.children)
    return count


def _php_syntax_regression(file_name: str, original: str, new_content: str) -> Optional[str]:
    """Deterministic syntax gate for PHP rewrites: rejects a rewrite that
    breaks syntax the original didn't. Uses `php -l` when PHP is installed,
    otherwise tree-sitter's PHP grammar. None means OK (or can't check).
    """
    if not file_name.endswith(".php"):
        return None
    if shutil.which("php"):
        try:
            new_error = _php_lint_error(new_content)
            if new_error and not _php_lint_error(original):
                return f"`php -l` fails: {new_error}"
        except (OSError, subprocess.SubprocessError):
            pass
        return None
    new_errors = _tree_sitter_php_errors(new_content)
    if new_errors is None:
        return None
    original_errors = _tree_sitter_php_errors(original) or 0
    if new_errors > original_errors:
        return f"PHP parser finds {new_errors} syntax error(s), the original had {original_errors}"
    return None


# Findings about leaking secrets/credentials/debug info. For these, a fix
# that leaves the offending output in place is checked deterministically
# rather than trusting the LLM verifier (which, on a 7B model, has approved
# exactly that kind of non-fix).
_EXPOSURE_HINTS = (
    "secret", "credential", "password", "api key", "api_key", "expos", "leak",
    "disclos", "debug", "sensitive",
)
_SECRET_NAMES = r"client_secret|secret_key|api_?key|passw(?:or)?d|private_?key|access_?token|db_pass"
_SECRET_NAME = re.compile(_SECRET_NAMES, re.IGNORECASE)
# A secret name used as an array key or env/constant lookup - $cfg['api_key'],
# getenv('OPENAI_API_KEY') - i.e. the value is being read, not just named.
# Deliberately not any function argument: die('Set GOOGLE_CLIENT_SECRET...')
# names the secret in a message without reading it.
_SECRET_ACCESS = re.compile(
    rf"""\[\s*['"][^'"]*(?:{_SECRET_NAMES})[^'"]*['"]\s*\]"""
    rf"""|\b(?:getenv|env|constant)\s*\(\s*['"][^'"]*(?:{_SECRET_NAMES})[^'"]*['"]\s*\)""",
    re.IGNORECASE,
)
_STRING_LITERAL = re.compile(r"'(?:[^'\\]|\\.)*'" + r'|"(?:[^"\\]|\\.)*"')
_OUTPUT_CALL = re.compile(
    r"\b(echo|print|printf|print_r|var_dump|var_export|die|exit)\b", re.IGNORECASE,
)


def _reads_secret(line: str) -> bool:
    """True if the line reads a secret-looking value in code - a variable,
    constant, array key, or getenv() argument - as opposed to merely naming
    one in message text ("Add OPENAI_API_KEY to the server...").
    """
    code_only = _STRING_LITERAL.sub("''", line)
    return bool(_SECRET_NAME.search(code_only) or _SECRET_ACCESS.search(line))


def _outputs_secret(line: str) -> bool:
    return bool(_OUTPUT_CALL.search(line)) and _reads_secret(line)


def _is_exposure_finding(finding: Finding) -> bool:
    text = f"{finding.title} {finding.detail}".lower()
    return any(hint in text for hint in _EXPOSURE_HINTS)


def _exposure_still_present(finding: Finding, original: str, new_content: str) -> Optional[str]:
    """For exposure-type findings only: the fix must change the line the
    finding pointed at, and must not keep any line that outputs a
    secret-looking value. Returns why the rewrite fails, or None.
    """
    if not _is_exposure_finding(finding):
        return None
    new_lines = {line.strip() for line in new_content.splitlines()}
    original_lines = original.splitlines()

    # Only hold the fix to the flagged line when that line itself reads a
    # secret - LLM line numbers are approximate, and a generic line like
    # `exit;` or an unrelated echo survives any correct rewrite.
    if finding.line and 1 <= finding.line <= len(original_lines):
        flagged = original_lines[finding.line - 1].strip()
        if _reads_secret(flagged) and flagged in new_lines:
            return f"line {finding.line} flagged by the finding is unchanged: {flagged[:100]}"

    for line in original_lines:
        stripped = line.strip()
        if _outputs_secret(stripped) and stripped in new_lines:
            return f"still outputs a secret-looking value: {stripped[:100]}"
    return None


def _verify_fix(finding: Finding, original: str, new_content: str) -> tuple[bool, str]:
    """Second LLM pass: does the edit resolve this finding without breaking
    anything else? Raises OllamaTimeout; any other failure counts as not
    verified. Returns (ok, reason).
    """
    diff = "".join(difflib.unified_diff(
        original.splitlines(keepends=True), new_content.splitlines(keepends=True),
        fromfile=f"a/{finding.file}", tofile=f"b/{finding.file}", n=8,
    ))
    user_msg = (
        f"Issue: {finding.title}\n"
        f"Detail: {finding.detail}\n"
        f"File: {finding.file}\n\n"
        f"--- DIFF ---\n{diff}"
    )
    if len(new_content) <= config.FIXER_MAX_FILE_CHARS:
        user_msg += f"\n\n--- FILE AFTER CHANGE ---\n{new_content}"
    with llm_role("verifier"):
        response = chat(
            model=config.AUDITOR_MODEL,
            messages=[
                {"role": "system", "content": VERIFY_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            options={"num_ctx": config.OLLAMA_NUM_CTX},
        )
    text = extract_content(response)
    match = re.search(r"\{.*\}", text, re.DOTALL)
    try:
        verdict = json.loads(match.group(0) if match else text)
    except json.JSONDecodeError:
        return False, f"verifier returned unparseable output: {text[:200]!r}"
    reason = str(verdict.get("reason", ""))
    if not verdict.get("resolved"):
        return False, f"doesn't resolve the issue ({reason})"
    if verdict.get("preserves_behavior") is False:
        if _is_exposure_finding(finding):
            # For exposure fixes the 7B verifier reliably misreads "the log no
            # longer contains session data" as broken behavior - but that IS
            # the fix. The deterministic data-flow, placeholder, undefined-
            # function and syntax checks already ran, so don't let this veto.
            return True, f"{reason} (behavior flag overridden: removing the exposure is the fix)"
        return False, f"breaks existing behavior ({reason})"
    return True, reason


# --- targeted edits -------------------------------------------------------

_EDIT_BLOCK = re.compile(
    r"^<{5,}[ \t]*SEARCH[ \t]*\n(.*?)^={5,}[ \t]*\n(.*?)^>{5,}[ \t]*REPLACE", re.DOTALL | re.MULTILINE,
)
_LINE_NUMBER_PREFIX = re.compile(r"^\s*\d+\| ?")
_WINDOW_LINES = 60


def _numbered_view(lines: list[str], finding_line: Optional[int]) -> Optional[str]:
    """File content with line numbers for the fixer prompt. Files over the
    char budget get a window around the finding instead (None if there's no
    line to center it on).
    """
    start, end = 0, len(lines)
    if len("\n".join(lines)) > config.FIXER_MAX_FILE_CHARS:
        if not finding_line:
            return None
        window = _WINDOW_LINES
        while True:
            start = max(0, finding_line - 1 - window)
            end = min(len(lines), finding_line + window)
            if len("\n".join(lines[start:end])) <= config.FIXER_MAX_FILE_CHARS or window <= 10:
                break
            window -= 10
    width = len(str(end))
    view = "\n".join(f"{i + 1:>{width}}| {lines[i]}" for i in range(start, end))
    if (start, end) != (0, len(lines)):
        view = f"(showing lines {start + 1}-{end} of {len(lines)})\n{view}"
    return view


def _strip_line_numbers(block: str) -> str:
    """7B models sometimes copy the '  12| ' prefixes from the prompt into
    their edit blocks - strip them when every non-blank line has one.
    """
    lines = block.split("\n")
    non_blank = [line for line in lines if line.strip()]
    if non_blank and all(_LINE_NUMBER_PREFIX.match(line) for line in non_blank):
        return "\n".join(_LINE_NUMBER_PREFIX.sub("", line, count=1) for line in lines)
    return block


def _parse_edits(text: str) -> list[tuple[str, str]]:
    edits = []
    for search, replace in _EDIT_BLOCK.findall(text):
        search = _strip_line_numbers(search.removesuffix("\n"))
        replace = _strip_line_numbers(replace.removesuffix("\n"))
        edits.append((search, replace))
    return edits


def _find_block(lines: list[str], wanted: list[str], normalize) -> list[int]:
    size = len(wanted)
    target = [normalize(line) for line in wanted]
    return [
        i for i in range(len(lines) - size + 1)
        if [normalize(line) for line in lines[i : i + size]] == target
    ]


def _leading_ws(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _apply_edits(text: str, edits: list[tuple[str, str]]) -> tuple[Optional[str], str]:
    """Applies edits in order. Matching is on whole lines (a SEARCH of
    `echo $x;` must not match inside `    echo $x;` mid-line), exact first,
    then ignoring surrounding whitespace since small models often get
    indentation slightly off - in which case the replacement is re-indented
    to match the file. Each SEARCH must match exactly one place. Returns
    (new_text, "") or (None, why it failed).
    """
    lines = text.split("\n")
    for n, (search, replace) in enumerate(edits, 1):
        if not search.strip():
            return None, f"edit {n} has an empty SEARCH"
        wanted = search.split("\n")
        new_lines = replace.split("\n") if replace else []
        hits = _find_block(lines, wanted, lambda line: line)
        if not hits:
            hits = _find_block(lines, wanted, str.strip)
            if len(hits) == 1:
                file_indent, search_indent = _leading_ws(lines[hits[0]]), _leading_ws(wanted[0])
                first_new = next((line for line in new_lines if line.strip()), "")
                # Re-indent only when REPLACE is off by the same amount as
                # SEARCH; if the model already indented REPLACE correctly, keep it.
                if (
                    file_indent.startswith(search_indent)
                    and file_indent != search_indent
                    and _leading_ws(first_new) == search_indent
                ):
                    extra = file_indent[len(search_indent):]
                    new_lines = [extra + line if line.strip() else line for line in new_lines]
        if len(hits) != 1:
            where = f"matches {len(hits)} places" if hits else "isn't in the file"
            return None, f"edit {n}'s SEARCH text {where}"
        i = hits[0]
        lines[i : i + len(wanted)] = new_lines
    return "\n".join(lines), ""


# --- deterministic checks for logic regressions ---------------------------

_ASSIGNMENT = re.compile(r"^\s*\$(\w+)\s*((?:\[[^\]]*\])*)\s*(\.=|\+=|-=|=)(?!=)")
_PLACEHOLDER = re.compile(r"YOUR_|_HERE\b|CHANGE_?ME|PLACEHOLDER|REPLACE_?ME", re.IGNORECASE)
_LITERAL_COMPARISON = re.compile(r"""(?:===?|!==?)\s*(['"])(.*?)\1|(['"])(.*?)\3\s*(?:===?|!==?)""")


# Common PHP built-ins, used to recognise legitimate new calls when no `php`
# binary is installed to ask for the exact list.
_COMMON_PHP_BUILTINS = frozenset("""
htmlspecialchars htmlentities html_entity_decode strip_tags nl2br filter_var filter_input
hash_equals hash hash_hmac password_hash password_verify password_needs_rehash random_bytes
random_int bin2hex hex2bin intval floatval boolval strval settype gettype is_int is_numeric
is_string is_array is_bool is_null is_object is_callable ctype_digit ctype_alnum ctype_alpha
trim ltrim rtrim strlen substr str_replace str_ireplace strpos stripos strrpos str_contains
str_starts_with str_ends_with strtolower strtoupper ucfirst ucwords lcfirst sprintf printf
vsprintf number_format explode implode str_split str_pad str_repeat wordwrap mb_strlen
mb_substr mb_strtolower mb_strtoupper preg_match preg_match_all preg_replace preg_quote
preg_split preg_replace_callback in_array array_key_exists array_keys array_values array_map
array_filter array_merge array_slice array_search array_unique array_diff array_intersect
array_push array_pop array_shift array_column array_combine array_fill array_flip
array_reverse count sort ksort usort json_encode json_decode serialize unserialize
urlencode urldecode rawurlencode rawurldecode http_build_query parse_url parse_str basename
dirname pathinfo realpath file_exists is_file is_dir is_readable is_writable
file_get_contents file_put_contents fopen fclose fwrite fread unlink mkdir
error_log trigger_error header headers_sent http_response_code setcookie session_start
session_regenerate_id session_destroy session_id session_status date time strtotime mktime
microtime uniqid md5 sha1 crc32 base64_encode base64_decode openssl_random_pseudo_bytes
addslashes stripslashes escapeshellarg escapeshellcmd mysqli_real_escape_string getenv
putenv ini_get ini_set function_exists method_exists class_exists defined constant
max min abs round floor ceil
""".split())
_PHP_CALL_KEYWORDS = frozenset("""
if elseif while for foreach switch match array list isset empty unset echo print return
function fn include include_once require require_once die exit new catch declare use and
or not clone yield static self parent eval
""".split())
_FUNCTION_CALL = re.compile(r"(?<![\w$>:\\])([A-Za-z_]\w*)\s*\(")
_php_internal_functions: Optional[frozenset[str]] = None


def _php_builtins() -> frozenset[str]:
    """Exact built-in function list from `php` when installed, else the
    common-functions fallback. Cached per process.
    """
    global _php_internal_functions
    if _php_internal_functions is None:
        names = _COMMON_PHP_BUILTINS
        if shutil.which("php"):
            try:
                out = subprocess.run(
                    ["php", "-r", 'echo implode("\\n", get_defined_functions()["internal"]);'],
                    capture_output=True, text=True, timeout=30,
                ).stdout
                if out.strip():
                    names = frozenset(out.lower().split())
            except (OSError, subprocess.SubprocessError):
                pass
        _php_internal_functions = names
    return _php_internal_functions


def _called_functions(code: str) -> set[str]:
    names = set()
    for line in code.split("\n"):
        code_only = _STRING_LITERAL.sub("''", line)
        for m in _FUNCTION_CALL.finditer(code_only):
            if re.search(r"\bnew\s+$", code_only[: m.start()]):
                continue  # `new Foo(` is a class, not a function
            names.add(m.group(1).lower())
    return names - _PHP_CALL_KEYWORDS


def _undefined_functions(repo_path: str, file_name: str, original: str, new_content: str) -> list[str]:
    """New function calls an edit introduces that exist nowhere: not a PHP
    built-in, not defined in the repo, and not already called anywhere in
    it. Small models invent helpers (`validate_api_key(...)`) that make
    every request fatal-error with "Call to undefined function".
    """
    if not file_name.endswith(".php"):
        return []
    candidates = _called_functions(new_content) - _called_functions(original) - _php_builtins()
    if not candidates:
        return []
    known: set[str] = set()
    for php_file in Path(repo_path).rglob("*.php"):
        if ".git" in php_file.parts:
            continue
        try:
            text = php_file.read_text(errors="ignore")
        except OSError:
            continue
        if php_file == Path(repo_path) / file_name:
            text = original  # judge against the file as it was, not the edit
        known |= {n.lower() for n in re.findall(r"\bfunction\s+&?\s*(\w+)\s*\(", text)}
        known |= _called_functions(text)
    return sorted(candidates - known)


def _removed_lines(original: str, new_content: str) -> list[str]:
    a, b = original.split("\n"), new_content.split("\n")
    removed = []
    for tag, i1, i2, _, _ in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag in ("replace", "delete"):
            removed.extend(a[i1:i2])
    return removed


def _assignment_key(line: str) -> Optional[tuple[str, str]]:
    """('messages', '[]=') for `$messages[] = ...` - the variable plus the
    kind of write, so replacing `$x = a` with `$x = b` isn't a removal but
    dropping the only `$x[] = ...` is.
    """
    m = _ASSIGNMENT.match(line)
    if not m:
        return None
    index = m.group(2)
    kind = "[]" if index == "[]" else ("[k]" if index else "")
    return m.group(1), kind + m.group(3)


def _dropped_data_flow(original: str, new_content: str) -> Optional[str]:
    """Catches an edit that deletes the code filling a variable the rest of
    the file still uses - e.g. removing the loop that appends the user's
    question to $messages while $messages is still sent to the API.
    """
    remaining = {k for line in new_content.split("\n") if (k := _assignment_key(line))}
    for line in _removed_lines(original, new_content):
        key = _assignment_key(line)
        if not key or key in remaining:
            continue
        var = key[0]
        if re.search(rf"\${var}\b", new_content):
            return (
                f"removed `{line.strip()[:90]}`, but ${var} is still used and "
                f"nothing else fills it that way any more"
            )
    return None


def _dropped_placeholder_check(original: str, new_content: str) -> Optional[str]:
    """Catches an edit that deletes a comparison against a placeholder such
    as 'YOUR_GOOGLE_CLIENT_ID_HERE' - after which unconfigured credentials
    silently pass as configured.
    """
    for line in _removed_lines(original, new_content):
        for m in _LITERAL_COMPARISON.finditer(line):
            literal = m.group(2) or m.group(4)
            if literal and _PLACEHOLDER.search(literal) and literal not in new_content:
                return (
                    f"removed the check against placeholder '{literal}' - unconfigured "
                    f"credentials would now pass as configured"
                )
    return None


def _python_syntax_regression(rel_path: Optional[str], original: str, new_content: str) -> Optional[str]:
    """A Python file that parsed before must still parse after the edit."""
    if not rel_path or not rel_path.endswith(".py"):
        return None
    try:
        ast.parse(original)
    except SyntaxError:
        return None  # already broken; not this edit's doing
    try:
        ast.parse(new_content)
    except SyntaxError as exc:
        return f"line {exc.lineno}: {exc.msg}"
    return None


def _defined_names(source: str) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
    return names


def _removed_definitions(rel_path: Optional[str], original: str, new_content: str) -> list[str]:
    """Functions/classes the edit deleted. A targeted fix never needs to
    remove one; when a model does, it's usually lost code past the edit."""
    if not rel_path or not rel_path.endswith(".py"):
        return []
    try:
        return sorted(_defined_names(original) - _defined_names(new_content))
    except SyntaxError:
        return []


def _ruff_undefined_names(source: str, rel_path: str) -> Optional[list[str]]:
    ruff = tool_path("ruff")
    try:
        proc = subprocess.run(
            [ruff, "check", "--isolated", "--no-cache", "--select", "F821",
             "--output-format", "json", "--stdin-filename", rel_path, "-"],
            input=source, capture_output=True, text=True, timeout=30,
        )
        items = json.loads(proc.stdout or "[]")
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    names = []
    for item in items:
        m = re.search(r"`([^`]+)`", item.get("message", ""))
        if m:
            names.append(m.group(1))
    return names


def _new_undefined_names(rel_path: Optional[str], original: str, new_content: str) -> list[str]:
    """Names the edit uses that exist nowhere (e.g. `sql.escape(...)` with no
    `sql` imported). These parse and import fine and only crash when the
    line runs, so the syntax and import checks can't see them."""
    if not rel_path or not rel_path.endswith(".py"):
        return []
    before = _ruff_undefined_names(original, rel_path)
    after = _ruff_undefined_names(new_content, rel_path)
    if before is None or after is None:
        return []
    return sorted(set(after) - set(before))


def _module_name(rel_path: str) -> Optional[str]:
    parts = list(Path(rel_path).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts or not all(part.isidentifier() for part in parts):
        return None
    return ".".join(parts)


def _import_check(repo_path: str, rel_path: Optional[str]) -> Optional[tuple[bool, str]]:
    """Imports the module with the project's own interpreter (or ours, if
    the repo has no virtualenv). Returns (ok, last error line), or None when
    the check can't run for this file."""
    if not config.FIXER_IMPORT_CHECK or not rel_path or not rel_path.endswith(".py"):
        return None
    name = _module_name(rel_path)
    if not name:
        return None
    python = project_python(repo_path) or sys.executable
    try:
        proc = subprocess.run(
            [python, "-c", f"import importlib; importlib.import_module({name!r})"],
            cwd=repo_path, capture_output=True, text=True,
            timeout=config.FIXER_IMPORT_TIMEOUT_SECONDS,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    lines = [line for line in proc.stderr.strip().splitlines() if line.strip()]
    return proc.returncode == 0, (lines[-1] if lines else "")


def _reject(on_event: EventCallback, finding: Finding, reason: str, notes: str, timed_out: bool = False) -> FixAttempt:
    _emit(on_event, "fix_rejected", {"finding_id": finding.id, "reason": reason})
    return FixAttempt(
        finding_id=finding.id, file=finding.file, patch_applied=False,
        timed_out=timed_out, notes=notes, reason=reason,
    )


def apply_fix(
    repo_path: str,
    finding: Finding,
    on_event: EventCallback = None,
    previous: Optional[dict] = None,
    attempt_number: int = 0,
) -> FixAttempt:
    """Asks the model for a targeted edit, runs it through the gates, and
    writes it if everything passes. The returned attempt carries the
    rejection reason, the time taken and the full conversation (for the run
    log).

    `previous` ({"reason", "notes"}) is the last rejection of this same
    finding: it goes into the prompt, and `attempt_number` shifts the
    sampling seed, so a retry isn't a byte-identical repeat of a request the
    model already answered wrongly.
    """
    started = time.monotonic()
    transcript: list[dict] = []
    attempt = _apply_fix(repo_path, finding, on_event, transcript, previous, attempt_number)
    attempt.duration_s = round(time.monotonic() - started, 2)
    attempt.transcript = transcript
    return attempt


def _apply_fix(
    repo_path: str, finding: Finding, on_event: EventCallback, transcript: list[dict],
    previous: Optional[dict], attempt_number: int,
) -> FixAttempt:
    if not finding.file:
        return FixAttempt(
            finding_id=finding.id,
            file=None,
            patch_applied=False,
            notes="Finding has no associated file; skipped (needs manual review).",
            reason="no_file",
        )

    target = Path(repo_path) / finding.file
    if not target.exists():
        return FixAttempt(
            finding_id=finding.id,
            file=finding.file,
            patch_applied=False,
            notes=f"File {finding.file} not found on disk; skipped.",
            reason="file_missing",
        )

    _emit(on_event, "fix_start", {"finding_id": finding.id, "file": finding.file})

    # Decode bytes directly: read_text() would translate CRLF to LF and hide
    # the file's real line endings from _match_formatting. Edits are made on
    # an LF copy and the original endings restored afterwards.
    original = target.read_bytes().decode("utf-8", errors="ignore")
    original_lf = original.replace("\r\n", "\n")

    view = _numbered_view(original_lf.split("\n"), finding.line)
    if view is None:
        return FixAttempt(
            finding_id=finding.id,
            file=finding.file,
            patch_applied=False,
            notes=(
                f"File is over FIXER_MAX_FILE_CHARS ({config.FIXER_MAX_FILE_CHARS}) and the "
                "finding has no line number to focus on; skipped."
            ),
            reason="file_too_large",
        )

    user_msg = (
        f"Issue: {finding.title}\n"
        f"Category: {finding.category.value}, Severity: {finding.severity.value}\n"
        f"Detail: {finding.detail}\n"
        + (f"Suggested fix: {finding.resolution}\n" if finding.resolution else "")
        + f"File: {finding.file}"
        + (f" (near line {finding.line})" if finding.line else "")
        + f"\n\n--- FILE CONTENT ---\n{view}"
    )
    if previous:
        user_msg += (
            f"\n\nYour previous edit for this issue was rejected because: {previous.get('notes')} "
            f"({previous.get('reason')}). Make a different edit that avoids that problem: keep it "
            "as small as possible, copy SEARCH lines exactly from the file, and only use names "
            "that are already defined or imported."
        )

    messages = transcript
    messages += [
        {"role": "system", "content": FIX_SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]
    # One retry with feedback when the edit can't be applied: small models
    # often skip a comment or blank line inside SEARCH, and told exactly why
    # it failed they usually get it right the second time.
    for attempt in (1, 2):
        try:
            with llm_role("fixer"):
                response = chat(
                    model=config.FIXER_MODEL,
                    messages=list(messages),
                    options={
                        "num_ctx": config.OLLAMA_NUM_CTX,
                        "num_predict": config.FIXER_NUM_PREDICT,
                        # Fixer calls only: audits and the verifier keep the fixed seed.
                        "seed": config.OLLAMA_SEED + attempt_number,
                    },
                    timeout=config.FIXER_TIMEOUT_SECONDS,
                )
        except OllamaTimeout as exc:
            return _reject(
                on_event, finding, "timeout",
                f"Ollama didn't finish fixing {finding.file} within {config.FIXER_TIMEOUT_SECONDS}s. "
                f"Either the model is too slow for this file (raise FIXER_TIMEOUT_SECONDS) or the "
                f"runner is wedged (check `ollama ps` for a model stuck on 'Stopping...'). ({exc})",
                timed_out=True,
            )
        except Exception as exc:  # noqa: BLE001 - one finding's failure shouldn't kill the whole fix pass
            return _reject(
                on_event, finding, "ollama_unreachable",
                f"Could not reach Ollama at {config.OLLAMA_HOST} with model "
                f"'{config.FIXER_MODEL}'. Is `ollama serve` running and have you "
                f"run `ollama pull {config.FIXER_MODEL}`? ({exc})",
            )

        if was_truncated(response):
            # Hit FIXER_NUM_PREDICT - almost always a repetition loop. Applying
            # whichever blocks happened to complete could leave a half-fix.
            return _reject(
                on_event, finding, "truncated",
                f"Rejected: the model's reply hit the {config.FIXER_NUM_PREDICT}-token cap "
                "(usually a repetition loop), so its edits may be incomplete.",
            )
        reply = extract_content(response)
        messages.append({"role": "assistant", "content": reply})
        edits = _parse_edits(reply)
        if edits:
            new_lf, why = _apply_edits(original_lf, edits)
            reason = "edit_mismatch"
        else:
            new_lf, why = None, f"the reply had no usable SEARCH/REPLACE edit blocks: {reply[:200]!r}"
            reason = "no_edits"
        if new_lf is not None:
            break
        if attempt == 2:
            return _reject(on_event, finding, reason, f"Rejected after a retry: {why}.")
        _emit(on_event, "fix_retry", {"finding_id": finding.id, "reason": why})
        messages += [
            {"role": "user", "content": (
                f"That edit couldn't be applied: {why}. Send the edit blocks again. Each "
                "SEARCH must be consecutive lines copied exactly from the file - including "
                "any comment and blank lines between them - without the line numbers."
            )},
        ]

    new_content = _match_formatting(original, new_lf)
    if new_content == original:
        return _reject(on_event, finding, "no_change", "Rejected: the edits don't change the file.")

    syntax_error = _php_syntax_regression(finding.file, original, new_content)
    if syntax_error:
        return _reject(on_event, finding, "php_syntax_error", f"Rejected: edit breaks PHP syntax - {syntax_error}")

    py_syntax_error = _python_syntax_regression(finding.file, original_lf, new_lf)
    if py_syntax_error:
        return _reject(
            on_event, finding, "python_syntax_error",
            f"Rejected: edit breaks Python syntax - {py_syntax_error}",
        )

    removed = _removed_definitions(finding.file, original_lf, new_lf)
    if removed:
        return _reject(
            on_event, finding, "removed_definition",
            f"Rejected: edit deletes existing code ({', '.join(removed)}) instead of fixing it.",
        )

    undefined_names = _new_undefined_names(finding.file, original_lf, new_lf)
    if undefined_names:
        return _reject(
            on_event, finding, "undefined_name",
            f"Rejected: edit uses name(s) that aren't defined or imported anywhere in the file: "
            f"{', '.join(undefined_names)} - this would crash when the line runs.",
        )

    exposure = _exposure_still_present(finding, original, new_content)
    if exposure:
        return _reject(
            on_event, finding, "exposure_still_present",
            f"Rejected: edit doesn't remove the exposure - {exposure}",
        )

    regression = _dropped_data_flow(original_lf, new_lf) or _dropped_placeholder_check(original_lf, new_lf)
    if regression:
        return _reject(on_event, finding, "breaks_behavior", f"Rejected: edit {regression}.")

    undefined = _undefined_functions(repo_path, finding.file, original_lf, new_lf)
    if undefined:
        return _reject(
            on_event, finding, "undefined_function",
            f"Rejected: edit calls function(s) that don't exist anywhere in the repo or PHP: "
            f"{', '.join(undefined)} - this would fatal-error at runtime.",
        )

    _emit(on_event, "fix_verify_start", {"finding_id": finding.id})
    try:
        ok, reason = _verify_fix(finding, original_lf, new_lf)
    except OllamaTimeout as exc:
        return _reject(
            on_event, finding, "verify_timeout",
            f"Rejected: couldn't verify the edit - Ollama timed out. ({exc})", timed_out=True,
        )
    except Exception as exc:  # noqa: BLE001 - an unverified edit is never written
        return _reject(
            on_event, finding, "verify_failed",
            f"Rejected: verification call failed, so the edit wasn't written. ({exc})",
        )
    transcript.append({"role": "verifier", "content": f"{'approved' if ok else 'rejected'}: {reason}"})
    if not ok:
        return _reject(on_event, finding, "not_verified", f"Rejected by verifier: {reason}")

    # Does the module import today? Checked before writing, so a module that
    # was already broken (or needs config we don't have) doesn't block fixes.
    before = _import_check(repo_path, finding.file)
    original_bytes = target.read_bytes()

    # newline="" so write_text doesn't translate the line endings we just restored.
    target.write_text(new_content, newline="")

    if before and before[0]:
        after = _import_check(repo_path, finding.file)
        if after and not after[0]:
            target.write_bytes(original_bytes)
            return _reject(
                on_event, finding, "breaks_import",
                f"Rejected and rolled back: the module stopped importing after the edit ({after[1]}).",
            )

    _emit(on_event, "fix_applied", {"finding_id": finding.id, "file": finding.file})
    return FixAttempt(
        finding_id=finding.id,
        file=finding.file,
        patch_applied=True,
        notes=f"Edited by fixer agent ({len(edits)} edit block(s)) and verified: {reason}",
    )
