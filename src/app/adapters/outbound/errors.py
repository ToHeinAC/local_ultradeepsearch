"""Errors of the outbound package. Providers make one attempt and raise these; the gateway
decides about retries, provider switching and logging."""


class OutboundBlocked(Exception):
    """A request was refused before anything left the machine."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class DenylistBlocked(OutboundBlocked):
    """The payload contains a denylisted term (or the sanitizer left nothing to send)."""


class PrivateUrlBlocked(OutboundBlocked):
    """The URL points into the LAN, at this machine, or cannot be verified as public."""


class ProviderError(Exception):
    """A provider answered with an error that retrying will not fix."""

    def __init__(self, provider: str, message: str, status: int | None = None) -> None:
        super().__init__(f"{provider}: {message}" + (f" (HTTP {status})" if status else ""))
        self.provider = provider
        self.status = status


class TransientProviderError(ProviderError):
    """Timeout, connection failure, 5xx or rate limiting: worth retrying."""


class PlanLimitError(ProviderError):
    """Tavily HTTP 432/433: the plan or pay-as-you-go limit is exhausted."""


class ProviderAuthError(ProviderError):
    """HTTP 401/403: the API key is missing or invalid."""


class SearchUnavailable(Exception):
    """Every attempt failed. Callers record a coverage gap and continue (PRD M2)."""
