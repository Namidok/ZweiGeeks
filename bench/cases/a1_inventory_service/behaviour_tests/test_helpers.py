import helpers


def test_describe_none_returns_the_label():
    assert helpers.describe(None) == "item"


def test_describe_serialises_items():
    assert helpers.describe({"sku": "A1", "qty": 2}) == '{"sku": "A1", "qty": 2}'
