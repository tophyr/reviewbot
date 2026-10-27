import pytest

from shop.cart import apply_discount, line_total


def test_line_total():
    assert line_total({"price": 250, "quantity": 3}) == 750


def test_apply_discount():
    assert apply_discount(1000, 10) == 900
    assert apply_discount(1000, 0) == 1000


def test_apply_discount_rejects_bad_percent():
    with pytest.raises(ValueError):
        apply_discount(1000, 101)
