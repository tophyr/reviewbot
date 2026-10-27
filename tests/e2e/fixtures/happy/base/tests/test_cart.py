from shop.cart import line_total


def test_line_total():
    assert line_total({"price": 250, "quantity": 3}) == 750
