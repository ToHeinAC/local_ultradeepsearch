"""Live check of the M3 pipeline: two neutral public pages through the real gateway and `extract`.

Run with `pytest -m live tests/live/test_live_vault.py -s`. It measures `claims_drop_rate`
(PRD risk R2: above 0.30, propose a gemma model for `UDR_MODEL_EXTRACT`); the number is printed.
"""

from pathlib import Path

import pytest
from support import make_settings

from app import bootstrap
from app.adapters.ollama_instance import InstanceState
from app.events import JsonlEventSink
from app.pipeline.extraction import Focus

pytestmark = pytest.mark.live

URLS = (
    "https://en.wikipedia.org/wiki/Heat_pump",
    "https://en.wikipedia.org/wiki/Photosynthesis",
)
FOCUS = Focus("Heat pumps and photosynthesis", ("How does a heat pump work?",))


def test_two_public_pages_are_ingested_and_the_drop_rate_is_measured(
    tmp_path: Path,
) -> None:
    settings = make_settings(data_dir=tmp_path)
    rt = bootstrap.build_runtime(settings, JsonlEventSink(settings.data_dir / "events.jsonl"))
    assert rt.status.state in (InstanceState.ADOPTED, InstanceState.STARTED), rt.status.reason
    pipeline = bootstrap.build_pipeline(rt, "live-vault", tier="light", focus=FOCUS)
    pipeline.resume()
    results = pipeline.ingest_many([(url, None) for url in URLS])
    stats = bootstrap.open_vault(settings, "live-vault").stats()
    print(f"\nresults: {results}\nstats: {stats}")
    assert stats.claims_kept > 0, stats
