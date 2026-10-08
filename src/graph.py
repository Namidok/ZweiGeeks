"""LangGraph wiring for the Auditor <-> Fixer loop.

Graph shape:

    audit --(blocking findings remain, budget left)--> fix --> audit
    audit --(clean, incomplete, or budget exhausted)--> END
    fix --(fixer kept timing out)--> END

Everything happens on a throwaway git branch created before the graph runs.
We never push and never touch the branch the user had checked out - and the
repo is switched back to that branch when the run ends (or fails), with the
fixes left on the sandbox branch for review.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable, Optional, TypedDict

from langgraph.graph import END, StateGraph

from . import config, git_utils
from .agents import auditor, fixer
from .fingerprint import fingerprint_of
from .ollama_client import call_listener
from .report import generate_pdf_report
from .runlog import RunLog, format_summary
from .schemas import AuditReport, Finding, FixAttempt

EventCallback = Optional[Callable[[str, dict], None]]


class AgentState(TypedDict):
    repo_path: str
    branch_name: str
    iteration: int
    max_iterations: int
    report: AuditReport
    fix_attempts: list[FixAttempt]
    done: bool
    stop_reason: Optional[str]
    # Carried across fix passes so the circuit breaker still trips when each
    # pass has fewer blocking findings than OLLAMA_MAX_CONSECUTIVE_TIMEOUTS.
    consecutive_fix_timeouts: int
    # Per finding fingerprint: {"attempts", "last_reason", "last_notes",
    # "file_hash"}. The memory between passes that stops the Fixer from
    # sending the model the same request over and over.
    history: dict[str, dict]
    # The first audit's suggestions; re-audits don't regenerate them.
    improvements: list[str]


def _emit(cb: EventCallback, event: str, payload: dict) -> None:
    if cb:
        cb(event, payload)


# Rejections that come from the file itself, not from what the model wrote:
# retrying them on an unchanged file can't turn out differently.
_NOT_RETRYABLE = {"no_file", "file_missing", "file_too_large"}


def _file_hash(repo_path: str, rel: Optional[str]) -> Optional[str]:
    if not rel:
        return None
    try:
        return hashlib.sha1((Path(repo_path) / rel).read_bytes()).hexdigest()
    except OSError:
        return None


def _key(finding: Finding) -> str:
    return finding.fingerprint or fingerprint_of(finding)


def _plan(state: "AgentState") -> list[Finding]:
    """The findings the next fix pass will attempt, most severe first.

    A finding drops out once it has used FIXER_MAX_ATTEMPTS_PER_FINDING
    attempts (marked needs_manual_fix), or when its last rejection came from
    the file itself and the file hasn't changed since.
    """
    todo = []
    for finding in state["report"].fixable_findings(config.FIXER_MIN_SEVERITY):
        past = state["history"].get(_key(finding))
        if past:
            if past["attempts"] >= config.FIXER_MAX_ATTEMPTS_PER_FINDING:
                finding.needs_manual_fix = True
                finding.fix_notes = (
                    f"ZweiGeeks gave up after {past['attempts']} attempts. Last rejection: "
                    f"{past['last_notes']}"
                )
                continue
            if past["last_reason"] in _NOT_RETRYABLE and past["file_hash"] == _file_hash(
                state["repo_path"], finding.file
            ):
                finding.needs_manual_fix = True
                finding.fix_notes = past["last_notes"]
                continue
        todo.append(finding)
    return todo


def build_graph(on_event: EventCallback = None, runlog: Optional[RunLog] = None):
    def audit_node(state: AgentState) -> AgentState:
        _emit(on_event, "iteration_start", {"iteration": state["iteration"] + 1})
        first = state["iteration"] == 0
        report = auditor.run_audit(state["repo_path"], on_event=on_event, improvements=first)
        if first:
            state["improvements"] = report.improvements
        else:
            report.improvements = state["improvements"]
        state["report"] = report
        state["iteration"] += 1
        todo = _plan(state)
        if runlog:
            runlog.audit(state["iteration"], report)

        if report.audit_incomplete:
            # The LLM review didn't get to finish - an Ollama problem, not a
            # clean repo. Reporting "clean" here would be actively wrong, so
            # this is its own stop reason, never folded into "no findings".
            reason = report.incomplete_reason or "unknown"
            state["done"] = True
            state["stop_reason"] = f"{reason}_incomplete"
            _emit(on_event, "loop_end", {"reason": f"incomplete_{reason}", "blocking_count": len(todo)})
        elif not todo:
            stuck = [f for f in report.fixable_findings(config.FIXER_MIN_SEVERITY)] + [
                f for f in report.open_findings() if f.needs_manual_fix
            ]
            state["done"] = True
            if stuck:
                state["stop_reason"] = "needs_manual_fix"
                _emit(on_event, "loop_end", {"reason": "needs_manual_fix", "remaining": len(stuck)})
            else:
                state["stop_reason"] = "no_blocking_findings"
                _emit(on_event, "loop_end", {"reason": "clean"})
        elif state["iteration"] > state["max_iterations"]:
            # max_iterations counts fix passes. Each one is followed by an
            # audit, so the last audit checks the last fixes; only then stop.
            state["done"] = True
            state["stop_reason"] = "max_iterations_reached"
            _emit(on_event, "loop_end", {"reason": "budget_exhausted", "remaining": len(todo)})
        else:
            _emit(on_event, "loop_continue", {"blocking_count": len(todo)})
        return state

    def route_after_audit(state: AgentState) -> str:
        return "end" if state["done"] else "fix"

    def route_after_fix(state: AgentState) -> str:
        return "end" if state["done"] else "audit"

    def fix_node(state: AgentState) -> AgentState:
        todo = _plan(state)[: config.FIXER_MAX_FIXES_PER_PASS]
        edited: set[str] = set()
        applied = 0
        for finding in todo:
            if state["done"]:
                break
            key = _key(finding)
            # Retry right away (not next pass): with the no-progress stop, a
            # finding rejected in a pass where nothing else landed would never
            # get another chance. Each retry carries the last rejection and a
            # different seed, so it's never a repeat of the same request.
            while True:
                past = state["history"].get(key) or {"attempts": 0}
                if past["attempts"] >= config.FIXER_MAX_ATTEMPTS_PER_FINDING:
                    finding.needs_manual_fix = True
                    finding.fix_notes = (
                        f"ZweiGeeks gave up after {past['attempts']} attempts. Last rejection: "
                        f"{past['last_notes']}"
                    )
                    break
                file_hash = _file_hash(state["repo_path"], finding.file)
                previous = (
                    {"reason": past["last_reason"], "notes": past["last_notes"]} if past["attempts"] else None
                )
                attempt = fixer.apply_fix(
                    state["repo_path"], finding, on_event=on_event,
                    previous=previous, attempt_number=past["attempts"],
                )
                state["history"][key] = {
                    "attempts": past["attempts"] + 1,
                    "last_reason": attempt.reason,
                    "last_notes": attempt.notes,
                    "file_hash": file_hash,
                }
                state["fix_attempts"].append(attempt)
                if runlog:
                    runlog.fix_attempt(
                        state["iteration"], finding, attempt, attempt_no=past["attempts"] + 1,
                        seed=config.OLLAMA_SEED + past["attempts"],
                    )
                if attempt.timed_out:
                    state["consecutive_fix_timeouts"] += 1
                    if state["consecutive_fix_timeouts"] >= config.OLLAMA_MAX_CONSECUTIVE_TIMEOUTS:
                        # Another attempt would just time out again, so end
                        # the run here (keeping whatever did get fixed).
                        state["done"] = True
                        state["stop_reason"] = "fixer_ollama_timeouts"
                        _emit(on_event, "fix_pass_aborted", {"reason": "ollama_wedged"})
                        _emit(on_event, "loop_end", {"reason": "fixer_ollama_timeouts"})
                    break  # a slow model won't get faster on a retry
                state["consecutive_fix_timeouts"] = 0
                if attempt.patch_applied:
                    applied += 1
                    finding.fixed = True
                    finding.fix_notes = attempt.notes
                    if attempt.file:
                        edited.add(attempt.file)
                    break
                if attempt.reason in _NOT_RETRYABLE or attempt.reason == "ollama_unreachable":
                    break

        repo = git_utils.open_repo(state["repo_path"])
        changed = git_utils.commit_files(
            repo, sorted(edited), f"zweigeeks: pass {state['iteration']} fixes"
        )
        _emit(on_event, "commit", {"iteration": state["iteration"], "changed": changed})
        if applied == 0 and not state["done"]:
            # Nothing changed, so another audit would find the same things and
            # the next pass would face the same files: stop instead.
            state["done"] = True
            state["stop_reason"] = "no_progress"
            _emit(on_event, "loop_end", {"reason": "no_progress", "remaining": len(todo)})
        return state

    graph = StateGraph(AgentState)
    graph.add_node("audit", audit_node)
    graph.add_node("fix", fix_node)
    graph.set_entry_point("audit")
    graph.add_conditional_edges("audit", route_after_audit, {"fix": "fix", "end": END})
    graph.add_conditional_edges("fix", route_after_fix, {"audit": "audit", "end": END})
    return graph.compile()


def run_audit_only(repo_path: str, on_event: EventCallback = None) -> dict:
    """Runs just the Auditor - no sandbox branch, no Fixer, no writes to the
    target repo at all - and generates the PDF report from the result.

    Useful when you want a findings report without letting the Fixer touch
    any files (e.g. read-only scans, or reviewing findings before deciding
    whether to let the full audit<->fix loop run).
    """
    runlog = RunLog(repo_path, "audit")
    with call_listener(runlog.on_llm_call):
        report = auditor.run_audit(repo_path, on_event=on_event)
    runlog.audit(1, report)
    _emit(
        on_event,
        "audit_only_done",
        {"finding_count": len(report.findings), "blocking": len(report.blocking_findings())},
    )

    pdf_path = None
    try:
        pdf_path = generate_pdf_report(report, repo_path)
        _emit(on_event, "pdf_report", {"path": pdf_path})
    except Exception as exc:  # noqa: BLE001 - a report failure shouldn't hide the audit results
        _emit(on_event, "pdf_report_failed", {"error": str(exc)})

    stop_reason = f"{report.incomplete_reason}_incomplete" if report.audit_incomplete else "audit_only"
    summary = _finish_summary(runlog, report, stop_reason, on_event)
    return {"report": report, "pdf_path": pdf_path, "summary": summary}


def _finish_summary(runlog: RunLog, report: AuditReport, stop_reason: Optional[str], on_event: EventCallback) -> dict:
    summary = runlog.summary(report, stop_reason)
    print(format_summary(summary), flush=True)
    _emit(on_event, "run_summary", summary)
    return summary


def run_pipeline(
    repo_path: str,
    max_iterations: int = 5,
    on_event: EventCallback = None,
    apply_fixes: bool = False,
) -> AgentState:
    """Audit -> fix -> re-audit for up to `max_iterations` fix passes, on a
    sandbox branch. The repo is switched back to the user's branch at the end.

    With `apply_fixes`, the fixes are then also put into the working copy, on
    a new `<sandbox>-review` branch off the user's branch, as uncommitted
    changes - so they show up in the editor's source-control view.
    """
    repo = git_utils.open_repo(repo_path)
    original_branch = repo.active_branch.name
    branch_name = git_utils.create_sandbox_branch(repo)
    _emit(on_event, "branch_created", {"branch": branch_name, "from": original_branch})

    initial_state: AgentState = {
        "repo_path": repo_path,
        "branch_name": branch_name,
        "iteration": 0,
        "max_iterations": max_iterations,
        "report": AuditReport(),
        "fix_attempts": [],
        "done": False,
        "stop_reason": None,
        "consecutive_fix_timeouts": 0,
        "history": {},
        "improvements": [],
    }

    runlog = RunLog(repo_path, "fix")
    graph = build_graph(on_event=on_event, runlog=runlog)
    try:
        with call_listener(runlog.on_llm_call):
            final_state = graph.invoke(initial_state, config={"recursion_limit": max_iterations * 3 + 5})
        diff = git_utils.diff_against(repo, original_branch)
    finally:
        # Always hand the repo back on the branch the user had checked out, so
        # the next run starts from their code rather than the previous run's
        # fixes. Anything a crash or Ctrl+C left mid-fix-pass is committed to
        # the sandbox branch first, so it's kept for review and doesn't leave
        # a dirty tree that blocks the next run.
        if git_utils.commit_tracked_changes(repo, "zweigeeks: partial fixes (run interrupted)"):
            _emit(on_event, "commit", {"iteration": "interrupted", "changed": True})
        git_utils.return_to_original_branch(repo, original_branch)
        _emit(on_event, "branch_restored", {"branch": original_branch, "fixes_on": branch_name})

    pdf_path = None
    try:
        pdf_path = generate_pdf_report(final_state["report"], repo_path)
        _emit(on_event, "pdf_report", {"path": pdf_path})
    except Exception as exc:  # noqa: BLE001 - a report failure shouldn't hide the audit results
        _emit(on_event, "pdf_report_failed", {"error": str(exc)})

    _emit(
        on_event,
        "pipeline_done",
        {
            "branch": branch_name,
            "original_branch": original_branch,
            "stop_reason": final_state["stop_reason"],
            "diff_lines": len(diff.splitlines()),
            "pdf_path": pdf_path,
        },
    )
    final_state["diff"] = diff
    final_state["pdf_path"] = pdf_path
    final_state["summary"] = _finish_summary(runlog, final_state["report"], final_state["stop_reason"], on_event)
    final_state["original_branch"] = original_branch
    final_state["review_branch"] = None
    final_state["review_files"] = []
    if apply_fixes and diff.strip():
        try:
            review, files = git_utils.open_for_review(repo, original_branch, branch_name)
            final_state["review_branch"], final_state["review_files"] = review, files
            _emit(on_event, "fixes_in_working_copy", {"branch": review, "files": files})
        except Exception as exc:  # noqa: BLE001 - the fixes are still on the sandbox branch
            _emit(on_event, "fixes_in_working_copy_failed", {"error": str(exc)})
    return final_state