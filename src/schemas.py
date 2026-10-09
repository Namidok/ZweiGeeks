"""Shared data shapes passed between the Auditor, the Fixer, and the UI."""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class Category(str, Enum):
    SECURITY = "security"
    API = "api"
    UI = "ui"
    TESTING = "testing"
    QUALITY = "quality"


class Finding(BaseModel):
    id: str = Field(description="Stable short id, e.g. 'SEC-001'")
    category: Category
    severity: Severity
    file: Optional[str] = None
    line: Optional[int] = None
    title: str
    detail: str
    resolution: Optional[str] = Field(
        default=None, description="Suggested fix, shown in the PDF report next to the issue."
    )
    source_tool: str = Field(description="Which tool/check produced this, e.g. 'bandit', 'llm'")
    rule: Optional[str] = Field(
        default=None, description="The tool's rule id (bandit B602, ruff F401, a CVE id); None for LLM findings."
    )
    line_text: Optional[str] = Field(
        default=None, description="The flagged line, stripped - finds the finding again after edits move it."
    )
    fingerprint: Optional[str] = Field(
        default=None,
        description="Stable identity across audits: source tool, file, rule (or normalised title) and "
        "flagged line text. Unlike `id` and `line`, it survives re-audits and edits elsewhere in the file.",
    )
    fixed: bool = False
    fix_notes: Optional[str] = None
    dismissed: bool = Field(
        default=False, description="The Fixer looked and found nothing to fix: most likely a false positive."
    )
    needs_manual_fix: bool = Field(
        default=False, description="The Fixer gave up on it (out of attempts, or nothing it can change)."
    )
    autofix: Optional[dict] = Field(
        default=None,
        description="Set when a deterministic fixer can handle it without the LLM: {'tool': 'ruff'} "
        "for ruff's safe fixes, {'tool': 'pin', 'package', 'version', 'fix_versions'} for pip-audit.",
    )

    def report_bucket(self) -> str:
        """Collapses the 5 internal severities into the 3-tier red/yellow/green
        scheme used in the PDF report: high, medium, low.
        """
        if self.severity in (Severity.CRITICAL, Severity.HIGH):
            return "high"
        if self.severity == Severity.MEDIUM:
            return "medium"
        return "low"


class AuditReport(BaseModel):
    findings: list[Finding] = Field(default_factory=list)
    raw_tool_output: dict[str, str] = Field(default_factory=dict)
    improvements: list[str] = Field(
        default_factory=list,
        description="Broader, strategic suggestions - not tied to a single finding.",
    )
    speculative_dropped: int = Field(
        default=0, description="LLM findings dropped because they said the file shows no sign of the issue."
    )
    review_cache_hits: int = Field(
        default=0, description="Files whose LLM review came from the cache (unchanged since last reviewed)."
    )
    audit_incomplete: bool = Field(
        default=False,
        description="True if the per-file LLM review was aborted early (e.g. Ollama "
        "wedged) before all candidate files were reviewed. A 'clean' result alongside "
        "this flag means 'nothing reviewed', not 'nothing found' - never treat it as a "
        "pass.",
    )
    incomplete_reason: Optional[str] = Field(
        default=None,
        description="Why audit_incomplete is set: 'ollama_wedged' (repeated timeouts), "
        "'ollama_error' (repeated Ollama errors, e.g. server down or model missing), or "
        "'no_files_reviewed' (no file review produced usable output).",
    )

    def open_findings(self) -> list[Finding]:
        return [f for f in self.findings if not f.fixed]

    def fixable_findings(self, min_severity: "Severity | str" = "high") -> list[Finding]:
        """Open findings the LLM Fixer can act on, most severe first.

        Excluded, because no edit to a file by the Fixer can resolve them:
        findings without a file (a whole-suite pytest failure), repo-hygiene
        findings (a leaked key needs rotating, a missing file restoring),
        pip-audit findings (handled by the deterministic pin bump instead),
        and findings the Fixer already gave up on. Only fixable findings can
        keep the audit<->fix loop going; everything else is just reported.
        """
        floor = _SEVERITY_RANK[Severity(min_severity)]
        fixable = [
            f for f in self.open_findings()
            if f.file
            and f.source_tool not in _NOT_LLM_FIXABLE
            and not f.needs_manual_fix
            and not f.dismissed
            and _SEVERITY_RANK[f.severity] >= floor
        ]
        return sorted(fixable, key=lambda f: (
            -_SEVERITY_RANK[f.severity], f.category != Category.SECURITY, f.file or "", f.line or 0,
        ))

    def blocking_findings(self) -> list[Finding]:
        """Fixable CRITICAL/HIGH findings (see fixable_findings)."""
        return self.fixable_findings(Severity.HIGH)


_SEVERITY_RANK = {Severity.INFO: 0, Severity.LOW: 1, Severity.MEDIUM: 2, Severity.HIGH: 3, Severity.CRITICAL: 4}
_NOT_LLM_FIXABLE = {"repo-hygiene", "pytest", "pip-audit"}


class FixAttempt(BaseModel):
    finding_id: str
    file: Optional[str] = None
    patch_applied: bool
    notes: str
    timed_out: bool = False
    reason: Optional[str] = Field(
        default=None, description="Rejection/skip code (edit_mismatch, not_verified, ...); None when applied."
    )
    kind: str = Field(default="llm", description="'llm' or 'deterministic' (ruff --fix, pin bump).")
    duration_s: float = 0.0
    # The full fixer conversation (prompt, raw replies). Written to the run
    # log, kept out of API payloads.
    transcript: list[dict] = Field(default_factory=list, exclude=True)


class GraphState(BaseModel):
    """The state LangGraph threads through the Auditor <-> Fixer loop."""

    repo_path: str
    branch_name: str
    iteration: int = 0
    max_iterations: int = 5
    report: AuditReport = Field(default_factory=AuditReport)
    fix_attempts: list[FixAttempt] = Field(default_factory=list)
    done: bool = False
    stop_reason: Optional[str] = None

    class Config:
        arbitrary_types_allowed = True