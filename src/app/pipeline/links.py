"""Outgoing links of an HTML page, kept so later steps can chase leads without a refetch."""

from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin

MAX_LINKS = 300


class _Collector(HTMLParser):
    def __init__(self, page_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = page_url
        self._base_seen = False
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        href = dict(attrs).get("href")
        if tag == "base" and href and not self._base_seen:
            self._base_seen = True
            self.base = urljoin(self.base, href)
        elif tag == "a" and href:
            self.hrefs.append(href)


def page_links(html: str, base_url: str) -> tuple[str, ...]:
    """Absolute http(s) links in document order, without fragments or duplicates (≤ MAX_LINKS)."""
    collector = _Collector(base_url)
    collector.feed(html)
    collector.close()
    found: dict[str, None] = {}
    for href in collector.hrefs:
        target = href.strip()
        if not target or target.startswith("#"):
            continue
        absolute = urldefrag(urljoin(collector.base, target)).url
        if absolute.startswith(("http://", "https://")):
            found.setdefault(absolute)
        if len(found) >= MAX_LINKS:
            break
    return tuple(found)
