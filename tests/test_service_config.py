"""PRD AD3: the numbers of the service (M6) are data in `config/profiles.toml` `[service]`."""

from pathlib import Path

import pytest
from pydantic import ValidationError
from support import make_settings

from app.pipeline.profiles import load_phase1, load_service_limits


def test_the_service_numbers_are_in_the_config() -> None:
    limits = load_service_limits(make_settings().config_dir)
    assert limits.worker_poll_s == 2
    assert limits.sse_poll_s == 1
    assert limits.session_threads == 2
    assert limits.summarize_models == ("gemma4:e4b", "gemma4:e2b")


def test_the_other_loaders_still_accept_a_config_with_a_service_section() -> None:
    assert load_phase1(make_settings().config_dir).max_rounds == 5


@pytest.mark.parametrize(
    "bad", ["worker_poll_s = 0", "session_threads = 0", "summarize_models = []"]
)
def test_a_bad_service_number_is_an_error(tmp_path: Path, bad: str) -> None:
    good = {
        "worker_poll_s": "2",
        "sse_poll_s": "1",
        "session_threads": "2",
        "summarize_models": '["a"]',
    }
    key = bad.split(" = ")[0]
    good[key] = bad.split(" = ")[1]
    text = "[service]\n" + "\n".join(f"{k} = {v}" for k, v in good.items()) + "\n"
    (tmp_path / "profiles.toml").write_text(text, encoding="utf-8")
    with pytest.raises(ValidationError):
        load_service_limits(tmp_path)
