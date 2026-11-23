import pytest

from shopkit.pricing import apply_discount, bulk_price


def test_discount():
    assert apply_discount(80, 25) == 60.0
    assert apply_discount(19.99, 0) == 19.99


def test_discount_out_of_range():
    with pytest.raises(ValueError):
        apply_discount(10, 150)


def test_bulk_small_order():
    assert bulk_price(2, 5.0) == 10.0


def test_bulk_large_order():
    assert bulk_price(20, 1.0) == 18.0
