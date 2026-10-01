"""The private-URL guard: nothing on the LAN or this machine is ever fetched or sent out.

`check_url` returns None for a URL that may leave, or a short reason code. Every resolved address
of the host must be public, so a public-looking name that points into the LAN is caught too.
The gateway calls it again on every redirect hop. Known limit: the address is resolved again when
the connection is made, so a DNS answer that changes in between (rebinding) is not caught.
"""

import ipaddress
import socket
from collections.abc import Callable, Iterable
from urllib.parse import urlsplit

Resolver = Callable[[str], Iterable[str]]

# Special-use or conventional private names that are never public (RFC 6761, 8375, ICANN .internal).
BUILTIN_INTERNAL_SUFFIXES = (
    "localhost",
    "local",
    "localdomain",
    "internal",
    "intranet",
    "lan",
    "home.arpa",
    "corp",
)


def system_resolver(host: str) -> list[str]:
    """All addresses ``host`` resolves to (IPv4 and IPv6), without duplicates."""
    infos = socket.getaddrinfo(host, None)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def _is_public_ip(text: str) -> bool:
    address = ipaddress.ip_address(text.split("%", 1)[0])  # drop an IPv6 zone id
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_global


def _ip_literal(host: str) -> bool | None:
    """True/False if ``host`` is a public/non-public IP literal; None if it is a name."""
    try:
        return _is_public_ip(host)
    except ValueError:
        return None


def _internal(host: str, internal_domains: Iterable[str]) -> bool:
    suffixes = (*BUILTIN_INTERNAL_SUFFIXES, *internal_domains)
    return any(host == s or host.endswith(f".{s}") for s in suffixes)


def _resolution_reason(host: str, resolve: Resolver) -> str | None:
    try:
        addresses = list(resolve(host))
    except (OSError, UnicodeError):
        return "dns_failed"
    if not addresses:
        return "dns_failed"
    try:
        return None if all(_is_public_ip(a) for a in addresses) else "resolves_private"
    except ValueError:
        return "dns_failed"


def check_url(
    url: str, *, internal_domains: Iterable[str] = (), resolve: Resolver = system_resolver
) -> str | None:
    """None if ``url`` may be requested from outside, else the reason it may not."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").rstrip(".").lower()
    except ValueError:
        return "unparseable"
    if parts.scheme not in ("http", "https"):
        return "scheme"
    if not host:
        return "no_host"
    public_literal = _ip_literal(host)
    if public_literal is not None:
        return None if public_literal else "private_ip"
    if "." not in host:
        return "single_label"
    if _internal(host, internal_domains):
        return "internal_domain"
    return _resolution_reason(host, resolve)
