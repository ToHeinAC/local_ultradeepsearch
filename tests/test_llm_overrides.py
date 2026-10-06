"""M6 D9: `LLMService.with_models` serves a run its own summarize model."""

from research_rig import REGISTRY, URLS

from app.events import MemoryEventSink
from app.llm.fakes import CallbackTransport, reply
from app.llm.service import LLMService
from app.llm.types import Role


def service() -> tuple[LLMService, CallbackTransport, MemoryEventSink]:
    events = MemoryEventSink()
    transport = CallbackTransport(lambda _request: reply("ok"))
    return LLMService(REGISTRY, URLS, transport, events, timeout_s=5), transport, events


def ask(llm: LLMService, role: Role) -> None:
    llm.text(role, [{"role": "user", "content": "Hallo"}])


def test_the_overridden_roles_send_the_override_and_the_others_their_own_model() -> None:
    llm, transport, _events = service()
    other = llm.with_models({Role.SUMMARIZE: "gemma4:e2b", Role.EXTRACT: "gemma4:e2b"})
    for role in (Role.SUMMARIZE, Role.EXTRACT, Role.REASON):
        ask(other, role)
    sent = [request.model for request in transport.calls]
    assert sent[:2] == ["gemma4:e2b", "gemma4:e2b"]
    assert sent[2] == REGISTRY[Role.REASON].model


def test_the_original_service_is_not_changed() -> None:
    llm, transport, _events = service()
    llm.with_models({Role.SUMMARIZE: "gemma4:e2b"})
    ask(llm, Role.SUMMARIZE)
    assert transport.calls[0].model == REGISTRY[Role.SUMMARIZE].model


def test_the_override_keeps_the_endpoint_the_context_and_the_events() -> None:
    llm, transport, events = service()
    other = llm.with_models({Role.SUMMARIZE: "gemma4:e2b"})
    ask(other, Role.SUMMARIZE)
    assert transport.calls[0].num_ctx == REGISTRY[Role.SUMMARIZE].num_ctx
    assert transport.urls[0] == URLS[REGISTRY[Role.SUMMARIZE].endpoint]
    assert events.events  # the telemetry of the override service reaches the same sink
