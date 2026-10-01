"""The LLM error hierarchy. Callers catch `LLMError`; each subclass says what to do next."""


class LLMError(Exception):
    """Base class for every error raised by the LLM layer."""


class LLMUnavailableError(LLMError):
    """The endpoint refused, timed out or answered 5xx, and retries are exhausted."""


class LLMModelMissingError(LLMError):
    """The endpoint does not have the requested model. Never retried."""


class LLMOutputError(LLMError):
    """The output was still not valid for the schema after the repair attempts."""

    def __init__(self, message: str, *, raw: str) -> None:
        super().__init__(message)
        self.raw = raw


class LLMTruncatedError(LLMError):
    """The model hit its output limit again after one retry with a larger budget."""

    def __init__(self, message: str, *, raw: str) -> None:
        super().__init__(message)
        self.raw = raw


class PromptTooLargeError(LLMError):
    """The prompt plus the output reserve does not fit the role's context window."""

    def __init__(self, *, estimate: int, limit: int) -> None:
        super().__init__(f"prompt needs about {estimate} tokens, limit is {limit}")
        self.estimate = estimate
        self.limit = limit
