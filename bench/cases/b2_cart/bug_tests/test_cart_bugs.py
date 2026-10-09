import cart


def test_new_carts_do_not_share_lines():
    first = cart.Cart()
    first.add("apple", 0.5)
    assert cart.Cart().skus() == []


def test_unknown_discount_code_gives_no_discount():
    assert cart.apply_discount(100.0, "BOGUS") == 100.0


def test_removing_every_unit_drops_the_line():
    c = cart.Cart([])
    c.add("apple", 0.5, 2)
    c.remove("apple", 2)
    assert "apple" not in c.skus()
