from reportlab.platypus import Paragraph, Table

from src import report as report_mod
from src.report import generate_pdf_report
from src.schemas import AuditReport, Category, Finding, Severity


def markup_finding(severity=Severity.LOW):
    # Tool/LLM text routinely contains literal tags. Unescaped, ReportLab
    # parses "<img>" as a real image tag with no src and the build fails.
    return Finding(
        id="UI-001", category=Category.UI, severity=severity, file="templates/<base>.html", line=12,
        title="<img> tag has no alt attribute",
        detail="<img src=x> and <script>alert(1)</script> & friends",
        resolution='Add alt="" to the <img> tag.', source_tool="ui-templates",
    )


def _paragraphs(flowables):
    """Paragraphs in story order, including those nested in Table cells."""
    for f in flowables:
        if isinstance(f, Paragraph):
            yield f
        elif isinstance(f, Table):
            for row in f._cellvalues:
                for cell in row:
                    yield from _paragraphs(cell if isinstance(cell, (list, tuple)) else [cell])


def story_text(monkeypatch, report) -> list[str]:
    """Captures the flowables handed to ReportLab instead of rendering them."""
    captured = []
    monkeypatch.setattr(report_mod.SimpleDocTemplate, "build", lambda self, story: captured.extend(story))
    generate_pdf_report(report, "/tmp/repo", output_path="unused.pdf")
    return [p.getPlainText() for p in _paragraphs(captured)]


def test_pdf_builds_when_finding_text_contains_literal_tags(tmp_path):
    report = AuditReport(
        findings=[markup_finding(Severity.CRITICAL), markup_finding(Severity.MEDIUM), markup_finding()],
        improvements=["Stop putting <br> in titles"],
    )
    out = tmp_path / "report.pdf"

    path = generate_pdf_report(report, "/tmp/some <repo>", output_path=str(out))

    assert path == str(out)
    assert out.read_bytes().startswith(b"%PDF")


def test_default_output_path_uses_reports_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(report_mod.config, "REPORTS_DIR", str(tmp_path / "out"))
    path = generate_pdf_report(AuditReport(), "/x/my-repo")
    assert path == str(tmp_path / "out" / "audit_report_my-repo.pdf")
    assert (tmp_path / "out" / "audit_report_my-repo.pdf").exists()


def test_incomplete_audit_gets_a_warning_banner_and_is_never_called_clean(monkeypatch):
    texts = story_text(monkeypatch, AuditReport(audit_incomplete=True, incomplete_reason="ollama_wedged"))

    banner = next(t for t in texts if "did not finish" in t)
    assert "stopped responding" in banner
    assert texts.index(banner) == 3  # straight after title, subtitle and summary counts
    assert not any("Clean run" in t for t in texts)
    assert "No findings recorded." in texts


def test_complete_empty_audit_is_reported_clean(monkeypatch):
    texts = story_text(monkeypatch, AuditReport())
    assert "No findings. Clean run." in texts
    assert not any("did not finish" in t for t in texts)


def test_findings_render_their_literal_text_and_summary_counts(monkeypatch):
    texts = story_text(monkeypatch, AuditReport(findings=[markup_finding(Severity.HIGH), markup_finding()]))

    assert "[UI-001] <img> tag has no alt attribute" in texts
    assert any("<script>alert(1)</script> & friends" in t for t in texts)
    assert any(t.startswith("1 high") and "2 total findings" in t for t in texts)
    assert any(t.startswith("HIGH PRIORITY") for t in texts)
