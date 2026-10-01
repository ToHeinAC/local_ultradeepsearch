"""Best-effort removal of confidential details from outbound queries (PRD §3.2).

The `reason` model rewrites each query against the run's confidential context (brief and upload
facts). It fails closed: if no valid answer comes back, the query is not sent. The denylist, not
this module, is the hard guarantee.
"""

import threading

from pydantic import BaseModel

from app.llm.errors import LLMError
from app.llm.service import LLMService
from app.llm.types import Message, Role
from app.prompts.outbound import SANITIZER_SYSTEM, SANITIZER_USER


class SanitizerError(Exception):
    """The sanitizer produced no usable answer; the query must not be sent."""


class SanitizedQuery(BaseModel):
    sanitized_query: str
    removed_terms: list[str]


class Sanitizer:
    def __init__(self, llm: LLMService, confidential_context: str) -> None:
        self._llm = llm
        self._context = confidential_context
        self._cache: dict[str, SanitizedQuery] = {}
        self._lock = threading.Lock()

    def sanitize(self, query: str) -> SanitizedQuery:
        with self._lock:
            cached = self._cache.get(query)
        if cached is not None:
            return cached
        messages: tuple[Message, ...] = (
            {"role": "system", "content": SANITIZER_SYSTEM},
            {"role": "user", "content": SANITIZER_USER.format(context=self._context, query=query)},
        )
        try:
            raw = self._llm.structured(Role.REASON, messages, SanitizedQuery)
        except LLMError as exc:
            raise SanitizerError(f"sanitizer failed, query not sent: {exc}") from exc
        result = SanitizedQuery(
            sanitized_query=" ".join(raw.sanitized_query.split()),
            removed_terms=[t.strip() for t in raw.removed_terms if t.strip()],
        )
        with self._lock:
            return self._cache.setdefault(query, result)
