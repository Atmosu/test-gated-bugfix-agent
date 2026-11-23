"""Text helpers for product pages."""
import re


def slugify(title):
    """URL slug: lowercase words joined by single hyphens, e.g. 'Red  Shoes!' -> 'red-shoes'."""
    words = re.findall(r"[a-z0-9]+", title.lower())
    return "-".join(words)


def truncate(text, limit):
    """Shorten text to at most `limit` characters, ending with '...' when cut."""
    if len(text) <= limit:
        return text
    return text[: max(limit - 3, 0)] + "..."
