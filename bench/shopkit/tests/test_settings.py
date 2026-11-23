from shopkit.settings import parse_bool, parse_settings


def test_parse_settings():
    assert parse_settings("currency=EUR; tax=0.2") == {"currency": "EUR", "tax": "0.2"}


def test_parse_bool():
    assert parse_bool("yes") and not parse_bool("no")
