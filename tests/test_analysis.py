import json
from collections.abc import Callable
from dataclasses import dataclass

import pytest
from support import make_note, make_settings

from app.events import MemoryEventSink
from app.llm.errors import LLMModelMissingError
from app.llm.fakes import CallbackTransport, reply
from app.llm.roles import build_registry
from app.llm.service import LLMService
from app.llm.types import ChatReply, ChatRequest, Endpoint, Role
from app.pipeline.analysis import (
    MAP_CHUNK_CHARS,
    REDUCE_BATCH_CHARS,
    SourceAnalyzer,
    needs_analysis,
    render_analysis,
)
from app.pipeline.extraction import Focus
from app.pipeline.profiles import Profile
from app.pipeline.schemas import SourceAnalysis

FOCUS = Focus(title="Decommissioning of research reactors", questions=("How long does it take?",))
URLS = {Endpoint.OWN: "http://own", Endpoint.SHARED: "http://shared"}
REGISTRY = build_registry(make_settings())
SUMMARIZE_MODEL = REGISTRY[Role.SUMMARIZE].model
Handler = Callable[[ChatRequest], ChatReply | Exception]


@dataclass
class Rig:
    analyzer: SourceAnalyzer
    transport: CallbackTransport
    events: MemoryEventSink

    def stages(self) -> list[str]:
        return [stage_of(r) for r in self.transport.calls]


def rig(handler: Handler) -> Rig:
    transport = CallbackTransport(handler)
    events = MemoryEventSink()
    service = LLMService(REGISTRY, URLS, transport, events, timeout_s=5, sleep=lambda _s: None)
    return Rig(SourceAnalyzer(service, events), transport, events)


def stage_of(request: ChatRequest) -> str:
    assert request.schema is not None
    return str(request.schema["title"])


def partial(points: list[str], quotes: list[str] | None = None) -> ChatReply:
    body = {"key_points": points, "numbers": ["12 years"], "quotes": quotes or []}
    return reply(json.dumps(body))


def final(**overrides: object) -> ChatReply:
    body: dict[str, object] = {
        "thesis": "Dismantling is slow but safe.",
        "methodology": "Case study of three reactors.",
        "key_findings": ["It takes about twelve years.", "Costs vary widely."],
        "load_bearing_citations": ["IAEA (2019) Technical Report 12"],
        "caveats": "Small sample.",
        "relevance_to_query": "Answers the duration question directly.",
        "quotes": [],
        "relevance": "load-bearing",
    }
    return reply(json.dumps({**body, **overrides}))


SIX_CHUNKS = MAP_CHUNK_CHARS * 6 - 2000  # whole-sentence chunks hold a little under the limit


def long_body(chars: int) -> str:
    sentence = "The research reactor dismantling programme proceeded in several documented phases. "
    return (sentence * (chars // len(sentence) + 1))[:chars]


# ---- map and reduce -------------------------------------------------------------------------


def test_map_chunks_then_one_final_reduce_when_the_partials_fit() -> None:
    def handler(req: ChatRequest) -> ChatReply:
        return final() if stage_of(req) == "SourceAnalysis" else partial(["a point"])

    r = rig(handler)
    result = r.analyzer.analyze(make_note(body=long_body(MAP_CHUNK_CHARS * 2 + 5000)), FOCUS)
    assert result is not None
    assert r.stages() == ["PartialAnalysis"] * 3 + ["SourceAnalysis"]
    assert {c.model for c in r.transport.calls} == {SUMMARIZE_MODEL}
    assert set(r.transport.urls) == {"http://shared"}
    assert all(c.think is False for c in r.transport.calls)


def test_many_large_partials_are_reduced_in_batches_without_overflowing_the_prompt() -> None:
    big = ["point " * 400] * 5  # about 12 000 characters per partial

    def handler(req: ChatRequest) -> ChatReply:
        stage = stage_of(req)
        if stage == "SourceAnalysis":
            return final()
        return partial(big) if "Source part" in req.messages[-1]["content"] else partial(["merged"])

    r = rig(handler)
    result = r.analyzer.analyze(make_note(body=long_body(SIX_CHUNKS)), FOCUS)
    assert result is not None
    # 6 map calls; 2 partials fit one batch -> 3 reduce calls; the 3 merged ones fit one final call
    assert r.stages() == ["PartialAnalysis"] * 6 + ["PartialAnalysis"] * 3 + ["SourceAnalysis"]
    longest = max(len(c.messages[-1]["content"]) for c in r.transport.calls)
    assert longest < REDUCE_BATCH_CHARS + 3000


def test_an_odd_partial_is_carried_over_without_a_model_call() -> None:
    big = ["point " * 400] * 5

    def handler(req: ChatRequest) -> ChatReply:
        if stage_of(req) == "SourceAnalysis":
            return final()
        return partial(big) if "Source part" in req.messages[-1]["content"] else partial(["merged"])

    r = rig(handler)
    # 7 chunks -> 7 big partials -> batches of 2, 2, 2 and a lone one that needs no merge
    assert r.analyzer.analyze(make_note(body=long_body(MAP_CHUNK_CHARS * 6 + 2000)), FOCUS)
    assert r.stages() == ["PartialAnalysis"] * 7 + ["PartialAnalysis"] * 3 + ["SourceAnalysis"]


def test_the_prompts_fence_the_source_text() -> None:
    marker = "UNIQUE-SOURCE-MARKER"
    body = f"{marker} " + long_body(40_000)
    r = rig(lambda req: final() if stage_of(req) == "SourceAnalysis" else partial(["p"]))
    r.analyzer.analyze(make_note(body=body, final_url="https://example.org/long"), FOCUS)
    first = r.transport.calls[0]
    system, user = first.messages
    assert marker not in system["content"]
    assert marker in user["content"].split("<untrusted-source", 1)[1]
    assert 'url="https://example.org/long"' in user["content"]
    for call in r.transport.calls:
        assert "untrusted" in call.messages[0]["content"].lower()


# ---- verification and rendering --------------------------------------------------------------


def test_only_verbatim_quotes_survive_and_at_most_ten() -> None:
    sentences = [
        f"Phase {i} of the programme finished within the planned budget." for i in range(30)
    ]
    body = " ".join(sentences)
    invented = "Everything was completed in a single week."
    quotes = [invented, *sentences[:14]]  # the invented one first, so only verification removes it

    def handler(req: ChatRequest) -> ChatReply:
        return final(quotes=quotes) if stage_of(req) == "SourceAnalysis" else partial(["p"])

    result = rig(handler).analyzer.analyze(make_note(body=body), FOCUS)
    assert result is not None
    assert result.quotes == sentences[:10]
    assert "single week" not in " ".join(result.quotes)


def test_the_analysis_is_rendered_as_markdown_with_the_original_headings() -> None:
    analysis = SourceAnalysis.model_validate_json(final(quotes=["Phase 1 finished."]).content)
    title, body = render_analysis(make_note(title="Long report"), analysis)
    assert title == "Analysis: Long report"
    headings = [line for line in body.splitlines() if line.startswith("## ")]
    assert headings == [
        "## Thesis",
        "## Methodology",
        "## Key findings",
        "## Load-bearing citations",
        "## Caveats",
        "## Relevance to the question",
        "## Quotes",
    ]
    assert "Dismantling is slow but safe." in body
    assert "- It takes about twelve years." in body
    assert "load-bearing" in body
    assert '> "Phase 1 finished."' in body


def test_empty_lists_render_as_none_stated() -> None:
    analysis = SourceAnalysis.model_validate_json(
        final(key_findings=[], load_bearing_citations=[], quotes=[], methodology="").content
    )
    _, body = render_analysis(make_note(), analysis)
    assert body.count("None stated.") == 4


# ---- failures -------------------------------------------------------------------------------


def test_a_failing_map_chunk_is_skipped_with_an_event() -> None:
    calls = {"n": 0}

    def handler(req: ChatRequest) -> ChatReply | Exception:
        if stage_of(req) == "SourceAnalysis":
            return final()
        calls["n"] += 1
        return LLMModelMissingError("gone") if calls["n"] == 2 else partial(["p"])

    r = rig(handler)
    result = r.analyzer.analyze(make_note(body=long_body(MAP_CHUNK_CHARS * 2 + 100)), FOCUS)
    assert result is not None
    (event,) = r.events.of_type("source_analysis_chunk_failed")
    assert (event.data["note_id"], event.data["chunk"]) == ("n0001", 2)
    assert r.stages().count("SourceAnalysis") == 1


def test_when_every_map_chunk_fails_there_is_no_analysis() -> None:
    r = rig(lambda req: LLMModelMissingError("gone"))
    assert r.analyzer.analyze(make_note(body=long_body(40_000)), FOCUS) is None
    (failed,) = r.events.of_type("source_analysis_failed")
    assert failed.data["stage"] == "map"
    assert "SourceAnalysis" not in r.stages()


def test_a_failing_final_reduce_means_no_analysis() -> None:
    def handler(req: ChatRequest) -> ChatReply | Exception:
        return LLMModelMissingError("gone") if stage_of(req) == "SourceAnalysis" else partial(["p"])

    r = rig(handler)
    assert r.analyzer.analyze(make_note(body=long_body(40_000)), FOCUS) is None
    (failed,) = r.events.of_type("source_analysis_failed")
    assert failed.data["stage"] == "reduce"


def test_a_failing_intermediate_reduce_means_no_analysis() -> None:
    big = ["point " * 400] * 5

    def handler(req: ChatRequest) -> ChatReply | Exception:
        if "Source part" in req.messages[-1]["content"]:
            return partial(big)
        return LLMModelMissingError("gone")

    r = rig(handler)
    assert r.analyzer.analyze(make_note(body=long_body(SIX_CHUNKS)), FOCUS) is None
    assert r.events.of_type("source_analysis_failed")[0].data["stage"] == "reduce"


# ---- who gets an analysis -------------------------------------------------------------------

PROFILE = Profile(credit_cap=60, source_analysis_cap=6, long_source_words=5000)


@pytest.mark.parametrize(
    ("overrides", "existing", "expected"),
    [
        ({"word_count": 5000}, 0, True),
        ({"word_count": 4999}, 0, False),
        ({"word_count": 20000}, 5, True),
        ({"word_count": 20000}, 6, False),
        ({"word_count": 20000, "derivative_of": "n0001"}, 0, False),
        ({"word_count": 20000, "extract_failed": True}, 0, False),
        ({"word_count": 20000, "kind": "source_analysis"}, 0, False),
    ],
)
def test_needs_analysis(overrides: dict[str, object], existing: int, expected: bool) -> None:
    assert needs_analysis(make_note(**overrides), PROFILE, existing) is expected
