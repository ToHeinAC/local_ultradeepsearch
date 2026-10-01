from app.pipeline.links import MAX_LINKS, page_links

BASE = "https://example.org/dir/page.html"


def test_relative_links_become_absolute_and_fragments_are_dropped() -> None:
    html = '<a href="/a">1</a><a href="b">2</a><a href="../c#frag">3</a><a href="https://other.org/x#y">4</a>'
    assert page_links(html, BASE) == (
        "https://example.org/a",
        "https://example.org/dir/b",
        "https://example.org/c",
        "https://other.org/x",
    )


def test_only_http_and_https_pages_are_kept() -> None:
    html = (
        '<a href="mailto:a@b.c">m</a><a href="javascript:void(0)">j</a><a href="tel:+49">t</a>'
        '<a href="ftp://x.org/f">f</a><a href="#top">anchor</a><a href="">empty</a><a>no href</a>'
        '<a href="http://ok.org/">ok</a>'
    )
    assert page_links(html, BASE) == ("http://ok.org/",)


def test_duplicates_are_removed_keeping_the_first_position() -> None:
    html = '<a href="/a">1</a><a href="/b">2</a><a href="/a#x">3</a><a href="/b">4</a>'
    assert page_links(html, BASE) == ("https://example.org/a", "https://example.org/b")


def test_a_base_tag_changes_the_resolution() -> None:
    html = '<head><base href="https://cdn.example.net/docs/"></head><a href="intro.html">i</a>'
    assert page_links(html, BASE) == ("https://cdn.example.net/docs/intro.html",)


def test_the_number_of_links_is_capped() -> None:
    html = "".join(f'<a href="/p{i}">x</a>' for i in range(MAX_LINKS + 50))
    links = page_links(html, BASE)
    assert len(links) == MAX_LINKS
    assert links[0] == "https://example.org/p0"


def test_malformed_and_empty_html_do_not_crash() -> None:
    assert page_links("", BASE) == ()
    assert page_links("<a href='/x'><div><a href=\"/y\"", BASE)[0] == "https://example.org/x"
    assert page_links("plain text, no markup", BASE) == ()


def test_entities_in_urls_are_decoded() -> None:
    assert page_links('<a href="/s?a=1&amp;b=2">x</a>', BASE) == ("https://example.org/s?a=1&b=2",)
