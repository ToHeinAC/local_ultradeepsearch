import pytest
from pydantic import BaseModel

from app.llm.structured import (
    StructuredParseError,
    estimate_tokens,
    parse_structured,
    repair_messages,
    strip_think,
)
from app.llm.types import Message


class Answer(BaseModel):
    title: str
    score: int


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('<think>reasoning</think>\n{"a": 1}', '{"a": 1}'),
        ("<think>one</think>x<think>two</think>y", "xy"),
        ("<think>line 1\nline 2\n</think>\nbody", "body"),
        ("<THINK>loud</Think>quiet", "quiet"),
        ("answer<think>cut off while thinking", "answer"),
        ('thoughts without opening tag</think>{"a": 1}', '{"a": 1}'),
        ("  no tags at all  ", "no tags at all"),
        ("<think>only thinking</think>", ""),
    ],
)
def test_strip_think(raw: str, expected: str) -> None:
    assert strip_think(raw) == expected


def test_parse_valid_json() -> None:
    assert parse_structured('{"title": "t", "score": 3}', Answer) == Answer(title="t", score=3)


def test_parse_accepts_a_markdown_fence() -> None:
    raw = '```json\n{"title": "t", "score": 3}\n```'
    assert parse_structured(raw, Answer).score == 3


def test_parse_reports_invalid_json() -> None:
    with pytest.raises(StructuredParseError, match="JSON"):
        parse_structured('{"title": "t", "sc', Answer)


def test_parse_reports_schema_violations_by_field() -> None:
    with pytest.raises(StructuredParseError) as err:
        parse_structured('{"title": "t", "score": "high"}', Answer)
    assert "score" in str(err.value)


def test_parse_reports_missing_fields() -> None:
    with pytest.raises(StructuredParseError) as err:
        parse_structured('{"title": "t"}', Answer)
    assert "score" in str(err.value)
    assert "required" in str(err.value).lower() or "missing" in str(err.value).lower()


def test_repair_messages_append_the_bad_reply_and_the_error() -> None:
    original: tuple[Message, ...] = ({"role": "user", "content": "give me JSON"},)
    out = repair_messages(original, raw='{"title":', error="score: Field required")
    assert out[:1] == original
    assert out[1] == {"role": "assistant", "content": '{"title":'}
    assert out[2]["role"] == "user"
    assert "score: Field required" in out[2]["content"]
    assert len(original) == 1  # input is not mutated


def test_estimate_tokens_is_chars_over_three_rounded_up() -> None:
    msgs: list[Message] = [
        {"role": "system", "content": "abc"},
        {"role": "user", "content": "defgh"},
    ]
    assert estimate_tokens(msgs) == 3  # 8 chars → ceil(8/3)
    assert estimate_tokens([]) == 0
