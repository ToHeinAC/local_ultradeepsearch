import pytest

from app.pipeline.urls import canonicalize, dedup_key


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "HTTPS://Example.COM:443/a/b/?utm_source=x&b=2&a=1#frag",
            "https://example.com/a/b?a=1&b=2",
        ),
        ("http://example.com:80", "http://example.com/"),
        ("http://example.com:8080/x/", "http://example.com:8080/x"),
        ("https://example.com/", "https://example.com/"),
        ("https://example.com?x=1", "https://example.com/?x=1"),
        (
            "https://example.com/p?utm_medium=a&utm_campaign=b&fbclid=1&gclid=2&mc_cid=3&mc_eid=4",
            "https://example.com/p",
        ),
        ("https://example.com/p?_hsenc=1&_hsmi=2&id=5", "https://example.com/p?id=5"),
        ("https://example.com/p?b=&a=1", "https://example.com/p?a=1&b="),
        ("https://example.com/p?z=1&z=0", "https://example.com/p?z=0&z=1"),
        (
            "https://user:secret@example.com/x",
            "https://example.com/x",
        ),  # credentials are never stored
        ("http://[::1]:8080/a", "http://[::1]:8080/a"),
        ("https://example.com./a", "https://example.com/a"),
        ("  https://example.com/a  ", "https://example.com/a"),
        ("https://example.com/a%20b", "https://example.com/a%20b"),
    ],
)
def test_canonicalize(raw: str, expected: str) -> None:
    assert canonicalize(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "HTTPS://Example.COM:443/a/b/?utm_source=x&b=2&a=1#frag",
        "http://example.com:80",
        "https://user:secret@example.com/x?q=1",
        "http://[::1]:8080/a",
    ],
)
def test_canonicalize_is_idempotent(raw: str) -> None:
    once = canonicalize(raw)
    assert canonicalize(once) == once


@pytest.mark.parametrize(
    "raw", ["http://[::1", "", "not a url at all", "mailto:someone@example.com"]
)
def test_unparseable_input_never_raises(raw: str) -> None:
    assert isinstance(canonicalize(raw), str)


def test_dedup_key_ignores_www_and_everything_canonicalize_ignores() -> None:
    assert dedup_key("https://www.Example.com/a/") == dedup_key(
        "https://example.com/a?utm_source=x"
    )
    assert dedup_key("https://www.example.com/a") == "https://example.com/a"


def test_dedup_key_only_strips_a_leading_www_label() -> None:
    assert dedup_key("https://www2.example.com/a") == "https://www2.example.com/a"
    assert dedup_key("https://example.org/www.page") == "https://example.org/www.page"
    assert dedup_key("https://www.www.example.com/a") == "https://www.example.com/a"


def test_different_pages_stay_different() -> None:
    assert dedup_key("https://example.com/a?id=1") != dedup_key("https://example.com/a?id=2")
    assert dedup_key("https://example.com/a") != dedup_key("https://example.com/b")
