"""URL canonicalisation for exact deduplication of sources."""

from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

_TRACKING_KEYS = frozenset({"fbclid", "gclid", "mc_cid", "mc_eid", "_hsenc", "_hsmi"})
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _tracking(key: str) -> bool:
    return key.startswith("utm_") or key in _TRACKING_KEYS


def canonicalize(url: str) -> str:
    """Lowercase scheme and host, drop default port, credentials, fragment and tracking
    parameters, sort the query, strip a trailing slash. Input that is no URL is returned trimmed."""
    raw = url.strip()
    try:
        parts = urlsplit(raw)
        host = (parts.hostname or "").rstrip(".").lower()
        port = parts.port
    except ValueError:
        return raw
    if not parts.scheme or not host:
        return raw
    scheme = parts.scheme.lower()
    shown = f"[{host}]" if ":" in host else host
    netloc = shown if port is None or port == _DEFAULT_PORTS.get(scheme) else f"{shown}:{port}"
    path = parts.path.rstrip("/") or "/"
    pairs = sorted(
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _tracking(k)
    )
    query = urlencode(pairs, quote_via=quote)
    return urlunsplit((scheme, netloc, path, query, ""))


def dedup_key(url: str) -> str:
    """`canonicalize` plus one leading `www.` removed, so www and bare hosts are one source."""
    canonical = canonicalize(url)
    parts = urlsplit(canonical)
    host = parts.netloc
    if host.startswith("www."):
        return urlunsplit((parts.scheme, host.removeprefix("www."), parts.path, parts.query, ""))
    return canonical
