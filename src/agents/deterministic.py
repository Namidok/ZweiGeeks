"""Fixes that need no model: they run before the LLM Fixer in every fix pass.

- ruff's own *safe* fixes (`ruff check --fix`, which never applies the fixes
  ruff marks unsafe), only on files that already have ruff findings, with
  the project's own ruff configuration.
- Vulnerable dependency pins: `package==old` in requirements.txt rewritten to
  the lowest version that fixes every advisory pip-audit reported for that
  package. Nothing is installed; the next audit re-checks the new pin.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

from packaging.version import InvalidVersion, Version

from ..schemas import AuditReport, Finding, FixAttempt
from ..tools.project import relative_to_repo, tool_path

EventCallback = Optional[Callable[[str, dict], None]]

REQUIREMENTS = "requirements.txt"


def _emit(cb: EventCallback, event: str, payload: dict) -> None:
    if cb:
        cb(event, payload)


def _normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _pin_pattern(package: str) -> re.Pattern:
    # PEP 503 names: '-', '_' and '.' are interchangeable, case-insensitive.
    name = r"[-_.]+".join(re.escape(part) for part in re.split(r"[-_.]+", package))
    return re.compile(rf"^(\s*{name}\s*(?:\[[^\]]*\])?\s*==\s*)([^\s;#,]+)", re.IGNORECASE)


def find_pin(requirements: str, package: str) -> Optional[tuple[int, str]]:
    """(1-based line number, pinned version) of `package==x` in a requirements file."""
    pattern = _pin_pattern(package)
    for n, line in enumerate(requirements.splitlines(), 1):
        m = pattern.match(line)
        if m:
            return n, m.group(2)
    return None


def pending(report: AuditReport, protected: frozenset = frozenset()) -> list[Finding]:
    """Open findings a deterministic fixer can handle, leaving out files the
    user has uncommitted changes in."""
    return [f for f in report.open_findings() if f.autofix and f.file not in protected]


def run(
    repo_path: str, report: AuditReport, on_event: EventCallback = None, protected: frozenset = frozenset(),
) -> list[tuple[Finding, FixAttempt]]:
    """Applies every deterministic fix it can. Returns (finding, attempt)
    pairs - applied or not - for the run log and the fix-attempt list."""
    todo = pending(report, protected)
    results = _bump_pins(repo_path, [f for f in todo if f.autofix.get("tool") == "pin"], on_event)
    results += _ruff_fix(repo_path, [f for f in todo if f.autofix.get("tool") == "ruff"], on_event)
    for finding, attempt in results:
        if attempt.patch_applied:
            finding.fixed = True
            finding.fix_notes = attempt.notes
        else:
            # Nothing deterministic left to try. pip-audit and ruff-only
            # findings never go to the LLM, so this one is for a human.
            finding.autofix = None
            finding.fix_notes = attempt.notes
    return results


# --- dependency pins -------------------------------------------------------------

def _lowest_fix(findings: list[Finding], current: str) -> Optional[str]:
    """The lowest version that fixes every advisory: the highest of each
    advisory's lowest fixed version above the current pin."""
    try:
        now = Version(current)
    except InvalidVersion:
        return None
    needed = []
    for f in findings:
        candidates = []
        for v in f.autofix.get("fix_versions") or []:
            try:
                parsed = Version(v)
            except InvalidVersion:
                continue
            if parsed > now:
                candidates.append(parsed)
        if not candidates:
            return None  # an advisory with no fixed release: a pin bump can't fix it
        needed.append(min(candidates))
    return str(max(needed)) if needed else None


def _bump_pins(repo_path: str, findings: list[Finding], on_event: EventCallback) -> list[tuple[Finding, FixAttempt]]:
    if not findings:
        return []
    path = Path(repo_path) / REQUIREMENTS
    try:
        text = path.read_text()
    except OSError:
        return [(f, _skipped(f, "no_requirements", f"{REQUIREMENTS} not found")) for f in findings]

    by_package: dict[str, list[Finding]] = {}
    for f in findings:
        by_package.setdefault(_normalise(f.autofix["package"]), []).append(f)

    results = []
    lines = text.splitlines(keepends=True)
    for group in by_package.values():
        started = time.monotonic()
        package = group[0].autofix["package"]
        pin = find_pin(text, package)
        if not pin:
            notes = f"{package} isn't pinned with == in {REQUIREMENTS}; update it by hand."
            results += [(f, _skipped(f, "not_pinned", notes)) for f in group]
            continue
        line_no, current = pin
        target = _lowest_fix(group, current)
        if not target:
            notes = f"No released {package} version fixes every advisory; review them by hand."
            results += [(f, _skipped(f, "no_fixed_version", notes)) for f in group]
            continue
        old = lines[line_no - 1]
        lines[line_no - 1] = _pin_pattern(package).sub(lambda m: m.group(1) + target, old, count=1)
        notes = f"Pinned {package} {current} -> {target} in {REQUIREMENTS} (fixes {len(group)} advisories)."
        _emit(on_event, "deterministic_fix", {"tool": "pin", "file": REQUIREMENTS, "detail": notes})
        took = round(time.monotonic() - started, 2)
        results += [(f, _applied(f, REQUIREMENTS, notes, took)) for f in group]
    new_text = "".join(lines)
    if new_text != text:
        path.write_text(new_text)
    return results


# --- ruff safe fixes ---------------------------------------------------------------

def _ruff_json(repo_path: str, files: list[str], fix: bool) -> Optional[list[dict]]:
    cmd = [tool_path("ruff"), "check", "--output-format", "json", "--no-cache"]
    if fix:
        cmd.append("--fix")
    try:
        proc = subprocess.run([*cmd, *files], cwd=repo_path, capture_output=True, text=True, timeout=120)
        return json.loads(proc.stdout or "[]")
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _ruff_keys(repo_path: str, diagnostics: list[dict]) -> set[tuple]:
    """(file, rule, flagged line text) for each diagnostic - the same
    identity the findings carry, so they can be matched after the fix."""
    keys = set()
    cache: dict[str, list[str]] = {}
    for d in diagnostics:
        rel = relative_to_repo(d.get("filename"), repo_path)
        if rel not in cache:
            try:
                cache[rel] = (Path(repo_path) / rel).read_text(errors="ignore").splitlines()
            except OSError:
                cache[rel] = []
        row = (d.get("location") or {}).get("row") or 0
        text = cache[rel][row - 1].strip() if 0 < row <= len(cache[rel]) else ""
        keys.add((rel, d.get("code"), text))
    return keys


def _ruff_fix(repo_path: str, findings: list[Finding], on_event: EventCallback) -> list[tuple[Finding, FixAttempt]]:
    if not findings:
        return []
    started = time.monotonic()
    files = sorted({f.file for f in findings if f.file})
    # --fix applies only the fixes ruff marks safe; the project's own ruff
    # config (pyproject.toml / ruff.toml) applies because cwd is the repo.
    remaining = _ruff_json(repo_path, files, fix=True)
    if remaining is None:
        return [(f, _skipped(f, "ruff_failed", "ruff --fix didn't run")) for f in findings]
    still_there = _ruff_keys(repo_path, remaining)
    took = round(time.monotonic() - started, 2)
    results = []
    for f in findings:
        if (f.file, f.rule, f.line_text or "") in still_there:
            results.append((f, _skipped(f, "not_auto_fixable", f"ruff couldn't fix {f.rule} safely")))
        else:
            notes = f"ruff --fix applied its safe fix for {f.rule}."
            results.append((f, _applied(f, f.file, notes, took)))
    fixed_files = sorted({f.file for f, a in results if a.patch_applied})
    if fixed_files:
        _emit(on_event, "deterministic_fix", {"tool": "ruff", "files": fixed_files,
                                              "count": sum(a.patch_applied for _, a in results)})
    return results


def _applied(f: Finding, file: Optional[str], notes: str, took: float) -> FixAttempt:
    return FixAttempt(finding_id=f.id, file=file, patch_applied=True, notes=notes,
                      kind="deterministic", duration_s=took)


def _skipped(f: Finding, reason: str, notes: str) -> FixAttempt:
    return FixAttempt(finding_id=f.id, file=f.file, patch_applied=False, notes=notes,
                      reason=reason, kind="deterministic")
