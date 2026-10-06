"""The Phase-2 composition (PRD M5): the real service, graph, steps, vault and fetch pipeline,
wired by `bootstrap.build_research_service` with fakes only at the edges (models, search, fetch,
and the pandoc binary)."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fixtures_corpus import FakeFetcher, FakeModels
from research_rig import FakePandoc, FakePreparer, ResearchModels
from research_run_rig import RAW_BRIEF, TEMPLATE, make_searcher, served
from test_bootstrap import FakeHost, admin, settings

from app import bootstrap
from app.adapters.outbound.gateway import Document, FetchFailure, PreparedQuery
from app.adapters.outbound.types import ScholarlyRecord, SearchHit
from app.events import MemoryEventSink
from app.llm.fakes import CallbackTransport
from app.llm.types import ChatReply, ChatRequest
from app.pipeline.profiles import Profile
from app.research.service import ResearchService
from app.store.db import Database
from app.store.runs import RunStore

NOW = datetime(2026, 10, 4, 9, 30, 15, tzinfo=UTC)


class RunGateway:
    """The three roles of the outbound gateway (prepare, search, fetch) over fakes."""

    def __init__(self) -> None:
        self.preparer = FakePreparer()
        self.searcher = make_searcher()
        self.fetcher = FakeFetcher(served())

    def prepare_query(self, query: str, *, step: str) -> PreparedQuery:
        return self.preparer.prepare_query(query, step=step)

    def search_web(
        self, prepared: PreparedQuery, *, step: str, max_results: int = 10
    ) -> list[SearchHit]:
        return self.searcher.search_web(prepared, step=step, max_results=max_results)

    def search_scholarly(
        self, prepared: PreparedQuery, *, step: str, source: str, max_results: int = 10
    ) -> list[ScholarlyRecord]:
        return self.searcher.search_scholarly(
            prepared,
            step=step,
            source=source,  # type: ignore[arg-type]
            max_results=max_results,
        )

    def fetch(self, url: str, *, step: str) -> Document | FetchFailure:
        return self.fetcher.fetch(url, step=step)


class AllModels(ResearchModels):
    """One model service answers both the research steps and the fetch pipeline's extraction."""

    def __init__(self) -> None:
        super().__init__()
        self.pipeline = FakeModels()
        self.pipeline_models: dict[str, set[str]] = {}  # schema title -> models asked

    def __call__(self, request: ChatRequest) -> ChatReply | Exception:
        title = str(request.schema["title"]) if request.schema else ""
        if title in ("ChunkExtraction", "MergedSummary", "PartialAnalysis", "SourceAnalysis"):
            self.pipeline_models.setdefault(title, set()).add(request.model)
            return self.pipeline(request)
        return super().__call__(request)


def wired(
    tmp_path: Path, *, pandoc: FakePandoc | None = None, gateway: RunGateway | None = None
) -> tuple[ResearchService, RunGateway, ResearchModels, bootstrap.Runtime]:
    """One 'process': a runtime on scripted models and the research service on top of it."""
    models = AllModels()
    rt = bootstrap.build_runtime(
        settings(tmp_path),
        MemoryEventSink(),
        probe=FakeHost(),
        admin=admin(),
        transport=CallbackTransport(models),
        env={},
    )
    gw = gateway or RunGateway()

    def factory(_run_dir: Path, _brief: str, _profile: Profile) -> RunGateway:
        return gw

    service = bootstrap.build_research_service(
        rt, now=lambda: NOW, gateway_factory=factory, pandoc=pandoc
    )
    return service, gw, models, rt


def test_a_run_goes_from_an_external_brief_to_a_report_through_the_composition(
    tmp_path: Path,
) -> None:
    service, _gw, _models, rt = wired(tmp_path, pandoc=FakePandoc())
    run_id = service.create_external_run(
        RAW_BRIEF, tier="light", template_id=TEMPLATE, language="de"
    ).run_id
    waiting = service.run(run_id)
    assert (waiting.status, waiting.waiting_for) == ("awaiting_plan_approval", "plan")
    done = service.approve_and_run(run_id, str(waiting.plan_sha256))
    assert done.error is None, done.error
    assert (done.status, done.exports) == ("done", {"docx": "ok", "pdf": "ok"})
    data = rt.settings.data_dir
    run_dir = data / "runs" / run_id
    report = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "2026-10-04 09:30:15" in report  # the approval time comes from the archive name
    for name in ("query.md", "run.json", "search-plan.json", "report.md"):
        assert (run_dir / name).is_file()
    assert (data / bootstrap.CHECKPOINT_FILE).exists()
    assert (data / bootstrap.VAULT_FILE).exists()


def test_a_run_waiting_for_its_plan_survives_building_the_service_again(tmp_path: Path) -> None:
    first, _, _, _ = wired(tmp_path)
    run_id = first.create_external_run(
        RAW_BRIEF, tier="light", template_id=TEMPLATE, language="de"
    ).run_id
    plan = first.run(run_id).plan_sha256
    again, _, models, _ = wired(tmp_path)  # a restarted process on the same files
    view = again.view(run_id)
    assert (view.waiting_for, view.plan_sha256) == ("plan", plan)
    assert again.run(run_id).status == "awaiting_plan_approval"
    assert models.calls == {}


def test_without_a_pandoc_binary_the_run_still_ends_and_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "")  # the real adapter finds no binary
    service, _gw, _models, rt = wired(tmp_path)
    run_id = service.create_external_run(
        RAW_BRIEF, tier="light", template_id=TEMPLATE, language="de"
    ).run_id
    done = service.approve_and_run(run_id, str(service.run(run_id).plan_sha256))
    assert done.status == "done"
    assert done.exports == {"docx": "pandoc_missing", "pdf": "ok"}
    pdf = rt.settings.data_dir / "runs" / run_id / "report.pdf"
    assert pdf.read_bytes().startswith(b"%PDF")  # made in-process, no binary involved
    saved = json.loads((rt.settings.data_dir / "runs" / run_id / "run.json").read_text("utf-8"))
    assert saved["exports"] == done.exports


def test_the_real_gateway_is_built_per_run_with_the_tiers_credit_cap(tmp_path: Path) -> None:
    models = ResearchModels()
    rt = bootstrap.build_runtime(
        settings(tmp_path),
        MemoryEventSink(),
        probe=FakeHost(),
        admin=admin(),
        transport=CallbackTransport(models),
        env={},
    )
    service = bootstrap.build_research_service(rt, pandoc=FakePandoc())
    run_id = service.create_external_run(
        RAW_BRIEF, tier="light", template_id=TEMPLATE, language="de"
    ).run_id
    ctx = service._d.contexts(run_id)  # pyright: ignore[reportPrivateUsage]
    from app.adapters.outbound.gateway import OutboundGateway

    assert isinstance(ctx.preparer, OutboundGateway)
    assert ctx.preparer.credit_cap == 60  # config/profiles.toml [light]
    assert ctx.searcher is ctx.preparer is ctx.ingestor.fetcher  # type: ignore[attr-defined]


def test_the_worker_takes_a_queued_run_through_the_composition(tmp_path: Path) -> None:
    service, _gw, _models, rt = wired(tmp_path, pandoc=FakePandoc())
    run_id = service.create_external_run(
        RAW_BRIEF, tier="light", template_id=TEMPLATE, language="de"
    ).run_id
    worker = bootstrap.build_worker(rt, service)
    assert worker.run_once() == run_id
    assert service.view(run_id).status == "awaiting_plan_approval"
    assert worker.run_once() is None


def test_a_runs_own_summarize_model_reaches_the_fetch_pipelines_extraction(
    tmp_path: Path,
) -> None:
    service, _gw, _models, rt = wired(tmp_path, pandoc=FakePandoc())
    run_id = service.create_external_run(
        RAW_BRIEF, tier="light", template_id=TEMPLATE, language="de"
    ).run_id
    runs = RunStore(Database(rt.settings.data_dir / bootstrap.VAULT_FILE))
    row = runs.get_run(run_id)
    assert row is not None
    assert row.settings_json is not None
    runs.set_settings(
        run_id, json.dumps({**json.loads(row.settings_json), "summarize_model": "x:e2b"})
    )
    fresh, _gw, models, _rt = wired(tmp_path, pandoc=FakePandoc())  # the process that works it
    fresh.run(run_id)
    fresh.approve_and_run(run_id, str(fresh.view(run_id).plan_sha256))
    assert isinstance(models, AllModels)
    assert models.pipeline_models["ChunkExtraction"] == {"x:e2b"}
