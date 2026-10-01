import json

import pytest
from support import make_settings

from app.adapters.outbound.sanitizer import Sanitizer, SanitizerError
from app.events import MemoryEventSink
from app.llm.fakes import ScriptedTransport, reply
from app.llm.roles import build_registry
from app.llm.service import LLMService
from app.llm.types import ChatReply, Endpoint, Role
from app.prompts.outbound import SANITIZER_SYSTEM

CONTEXT = "Client: Müller-Werke GmbH, project Kranich, contact Dr. Anna Schmidt."


def sanitizer(script: list[ChatReply | Exception]) -> tuple[Sanitizer, ScriptedTransport]:
    transport = ScriptedTransport(script)
    service = LLMService(
        build_registry(make_settings()),
        {Endpoint.OWN: "http://own", Endpoint.SHARED: "http://shared"},
        transport,
        MemoryEventSink(),
        timeout_s=5,
        sleep=lambda _s: None,
    )
    return Sanitizer(service, CONTEXT), transport


def answer(query: str, removed: list[str]) -> ChatReply:
    return reply(json.dumps({"sanitized_query": query, "removed_terms": removed}))


def test_returns_the_rewritten_query_and_removed_terms() -> None:
    s, _ = sanitizer([answer("  decommissioning   costs nuclear plant ", ["Müller-Werke"])])
    result = s.sanitize("Müller-Werke decommissioning costs nuclear plant")
    assert result.sanitized_query == "decommissioning costs nuclear plant"
    assert result.removed_terms == ["Müller-Werke"]


def test_uses_the_reason_role_without_thinking_and_sends_the_context() -> None:
    s, transport = sanitizer([answer("q", [])])
    s.sanitize("Kranich status report")
    base_url, request = transport.calls[0]
    assert base_url == "http://own"
    assert request.model == build_registry(make_settings())[Role.REASON].model
    assert request.think is False
    system, user = request.messages
    assert system == {"role": "system", "content": SANITIZER_SYSTEM}
    assert "<confidential_context>" in user["content"]
    assert CONTEXT in user["content"]
    assert "<query>\nKranich status report\n</query>" in user["content"]


def test_results_are_cached_per_query() -> None:
    s, transport = sanitizer([answer("a", []), answer("b", [])])
    first = s.sanitize("same query")
    second = s.sanitize("same query")
    third = s.sanitize("other query")
    assert first is second
    assert (first.sanitized_query, third.sanitized_query) == ("a", "b")
    assert len(transport.calls) == 2


def test_fails_closed_when_the_model_cannot_answer() -> None:
    s, _ = sanitizer([reply("nope"), reply("nope"), reply("nope")])
    with pytest.raises(SanitizerError, match="not sent"):
        s.sanitize("anything")


def test_an_unreachable_model_also_fails_closed() -> None:
    from app.llm.errors import LLMModelMissingError

    s, _ = sanitizer([LLMModelMissingError("gone")])
    with pytest.raises(SanitizerError):
        s.sanitize("anything")
