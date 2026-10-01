"""Junk gates (PRD M3): pages that carry no usable source text."""

import unicodedata

TOO_SHORT_CHARS = 300
LOGIN_WALL_CHARS = 1000
COOKIE_WALL_CHARS = 1500
GARBAGE_WINDOW = 2000
GARBAGE_RATIO = 0.05

LOGIN_MARKERS = (
    "sign in",
    "log in",
    "login",
    "anmelden",
    "einloggen",
    "password",
    "passwort",
    "create account",
    "konto erstellen",
    "registrieren",
    "subscribe to read",
    "jetzt abonnieren",
)
COOKIE_MARKERS = (
    "cookie",
    "accept all",
    "alle akzeptieren",
    "einwilligung",
    "consent",
    "datenschutzeinstellungen",
    "privacy settings",
)


def _is_garbage(char: str) -> bool:
    if char == "�":
        return True
    category = unicodedata.category(char)
    return category == "Co" or (category == "Cc" and char not in "\n\r\t")


def _garbage_ratio(text: str) -> float:
    window = text[:GARBAGE_WINDOW]
    return sum(1 for c in window if _is_garbage(c)) / len(window) if window else 0.0


def junk_reason(text: str) -> str | None:
    """Why ``text`` is not a usable source, or None. A scanned PDF has no text: `too_short`."""
    body = text.strip()
    if len(body) < TOO_SHORT_CHARS:
        return "too_short"
    if _garbage_ratio(body) > GARBAGE_RATIO:
        return "binary_garbage"
    lowered = body.casefold()
    if len(body) < LOGIN_WALL_CHARS and any(m in lowered for m in LOGIN_MARKERS):
        return "login_wall"
    if len(body) < COOKIE_WALL_CHARS and any(m in lowered for m in COOKIE_MARKERS):
        return "cookie_wall"
    return None
