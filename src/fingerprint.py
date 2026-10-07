"""Stable identities for findings.

A finding's `id` (SEC-003) is assigned in discovery order and its line
number moves whenever anything above it changes, so neither can tell
"the same issue, seen again on the next audit" from a new one. The
fingerprint uses what doesn't move: which tool found it, in which file,
under which rule (or, for LLM findings, the normalised title), and the
text of the flagged line.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Iterable, Optional

from .schemas import Finding

_NON_WORD = re.compile(r"[^a-z0-9]+")


def normalise_title(title: str) -> str:
    return _NON_WORD.sub(" ", title.lower()).strip()


def _sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()


def flagged_line(repo_path: str, finding: Finding) -> Optional[str]:
    if not finding.file or not finding.line:
        return None
    try:
        lines = (Path(repo_path) / finding.file).read_text(errors="ignore").splitlines()
    except OSError:
        return None
    if 1 <= finding.line <= len(lines):
        return lines[finding.line - 1].strip()
    return None


def fingerprint_of(finding: Finding) -> str:
    key = "|".join([
        finding.source_tool,
        finding.file or "",
        finding.rule or normalise_title(finding.title),
        _sha(finding.line_text or ""),
    ])
    return _sha(key)[:16]


def assign_fingerprints(findings: Iterable[Finding], repo_path: str) -> None:
    for finding in findings:
        if finding.line_text is None:
            finding.line_text = flagged_line(repo_path, finding)
        finding.fingerprint = fingerprint_of(finding)
