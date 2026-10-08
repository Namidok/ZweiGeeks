"""Per-run measurement: a JSONL log of every audit and fix attempt, and the
summary table printed (and sent to the UI) at the end of each run.

The log is what to read when fixes don't land: each fix attempt has the
finding's fingerprint, the full prompt, the model's raw reply, which gate
rejected it and why, and how long it took.

    audit_results/<run_id>/run.jsonl
"""

from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

from . import config
from .schemas import AuditReport, Finding, FixAttempt


class RunLog:
    def __init__(self, repo_path: str, mode: str, reports_dir: Optional[str] = None):
        self.repo_path = repo_path
        self.mode = mode
        self.run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{Path(repo_path).name or 'repo'}"
        self.dir = Path(reports_dir or config.REPORTS_DIR) / self.run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "run.jsonl"
        self._started = time.monotonic()
        self._llm: dict[str, dict] = defaultdict(lambda: {"calls": 0, "seconds": 0.0, "failed": 0})
        self._attempts: list[dict] = []
        self._audits: list[dict] = []
        self._extra: dict = {}
        self.write("run_start", repo=repo_path, mode=mode, models={
            "auditor": config.AUDITOR_MODEL, "fixer": config.FIXER_MODEL,
        })

    # --- recording ---------------------------------------------------------

    def write(self, record_type: str, **data) -> None:
        record = {"type": record_type, "t": round(time.monotonic() - self._started, 2), **data}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")

    def on_llm_call(self, *, role: str, model: str, seconds: float, ok: bool) -> None:
        """ollama_client.call_listener hook: one call per model request."""
        stats = self._llm[role]
        stats["calls"] += 1
        stats["seconds"] += seconds
        stats["failed"] += 0 if ok else 1

    def audit(self, pass_no: int, report: AuditReport) -> None:
        by_source = Counter(f.source_tool for f in report.findings)
        self._audits.append({"pass": pass_no, "by_source": dict(by_source), "cache_hits": report.review_cache_hits})
        self.write(
            "audit", pass_no=pass_no, findings=len(report.findings), by_source=dict(by_source),
            incomplete=report.audit_incomplete, review_cache_hits=report.review_cache_hits,
        )

    def fix_attempt(self, pass_no: int, finding: Finding, attempt: FixAttempt, **extra) -> None:
        row = {
            "pass_no": pass_no,
            "fingerprint": finding.fingerprint,
            "finding_id": finding.id,
            "source_tool": finding.source_tool,
            "rule": finding.rule,
            "severity": finding.severity.value,
            "file": finding.file,
            "line": finding.line,
            "kind": attempt.kind,
            "applied": attempt.patch_applied,
            "reason": attempt.reason,
            "notes": attempt.notes,
            "duration_s": attempt.duration_s,
            **extra,
        }
        self._attempts.append(row)
        self.write("fix_attempt", **row, transcript=attempt.transcript)

    def note(self, **data) -> None:
        """Extra summary fields (skipped files, cache hits, ...)."""
        self._extra.update(data)
        self.write("note", **data)

    # --- summary -----------------------------------------------------------

    def summary(self, report: Optional[AuditReport], stop_reason: Optional[str]) -> dict:
        first = self._audits[0]["by_source"] if self._audits else {}
        last = self._audits[-1]["by_source"] if self._audits else {}
        sources = sorted(set(first) | set(last))
        applied = Counter(a["kind"] for a in self._attempts if a["applied"])
        rejections = Counter(a["reason"] or "unknown" for a in self._attempts if not a["applied"])
        llm = {role: {**v, "seconds": round(v["seconds"], 1)} for role, v in sorted(self._llm.items())}
        summary = {
            "run_id": self.run_id,
            "repo": self.repo_path,
            "mode": self.mode,
            "stop_reason": stop_reason,
            "wall_time_s": round(time.monotonic() - self._started, 1),
            "audits": len(self._audits),
            "fix_passes": len({a["pass_no"] for a in self._attempts}),
            "fix_attempts": len(self._attempts),
            "review_cache_hits": sum(a.get("cache_hits", 0) for a in self._audits),
            "needs_manual_fix": sum(1 for f in (report.findings if report else []) if f.needs_manual_fix),
            "fixes_applied": {"llm": applied.get("llm", 0), "deterministic": applied.get("deterministic", 0)},
            "rejections_by_reason": dict(rejections.most_common()),
            "findings_by_source": {s: {"first": first.get(s, 0), "last": last.get(s, 0)} for s in sources},
            "findings_total": len(report.findings) if report else 0,
            "llm_calls": llm,
            "llm_total": {
                "calls": sum(v["calls"] for v in llm.values()),
                "seconds": round(sum(v["seconds"] for v in llm.values()), 1),
            },
            "log_path": str(self.path),
            **self._extra,
        }
        self.write("summary", **summary)
        return summary


def format_summary(s: dict) -> str:
    """The summary as a plain-text table, for the terminal."""
    out = [
        f"ZweiGeeks run {s['run_id']}  ({s['mode']}, stop: {s['stop_reason']}, {s['wall_time_s']}s)",
        f"Audits: {s['audits']}   Fix passes: {s['fix_passes']}   Fix attempts: {s['fix_attempts']}   "
        f"Applied: {s['fixes_applied']['llm']} LLM + {s['fixes_applied']['deterministic']} deterministic",
        f"Needs manual fix: {s.get('needs_manual_fix', 0)}   Review cache hits: {s.get('review_cache_hits', 0)}",
        "",
        f"{'Findings by source':<26}{'first':>7}{'last':>7}",
    ]
    for source, n in s["findings_by_source"].items():
        out.append(f"  {source:<24}{n['first']:>7}{n['last']:>7}")
    out.append("")
    out.append(f"{'Rejections by reason':<26}{'count':>7}")
    for reason, n in (s["rejections_by_reason"] or {"(none)": 0}).items():
        out.append(f"  {reason:<24}{n:>7}")
    out.append("")
    out.append(f"{'LLM calls':<26}{'calls':>7}{'seconds':>10}")
    for role, v in s["llm_calls"].items():
        out.append(f"  {role:<24}{v['calls']:>7}{v['seconds']:>10}")
    out.append(f"  {'total':<24}{s['llm_total']['calls']:>7}{s['llm_total']['seconds']:>10}")
    out.append("")
    out.append(f"Log: {s['log_path']}")
    return "\n".join(out)
