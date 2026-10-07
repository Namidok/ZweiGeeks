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

from typing import Callable, Optional, TypedDict

from langgraph.graph import END, StateGraph

from . import config, git_utils
from .agents import auditor, fixer
from .ollama_client import call_listener
from .report import generate_pdf_report
from .runlog import RunLog, format_summary
from .schemas import AuditReport, FixAttempt

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


def _emit(cb: EventCallback, event: str, payload: dict) -> None:
    if cb:
        cb(event, payload)


def build_graph(on_event: EventCallback = None, runlog: Optional[RunLog] = None):
    def audit_node(state: AgentState) -> AgentState:
        _emit(on_event, "iteration_start", {"iteration": state["iteration"] + 1})
        report = auditor.run_audit(state["repo_path"], on_event=on_event)
        state["report"] = report
        state["iteration"] += 1
        if runlog:
            runlog.audit(state["iteration"], report)

        blocking = report.blocking_findings()
        if report.audit_incomplete:
            # The LLM review didn't get to finish - an Ollama problem, not a
            # clean repo. Reporting "clean" here would be actively wrong, so
            # this is its own stop reason, never folded into "no findings".
            reason = report.incomplete_reason or "unknown"
            state["done"] = True
            state["stop_reason"] = f"{reason}_incomplete"
            _emit(on_event, "loop_end", {"reason": f"incomplete_{reason}", "blocking_count": len(blocking)})
        elif not blocking:
            state["done"] = True
            state["stop_reason"] = "no_blocking_findings"
            _emit(on_event, "loop_end", {"reason": "clean"})
        elif state["iteration"] > state["max_iterations"]:
            # max_iterations counts fix passes. Each one is followed by an
            # audit, so the last audit checks the last fixes; only then stop.
            state["done"] = True
            state["stop_reason"] = "max_iterations_reached"
            _emit(on_event, "loop_end", {"reason": "budget_exhausted", "remaining": len(blocking)})
        else:
            _emit(on_event, "loop_continue", {"blocking_count": len(blocking)})
        return state

    def route_after_audit(state: AgentState) -> str:
        return "end" if state["done"] else "fix"

    def route_after_fix(state: AgentState) -> str:
        return "end" if state["done"] else "audit"

    def fix_node(state: AgentState) -> AgentState:
        blocking = state["report"].blocking_findings()
        edited: set[str] = set()
        for finding in blocking:
            attempt = fixer.apply_fix(state["repo_path"], finding, on_event=on_event)
            state["fix_attempts"].append(attempt)
            if runlog:
                runlog.fix_attempt(state["iteration"], finding, attempt)
            if attempt.patch_applied:
                finding.fixed = True
                finding.fix_notes = attempt.notes
                if attempt.file:
                    edited.add(attempt.file)
            if attempt.timed_out:
                state["consecutive_fix_timeouts"] += 1
                if state["consecutive_fix_timeouts"] >= config.OLLAMA_MAX_CONSECUTIVE_TIMEOUTS:
                    # Another audit->fix round would just time out again, so
                    # end the run here (after committing what did get fixed).
                    state["done"] = True
                    state["stop_reason"] = "fixer_ollama_timeouts"
                    _emit(on_event, "fix_pass_aborted", {"reason": "ollama_wedged"})
                    _emit(on_event, "loop_end", {"reason": "fixer_ollama_timeouts"})
                    break
            else:
                state["consecutive_fix_timeouts"] = 0

        repo = git_utils.open_repo(state["repo_path"])
        changed = git_utils.commit_files(
            repo, sorted(edited), f"zweigeeks: pass {state['iteration']} fixes"
        )
        _emit(on_event, "commit", {"iteration": state["iteration"], "changed": changed})
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