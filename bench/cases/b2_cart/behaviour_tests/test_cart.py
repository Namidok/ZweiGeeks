"""Every cart here gets its own list: the default-argument bug would
otherwise make these tests depend on their order."""

import pytest

import cart


def test_add_accumulates_quantities():
    c = cart.Cart([])
    c.add("apple", 0.5, 2)
    c.add("apple", 0.5, 3)
    c.add("pear", 0.75)
    assert c.skus() == ["apple", "pear"]
    assert c.item_count() == 6
    assert c.subtotal() == 3.25


def test_add_rejects_non_positive_quantities():
    with pytest.raises(ValueError):
        cart.Cart([]).add("apple", 0.5, 0)


def test_partial_remove_keeps_the_line():
    c = cart.Cart([])
    c.add("apple", 0.5, 3)
    c.remove("apple", 1)
    assert c.skus() == ["apple"] and c.item_count() == 2


def test_remove_unknown_sku_raises():
    with pytest.raises(KeyError):
        cart.Cart([]).remove("plum")


def test_known_discount_codes():
    c = cart.Cart([])
    c.add("book", 20.0, 5)
    assert c.total("SAVE10") == 90.0
    assert c.total("welcome15") == 85.0
    assert c.total() == 100.0
    assert cart.apply_discount(50.0, None) == 50.0
