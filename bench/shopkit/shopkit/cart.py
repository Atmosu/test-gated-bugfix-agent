"""Shopping cart."""


class Cart:
    def __init__(self, items=None):
        # items maps product name -> (unit_price, quantity)
        self.items = dict(items) if items else {}

    def add(self, name, unit_price, quantity=1):
        _, current = self.items.get(name, (unit_price, 0))
        self.items[name] = (unit_price, current + quantity)

    def remove(self, name):
        self.items.pop(name, None)

    def total(self):
        return round(sum(price * qty for price, qty in self.items.values()), 2)

    def count(self):
        return sum(qty for _, qty in self.items.values())
