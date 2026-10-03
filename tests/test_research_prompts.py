"""PRD AD3: prompt text contains no hardcoded numbers; limits arrive through placeholders."""

import re

import pytest

from app.prompts import research as prompts


def constants() -> dict[str, str]:
    found: dict[str, str] = {}
    for name, value in vars(prompts).items():
        if not name.isupper():
            continue
        if isinstance(value, str):
            found[name] = value
        elif isinstance(value, dict):
            for key, text in value.items():
                found[f"{name}[{key}]"] = str(text)
    return found


def test_the_detector_sees_a_hardcoded_number() -> None:
    assert re.search(r"\d", re.sub(r"\{[a-z_]+\}", "", "at most 5 claims {limit}"))


@pytest.mark.parametrize("name", sorted(constants()))
def test_prompt_text_contains_no_digits_outside_placeholders(name: str) -> None:
    text = re.sub(r"\{[a-z_]+\}", "", constants()[name])
    assert not re.search(r"\d", text), f"{name} hardcodes a number"
