"""Price calculations. All amounts are in the shop currency and rounded to cents."""

BULK_THRESHOLD = 10
BULK_RATE = 0.9


def apply_discount(price, percent):
    """Price after a percentage discount, e.g. apply_discount(80, 25) == 60.0."""
    if not 0 <= percent <= 100:
        raise ValueError("percent must be between 0 and 100")
    return round(price * (1 - percent / 100), 2)


def bulk_price(quantity, unit_price):
    """Total for an order line. Orders of BULK_THRESHOLD units or more get the bulk rate."""
    total = quantity * unit_price
    if quantity >= BULK_THRESHOLD:
        total *= BULK_RATE
    return round(total, 2)
