"""Ten planted bugs for the evaluation. Each one turns correct code in bench/shopkit into buggy code
with a single replacement. The visible tests still pass with the bug; the hidden test (never shown
to the agent) fails with the bug and passes on the correct code."""

BUGS = [
    {
        "id": "leap-century", "kind": "wrong rule", "file": "shopkit/dates.py",
        "old": "return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)",
        "new": "return year % 4 == 0",
        "issue": "is_leap_year(1900) returns True, but 1900 was not a leap year. Delivery dates in "
                 "February 1900-style years come out a day off.",
        "hidden": "from shopkit.dates import is_leap_year\n\n\ndef test_hidden():\n"
                  "    assert not is_leap_year(1900)\n    assert not is_leap_year(2100)\n    assert is_leap_year(2000)\n",
    },
    {
        "id": "december", "kind": "off-by-one", "file": "shopkit/dates.py",
        "old": "if not 1 <= month <= 12:",
        "new": "if not 1 <= month < 12:",
        "issue": "days_in_month(2025, 12) raises ValueError: month must be 1 to 12. December should "
                 "return 31.",
        "hidden": "from shopkit.dates import days_in_month\n\n\ndef test_hidden():\n"
                  "    assert days_in_month(2025, 12) == 31\n    assert days_in_month(2025, 1) == 31\n",
    },
    {
        "id": "discount-int", "kind": "integer division", "file": "shopkit/pricing.py",
        "old": "return round(price * (1 - percent / 100), 2)",
        "new": "return round(price - price * percent // 100, 2)",
        "issue": "apply_discount(15, 10) returns 14 instead of 13.5. Discounts that do not come out "
                 "to a whole number are wrong.",
        "hidden": "from shopkit.pricing import apply_discount\n\n\ndef test_hidden():\n"
                  "    assert apply_discount(15, 10) == 13.5\n    assert apply_discount(9.5, 20) == 7.6\n",
    },
    {
        "id": "bulk-boundary", "kind": "wrong comparison", "file": "shopkit/pricing.py",
        "old": "if quantity >= BULK_THRESHOLD:",
        "new": "if quantity > BULK_THRESHOLD:",
        "issue": "Ordering exactly 10 units is charged the full price. The docs say orders of 10 units "
                 "or more get the bulk rate.",
        "hidden": "from shopkit.pricing import bulk_price\n\n\ndef test_hidden():\n"
                  "    assert bulk_price(10, 1.0) == 9.0\n    assert bulk_price(9, 1.0) == 9.0\n",
    },
    {
        "id": "slug-chars", "kind": "missing normalisation", "file": "shopkit/text.py",
        "old": 'words = re.findall(r"[a-z0-9]+", title.lower())\n    return "-".join(words)',
        "new": 'return title.lower().strip().replace(" ", "-")',
        "issue": 'slugify("Summer Sale: 50% off!") returns "summer-sale:-50%-off!". Slugs should only '
                 "contain lowercase letters, digits and single hyphens.",
        "hidden": "from shopkit.text import slugify\n\n\ndef test_hidden():\n"
                  '    assert slugify("Summer Sale: 50% off!") == "summer-sale-50-off"\n'
                  '    assert slugify("Red  Shoes") == "red-shoes"\n',
    },
    {
        "id": "truncate-length", "kind": "off-by-one", "file": "shopkit/text.py",
        "old": 'return text[: max(limit - 3, 0)] + "..."',
        "new": 'return text[:limit] + "..."',
        "issue": 'truncate("abcdefghij", 8) returns "abcdefgh..." which is 11 characters. The result '
                 "must be at most `limit` characters, including the '...'.",
        "hidden": "from shopkit.text import truncate\n\n\ndef test_hidden():\n"
                  '    assert truncate("abcdefghij", 8) == "abcde..."\n    assert len(truncate("x" * 50, 10)) == 10\n',
    },
    {
        "id": "cart-shared", "kind": "mutable default", "file": "shopkit/cart.py",
        "old": "def __init__(self, items=None):\n        # items maps product name -> (unit_price, quantity)\n"
               "        self.items = dict(items) if items else {}",
        "new": "def __init__(self, items={}):\n        # items maps product name -> (unit_price, quantity)\n"
               "        self.items = items",
        "issue": "Two customers' carts share their contents: after Cart().add('pen', 1.5), a brand-new "
                 "Cart() already contains the pen.",
        "hidden": "from shopkit.cart import Cart\n\n\ndef test_hidden():\n"
                  "    a = Cart()\n    a.add('pen', 1.5)\n    b = Cart()\n    assert b.items == {}\n"
                  "    assert b.total() == 0\n",
    },
    {
        "id": "cart-quantity", "kind": "ignored field", "file": "shopkit/cart.py",
        "old": "return round(sum(price * qty for price, qty in self.items.values()), 2)",
        "new": "return round(sum(price for price, qty in self.items.values()), 2)",
        "issue": "Cart.total() ignores quantities: adding 3 pens at 2.0 each gives a total of 2.0, "
                 "expected 6.0.",
        "hidden": "from shopkit.cart import Cart\n\n\ndef test_hidden():\n"
                  "    c = Cart()\n    c.add('pen', 2.0, 3)\n    c.add('pen', 2.0)\n    assert c.total() == 8.0\n",
    },
    {
        "id": "settings-equals", "kind": "parsing", "file": "shopkit/settings.py",
        "old": 'key, value = part.split("=", 1)',
        "new": 'key, value = part.split("=")',
        "issue": 'parse_settings("webhook=https://x.test/?a=1") raises "ValueError: too many values to '
                 "unpack\". Values may contain '='.",
        "hidden": "from shopkit.settings import parse_settings\n\n\ndef test_hidden():\n"
                  '    assert parse_settings("webhook=https://x.test/?a=1") == {"webhook": "https://x.test/?a=1"}\n',
    },
    {
        "id": "bool-case", "kind": "case sensitivity", "file": "shopkit/settings.py",
        "old": "return value.strip().lower() in TRUE_WORDS",
        "new": "return value.strip() in TRUE_WORDS",
        "issue": 'parse_bool("TRUE") and parse_bool("Yes") return False. Settings written in capitals '
                 "are ignored.",
        "hidden": "from shopkit.settings import parse_bool\n\n\ndef test_hidden():\n"
                  '    assert parse_bool("TRUE") and parse_bool("Yes") and parse_bool(" On ")\n'
                  '    assert not parse_bool("No")\n',
    },
]
