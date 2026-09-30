"""Example domain logic: pure, typed, small. Replace with your own."""

import re

_WORD = re.compile(r"[^\W_]+")  # Unicode letters/digits, no underscore


def slugify(text: str) -> str:
    """Lowercase ``text`` and join its alphanumeric words with single hyphens."""
    return "-".join(_WORD.findall(text.lower()))
