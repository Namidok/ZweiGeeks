import pytest

import pagination


def test_total_pages_on_exact_multiples_and_empty_lists():
    assert pagination.total_pages(9, 3) == 3
    assert pagination.total_pages(0, 5) == 0
    with pytest.raises(ValueError):
        pagination.total_pages(10, 0)


def test_paginate_validates_its_arguments():
    with pytest.raises(ValueError):
        pagination.paginate([1, 2, 3], 0, 2)
    with pytest.raises(ValueError):
        pagination.paginate([1, 2, 3], 1, 0)


def test_paginate_returns_at_most_one_page_of_items():
    page = pagination.paginate(list(range(20)), 2, 5)
    assert page.number == 2 and page.per_page == 5 and page.total_items == 20
    assert len(page.items) == 5


def test_page_navigation_flags():
    assert pagination.Page([], 1, 3, 9).has_next
    assert not pagination.Page([], 3, 3, 9).has_next
    assert pagination.Page([], 2, 3, 9).has_previous
    assert not pagination.Page([], 1, 3, 9).has_previous


def test_page_numbers_window():
    assert pagination.page_numbers(5, 10) == [1, 3, 4, 5, 6, 7, 10]
    assert pagination.page_numbers(1, 1) == [1]
    assert pagination.page_numbers(1, 0) == []


def test_issue_token_sets_the_expiry():
    token = pagination.issue_token(42, ttl_seconds=60, now=1000.0)
    assert token == pagination.PageToken(cursor=42, expires_at=1060.0)
