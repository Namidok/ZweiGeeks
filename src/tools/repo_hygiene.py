"""Deterministic, language-agnostic repo hygiene checks. No LLM involved -
these are the highest-confidence findings ZweiGeeks produces, because
they're just git + filesystem facts, not a model's opinion.

Covers a pattern common in real-world repos: secrets committed to git despite
a .gitignore rule (added after the fact, or force-added), stray backup
files, debug/test scripts left in the deployable tree, and broken
require/include targets that mean the app can't run.
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
from pathlib import Path

SECRET_NAME_PATTERNS = [
    ".env", ".env.*", "*.pem", "*.key", "*credentials*", "*secret*",
    "*.p12", "*.pfx", "id_rsa", "id_rsa.pub",
]
STRAY_NAME_PATTERNS = [
    "*.backup", "*.bak", "*.orig", "*~", "New Text Document*", "Untitled*",
    ".DS_Store",
]
# Throwaway scripts that end up web-reachable when the repo root is the
# docroot. Patterns are anchored on purpose: "test_*.php", not "test*.php",
# which would also match testimonials.php.
DEBUG_SCRIPT_PATTERNS = ["debug.php", "debug_*.php", "debug-*.php", "*_debug.php", "phpinfo*.php"]
TEST_SCRIPT_PATTERNS = [
    "test.php", "test_*.php", "test-*.php", "*_test.php", "example_*.php", "*_example.php",
]
# Real test suites live here and aren't served - don't flag them.
NON_SERVED_DIRS = {"tests", "test", "spec", "__tests__", "vendor", "node_modules"}
PHPINFO_RE = re.compile(r"\bphpinfo\s*\(", re.IGNORECASE)
REQUIRE_RE = re.compile(
    r"""(?:require|include)(_once)?\s*\(?\s*(__DIR__\s*\.\s*)?['"]([^'"]+)['"]""", re.IGNORECASE
)


def _tracked_files(repo_path: str) -> list[str]:
    try:
        out = subprocess.run(
            ["git", "ls-files"], cwd=repo_path, capture_output=True, text=True, timeout=30
        )
        return [line for line in out.stdout.splitlines() if line.strip()]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []


def _gitignore_patterns(repo_path: str) -> list[str]:
    gi = Path(repo_path) / ".gitignore"
    if not gi.exists():
        return []
    patterns = []
    for line in gi.read_text(errors="ignore").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            patterns.append(line.lstrip("/"))
    return patterns


def find_tracked_secrets(repo_path: str) -> list[dict]:
    tracked = _tracked_files(repo_path)
    ignore_patterns = _gitignore_patterns(repo_path)
    findings = []
    for f in tracked:
        name = Path(f).name
        looks_secret = any(
            fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(f, pat) for pat in SECRET_NAME_PATTERNS
        )
        if not looks_secret:
            continue
        also_ignored = any(
            fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(f, pat) for pat in ignore_patterns
        )
        detail = f"'{f}' looks like a secrets/credentials file and is committed to git."
        if also_ignored:
            detail += (
                " It also matches a .gitignore rule, meaning it was likely committed "
                "before that rule existed (or force-added) - it's still live in history."
            )
        findings.append({"file": f, "also_gitignored": also_ignored, "detail": detail})
    return findings


def find_stray_files(repo_path: str) -> list[dict]:
    tracked = _tracked_files(repo_path)
    findings = []
    for f in tracked:
        name = Path(f).name
        if any(fnmatch.fnmatch(name, pat) for pat in STRAY_NAME_PATTERNS):
            findings.append(
                {"file": f, "detail": f"'{f}' looks like a stray/backup/scratch file committed to the repo."}
            )
    return findings


def find_debug_scripts(repo_path: str) -> list[dict]:
    """Flags tracked debug/test/example PHP scripts outside real test dirs,
    plus any PHP file calling phpinfo(). These typically dump config,
    session, or environment details to whoever requests them, and the right
    fix is deleting the file - not an LLM patching around it - so they're
    reported, never routed to the Fixer.
    """
    root = Path(repo_path)
    findings = []
    for f in _tracked_files(repo_path):
        path = Path(f)
        name = path.name.lower()
        if not name.endswith(".php"):
            continue
        if any(part.lower() in NON_SERVED_DIRS for part in path.parts[:-1]):
            continue
        try:
            calls_phpinfo = bool(PHPINFO_RE.search((root / f).read_text(errors="ignore")))
        except OSError:
            calls_phpinfo = False
        if calls_phpinfo or any(fnmatch.fnmatchcase(name, pat) for pat in DEBUG_SCRIPT_PATTERNS):
            kind = "debug"
        elif any(fnmatch.fnmatchcase(name, pat) for pat in TEST_SCRIPT_PATTERNS):
            kind = "test"
        else:
            continue
        detail = f"'{f}' looks like a {kind} script committed alongside the app"
        detail += (
            " and calls phpinfo(), which dumps the full server configuration."
            if calls_phpinfo else
            ". If the repo is deployed as the web root it's reachable by anyone and "
            "typically exposes config, session, or environment details."
        )
        findings.append({"file": f, "kind": kind, "phpinfo": calls_phpinfo, "detail": detail})
    return findings


def find_broken_php_includes(repo_path: str) -> list[dict]:
    """Flags require/require_once/include/include_once targets that don't
    exist anywhere in the repo - usually means the app fatal-errors on the
    very first request.

    Grouped by missing target rather than one finding per referencing file:
    a single missing shared include (e.g. includes/user_nav.php) is often
    pulled in from a dozen call sites, and reporting each call site as its
    own finding just drowns out everything else without adding information
    - the fix is the same regardless of how many places reference it.
    """
    root = Path(repo_path).resolve()
    occurrences: dict[str, list[dict]] = {}
    for php_file in root.rglob("*.php"):
        if any(part in (".git", "vendor", "node_modules") for part in php_file.parts):
            continue
        try:
            text = php_file.read_text(errors="ignore")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines()):
            m = REQUIRE_RE.search(line)
            if not m:
                continue
            rel = m.group(3)
            if rel.startswith(("http://", "https://")):
                continue
            candidate = (php_file.parent / rel).resolve()
            alt = (root / rel.lstrip("./")).resolve()
            if candidate.exists() or alt.exists():
                continue
            occurrences.setdefault(rel, []).append(
                {"file": str(php_file.relative_to(root)), "line": i + 1}
            )

    findings = []
    for rel, locations in occurrences.items():
        first, others = locations[0], locations[1:]
        detail = f"require/include target '{rel}' does not exist anywhere in the repo."
        if others:
            shown = ", ".join(f"{o['file']}:{o['line']}" for o in others[:8])
            more = f" (+{len(others) - 8} more)" if len(others) > 8 else ""
            detail += (
                f" Referenced from {len(locations)} locations total - also "
                f"missing from {shown}{more}."
            )
        findings.append({"file": first["file"], "line": first["line"], "detail": detail})
    return findings