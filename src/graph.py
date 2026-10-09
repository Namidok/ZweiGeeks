"""LangGraph wiring for the Auditor <-> Fixer loop.

Graph shape:

    audit --(fixable findings or deterministic fixes pending, budget left)--> fix --> audit
    audit --(clean, incomplete, nothing left the Fixer can do, or budget exhausted)--> END
    fix --(a pass applied nothing, or the Fixer kept timing out)--> END

The graph runs against a private detached git worktree of the user's HEAD,
never the user's own working tree (see git_utils). When it ends, run_pipeline
applies what changed to the user's working tree as unstaged edits, file by
file, and always removes the worktree.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable, Optional, TypedDict

from langgraph.graph import END, StateGraph

from . import config, git_utils
from .agents import auditor, deterministic, fixer
from .fingerprint import fingerprint_of
from .ollama_client import call_listener
from .report import generate_pdf_report
from .runlog import RunLog, format_summary
from .schemas import AuditReport, Finding, FixAttempt

EventCallback = Optional[Callable[[str, dict], None]]


class AgentState(TypedDict):
    repo_path: str  # the private worktree, not the user's repo
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
    # Files the user has uncommitted changes in: never edited, so everything
    # in `git diff` afterwards is the Fixer's.
    protected: frozenset
    # Protected files that had something to fix (for the run summary).
    left_alone: list[str]
    # The first audit's suggestions; re-audits don't regenerate them.
    improvements: list[str]


def _emit(cb: EventCallback, event: str, payload: dict) -> None:
    if cb:
        cb(event, payload)


# Rejections that come from the file itself, not from what the model wrote:
# retrying them on an unchanged file can't turn out differently.
_NOT_RETRYABLE = {"no_file", "file_missing", "file_too_large", "dismissed"}


def _file_hash(repo_path: str, rel: Optional[str]) -> Optional[str]:
    if not rel:
        return None
    try:
        return hashlib.sha1((Path(repo_path) / rel).read_bytes()).hexdigest()
    except OSError:
        return None


def _key(finding: Finding) -> str:
    return finding.fingerprint or fingerprint_of(finding)


def _relocate(repo_path: str, findings: list[Finding], files: set[str]) -> None:
    """After an edit, move each remaining finding in the edited files to the
    line that still holds its flagged text (nearest the old line), so the
    Fixer's window and edit matching aren't aimed at a stale line number."""
    for finding in findings:
        if finding.fixed or finding.file not in files or not finding.line_text:
            continue
        try:
            lines = (Path(repo_path) / finding.file).read_text(errors="ignore").splitlines()
        except OSError:
            continue
        hits = [n for n, line in enumerate(lines, 1) if line.strip() == finding.line_text]
        if hits:
            old = finding.line or hits[0]
            finding.line = min(hits, key=lambda n: abs(n - old))


def _flagged_line_gone(repo_path: str, finding: Finding) -> bool:
    if not finding.file or not finding.line_text:
        return False
    try:
        lines = (Path(repo_path) / finding.file).read_text(errors="ignore").splitlines()
    except OSError:
        return False
    return not any(line.strip() == finding.line_text for line in lines)


def _plan(state: "AgentState") -> list[Finding]:
    """The findings the next fix pass will attempt, most severe first.

    A finding drops out once it has used FIXER_MAX_ATTEMPTS_PER_FINDING
    attempts (marked needs_manual_fix), or when its last rejection came from
    the file itself and the file hasn't changed since.
    """
    todo = []
    protected = state.get("protected") or frozenset()
    for finding in state["report"].fixable_findings(config.FIXER_MIN_SEVERITY):
        if finding.file in protected:
            finding.needs_manual_fix = True
            finding.fix_notes = "You have uncommitted changes in this file, so ZweiGeeks left it alone."
            if finding.file not in state.setdefault("left_alone", []):
                state["left_alone"].append(finding.file)
            continue
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
                if past["last_reason"] == "dismissed":
                    finding.dismissed = True
                else:
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
        mechanical = deterministic.pending(report, state.get("protected") or frozenset())
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
        elif not todo and not mechanical:
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
            _emit(on_event, "loop_end", {"reason": "budget_exhausted", "remaining": len(todo) + len(mechanical)})
        else:
            _emit(on_event, "loop_continue", {"blocking_count": len(todo) + len(mechanical)})
        return state

    def route_after_audit(state: AgentState) -> str:
        return "end" if state["done"] else "fix"

    def route_after_fix(state: AgentState) -> str:
        return "end" if state["done"] else "audit"

    def fix_node(state: AgentState) -> AgentState:
        edited: set[str] = set()
        applied = 0
        # Deterministic fixes first: no model, no gates to fail.
        for finding, attempt in deterministic.run(
            state["repo_path"], state["report"], on_event, state.get("protected") or frozenset(),
        ):
            state["fix_attempts"].append(attempt)
            if runlog:
                runlog.fix_attempt(state["iteration"], finding, attempt)
            if attempt.patch_applied:
                applied += 1
                if attempt.file:
                    edited.add(attempt.file)
        _relocate(state["repo_path"], state["report"].findings, edited)

        todo = _plan(state)[: config.FIXER_MAX_FIXES_PER_PASS]
        for finding in todo:
            if state["done"]:
                break
            if _flagged_line_gone(state["repo_path"], finding):
                # An earlier fix in this pass already rewrote the flagged line
                # (e.g. bandit's B608 and the LLM's "SQL injection" on the same
                # query). Don't ask the model about code that's gone; the next
                # audit says whether it's really fixed.
                finding.fix_notes = "The flagged line was changed by an earlier fix; the next audit re-checks it."
                _emit(on_event, "fix_skipped", {"finding_id": finding.id, "reason": "line_changed"})
                continue
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
                        _relocate(state["repo_path"], todo, {attempt.file})
                    break
                if attempt.reason == "dismissed":
                    finding.dismissed = True
                    finding.fix_notes = attempt.notes
                if attempt.reason in _NOT_RETRYABLE or attempt.reason == "ollama_unreachable":
                    break

        _emit(on_event, "fix_pass_done", {"iteration": state["iteration"], "changed": applied})
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
    """Runs just the Auditor - no worktree, no Fixer, no writes to the
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
) -> AgentState:
    """Audit -> fix -> re-audit for up to `max_iterations` fix passes.

    All work happens in a detached git worktree of HEAD in a temp directory;
    the user's working tree, index and branch are never touched during the
    run (no branch is created either). At the end the changes are applied to
    the user's working tree as unstaged edits, skipping any file the user has
    changed themselves, and the worktree is removed - also on errors and
    Ctrl+C.
    """
    repo = git_utils.open_repo(repo_path)
    if not config.ISOLATE_RUNS:
        return _run_in_place(repo, repo_path, max_iterations, on_event)
    git_utils.prune_stale_worktrees(repo)
    worktree = git_utils.create_worktree(repo)
    _emit(on_event, "worktree_created", {})

    runlog = RunLog(repo_path, "fix")
    initial_state: AgentState = {
        "repo_path": worktree,
        "iteration": 0,
        "max_iterations": max_iterations,
        "report": AuditReport(),
        "fix_attempts": [],
        "done": False,
        "stop_reason": None,
        "consecutive_fix_timeouts": 0,
        "history": {},
        "improvements": [],
        "protected": frozenset(),
    }

    graph = build_graph(on_event=on_event, runlog=runlog)
    diff = ""
    landed: dict = {"applied": [], "skipped": [], "skipped_patch": None, "fixes_patch": None}
    try:
        with call_listener(runlog.on_llm_call):
            final_state = graph.invoke(initial_state, config={"recursion_limit": max_iterations * 3 + 5})
    finally:
        # Reached on success, on errors and on Ctrl+C: whatever was fixed so
        # far still goes to the user's working tree, then the worktree goes.
        try:
            diff = git_utils.worktree_diff(worktree)
            landed = git_utils.apply_to_working_tree(repo_path, worktree, runlog.dir)
            if landed["applied"] or landed["skipped"]:
                _emit(on_event, "fixes_applied", {
                    "files": landed["applied"], "skipped": landed["skipped"],
                    "skipped_patch": landed["skipped_patch"],
                })
        except Exception as exc:  # noqa: BLE001 - cleanup below must still run
            _emit(on_event, "fixes_apply_failed", {"error": str(exc), "patch": str(runlog.dir / "fixes.patch")})
        finally:
            git_utils.remove_worktree(repo, worktree)
            _emit(on_event, "worktree_removed", {})

    pdf_path = None
    try:
        pdf_path = generate_pdf_report(final_state["report"], repo_path)
        _emit(on_event, "pdf_report", {"path": pdf_path})
    except Exception as exc:  # noqa: BLE001 - a report failure shouldn't hide the audit results
        _emit(on_event, "pdf_report_failed", {"error": str(exc)})

    runlog.note(
        files_applied=landed["applied"], files_skipped=landed["skipped"],
        skipped_patch=landed["skipped_patch"], fixes_patch=landed["fixes_patch"],
    )
    _emit(
        on_event,
        "pipeline_done",
        {
            "stop_reason": final_state["stop_reason"],
            "diff_lines": len(diff.splitlines()),
            "pdf_path": pdf_path,
        },
    )
    final_state["diff"] = diff
    final_state["pdf_path"] = pdf_path
    final_state["applied_files"] = landed["applied"]
    final_state["skipped_files"] = landed["skipped"]
    final_state["skipped_patch"] = landed["skipped_patch"]
    final_state["summary"] = _finish_summary(runlog, final_state["report"], final_state["stop_reason"], on_event)
    return final_state


def _run_in_place(repo, repo_path: str, max_iterations: int, on_event: EventCallback) -> AgentState:
    """Edits the repo's working tree directly, like any editor would, and
    leaves everything uncommitted: no branch, no staging, no commit. Files
    with the user's own uncommitted changes are never edited, so `git diff`
    afterwards shows only what ZweiGeeks changed - review it in Source
    Control, keep it by committing, undo it with `git checkout -- <file>`."""
    protected = frozenset(git_utils.user_modified_files(repo_path))
    runlog = RunLog(repo_path, "fix")
    initial_state: AgentState = {
        "repo_path": repo_path,
        "iteration": 0,
        "max_iterations": max_iterations,
        "report": AuditReport(),
        "fix_attempts": [],
        "done": False,
        "stop_reason": None,
        "consecutive_fix_timeouts": 0,
        "history": {},
        "improvements": [],
        "protected": protected,
        "left_alone": [],
    }
    graph = build_graph(on_event=on_event, runlog=runlog)
    final_state = None
    try:
        with call_listener(runlog.on_llm_call):
            final_state = graph.invoke(initial_state, config={"recursion_limit": max_iterations * 3 + 5})
    finally:
        # Also on errors and Ctrl+C: whatever was fixed so far stays in the files.
        changed = sorted(set(git_utils.changed_files(repo_path)) - protected)
        skipped = []
        if final_state:
            skipped = [
                {"file": f, "reason": "you have uncommitted changes in this file"}
                for f in sorted(final_state.get("left_alone") or [])
            ]
        diff = git_utils.diff_of(repo_path, changed)
        patch = None
        if changed:
            runlog.dir.mkdir(parents=True, exist_ok=True)
            patch = str(runlog.dir / "fixes.patch")
            Path(patch).write_text(diff)
            _emit(on_event, "fixes_applied", {"files": changed, "skipped": skipped, "skipped_patch": None})

    pdf_path = None
    try:
        pdf_path = generate_pdf_report(final_state["report"], repo_path)
        _emit(on_event, "pdf_report", {"path": pdf_path})
    except Exception as exc:  # noqa: BLE001 - a report failure shouldn't hide the audit results
        _emit(on_event, "pdf_report_failed", {"error": str(exc)})

    runlog.note(files_applied=changed, files_skipped=skipped, skipped_patch=None, fixes_patch=patch)
    _emit(on_event, "pipeline_done", {
        "stop_reason": final_state["stop_reason"], "diff_lines": len(diff.splitlines()), "pdf_path": pdf_path,
    })
    final_state["diff"] = diff
    final_state["pdf_path"] = pdf_path
    final_state["applied_files"] = changed
    final_state["skipped_files"] = skipped
    final_state["skipped_patch"] = None
    final_state["summary"] = _finish_summary(runlog, final_state["report"], final_state["stop_reason"], on_event)
    return final_state
