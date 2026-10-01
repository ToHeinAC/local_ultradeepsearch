import pytest

from app.pipeline.junk import junk_reason

SENTENCE = "The commission reviewed the application for the research reactor in detail. "


def prose(chars: int) -> str:
    return (SENTENCE * (chars // len(SENTENCE) + 1))[:chars]


def test_ordinary_text_passes() -> None:
    assert junk_reason(prose(300)) is None
    assert junk_reason(prose(5000)) is None


@pytest.mark.parametrize(
    ("chars", "reason"), [(0, "too_short"), (100, "too_short"), (299, "too_short"), (300, None)]
)
def test_too_short_threshold(chars: int, reason: str | None) -> None:
    assert junk_reason(prose(chars)) == reason


def test_whitespace_does_not_count_towards_the_length() -> None:
    assert junk_reason("  \n\t " + prose(250) + "   \n\n  ") == "too_short"


@pytest.mark.parametrize(
    "marker",
    [
        "Sign in",
        "log in to continue",
        "LOGIN",
        "Bitte anmelden",
        "Jetzt einloggen",
        "Passwort vergessen?",
        "Create account",
        "Subscribe to read",
    ],
)
def test_login_walls(marker: str) -> None:
    assert junk_reason(prose(500) + marker) == "login_wall"


@pytest.mark.parametrize(
    "marker",
    [
        "We use cookies",
        "Accept all",
        "Alle akzeptieren",
        "Datenschutzeinstellungen",
        "Privacy settings",
        "Einwilligung",
    ],
)
def test_cookie_walls(marker: str) -> None:
    assert junk_reason(prose(900) + marker) == "cookie_wall"


def test_wall_length_boundaries() -> None:
    assert junk_reason(prose(999 - 7) + "sign in") == "login_wall"  # 999 chars
    assert junk_reason(prose(1000 - 7) + "sign in") is None  # exactly 1000 is no longer a wall
    assert junk_reason(prose(1499 - 7) + "cookies") == "cookie_wall"
    assert junk_reason(prose(1500 - 7) + "cookies") is None


def test_login_is_reported_before_cookie() -> None:
    assert junk_reason(prose(600) + "Sign in. We use cookies.") == "login_wall"


def test_a_long_article_that_mentions_cookies_or_login_is_not_a_wall() -> None:
    article = prose(4000) + " The login page and the cookie banner were redesigned."
    assert junk_reason(article) is None


def garbage(percent: float, total: int = 2000, char: str = "�") -> str:
    bad = round(total * percent / 100)
    return "a" + (" word" * 600)[: total - bad - 1] + char * bad


@pytest.mark.parametrize("char", ["�", "\x00", "\x07", ""])
def test_binary_garbage_over_five_percent(char: str) -> None:
    assert junk_reason(garbage(6, char=char)) == "binary_garbage"


def test_five_percent_exactly_is_still_text() -> None:
    assert junk_reason(garbage(5)) is None


def test_garbage_is_measured_on_the_first_2000_characters_only() -> None:
    assert junk_reason(garbage(0) + "�" * 500) is None


def test_ordinary_control_whitespace_is_not_garbage() -> None:
    assert junk_reason(prose(1990) + "\n\r\t" * 100) is None


def test_short_garbage_is_reported_as_too_short() -> None:
    assert junk_reason("�" * 100) == "too_short"
