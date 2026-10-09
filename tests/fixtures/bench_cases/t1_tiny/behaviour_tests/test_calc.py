import calc


def test_average_of_nothing_is_zero():
    assert calc.average([]) == 0.0
