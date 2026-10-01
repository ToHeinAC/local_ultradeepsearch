import socket

import pytest

from app.adapters.outbound.guard import BUILTIN_INTERNAL_SUFFIXES, check_url, system_resolver

PUBLIC = "93.184.215.14"
DNS: dict[str, list[str]] = {
    "example.com": [PUBLIC],
    "www.example.com": [PUBLIC, "2606:2800:21f:cb07:6820:80da:af6b:8b2c"],
    "sneaky.example.net": [PUBLIC, "10.1.2.3"],  # one private address is enough to block
    "rebind.example.org": ["127.0.0.1"],
    "v6-local.example.org": ["fe80::1%eth0"],
    "mapped.example.org": ["::ffff:192.168.1.10"],
    "127.1": ["127.0.0.1"],
    "0x7f.1": ["127.0.0.1"],
}


def resolve(host: str) -> list[str]:
    if host not in DNS:
        raise OSError("no such host")
    return DNS[host]


def check(url: str, internal: tuple[str, ...] = ()) -> str | None:
    return check_url(url, internal_domains=internal, resolve=resolve)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/paper.pdf",
        "http://www.example.com:8080/a?b=c",
        f"https://{PUBLIC}/x",
        "https://EXAMPLE.com./",  # trailing dot, upper case
        f"http://[::ffff:{PUBLIC}]/",  # IPv4-mapped public address, same on every Python
    ],
)
def test_public_urls_pass(url: str) -> None:
    assert check(url) is None


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("ftp://example.com/x", "scheme"),
        ("file:///etc/passwd", "scheme"),
        ("https:///nohost", "no_host"),
        ("http://10.0.0.5/", "private_ip"),
        ("http://172.16.4.112:8540/", "private_ip"),
        ("http://172.31.255.255/", "private_ip"),
        ("http://192.168.1.1/", "private_ip"),
        ("http://127.0.0.1:11434/api/tags", "private_ip"),
        ("http://[::1]/", "private_ip"),
        ("http://169.254.169.254/latest/meta-data", "private_ip"),
        ("http://[fe80::1]/", "private_ip"),
        ("http://100.64.0.1/", "private_ip"),
        ("http://0.0.0.0/", "private_ip"),
        ("http://[::ffff:10.0.0.1]/", "private_ip"),
        ("http://intranet/", "single_label"),
        ("http://localhost:8540/", "single_label"),
        ("http://2130706433/", "single_label"),  # decimal 127.0.0.1
        ("http://wiki.brenk.local/page", "internal_domain"),
        ("http://printer.lan/", "internal_domain"),
        ("http://nas.home.arpa/", "internal_domain"),
        ("http://app.localhost/", "internal_domain"),
        ("http://sneaky.example.net/", "resolves_private"),
        ("http://rebind.example.org/", "resolves_private"),
        ("http://v6-local.example.org/", "resolves_private"),
        ("http://mapped.example.org/", "resolves_private"),
        ("http://127.1/", "resolves_private"),
        ("http://0x7f.1/", "resolves_private"),
        ("http://does-not-exist.example/", "dns_failed"),
    ],
)
def test_blocked_urls_name_their_reason(url: str, reason: str) -> None:
    assert check(url) == reason


def test_configured_internal_domains_match_suffixes_only() -> None:
    internal = ("corp.example.com",)
    assert check("https://wiki.corp.example.com/x", internal) == "internal_domain"
    assert check("https://corp.example.com/", internal) == "internal_domain"
    DNS["notcorp.example.com"] = [PUBLIC]
    assert check("https://notcorp.example.com/", internal) is None


def test_builtin_suffixes_cover_the_special_use_names() -> None:
    assert {"localhost", "local", "internal", "home.arpa", "lan"} <= set(BUILTIN_INTERNAL_SUFFIXES)


def test_unparseable_urls_are_blocked() -> None:
    assert check("http://[::1") == "unparseable"


def test_system_resolver_returns_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_getaddrinfo(host: str, port: object, *args: object) -> list[tuple[object, ...]]:
        assert host == "example.com"
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, 0)),
            (socket.AF_INET, socket.SOCK_DGRAM, 17, "", (PUBLIC, 0)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 0, 0, 0)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    addresses = system_resolver("example.com")
    assert len(addresses) == 2  # duplicates across socket types are dropped
    assert set(addresses) == {PUBLIC, "::1"}
