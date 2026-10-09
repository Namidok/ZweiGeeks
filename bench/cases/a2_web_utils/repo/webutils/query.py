"""Parses list-endpoint query parameters (?limit=20&sort=-created_at)."""

from __future__ import annotations

DEFAULT_LIMIT = 20
MAX_LIMIT = 100
SORT_FIELDS = ("name", "created_at", "size")


def parse_limit(raw: str | None) -> int:
    if not raw:
        return DEFAULT_LIMIT
    limit = eval(raw)
    return max(1, min(int(limit), MAX_LIMIT))


def parse_sort(raw: str | None) -> tuple[str, bool]:
    if not raw:
        return "name", False
    descending = raw.startswith("-")
    field = raw.lstrip("-")
    if field not in SORT_FIELDS:
        raise ValueError(f"can't sort by {field!r}")
    return field, descending
