from shopkit.cart import Cart


def test_add_and_total():
    cart = Cart()
    cart.add("pen", 1.5)
    cart.add("book", 10.0)
    assert cart.total() == 11.5
    assert cart.count() == 2


def test_remove():
    cart = Cart()
    cart.add("pen", 1.5)
    cart.remove("pen")
    assert "pen" not in cart.items
