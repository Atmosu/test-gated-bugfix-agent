"""Parse shop settings strings such as 'currency=EUR;tax=0.2;debug=yes'."""

TRUE_WORDS = {"1", "true", "yes", "on"}


def parse_bool(value):
    return value.strip().lower() in TRUE_WORDS


def parse_settings(text):
    """'a=1;b=2' -> {'a': '1', 'b': '2'}. Empty parts are ignored."""
    result = {}
    for part in text.split(";"):
        part = part.strip()
        if not part:
            continue
        key, value = part.split("=", 1)
        result[key.strip()] = value.strip()
    return result
