"""One (case, repeat) of the benchmark, start to finish.

bench.run starts this in a fresh subprocess per (case, repeat), because
src.config reads the models and OLLAMA_SEED from the environment at import
time. `run_case` can also be called in-process (the tests do, with the model
calls mocked).

    python -m bench.worker --case bench/cases/a1_inventory_service --repeat 0 \\
        --passes 5 --work /tmp/run --out /tmp/run/result.json
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
import traceback
from pathlib import Path

from bench import core

_GIT = ["git", "-c", "user.name=ZweiGeeks bench", "-c", "user.email=bench@example.invalid",
        "-c", "commit.gpgsign=false"]


def make_repo(source: Path, dest: Path) -> Path:
    """A copy of the case's repo/ as a git repo with one commit."""
    shutil.copytree(source, dest, ignore=shutil.ignore_patterns(*core.GENERATED))
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "benchmark case"]):
        subprocess.run([*_GIT, *args], cwd=dest, check=True, capture_output=True)
    return dest


def finding_dict(finding) -> dict:
    return {
        "id": finding.id, "source_tool": finding.source_tool, "source": core.source_of(finding.source_tool),
        "file": finding.file, "line": finding.line, "line_text": finding.line_text, "rule": finding.rule,
        "title": finding.title, "severity": finding.severity.value, "fingerprint": finding.fingerprint,
    }


def read_attempts(log_path) -> list[dict]:
    """The fix attempts recorded in a run's run.jsonl."""
    attempts = []
    try:
        lines = Path(log_path).read_text().splitlines()
    except (OSError, TypeError):
        return attempts
    for line in lines:
        record = json.loads(line)
        if record.get("type") == "fix_attempt":
            attempts.append({k: record.get(k) for k in (
                "finding_id", "fingerprint", "source_tool", "file", "line", "kind", "applied", "reason", "duration_s",
            )})
    return attempts


def _short_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}"


def run_case(case: core.Case, repeat: int, passes: int, work: Path) -> dict:
    from src import config, graph
    from src.agents import auditor

    work = Path(work)
    lines = {bug["id"]: core.bug_line(case.repo, bug) for bug in case.bugs}
    record = {
        "case": case.id, "tier": case.tier, "repeat": repeat, "seed": config.OLLAMA_SEED, "passes": passes,
        "models": {"auditor": config.AUDITOR_MODEL, "fixer": config.FIXER_MODEL, "verifier": config.VERIFIER_MODEL},
        "bugs": [bug["id"] for bug in case.bugs], "files": core.count_files(case.repo),
    }

    # Phase 1, detection: a read-only audit of a fresh copy.
    started = time.monotonic()
    detect_repo = make_repo(case.repo, work / "detect")
    try:
        out = graph.run_audit_only(str(detect_repo))
        report = out["report"]
        findings = [finding_dict(f) for f in report.findings]
        record["detection"] = {
            "findings": findings,
            "matched": core.score_detection(findings, case.bugs, lines),
            "audit_incomplete": report.audit_incomplete,
            "incomplete_reason": report.incomplete_reason,
            "summary": out.get("summary"),
        }
    except Exception as exc:  # noqa: BLE001 - a crashed phase is a result, not a reason to stop the benchmark
        record["detection"] = {"error": _short_error(exc)}
    record["detection"]["seconds"] = round(time.monotonic() - started, 1)

    # Phase 2, fixing: the audit -> fix loop on another fresh copy, judged by
    # the fix checks and by re-running the behaviour tests.
    started = time.monotonic()
    fix_repo = make_repo(case.repo, work / "fix")
    fixing: dict = {"behaviour_before": core.run_pytest(case.path / "behaviour_tests", fix_repo, work / "tests-before")}
    audited: list[dict] = []
    real_run_audit = auditor.run_audit

    def capturing_run_audit(*args, **kwargs):
        # Observes every audit of the loop (the first one has the original
        # line numbers), so applied edits can be traced back to the
        # finding - and the bug - they were aimed at. Behaviour unchanged.
        report = real_run_audit(*args, **kwargs)
        audited.extend(finding_dict(f) for f in report.findings)
        return report

    auditor.run_audit = capturing_run_audit
    state = None
    try:
        state = graph.run_pipeline(str(fix_repo), passes)
    except Exception as exc:  # noqa: BLE001
        fixing["error"] = _short_error(exc)
    finally:
        auditor.run_audit = real_run_audit

    findings_by_fp: dict = {}
    for finding in audited:
        findings_by_fp.setdefault(finding["fingerprint"], finding)
    summary = (state or {}).get("summary") or {}
    attempts = read_attempts(summary.get("log_path"))
    for attempt in attempts:
        attempt["source"] = core.attempt_source(attempt, findings_by_fp)
    fixing.update({
        "stop_reason": (state or {}).get("stop_reason"),
        "applied_files": (state or {}).get("applied_files") or [],
        "summary": summary,
        "attempts": attempts,
        "fixed": {bug["id"]: core.check_fix(bug["fix_check"], fix_repo, case.path, work / "fix-checks")
                  for bug in case.bugs},
        "attributed": core.attribute_fixes(attempts, findings_by_fp, case.bugs, lines),
        "behaviour_after": core.run_pytest(case.path / "behaviour_tests", fix_repo, work / "tests-after"),
    })
    review = (state or {}).get("review")
    if review is not None:
        fixing["review_verdict"] = review.verdict
    fixing["regressions"] = core.regressions(fixing["behaviour_before"], fixing["behaviour_after"])
    fixing["seconds"] = round(time.monotonic() - started, 1)
    record["fixing"] = fixing
    return record


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--case", required=True, type=Path)
    parser.add_argument("--repeat", required=True, type=int)
    parser.add_argument("--passes", required=True, type=int)
    parser.add_argument("--work", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    record = run_case(core.load_case(args.case), args.repeat, args.passes, args.work)
    args.out.write_text(json.dumps(record, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
