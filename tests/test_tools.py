import json
import subprocess
from types import SimpleNamespace
from unittest import mock

from conftest import ollama_reply, write

from src.agents import auditor
from src.schemas import Category, Severity
from src.tools import api_checks, quality, security, stack_detect, ui_checks

# --- stack detection ---------------------------------------------------------

def test_stack_detect_uses_marker_files(tmp_path):
    write(tmp_path, "composer.json", "{}")
    write(tmp_path, "package.json", "{}")
    write(tmp_path, "deep/thing.py", "")  # markers win; no extension scan
    assert stack_detect.detect_stack(str(tmp_path)) == {"php", "node"}


def test_stack_detect_falls_back_to_extensions_and_skips_vendor_dirs(tmp_path):
    write(tmp_path, "app/index.php", "<?php")
    write(tmp_path, "node_modules/lib/x.js", "")
    assert stack_detect.detect_stack(str(tmp_path)) == {"php"}


def test_stack_detect_on_missing_dir(tmp_path):
    assert stack_detect.detect_stack(str(tmp_path / "nope")) == set()


# --- UI heuristics ------------------------------------------------------------

def test_ui_checks_cover_php_templates_too(tmp_path):
    write(tmp_path, "index.php", "<html><img src='a.png'>\n<img src='b.png' alt=\"logo\">" + ' style=""' * 6)
    write(tmp_path, "vendor/lib/widget.html", "<img src='x'>")

    issues = {(i["file"], i["issue"]) for i in ui_checks.scan_templates(str(tmp_path))}

    assert issues == {
        ("index.php", "img_missing_alt"),
        ("index.php", "excess_inline_styles"),
        ("index.php", "missing_viewport_meta"),
    }


def test_ui_checks_accept_a_page_with_viewport(tmp_path):
    write(tmp_path, "base.html", '<meta name="viewport" content="width=device-width">')
    assert ui_checks.scan_templates(str(tmp_path)) == []


# --- API route heuristics -------------------------------------------------------

def test_api_checks_flag_unvalidated_unauthenticated_mutations(tmp_path):
    write(tmp_path, "api.py", (
        "@app.get('/items')\ndef list_items(): ...\n\n"
        "@app.post('/items')\ndef create(payload: dict): ...\n\n"
        "@app.put('/items/{i}')\ndef update(i: int, body: Item = Body(...), user=Depends(current_user)): ...\n"
    ))

    issues = {(i["line"], i["issue"]) for i in api_checks.scan_routes(str(tmp_path))}

    assert issues == {(4, "mutating_route_no_validation"), (4, "mutating_route_no_auth_hint")}


# --- tool wrappers (subprocess mocked) ----------------------------------------

def test_bandit_wrapper_parses_json_and_survives_missing_binary(monkeypatch):
    monkeypatch.setattr(security, "project_files", lambda repo, suffixes=None: ["app.py"])
    out = json.dumps({"results": [{"test_id": "B307", "filename": "app.py"}]})
    run = mock.Mock(return_value=SimpleNamespace(stdout=out, stderr=""))
    monkeypatch.setattr(subprocess, "run", run)
    assert security.run_bandit("/r") == ([{"test_id": "B307", "filename": "app.py"}], out)
    # Only the listed project files are scanned - never `-r <repo>`.
    assert run.call_args.args[0][-1] == "app.py" and "-r" not in run.call_args.args[0]

    monkeypatch.setattr(subprocess, "run", mock.Mock(side_effect=FileNotFoundError))
    assert security.run_bandit("/r") == ([], "bandit not installed")


def test_pip_audit_wrapper_returns_raw_on_bad_json(monkeypatch):
    monkeypatch.setattr(subprocess, "run", mock.Mock(return_value=SimpleNamespace(stdout="oops", stderr="")))
    assert security.run_pip_audit("/r") == ([], "oops")


def test_ruff_wrapper_handles_timeout(monkeypatch):
    monkeypatch.setattr(quality, "project_files", lambda repo, suffixes=None: ["app.py"])
    monkeypatch.setattr(subprocess, "run", mock.Mock(side_effect=subprocess.TimeoutExpired("ruff", 120)))
    assert quality.run_ruff("/r") == ([], "ruff timed out")


def test_pytest_wrapper_reports_failures(monkeypatch):
    proc = SimpleNamespace(returncode=1, stdout="1 failed, 3 passed", stderr="")
    monkeypatch.setattr(subprocess, "run", mock.Mock(return_value=proc))
    summary, _ = quality.run_tests_with_coverage("/r")
    assert summary == {"returncode": 1, "passed": False}


# --- static tool output -> Findings (Python stack) -----------------------------

def test_python_tool_output_is_mapped_straight_to_findings(tmp_path, monkeypatch):
    write(tmp_path, "requirements.txt", "flask==1.0\n")
    monkeypatch.setattr(auditor.security, "run_bandit", lambda repo: ([{
        "test_id": "B105", "test_name": "hardcoded_password_string", "issue_severity": "HIGH",
        "issue_text": "Possible hardcoded password", "filename": "app.py", "line_number": 3,
    }], "{}"))
    monkeypatch.setattr(auditor.security, "run_pip_audit", lambda repo: ([
        {"name": "flask", "version": "1.0", "vulns": [{"id": "PYSEC-1", "fix_versions": ["2.2.5"]}]},
        {"name": "click", "version": "8.0", "vulns": []},
    ], "{}"))
    monkeypatch.setattr(auditor.quality, "run_ruff", lambda repo: ([
        {"code": "F401", "message": "unused import", "filename": "app.py", "location": {"row": 1}},
    ], "[]"))
    monkeypatch.setattr(auditor.quality, "run_tests_with_coverage", lambda repo: ({"passed": False}, ""))
    monkeypatch.setattr(auditor.api_checks, "scan_routes", lambda repo: [{
        "file": "app.py", "line": 9, "issue": "mutating_route_no_auth_hint", "detail": "POST route",
    }])
    monkeypatch.setattr(auditor, "chat", mock.Mock(return_value=ollama_reply("[]")))

    report = auditor.run_audit(str(tmp_path))
    by_tool = {f.source_tool: f for f in report.findings}

    assert set(by_tool) == {"bandit", "pip-audit", "ruff", "pytest", "api-routes"}
    assert by_tool["bandit"].severity == Severity.HIGH
    assert "environment variable" in by_tool["bandit"].resolution  # canned, not LLM-written
    assert by_tool["pip-audit"].title == "flask 1.0: PYSEC-1"
    assert "2.2.5" in by_tool["pip-audit"].resolution
    assert (by_tool["ruff"].severity, by_tool["ruff"].line) == (Severity.LOW, 1)
    assert by_tool["pytest"].category == Category.TESTING
    assert "Depends" in by_tool["api-routes"].resolution
    assert set(report.raw_tool_output) == {"bandit", "pip-audit", "ruff", "pytest"}
    # Only the HIGH bandit and pip-audit findings would route to the Fixer.
    assert [f.source_tool for f in report.blocking_findings()] == ["bandit", "pip-audit"]
