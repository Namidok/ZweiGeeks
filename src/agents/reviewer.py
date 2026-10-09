"""JAX's closing review: after the audit -> fix loop, JAX reads the repo the
way a senior colleague would and writes a verdict, what's good, the bugs
still open, what was changed, and what would make it better - also when the
audit found nothing at all ("looks great, but here's what I'd still do").

In a fix run JAX then implements the improvements that name one existing
file, through the same Fixer and the same gates (syntax, undefined names,
verifier, import check) as any finding. Repo-level ones (add a LICENSE, add
CI) stay suggestions: those are the owner's call.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Callable, Optional

from .. import config
from ..ollama_client import OllamaTimeout, chat, extract_content, llm_role
from ..schemas import AuditReport, Category, Finding, FixAttempt, Review, ReviewItem, Severity
from ..tools import stack_detect
from . import fixer

EventCallback = Optional[Callable[[str, dict], None]]

REVIEW_SYSTEM_PROMPT = """You are JAX, a senior engineer giving a repository its final review \
after the automated fixes are done. You get the file list, the README, a few source files and a \
summary of what the audit found and fixed.

Review it like a senior colleague would:
- "strengths": 2 to 4 specific things the repo does well. Name the file or the practice you can see.
- "improvements": 3 to 6 concrete changes that would make the repo better. Each is either ONE \
small, specific change to ONE existing file from the file list ("file" = its exact path, e.g. \
"add a docstring to load_data in src/loader.py", "add a Setup section to README.md"), or a \
repo-level change with "file": null (e.g. add tests, CI, a LICENSE). Don't repeat the audit \
findings. No vague advice like "improve code quality" or "add more comments".

Output ONLY JSON, nothing else:
{"strengths": ["..."], "improvements": [{"title": "short imperative title", \
"detail": "1-2 sentences: what to change and why", "file": "exact/path or null"}]}
"""

_SOURCE_EXT = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".php", ".go", ".rs", ".java", ".kt", ".rb", ".cs",
    ".cpp", ".cc", ".c", ".h", ".hpp", ".swift", ".scala", ".sql", ".sh", ".dart", ".vue",
}
# Files JAX doesn't edit for an improvement: data, notebooks (JSON a
# search/replace edit mangles easily), lock files, generated or minified code.
_NOT_EDITABLE = {".csv", ".tsv", ".ipynb", ".lock", ".svg", ".min.js", ".map", ".pdf"}
_ENTRY_NAMES = re.compile(r"(^|/)(main|app|server|index|cli|run|manage|api)\.[a-z]+$")
_WINDOWS_BAD = re.compile(r'[:*?"<>|]')
_HIGH = {Severity.CRITICAL, Severity.HIGH}


def _emit(cb: EventCallback, event: str, payload: dict) -> None:
    if cb:
        cb(event, payload)


def _tracked_files(repo_path: str) -> list[str]:
    try:
        out = subprocess.run(
            ["git", "ls-files"], cwd=repo_path, capture_output=True, text=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
    return [line for line in out.stdout.splitlines() if line.strip()]


def _read(repo_path: str, rel: str, limit: int) -> str:
    try:
        return (Path(repo_path) / rel).read_text(errors="ignore")[:limit]
    except OSError:
        return ""


def _is_text(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return b"\0" not in fh.read(8192)
    except OSError:
        return False


def _editable(repo_path: str, rel: str) -> bool:
    lower = rel.lower()
    if any(lower.endswith(ext) for ext in _NOT_EDITABLE):
        return False
    path = Path(repo_path) / rel
    return path.is_file() and _is_text(path)


# --- deterministic checks: facts about the repo, no model involved ----------------


def _repo_checks(files: list[str], stack: list[str]) -> tuple[list[ReviewItem], list[ReviewItem], list[str]]:
    """(bugs, improvements, strengths) that follow from the file list alone."""
    names = {f.lower() for f in files}
    bugs: list[ReviewItem] = []
    improvements: list[ReviewItem] = []
    strengths: list[str] = []

    bad = [f for f in files if _WINDOWS_BAD.search(f)]
    if bad:
        bugs.append(ReviewItem(
            title="File names that can't be checked out on Windows",
            detail=f"{', '.join(bad[:5])} contain characters Windows forbids in file names "
            "(: * ? \" < > |), so cloning the repo there fails. Rename them.",
            file=bad[0], status="open",
        ))

    has_readme = any("/" not in n and n.startswith("readme") for n in names)
    has_license = any(n.split("/")[-1].startswith(("license", "licence", "copying")) for n in names)
    has_tests = any(
        re.search(r"(^|/)(tests?|spec|__tests__)/|(^|/)test_[^/]+$|_test\.[a-z]+$|\.(spec|test)\.[a-z]+$", n)
        for n in names
    )
    has_ci = any(
        n.startswith((".github/workflows/", ".circleci/"))
        or n in {".gitlab-ci.yml", "jenkinsfile", "azure-pipelines.yml"}
        for n in names
    )

    if not has_readme:
        improvements.append(ReviewItem(
            title="Add a README", status="suggested",
            detail="Say what the project does, how to install and run it, and what it needs.",
        ))
    else:
        strengths.append("Has a README, so a newcomer has somewhere to start.")
    if not has_license:
        improvements.append(ReviewItem(
            title="Add a LICENSE", status="suggested",
            detail="Without one, nobody else may legally reuse the code; pick one (MIT, Apache-2.0) on purpose.",
        ))
    if ".gitignore" not in names:
        improvements.append(ReviewItem(
            title="Add a .gitignore", status="suggested",
            detail="Keep build output, virtualenvs, caches and local secrets (.env) out of git.",
        ))
    if not has_tests:
        improvements.append(ReviewItem(
            title="Add automated tests", status="suggested",
            detail="Nothing checks that the code still works after a change; start with the core logic.",
        ))
    else:
        strengths.append("Has automated tests.")
    if not has_ci:
        improvements.append(ReviewItem(
            title="Run lint and tests in CI", status="suggested",
            detail="A GitHub Actions workflow on every push catches breakage before it reaches main.",
        ))
    else:
        strengths.append("CI runs on every push.")
    manifests = {"requirements.txt", "pyproject.toml", "setup.py", "pipfile", "environment.yml", "setup.cfg"}
    if "python" in stack and not any(n.split("/")[-1] in manifests for n in names):
        improvements.append(ReviewItem(
            title="Declare the Python dependencies", status="suggested",
            detail="Add a requirements.txt or pyproject.toml with version ranges so the project installs reproducibly.",
        ))
    return bugs, improvements, strengths


# --- the model's part --------------------------------------------------------------


def _key_sources(repo_path: str, files: list[str], limit: int = 3) -> list[str]:
    sources = [f for f in files if Path(f).suffix.lower() in _SOURCE_EXT and "test" not in f.lower()]
    entry = [f for f in sources if _ENTRY_NAMES.search(f.lower())]

    def size(rel: str) -> int:
        try:
            return (Path(repo_path) / rel).stat().st_size
        except OSError:
            return 0

    rest = sorted((f for f in sources if f not in entry and size(f) < 60_000), key=size, reverse=True)
    return (entry + rest)[:limit]


def _snapshot(repo_path: str, files: list[str], report: AuditReport, stack: list[str]) -> str:
    readme = next((f for f in files if "/" not in f and f.lower().startswith("readme")), None)
    open_findings = [f for f in report.findings if not f.fixed and not f.dismissed]
    parts = [
        f"Stack: {', '.join(stack) or 'unknown'}",
        f"Tracked files ({len(files)}):\n" + "\n".join(files[:150]) + ("\n..." if len(files) > 150 else ""),
        "Audit: {} findings, {} fixed, {} still open. Open: {}".format(
            len(report.findings), sum(f.fixed for f in report.findings), len(open_findings),
            "; ".join(f"{f.title} ({f.file})" for f in open_findings[:10]) or "none",
        ),
    ]
    if readme:
        parts.append(f"--- {readme} ---\n{_read(repo_path, readme, 2500)}")
    for rel in _key_sources(repo_path, files):
        parts.append(f"--- {rel} ---\n{_read(repo_path, rel, 2500)}")
    return "\n\n".join(parts)


def _parse_json_object(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        try:
            data = json.loads(text[start : end + 1]) if start != -1 and end > start else {}
        except json.JSONDecodeError:
            data = {}
    return data if isinstance(data, dict) else {}


def _ask_model(snapshot: str) -> dict:
    try:
        with llm_role("fixer"):
            response = chat(
                model=config.FIXER_MODEL,
                messages=[
                    {"role": "system", "content": REVIEW_SYSTEM_PROMPT},
                    {"role": "user", "content": snapshot},
                ],
                options={"num_ctx": config.OLLAMA_NUM_CTX},
            )
    except Exception:  # noqa: BLE001 - the review is extra; the deterministic part still stands
        return {}
    return _parse_json_object(extract_content(response))


def _model_items(data: dict, files: list[str], open_titles: set[str]) -> tuple[list[str], list[ReviewItem]]:
    tracked = set(files)
    strengths = [s.strip() for s in data.get("strengths") or [] if isinstance(s, str) and s.strip()][:4]
    items: list[ReviewItem] = []
    for raw in data.get("improvements") or []:
        if not isinstance(raw, dict) or not str(raw.get("title") or "").strip():
            continue
        title = str(raw["title"]).strip()
        if title.lower() in open_titles:
            continue
        file = raw.get("file")
        file = file.strip().removeprefix("./") if isinstance(file, str) and file.strip() not in ("", "null") else None
        items.append(ReviewItem(
            title=title[:140], detail=str(raw.get("detail") or "").strip()[:600],
            file=file if file in tracked else None, status="suggested",
        ))
    return strengths, items[:6]


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z]{4,}", text.lower()))


def _same(a: ReviewItem, b: ReviewItem) -> bool:
    wa, wb = _words(a.title), _words(b.title)
    return bool(wa and wb) and len(wa & wb) / min(len(wa), len(wb)) >= 0.6


def _n(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


# --- the review ---------------------------------------------------------------------


def write_review(
    repo_path: str, report: AuditReport, stack: Optional[list[str]] = None, on_event: EventCallback = None,
) -> Review:
    _emit(on_event, "review_start", {})
    stack = sorted(stack if stack is not None else stack_detect.detect_stack(repo_path))
    files = _tracked_files(repo_path)
    open_findings = [f for f in report.findings if not f.fixed and not f.dismissed]
    open_findings.sort(key=lambda f: (f.severity not in _HIGH, f.severity != Severity.MEDIUM, f.file or ""))

    bugs = [
        ReviewItem(
            title=f.title, detail=f.detail, file=f.file, line=f.line,
            status="needs_manual_fix" if f.needs_manual_fix else "open", notes=f.fix_notes,
        )
        for f in open_findings[:30]
    ]
    changes = [
        ReviewItem(title=f.title, detail=f.detail, file=f.file, line=f.line, status="fixed", notes=f.fix_notes)
        for f in report.findings if f.fixed
    ]
    fact_bugs, fact_improvements, fact_strengths = _repo_checks(files, stack)
    bugs = fact_bugs + bugs

    strengths: list[str] = []
    improvements: list[ReviewItem] = []
    if not report.audit_incomplete:
        data = _ask_model(_snapshot(repo_path, files, report, stack))
        strengths, improvements = _model_items(data, files, {f.title.lower() for f in open_findings})
    improvements += [i for i in fact_improvements if not any(_same(i, m) for m in improvements)]
    strengths = strengths or fact_strengths

    high = sum(f.severity in _HIGH for f in open_findings)
    medium = sum(f.severity == Severity.MEDIUM for f in open_findings)
    fixed_line = f"I fixed {_n(len(changes), 'thing')} along the way. " if changes else ""
    if report.audit_incomplete:
        verdict = "incomplete"
        headline = "KAI's review didn't finish, so I can't call this repo clean yet. Run it again once Ollama is healthy."
    elif not bugs:
        verdict = "perfect"
        headline = (
            f"This repo is in great shape: nothing left to fix. {fixed_line}"
            "It's already solid, but here's what I'd do to make it even better."
        )
    elif not high and not medium:
        verdict = "good"
        headline = f"In good shape. {fixed_line}Only {_n(len(bugs), 'small thing')} left, plus a few ideas to make it better."
    elif not high:
        verdict = "good"
        headline = f"Solid overall. {fixed_line}{_n(medium, 'medium-severity issue')} still worth a look."
    else:
        verdict = "needs_work"
        headline = f"Needs work: {_n(high, 'high-severity issue')} still open. {fixed_line}".strip()

    review = Review(
        verdict=verdict, headline=headline, strengths=strengths,
        bugs=bugs, changes=changes, improvements=improvements,
    )
    _emit(on_event, "review_done", {
        "verdict": verdict, "headline": headline, "bugs": len(bugs), "improvements": len(improvements),
        "to_apply": sum(1 for i in improvements if i.file),
    })
    return review


def apply_improvements(
    repo_path: str, review: Review, on_event: EventCallback = None, protected: frozenset = frozenset(),
) -> list[tuple[Finding, FixAttempt]]:
    """JAX implements the file-level improvements, each through the Fixer's
    gates. Returns (finding, attempt) pairs for the run log and the report."""
    results: list[tuple[Finding, FixAttempt]] = []
    todo = [i for i in review.improvements if i.file][: config.JAX_MAX_IMPROVEMENTS]
    if not todo:
        return results
    _emit(on_event, "improve_start", {"count": len(todo)})
    for n, item in enumerate(todo, 1):
        if item.file in protected:
            item.status, item.notes = "not_applied", "You have uncommitted changes in this file, so JAX left it alone."
            continue
        if not _editable(repo_path, item.file):
            item.notes = "Not a file JAX edits automatically (data, notebook or binary)."
            continue
        finding = Finding(
            id=f"IMP-{n:03d}", category=Category.QUALITY, severity=Severity.LOW, file=item.file,
            line=item.line, title=item.title, detail=item.detail or item.title,
            resolution=item.detail or None, source_tool="jax-review",
        )
        try:
            attempt = fixer.apply_fix(repo_path, finding, on_event=on_event)
        except OllamaTimeout:
            item.status, item.notes = "not_applied", "The model timed out."
            break
        attempt.kind = "improvement"
        results.append((finding, attempt))
        item.notes = attempt.notes
        item.status = "applied" if attempt.patch_applied else "not_applied"
        if attempt.reason in ("timeout", "ollama_unreachable"):
            break  # the next one would fail the same way
    _emit(on_event, "improve_done", {
        "applied": sum(1 for _, a in results if a.patch_applied), "attempted": len(results),
    })
    return results
