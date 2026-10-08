"""Fixes that need no model: ruff's safe fixes and dependency pin bumps.

In the measured baseline, all 21 pip-audit findings and every ruff finding
went unfixed: ruff findings are LOW (never sent to the LLM) and pip-audit
findings have no file for the LLM to edit.
"""

from __future__ import annotations

from unittest import mock

import pytest

from src import git_utils, graph
from src.agents import auditor, deterministic, fixer
from src.schemas import AuditReport, Category, Finding, Severity

PIP_AUDIT = [
    {"name": "PyYAML", "version": "5.3", "vulns": [
        {"id": "PYSEC-2020-96", "fix_versions": ["5.3.1"], "description": "arbitrary code execution"},
        {"id": "PYSEC-2021-142", "fix_versions": ["5.4"], "description": "arbitrary code execution"},
    ]},
    {"name": "requests", "version": "2.19.1", "vulns": [
        {"id": "PYSEC-2018-28", "fix_versions": ["2.20.0"], "description": "credentials leak"},
    ]},
]


@pytest.fixture
def audited(vuln_repo, monkeypatch):
    monkeypatch.setattr(auditor, "_review_file_with_llm", lambda *a, **k: [])
    monkeypatch.setattr(auditor, "_generate_improvements", lambda *a, **k: [])
    monkeypatch.setattr(auditor.security, "run_pip_audit", lambda repo: (PIP_AUDIT, "{}"))
    return vuln_repo, auditor.run_audit(str(vuln_repo))


def test_auditor_marks_what_a_deterministic_fixer_can_handle(audited):
    _, report = audited
    ruff = {f.rule: f.autofix for f in report.findings if f.source_tool == "ruff"}
    assert ruff == {"F401": {"tool": "ruff"}, "F541": {"tool": "ruff"}, "E711": None}  # E711's fix is unsafe
    pins = [f for f in report.findings if f.source_tool == "pip-audit"]
    assert {(f.file, f.line) for f in pins} == {("requirements.txt", 1), ("requirements.txt", 2)}
    assert all(f.autofix["tool"] == "pin" for f in pins)


def test_ruff_safe_fixes_and_pin_bumps_apply_without_the_llm(audited):
    repo, report = audited

    results = deterministic.run(str(repo), report)

    applied = {(f.source_tool, f.rule) for f, a in results if a.patch_applied}
    assert ("ruff", "F401") in applied and ("ruff", "F541") in applied
    assert all(a.kind == "deterministic" for _, a in results)
    helpers = (repo / "helpers.py").read_text()
    assert "import os" not in helpers and "import sys" not in helpers
    assert 'label = "item"' in helpers
    assert "item == None" in helpers  # unsafe fix: left for a human / the LLM
    # Lowest version that fixes *every* advisory: 5.4 for PyYAML (5.3.1 fixes only one).
    assert (repo / "requirements.txt").read_text() == "PyYAML==5.4\nrequests==2.20.0\n"
    pins = [f for f in report.findings if f.source_tool == "pip-audit"]
    assert all(f.fixed for f in pins)
    assert {"PyYAML 5.3 -> 5.4" in f.fix_notes or "requests 2.19.1 -> 2.20.0" in f.fix_notes for f in pins} == {True}


def dependency(name, version, fix_versions):
    return Finding(id=f"SEC-{name}", category=Category.SECURITY, severity=Severity.HIGH, title=name,
                   detail="d", source_tool="pip-audit", file="requirements.txt",
                   autofix={"tool": "pin", "package": name, "version": version, "fix_versions": fix_versions})


def test_pins_it_cant_bump_are_reported_not_guessed(git_repo):
    (git_repo / "requirements.txt").write_text("flask>=1.0  # not pinned\nolddep==1.0\n")
    report = AuditReport(findings=[dependency("flask", "1.0", ["2.2.5"]), dependency("olddep", "1.0", [])])

    results = {f.title: a for f, a in deterministic.run(str(git_repo), report)}

    assert results["flask"].reason == "not_pinned" and not results["flask"].patch_applied
    assert results["olddep"].reason == "no_fixed_version"
    assert (git_repo / "requirements.txt").read_text() == "flask>=1.0  # not pinned\nolddep==1.0\n"
    assert all(f.autofix is None for f in report.findings)  # not offered again next pass


def test_find_pin_handles_extras_case_and_separators():
    text = "# deps\nPy_YAML[libyaml] == 5.3 ; python_version>'3'\nrequests==2.19.1\n"
    assert deterministic.find_pin(text, "py-yaml") == (2, "5.3")
    assert deterministic.find_pin(text, "requests") == (3, "2.19.1")
    assert deterministic.find_pin(text, "flask") is None


def test_deterministic_work_alone_keeps_the_loop_going(audited, monkeypatch):
    """Only LOW ruff and file-bound pip-audit findings: nothing for the LLM,
    but the fix pass still runs - and the re-audit sees them gone."""
    repo, first = audited
    monkeypatch.setattr(git_utils, "open_repo", mock.Mock())
    monkeypatch.setattr(git_utils, "commit_files", mock.Mock(return_value=True))
    llm_fix = mock.Mock()
    monkeypatch.setattr(fixer, "apply_fix", llm_fix)
    first.findings = [f for f in first.findings if f.source_tool in ("ruff", "pip-audit")]
    reports = iter([first, AuditReport()])
    monkeypatch.setattr(auditor, "run_audit", lambda repo, on_event=None, **_: next(reports))

    final = graph.build_graph().invoke({
        "repo_path": str(repo), "branch_name": "b", "iteration": 0, "max_iterations": 5,
        "report": AuditReport(), "fix_attempts": [], "done": False, "stop_reason": None,
        "consecutive_fix_timeouts": 0, "history": {}, "improvements": [],
    }, config={"recursion_limit": 50})

    assert final["stop_reason"] == "no_blocking_findings"
    assert final["iteration"] == 2
    # F401 x2 + F541 from ruff, and 3 advisories (2 PyYAML, 1 requests) from 2 pin bumps.
    assert sum(a.patch_applied for a in final["fix_attempts"]) == 6
    llm_fix.assert_not_called()
