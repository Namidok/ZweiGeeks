"""The benchmark harness (bench/): truth.json validation, finding-to-bug
matching, the scoring math, the validity of every shipped case, and one
end-to-end run of the harness with every model call mocked."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bench import core, run, worker
from src.agents import auditor, fixer, reviewer
from tests.conftest import FIXTURES, ollama_reply

CASES = core.list_cases()
BENCH_FIXTURES = FIXTURES / "bench_cases"


def _bug(**overrides) -> dict:
    data = {
        "id": "x1", "file": "app.py", "line_text": "return 1", "category": "logic", "tier": "B",
        "fix_check": {"type": "absent_regex", "file": "app.py", "pattern": "return 1"},
    }
    data.update(overrides)
    return data


# --- truth.json ----------------------------------------------------------------


def test_every_shipped_case_has_a_valid_truth_file():
    assert {c.id for c in CASES} == {
        "a1_inventory_service", "a2_web_utils", "b1_pagination", "b2_cart", "c1_text_stats", "c2_config_loader",
    }
    for case in CASES:
        assert core.validate_truth(json.loads((case.path / "truth.json").read_text())) == []
        if case.tier in ("A", "B"):
            assert len(case.bugs) >= 3, case.id
        else:
            assert case.bugs == [], case.id


def test_a_valid_truth_file_passes():
    assert core.validate_truth({"tier": "B", "bugs": [_bug()]}) == []
    test_check = {"type": "test_passes", "path": "bug_tests/test_x.py::test_y"}
    assert core.validate_truth({"tier": "B", "bugs": [_bug(fix_check=test_check)]}) == []
    assert core.validate_truth({"tier": "C", "bugs": []}) == []


@pytest.mark.parametrize("truth, problem", [
    ([], "JSON object"),
    ({"tier": "D", "bugs": []}, "tier must be one of"),
    ({"tier": "B"}, "bugs must be a list"),
    ({"tier": "C", "bugs": [_bug(tier="C")]}, "clean control"),
    ({"tier": "B", "bugs": [_bug(file="")]}, "file must be a non-empty string"),
    ({"tier": "B", "bugs": [_bug(line_text="  return 1")]}, "must be stripped"),
    ({"tier": "B", "bugs": [_bug(tier="A")]}, "must match the case tier"),
    ({"tier": "B", "bugs": [_bug(), _bug()]}, "used twice"),
    ({"tier": "B", "bugs": [_bug(fix_check={"type": "absent_regex", "file": "a.py", "pattern": "("})]}, "doesn't compile"),
    ({"tier": "B", "bugs": [_bug(fix_check={"type": "test_passes", "path": "tests/test_x.py"})]}, "bug_tests/"),
    ({"tier": "B", "bugs": [_bug(fix_check={"type": "grep"})]}, "type must be one of"),
    ({"tier": "B", "bugs": [_bug(fix_check=None)]}, "must be an object"),
])
def test_invalid_truth_files_are_rejected(truth, problem):
    errors = core.validate_truth(truth)
    assert any(problem in e for e in errors), errors


def test_cases_can_be_picked_by_short_id():
    assert [c.id for c in core.list_cases(only=["a1", "b2_cart"])] == ["a1_inventory_service", "b2_cart"]
    with pytest.raises(ValueError, match="z9"):
        core.list_cases(only=["z9"])


# --- matching -------------------------------------------------------------------


@pytest.mark.parametrize("finding, expected", [
    ({"file": "app.py", "line": 40, "line_text": "return 1"}, True),     # same text, line moved
    ({"file": "app.py", "line": 13, "line_text": None}, True),           # +3
    ({"file": "app.py", "line": 7, "line_text": "x = 2"}, True),         # -3
    ({"file": "app.py", "line": 14, "line_text": "x = 2"}, False),       # +4
    ({"file": "./app.py", "line": 10, "line_text": None}, True),         # path spelled with ./
    ({"file": "lib.py", "line": 10, "line_text": "return 1"}, False),    # other file
    ({"file": None, "line": None, "line_text": None}, False),            # file-less (pytest suite)
    ({"file": "app.py", "line": None, "line_text": None}, False),
])
def test_matching(finding, expected):
    assert core.matches(finding, _bug(), 10) is expected


def test_detection_is_scored_per_source():
    bugs = [_bug(id="b1"), _bug(id="b2", line_text="x = 2")]
    findings = [
        {"source_tool": "bandit", "file": "app.py", "line": 10, "line_text": "return 1"},
        {"source_tool": "llm-review", "file": "app.py", "line": 31, "line_text": "x = 2"},
        {"source_tool": "llm-review", "file": "other.py", "line": 10, "line_text": "return 1"},
    ]
    assert core.score_detection(findings, bugs, {"b1": 10, "b2": 30}) == {
        "b1": {"static": True, "llm": False}, "b2": {"static": False, "llm": True},
    }


def test_fixes_are_attributed_to_the_source_of_the_finding_they_fixed():
    bugs = [_bug(id="b1"), _bug(id="b2", line_text="x = 2"), _bug(id="b3", line_text="y = 3")]
    lines = {"b1": 10, "b2": 30, "b3": 50}
    by_fp = {
        "fp-ruff": {"source_tool": "ruff", "file": "app.py", "line": 10, "line_text": "return 1"},
        "fp-llm": {"source_tool": "llm-review", "file": "app.py", "line": 30, "line_text": "x = 2"},
    }
    attempts = [
        {"fingerprint": "fp-ruff", "source_tool": "ruff", "applied": True},
        {"fingerprint": "fp-llm", "source_tool": "llm-review", "applied": False},  # rejected: no credit
        {"fingerprint": "fp-llm", "source_tool": "llm-review", "applied": True},
        {"fingerprint": "unknown", "source_tool": "bandit", "applied": True},      # finding not seen: skipped
        {"fingerprint": None, "source_tool": "jax-review", "file": "app.py", "line": 51, "applied": True},
    ]
    assert core.attribute_fixes(attempts, by_fp, bugs, lines) == {"b1": ["static"], "b2": ["llm"], "b3": ["other"]}
    assert core.attempt_source(attempts[0], by_fp) == "static"
    assert core.attempt_source(attempts[2], by_fp) == "llm"
    assert core.attempt_source(attempts[4], by_fp) == "other"


def test_regressions_only_count_tests_that_used_to_pass():
    before = {"t::a": "passed", "t::b": "passed", "t::c": "failed"}
    after = {"t::a": "passed", "t::b": "failed", "t::c": "failed"}
    assert core.regressions(before, after) == ["t::b"]
    assert core.regressions(before, {}) == ["t::a", "t::b"]  # the suite didn't run at all


# --- scoring math ---------------------------------------------------------------


def _run(case, tier, repeat, *, bugs, matched, fixed, attributed, findings=(), files=4,
         before=2, broken=0, attempts=(), calls=0, seconds=0.0) -> dict:
    return {
        "case": case, "tier": tier, "repeat": repeat, "seed": 42 + repeat, "bugs": bugs, "files": files,
        "detection": {"matched": matched, "findings": [{"source": s} for s in findings]},
        "fixing": {
            "fixed": fixed, "attributed": attributed, "attempts": [{"source": s} for s in attempts],
            "behaviour_before": {f"t::{i}": "passed" for i in range(before)},
            "regressions": [f"t::{i}" for i in range(broken)],
            "summary": {"llm_total": {"calls": calls, "seconds": seconds}},
            "stop_reason": "no_progress", "applied_files": [],
        },
    }


def test_counts_for_one_run():
    r = _run("b1", "B", 0, bugs=["x", "y", "z"],
             matched={"x": {"static": True, "llm": True}, "y": {"static": False, "llm": True}},
             fixed={"x": True, "y": True, "z": False}, attributed={"x": ["llm", "static"], "y": []},
             attempts=["static", "llm", "llm", "other"], before=4, broken=1, calls=10, seconds=20.0)
    c = core.run_counts(r)
    assert c["detected"] == {"static": 1, "llm": 2}
    assert c["fixed"] == {"static": 1, "llm": 1, "other": 1}  # x by both sources, y by neither
    assert c["fixed_any"] == 2
    assert c["attempts"] == {"static": 1, "llm": 2, "other": 1}
    assert (c["behaviour_tests"], c["regressions"], c["model_calls"]) == (4, 1, 10)


def test_tier_metrics_pool_cases_and_spread_over_repeats():
    runs = [
        # repeat 0: tier B pools 2 + 2 = 4 bugs
        _run("b1", "B", 0, bugs=["p", "q"], matched={"p": {"static": False, "llm": True}},
             fixed={"p": True}, attributed={"p": ["llm"]}, attempts=["llm", "llm"], calls=8, seconds=40.0),
        _run("b2", "B", 0, bugs=["r", "s"], matched={"r": {"static": False, "llm": True}},
             fixed={"r": True}, attributed={"r": ["llm"]}, attempts=["llm", "llm"], before=2, broken=1, calls=8,
             seconds=40.0),
        # repeat 1
        _run("b1", "B", 1, bugs=["p", "q"], matched={}, fixed={}, attributed={}, calls=4),
        _run("b2", "B", 1, bugs=["r", "s"], matched={"r": {"static": False, "llm": True}},
             fixed={}, attributed={}, calls=4),
        # clean control: 3 static + 1 llm findings over 4 + 6 files, then 0 + 2 over 10
        _run("c1", "C", 0, bugs=[], matched={}, fixed={}, attributed={}, findings=["static"] * 3 + ["llm"], files=4),
        _run("c2", "C", 0, bugs=[], matched={}, fixed={}, attributed={}, files=6),
        _run("c1", "C", 1, bugs=[], matched={}, fixed={}, attributed={}, findings=["llm", "llm"], files=4),
        _run("c2", "C", 1, bugs=[], matched={}, fixed={}, attributed={}, files=6),
        {"case": "b1", "tier": "B", "repeat": 2, "error": "worker crashed"},  # left out of the means
    ]
    agg = core.aggregate(runs)

    b = agg["B"]
    assert b["repeats"] == 2
    assert b["recall"]["llm"] == {"mean": 0.375, "min": 0.25, "max": 0.5, "n": 2}
    assert b["recall"]["static"] == {"mean": 0.0, "min": 0.0, "max": 0.0, "n": 2}
    assert b["fix_rate"]["llm"] == {"mean": 0.25, "min": 0.0, "max": 0.5, "n": 2}
    assert b["regression_rate"] == {"mean": 0.125, "min": 0.0, "max": 0.25, "n": 2}
    # repeat 0: 4 llm attempts / 2 llm fixes; repeat 1 fixed nothing, so it has no value
    assert b["attempts_per_fixed_bug"]["llm"] == {"mean": 2.0, "min": 2.0, "max": 2.0, "n": 1}
    assert b["attempts_per_fixed_bug"]["static"] is None
    assert b["model_calls_per_fixed_bug"] == {"mean": 8.0, "min": 8.0, "max": 8.0, "n": 1}
    assert b["model_seconds_per_fixed_bug"]["mean"] == 40.0

    c = agg["C"]
    assert c["false_alarms_per_file"]["static"] == pytest.approx({"mean": 0.15, "min": 0.0, "max": 0.3, "n": 2})
    assert c["false_alarms_per_file"]["llm"] == pytest.approx({"mean": 0.15, "min": 0.1, "max": 0.2, "n": 2})
    assert c["recall"]["llm"] is None  # no bugs, no recall

    table = core.markdown({"started": "x", "repeats": 2, "seeds": "42..43", "passes": 3, "models": {}}, runs, agg)
    assert "| Recall | 0.00 (0.00–0.00) | 0.38 (0.25–0.50) | |" in table
    assert "| False alarms per file | 0.15 (0.00–0.30) | 0.15 (0.10–0.20) | |" in table
    assert "worker crashed" in table


def test_spread_ignores_missing_values():
    assert core.spread([None, None]) is None
    assert core.spread([1.0, None, 3.0]) == {"mean": 2.0, "min": 1.0, "max": 3.0, "n": 2}


def test_results_are_never_overwritten(tmp_path):
    first = run._results_path(tmp_path, "20261009-120000")
    first.write_text("{}")
    second = run._results_path(tmp_path, "20261009-120000")
    assert first.name == "20261009-120000.json" and second.name == "20261009-120000-2.json"


def test_each_repeat_gets_its_own_seed_models_and_cache(tmp_path):
    case = CASES[0]
    env = run.job_env({"case": case, "repeat": 1, "seed": 43, "work": tmp_path,
                       "models": {"auditor": None, "fixer": "qwen2.5-coder:14b", "verifier": None}})
    assert env["OLLAMA_SEED"] == "43"
    assert env["FIXER_MODEL"] == "qwen2.5-coder:14b"
    assert env["REVIEW_CACHE_DIR"].startswith(str(tmp_path))
    assert env["REPORTS_DIR"].startswith(str(tmp_path))


# --- every shipped case is a valid experiment ------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_case_is_valid_on_the_unmodified_repo(case, tmp_path):
    for bug in case.bugs:
        assert core.bug_line(case.repo, bug), f"{bug['id']}: line_text not found in {bug['file']}"

    behaviour = core.run_pytest(case.path / "behaviour_tests", case.repo, tmp_path / "behaviour")
    assert behaviour, "no behaviour tests ran"
    assert set(behaviour.values()) == {"passed"}, behaviour

    if (case.path / "bug_tests").is_dir():
        bug_results = core.run_pytest(case.path / "bug_tests", case.repo, tmp_path / "bugs")
        assert bug_results, "no bug tests ran"
        assert "passed" not in bug_results.values(), bug_results

    for bug in case.bugs:
        check = bug["fix_check"]
        if check["type"] == "absent_regex":
            text = (case.repo / check["file"]).read_text()
            assert core.re.search(check["pattern"], text, core.re.MULTILINE), f"{bug['id']}: pattern not present"
        assert not core.check_fix(check, case.repo, case.path, tmp_path / "checks"), f"{bug['id']} already passes"

    assert not [p for p in case.path.rglob("*") if core.is_generated(p.relative_to(case.path))], \
        "running the case's tests left files in it"


# --- the harness end to end, models mocked --------------------------------------------------


def _fake_chat(model, messages, options=None, timeout=None):
    """Stands in for every model the run calls: KAI's review finds the logic
    bug, JAX fixes it, the verifier approves; everything else finds nothing."""
    system, user = messages[0]["content"], messages[-1]["content"]
    if system == auditor.FILE_REVIEW_SYSTEM_PROMPT:
        if "File: calc.py" in user:
            return ollama_reply(json.dumps([{
                "category": "quality", "severity": "high", "line": 9,
                "title": "average divides by the wrong count",
                "detail": "Dividing by len(values) + 1 makes every average too small.",
                "resolution": "Divide by len(values).",
            }]))
        return ollama_reply("[]")
    if system == fixer.FIX_SYSTEM_PROMPT:
        if "average divides" in user:
            return ollama_reply(
                "<<<<<<< SEARCH\n    return sum(values) / (len(values) + 1)\n=======\n"
                "    return sum(values) / len(values)\n>>>>>>> REPLACE\n"
            )
        return ollama_reply("NO_FIX_NEEDED: handled elsewhere")
    if system == fixer.VERIFY_SYSTEM_PROMPT:
        return ollama_reply('{"resolved": true, "preserves_behavior": true, "reason": "divides by the count"}')
    if system == reviewer.REVIEW_SYSTEM_PROMPT:
        return ollama_reply("{}")
    return ollama_reply("[]")  # KAI's broader suggestions


def test_harness_end_to_end_with_mocked_models(monkeypatch, tmp_path):
    for module in (auditor, fixer, reviewer):
        monkeypatch.setattr(module, "chat", _fake_chat)

    def in_process(job):
        return worker.run_case(job["case"], job["repeat"], job["passes"], Path(job["work"]))

    path = run.main(["--cases", "t1", "--repeats", "1", "--passes", "2", "--out", str(tmp_path / "results")],
                    cases_root=BENCH_FIXTURES, runner=in_process)

    data = json.loads(path.read_text())
    (r,) = data["runs"]
    assert r["detection"]["matched"] == {
        "t1-unused-import": {"static": True, "llm": False},
        "t1-average-denominator": {"static": False, "llm": True},
    }
    assert r["fixing"]["fixed"] == {"t1-unused-import": True, "t1-average-denominator": True}
    assert r["fixing"]["attributed"] == {"t1-unused-import": ["static"], "t1-average-denominator": ["llm"]}
    # The referee really ran, before and after: an empty result would also mean "no regressions".
    assert r["fixing"]["behaviour_before"] == r["fixing"]["behaviour_after"] == {
        "test_calc::test_average_of_nothing_is_zero": "passed",
    }
    assert r["fixing"]["regressions"] == []
    assert r["fixing"]["applied_files"] == ["calc.py"]

    a = data["aggregate"]["A"]
    assert a["recall"]["static"]["mean"] == 0.5 and a["recall"]["llm"]["mean"] == 0.5
    assert a["fix_rate"]["static"]["mean"] == 0.5 and a["fix_rate"]["llm"]["mean"] == 0.5
    assert a["regression_rate"]["mean"] == 0.0
    table = path.with_suffix(".md").read_text()
    assert "## Tier A" in table and "| t1_tiny | 0 |" in table
    # The case itself is untouched: every run works on its own copies.
    assert "(len(values) + 1)" in (BENCH_FIXTURES / "t1_tiny" / "repo" / "calc.py").read_text()
