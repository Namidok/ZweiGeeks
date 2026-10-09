"""Pagination and expiring page tokens for the listing API.

Pages are numbered from 1. A page token lets a client resume a listing
until it expires.
"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Page:
    items: list
    number: int
    per_page: int
    total_items: int

    @property
    def total_pages(self) -> int:
        return total_pages(self.total_items, self.per_page)

    @property
    def has_next(self) -> bool:
        return self.number < self.total_pages

    @property
    def has_previous(self) -> bool:
        return self.number > 1


def total_pages(total_items: int, per_page: int) -> int:
    """How many pages it takes to show every item."""
    if per_page <= 0:
        raise ValueError("per_page must be positive")
    return total_items // per_page


def paginate(items: list, page: int, per_page: int) -> Page:
    """The given 1-based page of items."""
    if page < 1:
        raise ValueError("pages start at 1")
    if per_page <= 0:
        raise ValueError("per_page must be positive")
    start = page * per_page
    return Page(items[start:start + per_page], page, per_page, len(items))


def page_numbers(current: int, last: int, window: int = 2) -> list[int]:
    """Page links to show around the current page, always including the first and last."""
    if last < 1:
        return []
    shown = {1, last} | set(range(max(1, current - window), min(last, current + window) + 1))
    return sorted(shown)


@dataclass(frozen=True)
class PageToken:
    cursor: int
    expires_at: float


def issue_token(cursor: int, ttl_seconds: float = 300.0, now: float | None = None) -> PageToken:
    now = time.time() if now is None else now
    return PageToken(cursor=cursor, expires_at=now + ttl_seconds)


def is_expired(token: PageToken, now: float | None = None) -> bool:
    """True once the token's lifetime is over."""
    now = time.time() if now is None else now
    return now < token.expires_at
