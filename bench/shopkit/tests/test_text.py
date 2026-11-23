from shopkit.text import slugify, truncate


def test_slugify():
    assert slugify("Red Shoes") == "red-shoes"


def test_truncate_short_text_unchanged():
    assert truncate("hat", 10) == "hat"
