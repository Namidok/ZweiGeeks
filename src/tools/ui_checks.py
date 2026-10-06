"""Lightweight static heuristics over HTML templates.

Not a Lighthouse replacement - just enough to surface obvious accessibility
and responsiveness misses without needing to render a live page.
"""

from __future__ import annotations

import re
from pathlib import Path

IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
ALT_ATTR_RE = re.compile(r'alt\s*=\s*"[^"]*"', re.IGNORECASE)
VIEWPORT_RE = re.compile(r'<meta[^>]*name=["\']viewport["\']', re.IGNORECASE)
INLINE_STYLE_RE = re.compile(r'style\s*=\s*"', re.IGNORECASE)


def scan_templates(repo_path: str) -> list[dict]:
    findings = []
    # Plain .html files aren't the only place UI markup lives - a PHP (or
    # similar server-rendered) app usually embeds HTML directly inside its
    # .php files, so a scan that only looked at *.html would silently see
    # zero templates and report a false-clean "0 findings" on those repos.
    html_files = list(Path(repo_path).rglob("*.html")) + list(Path(repo_path).rglob("*.php"))
    for html_file in html_files:
        if any(part in (".git", "vendor", "venv", ".venv", "node_modules") for part in html_file.parts):
            continue
        try:
            text = html_file.read_text(errors="ignore")
        except OSError:
            continue
        rel = str(html_file.relative_to(repo_path))

        for img in IMG_TAG_RE.finditer(text):
            if not ALT_ATTR_RE.search(img.group(0)):
                line = text[: img.start()].count("\n") + 1
                findings.append(
                    {
                        "file": rel,
                        "line": line,
                        "issue": "img_missing_alt",
                        "detail": "<img> tag has no alt attribute (accessibility).",
                    }
                )

        inline_count = len(INLINE_STYLE_RE.findall(text))
        if inline_count > 5:
            findings.append(
                {
                    "file": rel,
                    "line": None,
                    "issue": "excess_inline_styles",
                    "detail": f"{inline_count} inline style= attributes found; consider a stylesheet.",
                }
            )

    # Viewport meta tag only matters once per "page-like" template.
    PAGE_LIKE_HINTS = ("index", "base", "layout", "header", "master")
    top_level_html = [
        f for f in html_files if any(hint in f.name.lower() for hint in PAGE_LIKE_HINTS)
    ]
    for html_file in top_level_html:
        try:
            text = html_file.read_text(errors="ignore")
        except OSError:
            continue
        if not VIEWPORT_RE.search(text):
            findings.append(
                {
                    "file": str(html_file.relative_to(repo_path)),
                    "line": None,
                    "issue": "missing_viewport_meta",
                    "detail": "No <meta name=\"viewport\"> tag found; page likely isn't mobile responsive.",
                }
            )
    return findings