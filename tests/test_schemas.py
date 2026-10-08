import pytest

from src.schemas import AuditReport, Category, Finding, FixAttempt, Severity


def make_finding(fid="SEC-001", severity=Severity.HIGH, source_tool="llm-review", fixed=False, **kw):
    return Finding(
        id=fid, category=kw.pop("category", Category.SECURITY), severity=severity,
        title=kw.pop("title", "t"), detail=kw.pop("detail", "d"),
        source_tool=source_tool, fixed=fixed, **kw,
    )


@pytest.mark.parametrize(
    ("severity", "bucket"),
    [
        (Severity.CRITICAL, "high"),
        (Severity.HIGH, "high"),
        (Severity.MEDIUM, "medium"),
        (Severity.LOW, "low"),
        (Severity.INFO, "low"),
    ],
)
def test_report_bucket_collapses_five_severities_into_three(severity, bucket):
    assert make_finding(severity=severity).report_bucket() == bucket


def test_blocking_findings_are_open_high_or_critical_and_not_repo_hygiene():
    report = AuditReport(findings=[
        make_finding("A", Severity.CRITICAL, file="a.py"),
        make_finding("B", Severity.HIGH, source_tool="bandit", file="b.py"),
        make_finding("C", Severity.MEDIUM, file="c.py"),
        make_finding("D", Severity.LOW, file="d.py"),
        make_finding("E", Severity.INFO, file="e.py"),
        make_finding("F", Severity.CRITICAL, source_tool="repo-hygiene", file="f.env"),  # needs a human
        make_finding("G", Severity.HIGH, fixed=True, file="g.py"),
        make_finding("H", Severity.HIGH),  # no file: nothing for the Fixer to edit
        make_finding("I", Severity.HIGH, source_tool="pip-audit"),  # pin bump, not the LLM
        make_finding("J", Severity.HIGH, file="j.py", needs_manual_fix=True),  # Fixer gave up
    ])

    assert [f.id for f in report.blocking_findings()] == ["A", "B"]
    assert [f.id for f in report.fixable_findings("medium")] == ["A", "B", "C"]
    assert [f.id for f in report.open_findings()] == ["A", "B", "C", "D", "E", "F", "H", "I", "J"]


def test_fixable_findings_are_ordered_most_severe_first_security_first():
    report = AuditReport(findings=[
        make_finding("low", Severity.LOW, file="z.py"),
        make_finding("q", Severity.HIGH, category=Category.QUALITY, file="a.py"),
        make_finding("s", Severity.HIGH, category=Category.SECURITY, file="b.py"),
        make_finding("crit", Severity.CRITICAL, file="c.py"),
    ])
    assert [f.id for f in report.fixable_findings("low")] == ["crit", "s", "q", "low"]


def test_audit_report_defaults_to_complete():
    report = AuditReport()
    assert report.audit_incomplete is False
    assert report.incomplete_reason is None
    assert report.blocking_findings() == []


def test_fix_attempt_timed_out_defaults_false():
    attempt = FixAttempt(finding_id="SEC-001", patch_applied=False, notes="n")
    assert attempt.timed_out is False
