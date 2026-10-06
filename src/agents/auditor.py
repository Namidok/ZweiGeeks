"""The Auditor.

v2 design, after v1 produced near-empty results on a PHP repo: v1 only ran
Python-specific tools and only ever showed the LLM a JSON dump of tool
output - it never read an actual source file. Neither problem is fixable
by using a "smarter" model; both are architecture bugs.

v2 does three kinds of work, in order of how much we trust them:

1. Deterministic repo-hygiene checks (repo_hygiene.py) - no LLM involved.
   git ls-files + .gitignore cross-referencing + include/require resolution.
   These are facts, not opinions, and they catch the highest-severity stuff
   (secrets committed to git, an app that can't even boot).
2. Language-specific static tools (bandit/ruff/pytest/api-routes), only run
   when stack_detect says the language is actually present. Their output is
   mapped straight into Finding objects - no LLM synthesis step, so there's
   nothing for the model to hallucinate or mangle.
3. A real per-file LLM review: walk actual source files and ask the model
   to read and reason about each one, the way a human reviewer would. This
   is the only part that needs the LLM, and it's the only part capable of
   catching things like XSS, missing CSRF, or a debug endpoint that leaks a
   secret - none of which a static tool or a JSON-summarization pass can see.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Optional

from .. import config
from ..ollama_client import OllamaTimeout, chat, extract_content
from ..schemas import AuditReport, Category, Finding, Severity
from ..tools import api_checks, quality, repo_hygiene, security, stack_detect, ui_checks

EventCallback = Optional[Callable[[str, dict], None]]

SOURCE_EXTENSIONS = {
    ".py", ".php", ".js", ".ts", ".jsx", ".tsx", ".rb", ".go", ".java", ".html",
}
EXCLUDE_DIRS = {
    ".git", "node_modules", "vendor", "venv", ".venv", "__pycache__",
    ".pytest_cache", "dist", "build", "logs", ".idea", ".vscode",
}
HOT_NAME_HINTS = (
    "auth", "login", "session", "api", "config", "admin", "user",
    "payment", "upload", "debug", "secret", "token",
)

FILE_REVIEW_SYSTEM_PROMPT = """You are a senior security and code reviewer auditing ONE \
file from a larger repository. Actually read and reason about the code - don't just \
pattern-match keywords. Flag concrete issues: hardcoded secrets, injection risks \
(SQL/command/XSS), missing input validation, missing auth/CSRF checks around \
state-changing logic, unsafe file handling, inconsistent output escaping, logging of \
sensitive data, and any other real bug you can see with evidence in this file.

Only report things you can see evidence for IN THIS FILE - don't guess about other files \
you haven't seen. If there's nothing worth flagging, return an empty array.

Output ONLY a JSON array, nothing else:
[{"category": "security|api|ui|testing|quality", "severity": "critical|high|medium|low|info", \
"line": 12, "title": "short title", "detail": "1-3 sentences, specific to this file", \
"resolution": "1-2 sentences: the concrete fix"}]
"""

IMPROVEMENT_SYSTEM_PROMPT = """You are a principal engineer giving high-level improvement \
suggestions for a repository, based on a summary of issues already found by a security/quality \
audit. These are NOT individual bug fixes - give 3 to 6 broader, strategic suggestions \
(architecture, testing strategy, tooling, process) that would meaningfully raise the quality of \
this codebase going forward. One sentence each.

Output ONLY a JSON array of strings, nothing else: ["suggestion one", "suggestion two"]
"""

# Canned resolutions for findings that don't come from the per-file LLM review
# (which supplies its own). Keeps these reliable and free.
_BANDIT_RESOLUTIONS = {
    "hardcoded_password_string": "Move the secret into an environment variable or secrets manager; never hardcode credentials in source.",
    "hardcoded_sql_expressions": "Use parameterized queries / prepared statements instead of building SQL with string formatting.",
    "subprocess_popen_with_shell_equals_true": "Avoid shell=True; pass the command as a list of arguments instead.",
    "request_with_no_cert_validation": "Remove verify=False and let TLS certificates be validated normally.",
}

_SEVERITY_PREFIX = {
    Category.SECURITY: "SEC",
    Category.API: "API",
    Category.UI: "UI",
    Category.TESTING: "TEST",
    Category.QUALITY: "QA",
}


def _emit(cb: EventCallback, event: str, payload: dict) -> None:
    if cb:
        cb(event, payload)


class _FindingCollector:
    """Assigns stable per-category IDs (SEC-001, QA-002, ...) as findings
    come in from whichever source found them.
    """

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}
        self.findings: list[Finding] = []

    def add(
        self,
        category: Category,
        severity: Severity,
        detail: str,
        *,
        file: Optional[str] = None,
        line: Optional[int] = None,
        title: Optional[str] = None,
        source_tool: str = "tool",
        resolution: Optional[str] = None,
    ) -> None:
        prefix = _SEVERITY_PREFIX[category]
        self._counters[prefix] = self._counters.get(prefix, 0) + 1
        self.findings.append(
            Finding(
                id=f"{prefix}-{self._counters[prefix]:03d}",
                category=category,
                severity=severity,
                file=file,
                line=line,
                title=(title or detail[:60]),
                detail=detail,
                resolution=resolution,
                source_tool=source_tool,
            )
        )


def _discover_source_files(
    repo_path: str, max_files: int, max_chars: int, exclude: frozenset[str] = frozenset()
) -> list[Path]:
    root = Path(repo_path)
    candidates: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if str(p.relative_to(root)) in exclude:
            continue
        if any(part in EXCLUDE_DIRS for part in p.parts):
            continue
        if p.suffix.lower() not in SOURCE_EXTENSIONS:
            continue
        try:
            if p.stat().st_size > max_chars * 3:
                continue
        except OSError:
            continue
        candidates.append(p)

    def priority(p: Path) -> tuple:
        hot = any(hint in p.name.lower() for hint in HOT_NAME_HINTS)
        return (0 if hot else 1, str(p))

    candidates.sort(key=priority)
    return candidates[:max_files]


def _review_file_with_llm(
    model: str, file_path: Path, repo_root: Path, max_chars: int, num_ctx: int
) -> Optional[list[dict]]:
    """Returns the model's findings for one file ([] means reviewed, nothing
    found), or None if the model replied but its output couldn't be parsed.
    Ollama errors - OllamaTimeout included - propagate so the caller can
    count them: a failed review must never look like a clean one.
    """
    try:
        content = file_path.read_text(errors="ignore")
    except OSError:
        return []
    if not content.strip():
        return []
    content = content[:max_chars]
    rel = str(file_path.relative_to(repo_root))

    response = chat(
        model=model,
        messages=[
            {"role": "system", "content": FILE_REVIEW_SYSTEM_PROMPT},
            {"role": "user", "content": f"File: {rel}\n\n{content}"},
        ],
        options={"num_ctx": num_ctx},
    )

    text = extract_content(response)
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]

    try:
        items = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("["), text.rfind("]")
        if start == -1 or end == -1:
            return None
        try:
            items = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None

    if not isinstance(items, list):
        return None
    for item in items:
        if isinstance(item, dict):
            item["file"] = rel
    return items


def run_audit(repo_path: str, on_event: EventCallback = None) -> AuditReport:
    raw_outputs: dict[str, str] = {}
    collector = _FindingCollector()

    stack = stack_detect.detect_stack(repo_path)
    _emit(on_event, "stack_detected", {"stack": sorted(stack)})

    # --- 1. deterministic repo hygiene (no LLM) ---
    _emit(on_event, "tool_start", {"tool": "repo-hygiene"})
    secret_hits = repo_hygiene.find_tracked_secrets(repo_path)
    for h in secret_hits:
        collector.add(
            Category.SECURITY, Severity.CRITICAL, h["detail"],
            file=h["file"], source_tool="repo-hygiene",
            title=f"Secret-looking file committed: {h['file']}",
            resolution=(
                "Rotate every credential in this file immediately. Remove it from git "
                "tracking (`git rm --cached <file>`) and purge it from history (git "
                "filter-repo or BFG) - a later commit alone leaves it recoverable. "
                "Confirm the pattern is in .gitignore before committing again."
            ),
        )
    stray_hits = repo_hygiene.find_stray_files(repo_path)
    for h in stray_hits:
        collector.add(
            Category.QUALITY, Severity.LOW, h["detail"],
            file=h["file"], source_tool="repo-hygiene",
            title=f"Stray/backup file committed: {h['file']}",
            resolution=(
                "Delete this file from the repo and rely on git history instead of "
                "in-tree backups. Add its extension to .gitignore to stop it recurring."
            ),
        )
    debug_hits = repo_hygiene.find_debug_scripts(repo_path)
    for h in debug_hits:
        collector.add(
            Category.SECURITY, Severity.HIGH if h["kind"] == "debug" else Severity.MEDIUM,
            h["detail"], file=h["file"], source_tool="repo-hygiene",
            title=f"{h['kind'].capitalize()} script committed: {h['file']}",
            resolution=(
                "Delete this file (git history keeps it if you ever need it again). If "
                "it's genuinely needed, move it outside the web root or behind an admin "
                "auth check - don't patch around what it prints."
            ),
        )
    if "php" in stack:
        broken = repo_hygiene.find_broken_php_includes(repo_path)
        for h in broken:
            collector.add(
                Category.QUALITY, Severity.CRITICAL, h["detail"],
                file=h["file"], line=h.get("line"), source_tool="repo-hygiene",
                title="Broken require/include target",
                resolution=(
                    "Commit the missing file/directory, or fix the require/include path "
                    "if it was renamed or moved. Until then every request that reaches "
                    "this line fatal-errors."
                ),
            )
    _emit(
        on_event, "tool_done",
        {"tool": "repo-hygiene", "count": len(secret_hits) + len(stray_hits) + len(debug_hits)},
    )

    # --- 2. language-specific static tools, mapped straight to Findings ---
    if "python" in stack:
        _emit(on_event, "tool_start", {"tool": "bandit"})
        bandit_issues, bandit_raw = security.run_bandit(repo_path)
        raw_outputs["bandit"] = bandit_raw
        sev_map = {"HIGH": Severity.HIGH, "MEDIUM": Severity.MEDIUM, "LOW": Severity.LOW}
        for issue in bandit_issues:
            test_id = (issue.get("test_id") or "").lower()
            test_name_key = (issue.get("test_name") or "").lower()
            resolution = (
                _BANDIT_RESOLUTIONS.get(test_name_key)
                or _BANDIT_RESOLUTIONS.get(test_id)
                or f"Review and remediate per Bandit's guidance for {issue.get('test_id', 'this check')}."
            )
            collector.add(
                Category.SECURITY,
                sev_map.get(issue.get("issue_severity", "LOW"), Severity.LOW),
                issue.get("issue_text", ""),
                file=issue.get("filename"), line=issue.get("line_number"),
                source_tool="bandit", title=issue.get("test_name", "Bandit finding"),
                resolution=resolution,
            )
        _emit(on_event, "tool_done", {"tool": "bandit", "count": len(bandit_issues)})

        _emit(on_event, "tool_start", {"tool": "pip-audit"})
        pip_audit_deps, pip_audit_raw = security.run_pip_audit(repo_path)
        raw_outputs["pip-audit"] = pip_audit_raw
        pip_audit_count = 0
        for dep in pip_audit_deps:
            for vuln in dep.get("vulns") or []:
                pip_audit_count += 1
                fix_versions = vuln.get("fix_versions") or []
                resolution = (
                    f"Upgrade {dep.get('name', 'this package')} to "
                    f"{' or '.join(fix_versions)} or later."
                    if fix_versions
                    else (
                        f"Review advisory {vuln.get('id', '')} for "
                        f"{dep.get('name', 'this package')}; no fixed version is "
                        f"published yet."
                    )
                )
                collector.add(
                    Category.SECURITY,
                    Severity.HIGH,
                    vuln.get("description")
                    or f"Known vulnerability {vuln.get('id', '')} in "
                    f"{dep.get('name', '')} {dep.get('version', '')}.",
                    source_tool="pip-audit",
                    title=f"{dep.get('name', 'dependency')} {dep.get('version', '')}: "
                    f"{vuln.get('id', 'vulnerability')}",
                    resolution=resolution,
                )
        _emit(on_event, "tool_done", {"tool": "pip-audit", "count": pip_audit_count})

        _emit(on_event, "tool_start", {"tool": "ruff"})
        ruff_issues, ruff_raw = quality.run_ruff(repo_path)
        raw_outputs["ruff"] = ruff_raw
        for issue in ruff_issues:
            collector.add(
                Category.QUALITY, Severity.LOW, issue.get("message", ""),
                file=issue.get("filename"),
                line=(issue.get("location") or {}).get("row"),
                source_tool="ruff", title=issue.get("code", "Ruff finding"),
                resolution=(
                    f"Run `ruff check --fix` to auto-fix where possible, or address "
                    f"rule {issue.get('code', '')} manually."
                ),
            )
        _emit(on_event, "tool_done", {"tool": "ruff", "count": len(ruff_issues)})

        _emit(on_event, "tool_start", {"tool": "pytest"})
        test_summary, test_raw = quality.run_tests_with_coverage(repo_path)
        raw_outputs["pytest"] = test_raw
        if not test_summary.get("passed"):
            collector.add(
                Category.TESTING, Severity.MEDIUM,
                "The existing test suite did not pass (or no tests were collected).",
                source_tool="pytest", title="Test suite not passing",
                resolution=(
                    "Install the repo's own dependencies and re-run pytest inside the "
                    "correct environment; add tests if none exist yet."
                ),
            )
        _emit(on_event, "tool_done", {"tool": "pytest", "passed": test_summary.get("passed")})

        _emit(on_event, "tool_start", {"tool": "api-routes"})
        api_issues = api_checks.scan_routes(repo_path)
        for issue in api_issues:
            if issue["issue"] == "mutating_route_no_validation":
                resolution = "Add a request body validation model (e.g. a Pydantic model) to this route."
            else:
                resolution = "Add an auth/dependency check (e.g. Depends(get_current_user)) before this route mutates state."
            collector.add(
                Category.API, Severity.MEDIUM, issue["detail"],
                file=issue["file"], line=issue.get("line"),
                source_tool="api-routes", title=issue["issue"],
                resolution=resolution,
            )
        _emit(on_event, "tool_done", {"tool": "api-routes", "count": len(api_issues)})
    else:
        _emit(
            on_event, "tool_skip",
            {"tools": ["bandit", "pip-audit", "ruff", "pytest", "api-routes"], "reason": "no Python stack detected"},
        )

    _emit(on_event, "tool_start", {"tool": "ui-templates"})
    ui_issues = ui_checks.scan_templates(repo_path)
    _UI_RESOLUTIONS = {
        "img_missing_alt": "Add a descriptive alt attribute (or alt=\"\" if purely decorative).",
        "missing_viewport_meta": "Add <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"> to the page head.",
        "excess_inline_styles": "Move inline style= attributes into a stylesheet for maintainability.",
    }
    for issue in ui_issues:
        collector.add(
            Category.UI, Severity.LOW, issue["detail"],
            file=issue["file"], line=issue.get("line"),
            source_tool="ui-templates", title=issue["issue"],
            resolution=_UI_RESOLUTIONS.get(issue["issue"], "Review and address per the finding above."),
        )
    _emit(on_event, "tool_done", {"tool": "ui-templates", "count": len(ui_issues)})

    # --- 3. deep per-file LLM review - the only part that reads real code ---
    # Debug/test scripts are already reported for deletion above. Skipping
    # them here keeps the LLM from raising findings the Fixer would then try
    # to "patch", and frees their AUDIT_MAX_FILES slots for real app code.
    flagged_scripts = frozenset(h["file"] for h in debug_hits)
    if flagged_scripts:
        _emit(
            on_event, "file_review_skip",
            {"files": sorted(flagged_scripts), "reason": "flagged for deletion as debug/test scripts"},
        )
    files = _discover_source_files(
        repo_path, config.AUDIT_MAX_FILES, config.AUDIT_MAX_FILE_CHARS, exclude=flagged_scripts,
    )
    _emit(on_event, "file_review_start", {"candidates": len(files)})
    # Timeouts and Ollama errors (unreachable server, missing model, ...)
    # both count toward the circuit breaker: either way nothing is being
    # reviewed, and grinding on just produces a misleadingly empty report.
    consecutive_failures = 0
    files_reviewed = 0
    files_failed = 0
    files_errored = 0
    last_error_timed_out = False
    incomplete_reason: Optional[str] = None
    for f in files:
        rel = str(f.relative_to(repo_path))
        _emit(on_event, "file_review_file", {"file": rel})
        try:
            items = _review_file_with_llm(
                config.AUDITOR_MODEL, f, Path(repo_path),
                config.AUDIT_MAX_FILE_CHARS, config.OLLAMA_NUM_CTX,
            )
        except Exception as exc:  # noqa: BLE001 - counted below; one file's failure shouldn't kill the audit
            consecutive_failures += 1
            files_failed += 1
            files_errored += 1
            timed_out = last_error_timed_out = isinstance(exc, OllamaTimeout)
            _emit(
                on_event, "file_review_timeout" if timed_out else "file_review_failed",
                {"file": rel, "consecutive": consecutive_failures, "error": str(exc)},
            )
            if consecutive_failures >= config.OLLAMA_MAX_CONSECUTIVE_TIMEOUTS:
                incomplete_reason = "ollama_wedged" if timed_out else "ollama_error"
                _emit(
                    on_event, "audit_aborted",
                    {"reason": incomplete_reason, "files_reviewed": files_reviewed, "detail": str(exc)},
                )
                break
            continue
        consecutive_failures = 0
        if items is None:
            # Ollama answered, so it isn't a server problem - but this file
            # wasn't actually reviewed either.
            files_failed += 1
            _emit(on_event, "file_review_unparseable", {"file": rel})
            continue
        files_reviewed += 1
        for item in items:
            try:
                collector.add(
                    Category(item.get("category", "quality")),
                    Severity(item.get("severity", "low")),
                    item.get("detail", ""),
                    file=item.get("file"), line=item.get("line"),
                    source_tool="llm-review", title=item.get("title", "Finding"),
                    resolution=item.get("resolution"),
                )
            except (ValueError, TypeError):
                continue
    if files and files_reviewed == 0 and incomplete_reason is None:
        if files_errored == files_failed:
            # Every file hit an Ollama error - name that, not just "nothing reviewed".
            incomplete_reason = "ollama_wedged" if last_error_timed_out else "ollama_error"
        else:
            incomplete_reason = "no_files_reviewed"
        _emit(on_event, "audit_aborted", {"reason": incomplete_reason, "files_reviewed": 0})
    _emit(on_event, "file_review_done", {"files_reviewed": files_reviewed, "files_failed": files_failed})

    # --- 4. improvements pass: one cheap summarizing call, not per-file ---
    _emit(on_event, "improvements_start", {})
    improvements = _generate_improvements(collector.findings, sorted(stack))
    _emit(on_event, "improvements_done", {"count": len(improvements)})

    return AuditReport(
        findings=collector.findings,
        raw_tool_output=raw_outputs,
        improvements=improvements,
        audit_incomplete=incomplete_reason is not None,
        incomplete_reason=incomplete_reason,
    )


def _generate_improvements(findings: list[Finding], stack: list[str]) -> list[str]:
    if not findings:
        return []

    category_counts: dict[str, int] = {}
    for f in findings:
        category_counts[f.category.value] = category_counts.get(f.category.value, 0) + 1

    summary = {
        "stack": stack,
        "total_findings": len(findings),
        "counts_by_category": category_counts,
        "sample_titles": [f.title for f in findings[:15]],
    }

    try:
        response = chat(
            model=config.AUDITOR_MODEL,
            messages=[
                {"role": "system", "content": IMPROVEMENT_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(summary, default=str)},
            ],
            options={"num_ctx": config.OLLAMA_NUM_CTX},
        )
    except Exception:  # noqa: BLE001 - improvements are a nice-to-have, never fatal (OllamaTimeout included)
        return []

    text = extract_content(response)
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    try:
        items = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("["), text.rfind("]")
        try:
            items = json.loads(text[start : end + 1]) if start != -1 and end != -1 else []
        except json.JSONDecodeError:
            items = []

    return [str(i) for i in items if isinstance(i, str)] if isinstance(items, list) else []