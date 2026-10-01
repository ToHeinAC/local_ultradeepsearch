"""One HTTP GET to a web page: redirects not followed, body streamed up to a size cap."""

from importlib.metadata import PackageNotFoundError, version
from urllib.parse import urlsplit

import httpx

from app.adapters.outbound.errors import TransientProviderError
from app.adapters.outbound.types import HttpFactory, RawResponse, default_http


def _version() -> str:
    try:
        return version("local-ultradeepsearch")
    except PackageNotFoundError:  # pragma: no cover - only when run from an unpacked tree
        return "0"


# No URL and no e-mail address: websites only learn that a research tool is asking.
USER_AGENT = f"local-ultradeepsearch/{_version()} (research tool)"
ACCEPT = "text/html,application/xhtml+xml,application/pdf;q=0.9,text/plain;q=0.8,*/*;q=0.5"


class HttpGetter:
    def __init__(
        self,
        http: HttpFactory = default_http,
        *,
        timeout_s: float,
        max_html_bytes: int,
        max_pdf_bytes: int,
    ) -> None:
        self._http = http
        self._timeout_s = timeout_s
        self._max_html = max_html_bytes
        self._max_pdf = max_pdf_bytes

    def _cap(self, content_type: str | None, url: str) -> int:
        is_pdf = "pdf" in (content_type or "").lower() or urlsplit(url).path.lower().endswith(
            ".pdf"
        )
        return self._max_pdf if is_pdf else self._max_html

    @staticmethod
    def _read(response: httpx.Response, cap: int) -> tuple[bytes, bool]:
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_bytes():
            chunks.append(chunk)
            size += len(chunk)
            if size > cap:
                return b"".join(chunks), True
        return b"".join(chunks), False

    def get(self, url: str) -> RawResponse:
        headers = {"user-agent": USER_AGENT, "accept": ACCEPT}
        try:
            with (
                self._http(self._timeout_s) as client,
                client.stream("GET", url, headers=headers) as response,
            ):
                found = {k.lower(): v for k, v in response.headers.items()}
                cap = self._cap(found.get("content-type"), url)
                declared = found.get("content-length", "")
                if declared.isdigit() and int(declared) > cap:
                    return RawResponse(url, response.status_code, found, b"", too_large=True)
                body, too_large = self._read(response, cap)
        except httpx.HTTPError as exc:
            raise TransientProviderError("http_get", f"{type(exc).__name__}: {exc}") from exc
        return RawResponse(url, response.status_code, found, body, too_large)
