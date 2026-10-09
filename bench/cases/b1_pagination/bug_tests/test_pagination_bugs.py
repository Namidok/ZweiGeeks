import pagination


def test_total_pages_counts_the_last_partial_page():
    assert pagination.total_pages(10, 3) == 4


def test_first_page_starts_at_the_first_item():
    assert pagination.paginate(list(range(10)), 1, 3).items == [0, 1, 2]


def test_token_expires_after_its_ttl():
    token = pagination.issue_token(1, ttl_seconds=10, now=100.0)
    assert pagination.is_expired(token, now=111.0) is True
    assert pagination.is_expired(token, now=105.0) is False
