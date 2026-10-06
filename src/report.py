"""Turns an AuditReport into a PDF: findings grouped into the three-tier
High (red) / Medium (yellow) / Low (green) scheme, each with its
resolution, plus an Improvements section at the end.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    ListFlowable,
    ListItem,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from . import config
from .schemas import AuditReport, Finding

BUCKET_COLOR = {
    "high": colors.HexColor("#dc2626"),
    "medium": colors.HexColor("#d97706"),
    "low": colors.HexColor("#16a34a"),
}
BUCKET_LABEL = {"high": "HIGH PRIORITY", "medium": "MEDIUM PRIORITY", "low": "LOW PRIORITY"}
BUCKET_ORDER = ("high", "medium", "low")

_styles = getSampleStyleSheet()
_TITLE = ParagraphStyle("ZGTitle", parent=_styles["Title"], fontSize=20, spaceAfter=4)
_SUBTITLE = ParagraphStyle("ZGSubtitle", parent=_styles["Normal"], fontSize=10, textColor=colors.grey, spaceAfter=16)
_SUMMARY = ParagraphStyle("ZGSummary", parent=_styles["Normal"], fontSize=11, spaceAfter=18)
_WARNING = ParagraphStyle(
    "ZGWarning", parent=_styles["Normal"], fontSize=10.5, spaceAfter=18,
    textColor=colors.HexColor("#92400e"), backColor=colors.HexColor("#fef3c7"),
    borderPadding=8,
)
_INCOMPLETE_CAUSES = {
    "ollama_wedged": "the local LLM backend stopped responding (repeated timeouts)",
    "ollama_error": "calls to the local LLM backend kept failing (server down or model missing?)",
    "no_files_reviewed": "no file review produced usable output from the LLM",
}
_BUCKET_HEADER = ParagraphStyle(
    "ZGBucketHeader", parent=_styles["Heading2"], textColor=colors.white, fontSize=13,
    spaceBefore=0, spaceAfter=0, leftIndent=6, leading=20,
)
_FINDING_TITLE = ParagraphStyle("ZGFindingTitle", parent=_styles["Normal"], fontSize=11, fontName="Helvetica-Bold", spaceAfter=2)
_FINDING_META = ParagraphStyle("ZGFindingMeta", parent=_styles["Normal"], fontSize=8.5, textColor=colors.grey, spaceAfter=4)
_FINDING_DETAIL = ParagraphStyle("ZGFindingDetail", parent=_styles["Normal"], fontSize=9.5, spaceAfter=4, leading=13)
_FINDING_RESOLUTION = ParagraphStyle(
    "ZGFindingResolution", parent=_styles["Normal"], fontSize=9.5, leading=13,
    textColor=colors.HexColor("#1f2937"), backColor=colors.HexColor("#f3f4f6"),
    borderPadding=6, spaceAfter=2,
)
_SECTION_HEADER = ParagraphStyle("ZGSectionHeader", parent=_styles["Heading2"], fontSize=14, spaceBefore=20, spaceAfter=10)
_IMPROVEMENT_ITEM = ParagraphStyle("ZGImprovement", parent=_styles["Normal"], fontSize=10.5, leading=14)


def _bucket_header(bucket: str, count: int):
    label = f"{BUCKET_LABEL[bucket]}  ({count})"
    table = Table(
        [[Paragraph(label, _BUCKET_HEADER)]],
        colWidths=[6.6 * inch],
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), BUCKET_COLOR[bucket]),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    return table


def _finding_block(f: Finding, bucket: str):
    location = f.file or "-"
    if f.line:
        location += f":{f.line}"
    # Finding text comes from tool output / LLM output, not from us - it can
    # (and, in practice, does) contain literal "<...>" text that ReportLab's
    # Paragraph would otherwise try to parse as real markup (e.g. a finding
    # whose own detail text is "<img> tag has no alt attribute" breaks the
    # PDF build because ReportLab treats it as an actual <img> tag missing
    # its src). Escape every dynamic value; the &nbsp;/&bull;/<b> markup below
    # is ours, not user data, so it stays unescaped.
    meta = (
        f"{escape(f.category.value.upper())} &nbsp;&bull;&nbsp; "
        f"{escape(location)} &nbsp;&bull;&nbsp; via {escape(f.source_tool)}"
    )

    marker = Table([[""]], colWidths=[0.12 * inch], rowHeights=[0.12 * inch])
    marker.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), BUCKET_COLOR[bucket])]))

    content = [
        Paragraph(f"[{escape(f.id)}] {escape(f.title)}", _FINDING_TITLE),
        Paragraph(meta, _FINDING_META),
        Paragraph(escape(f.detail or ""), _FINDING_DETAIL),
    ]
    if f.resolution:
        content.append(Paragraph(f"<b>Resolution:</b> {escape(f.resolution)}", _FINDING_RESOLUTION))

    row = Table([[marker, content]], colWidths=[0.25 * inch, 6.35 * inch])
    row.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ]
        )
    )
    return row


def generate_pdf_report(report: AuditReport, repo_path: str, output_path: str | None = None) -> str:
    repo_name = Path(repo_path).name or "repo"

    if output_path is None:
        out_dir = Path(config.REPORTS_DIR)
        out_dir.mkdir(parents=True, exist_ok=True)
        output_path = str(out_dir / f"audit_report_{repo_name}.pdf")

    doc = SimpleDocTemplate(
        output_path, pagesize=LETTER,
        topMargin=0.7 * inch, bottomMargin=0.7 * inch,
        leftMargin=0.7 * inch, rightMargin=0.7 * inch,
    )

    story = [
        Paragraph("ZweiGeeks Audit Report", _TITLE),
        Paragraph(
            f"{escape(repo_name)} &nbsp;&bull;&nbsp; {escape(repo_path)} &nbsp;&bull;&nbsp; "
            f"generated {dt.datetime.now().strftime('%Y-%m-%d %H:%M')}",
            _SUBTITLE,
        ),
    ]

    buckets: dict[str, list[Finding]] = {"high": [], "medium": [], "low": []}
    for f in report.findings:
        buckets[f.report_bucket()].append(f)

    story.append(
        Paragraph(
            f"<b>{len(buckets['high'])}</b> high &nbsp;&bull;&nbsp; "
            f"<b>{len(buckets['medium'])}</b> medium &nbsp;&bull;&nbsp; "
            f"<b>{len(buckets['low'])}</b> low &nbsp;&bull;&nbsp; "
            f"<b>{len(report.findings)}</b> total findings",
            _SUMMARY,
        )
    )

    if report.audit_incomplete:
        cause = _INCOMPLETE_CAUSES.get(
            report.incomplete_reason or "", "the local LLM backend failed partway through"
        )
        story.append(
            Paragraph(
                f"&#9888; The automated code review did not finish - {cause}. "
                "Findings below may come only from deterministic checks and cover "
                "only part of the code; re-run once the LLM backend is working for "
                "a complete review. The absence of a finding here is not evidence "
                "that the code is clean.",
                _WARNING,
            )
        )

    if not report.findings:
        # An aborted review with nothing found reviewed nothing - never call that clean.
        text = "No findings recorded." if report.audit_incomplete else "No findings. Clean run."
        story.append(Paragraph(text, _styles["Normal"]))

    for bucket in BUCKET_ORDER:
        items = buckets[bucket]
        if not items:
            continue
        story.append(_bucket_header(bucket, len(items)))
        story.append(Spacer(1, 8))
        for f in items:
            story.append(_finding_block(f, bucket))
        story.append(Spacer(1, 6))

    if report.improvements:
        story.append(Paragraph("Improvements", _SECTION_HEADER))
        story.append(
            ListFlowable(
                [ListItem(Paragraph(escape(item), _IMPROVEMENT_ITEM)) for item in report.improvements],
                bulletType="bullet",
                leftIndent=14,
            )
        )

    doc.build(story)
    return output_path