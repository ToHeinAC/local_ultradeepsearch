"""The `udr` command line. Thin: it wires `bootstrap`, calls pure logic and prints."""

import signal
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from types import FrameType
from typing import Annotated

import click
import typer

from app import bootstrap
from app.adapters.ollama_instance import InstanceState
from app.adapters.outbound.denylist import Denylist
from app.api import server
from app.api.keys import KeyStore
from app.bootstrap import DENYLIST_FILE
from app.brief.console import ConsoleIO, run_session
from app.brief.errors import InvalidInput, NotFound, WrongState
from app.brief.service import BriefService
from app.brief.uploads import UploadFile
from app.calibration import CANDIDATES, CalibrationError, save_calibration
from app.doctor import evaluate, exit_code, render, render_json
from app.events import JsonlEventSink
from app.llm.errors import LLMError
from app.llm.types import Role
from app.research.console import render_plan, run_plan_review
from app.research.errors import EmptyPlan, PlanBlocked, StalePlan, WorkerBusy
from app.research.service import ResearchService, RunView, TierNotAvailable
from app.store.db import Database
from app.worker import Worker

app = typer.Typer(help="Local UltraDeep Researcher.", no_args_is_help=True, add_completion=False)


def _calibrate(rt: bootstrap.Runtime) -> str:
    """Run the measurement and store it. Returns an error message, or "" on success."""
    status = rt.status
    if status.state not in (InstanceState.ADOPTED, InstanceState.STARTED):
        return f"cannot calibrate: our own Ollama instance is not running ({status.reason})"
    try:
        result = bootstrap.calibrate(rt)
    except (CalibrationError, LLMError) as exc:
        return f"calibration failed: {exc}"
    spec = rt.registry[Role.REASON]
    if result is None:
        return (
            f"{spec.model} does not fit into the VRAM of GPU {rt.settings.own_ollama_gpu} "
            f"even at {min(CANDIDATES)} tokens of context"
        )
    save_calibration(bootstrap.calibration_path(rt.settings), result)
    typer.echo(f"calibrated: reason num_ctx = {result.reason_num_ctx}", err=True)
    return ""


@app.callback()
def _root() -> None:
    """Local UltraDeep Researcher. Keeps `doctor` a subcommand as more are added."""


@app.command()
def doctor(
    calibrate: Annotated[
        bool, typer.Option("--calibrate", help="Measure the largest reason context that fits VRAM.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Check models, endpoints, disk and GPU. Exit code 1 if anything is wrong."""
    settings = bootstrap.load_settings()
    events = JsonlEventSink(settings.data_dir / "events.jsonl")
    rt = bootstrap.build_runtime(settings, events)
    problem = _calibrate(rt) if calibrate else ""
    checks = evaluate(bootstrap.collect_snapshot(rt), rt.registry)
    typer.echo(render_json(checks) if as_json else render(checks))
    if problem:
        typer.echo(problem, err=True)
    raise typer.Exit(1 if problem else exit_code(checks))


def _brief_service() -> BriefService:
    """The Phase-1 service; sessions a crash left unfinished are continued first (PRD AD10)."""
    settings = bootstrap.load_settings()
    events = JsonlEventSink(settings.data_dir / "events.jsonl")
    service = bootstrap.build_brief_service(bootstrap.build_runtime(settings, events))
    service.recover()
    return service


def _read_uploads(paths: list[Path]) -> list[UploadFile]:
    for path in paths:
        if not path.is_file():
            typer.echo(f"Datei nicht gefunden: {path}", err=True)
            raise typer.Exit(2)
    return [UploadFile(path.name, path.read_bytes()) for path in paths]


def _print_sessions(service: BriefService) -> None:
    rows = service.list_sessions()
    if not rows:
        typer.echo("Keine Sitzungen.")
    for row in rows:
        typer.echo(
            f"{row.session_id}  {row.status:<18} {row.interview_language}  {row.created_at[:19]}"
        )


def _brief_io() -> ConsoleIO:
    return ConsoleIO(
        ask=lambda prompt: typer.prompt(prompt, default="", show_default=False),
        say=typer.echo,
        edit=lambda text: click.edit(text, extension=".md"),
    )


@app.command()
def brief(
    question: Annotated[
        str | None, typer.Argument(help="Die Forschungsfrage (sonst wird sie abgefragt).")
    ] = None,
    file: Annotated[
        list[Path] | None,
        typer.Option(
            "--file", "-f", help="Datei als Kontext (PDF, DOCX, MD, TXT); mehrfach möglich."
        ),
    ] = None,
    session: Annotated[
        str | None,
        typer.Option("--session", help="Eine offene oder gespeicherte Sitzung fortsetzen."),
    ] = None,
    list_sessions: Annotated[bool, typer.Option("--list", help="Die Sitzungen auflisten.")] = False,
) -> None:
    """Klärt die Forschungsfrage im Dialog und gibt den Brief frei (Phase 1, ohne Internet)."""
    if session and (question or file or list_sessions):
        typer.echo(
            "--session kann nicht mit einer Frage, Dateien oder --list kombiniert werden.", err=True
        )
        raise typer.Exit(2)
    uploads = _read_uploads(file or [])  # before any model is touched
    service = _brief_service()
    if list_sessions:
        _print_sessions(service)
        return
    if session:
        try:
            service.get(session)
        except NotFound as exc:
            typer.echo(f"Sitzung nicht gefunden: {exc}", err=True)
            raise typer.Exit(1) from exc
        session_id = session
    else:
        text = question if question is not None else typer.prompt("Was möchten Sie recherchieren?")
        if not text.strip():
            typer.echo("Die Frage darf nicht leer sein.", err=True)
            raise typer.Exit(2)
        try:
            session_id = service.start(text, uploads).session_id
        except InvalidInput as exc:
            typer.echo(f"Das ging nicht: {exc}", err=True)
            raise typer.Exit(2) from exc
    try:
        run_session(service, _brief_io(), session_id)
    except typer.Abort as exc:
        typer.echo(f"\nAbgebrochen. Weiter mit: udr brief --session {session_id}", err=True)
        raise typer.Exit(1) from exc


def _research_service() -> ResearchService:
    """The Phase-2 service on the real runtime."""
    settings = bootstrap.load_settings()
    events = JsonlEventSink(settings.data_dir / "events.jsonl")
    return bootstrap.build_research_service(bootstrap.build_runtime(settings, events))


def _fail(message: str, code: int) -> typer.Exit:
    typer.echo(message, err=True)
    return typer.Exit(code)


def _report(view: RunView) -> None:
    """Say where the run stands and exit with the matching code."""
    again = f"Weiter mit: udr run {view.run_id}"
    if view.status == "failed":
        raise _fail(f"Fehlgeschlagen im Schritt {view.step}: {view.error}\n{again}", 1)
    if view.status == "blocked":
        checks = ", ".join(view.gate_failed)
        raise _fail(f"Ship-Gate nicht bestanden: {checks}. Bericht: {view.report_path}", 1)
    if view.status == "done":
        exports = ", ".join(f"{k}: {v}" for k, v in view.exports.items())
        typer.echo(f"Fertig: {view.report_path} ({exports})")
    elif view.waiting_for == "plan":
        typer.echo(f"Der Plan wartet auf die Freigabe. {again}")
    else:
        typer.echo(f"Status: {view.status}. {again}")


def _show_plan_and_stop(view: RunView) -> None:
    """`--no-input`: print the plan and the hash an approval needs."""
    assert view.plan is not None
    typer.echo("\n".join(render_plan(view.plan)))
    typer.echo(f"\nFreigabe: udr run {view.run_id} --approve-plan {view.plan_sha256}")


def _advance(
    service: ResearchService, run_id: str, approve_plan: str | None, no_input: bool
) -> RunView:
    """Start or continue a run and take it through the plan gate as far as the options allow."""
    view = service.view(run_id)
    if view.status in ("queued", "failed", "running"):
        view = service.run(run_id)
    if view.status != "awaiting_plan_approval":
        return view
    if approve_plan:
        return service.approve_and_run(run_id, approve_plan)
    if no_input:
        _show_plan_and_stop(view)
        return view
    if run_plan_review(service, _brief_io(), run_id) == "quit":
        raise typer.Exit(0)
    return service.view(run_id)


def _create_external(
    service: ResearchService,
    brief_file: Path,
    tier: str | None,
    template: str | None,
    language: str | None,
    response_format: str | None,
) -> str:
    """The run of an external brief file; running it from the owner's shell is the approval."""
    if tier == "full":
        raise _fail("Full-Tier ab M8.", 2)
    if tier != "light" or not template:
        raise _fail("--brief braucht --tier light und --template.", 2)
    if not brief_file.is_file():
        raise _fail(f"Datei nicht gefunden: {brief_file}", 2)
    view = service.create_external_run(
        brief_file.read_text(encoding="utf-8"),
        tier=tier,
        template_id=template,
        language=language,
        response_format=response_format,
    )
    typer.echo(f"Lauf angelegt: {view.run_id}")
    return view.run_id


@app.command("run")
def run_cmd(
    run_id: Annotated[str | None, typer.Argument(help="Ein freigegebener Lauf.")] = None,
    brief: Annotated[Path | None, typer.Option("--brief", help="Brief aus einer Datei.")] = None,
    tier: Annotated[str | None, typer.Option("--tier", help="light (full ab M8).")] = None,
    template: Annotated[str | None, typer.Option("--template", help="Vorlagen-ID.")] = None,
    language: Annotated[str | None, typer.Option("--language", help="Berichtssprache.")] = None,
    response_format: Annotated[str | None, typer.Option("--format", help="Antwortformat.")] = None,
    approve_plan: Annotated[
        str | None, typer.Option("--approve-plan", help="Plan mit diesem Hash freigeben.")
    ] = None,
    no_input: Annotated[bool, typer.Option("--no-input", help="Keine Rückfragen.")] = False,
) -> None:
    """Startet oder setzt einen Recherchelauf fort (Phase 2)."""
    if bool(run_id) == bool(brief):
        raise _fail("Entweder eine Lauf-ID oder --brief angeben.", 2)
    service = _research_service()
    try:
        if brief:
            run_id = _create_external(service, brief, tier, template, language, response_format)
        assert run_id is not None
        view = _advance(service, run_id, approve_plan, no_input)
    except WorkerBusy as exc:
        raise _fail("Ein anderer Lauf ist aktiv.", 1) from exc
    except (NotFound, WrongState) as exc:
        raise _fail(f"Das ging nicht: {exc}", 1) from exc
    except (InvalidInput, TierNotAvailable, StalePlan, PlanBlocked, EmptyPlan) as exc:
        raise _fail(f"Das ging nicht: {exc}", 2) from exc
    _report(view)


def _worker() -> Worker:
    """The queue worker on the real runtime; it logs one line per run to the terminal."""
    settings = bootstrap.load_settings()
    events = JsonlEventSink(settings.data_dir / "events.jsonl")
    rt = bootstrap.build_runtime(settings, events)
    return bootstrap.build_worker(rt, bootstrap.build_research_service(rt), log=typer.echo)


def _stop_on_signal(stop: threading.Event) -> Callable[[int, FrameType | None], None]:
    """A signal handler that ends the worker. It leaves the loop at once, even inside a run: the
    run stays `running` and the next start resumes it from its last checkpoint."""

    def handler(signum: int, frame: FrameType | None) -> None:
        stop.set()
        raise SystemExit(0)

    return handler


@app.command("worker")
def worker_cmd() -> None:
    """Führt freigegebene Recherche-Läufe aus der Warteschlange aus, einen nach dem anderen."""
    worker = _worker()
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, _stop_on_signal(stop))
    typer.echo("Worker bereit.")
    worker.run_forever(stop)


@app.command("serve")
def serve_cmd() -> None:
    """Startet die API (REST unter /v1, MCP unter /mcp) auf 127.0.0.1."""
    settings = bootstrap.load_settings()
    typer.echo(f"API auf http://127.0.0.1:{settings.api_port}")
    server.serve(settings)


GUI_APP = Path(__file__).resolve().parent / "gui" / "app.py"


def gui_command(port: int) -> list[str]:
    """Streamlit on loopback, headless, with no usage statistics sent anywhere."""
    return [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(GUI_APP),
        "--server.address",
        "127.0.0.1",
        "--server.port",
        str(port),
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
    ]


@app.command("gui")
def gui_cmd() -> None:
    """Startet die Oberfläche (Streamlit) auf 127.0.0.1; die API muss laufen."""
    settings = bootstrap.load_settings()
    if settings.gui_api_key is None:
        raise _fail("UDR_GUI_API_KEY fehlt (Schlüssel mit `udr apikey create --self-approve`).", 1)
    typer.echo(f"Oberfläche auf http://127.0.0.1:{settings.gui_port}")
    subprocess.run(gui_command(settings.gui_port), check=False)


apikey_app = typer.Typer(help="API-Schlüssel für REST und MCP.", no_args_is_help=True)
app.add_typer(apikey_app, name="apikey")


def _key_store() -> KeyStore:
    settings = bootstrap.load_settings()
    return KeyStore(Database(settings.data_dir / bootstrap.VAULT_FILE))


@apikey_app.command("create")
def apikey_create(
    name: Annotated[str, typer.Option("--name", help="Wofür der Schlüssel dient.")],
    self_approve: Annotated[
        bool, typer.Option("--self-approve", help="Darf Briefs und Pläne freigeben.")
    ] = False,
) -> None:
    """Legt einen Schlüssel an und zeigt ihn einmal."""
    key, text = _key_store().create(name, self_approve=self_approve)
    typer.echo(f"Schlüssel {key.key_id} ({name}):\n\n  {text}\n")
    typer.echo("Er wird nicht noch einmal angezeigt; gespeichert wird nur sein Hash.")


@apikey_app.command("list")
def apikey_list() -> None:
    """Zeigt Id, Name, Freigaberecht und Daten der Schlüssel, nie einen Schlüssel selbst."""
    for key in _key_store().list():
        right = "darf freigeben" if key.self_approve else "ohne Freigabe"
        state = f"widerrufen {key.revoked_at}" if key.revoked_at else "aktiv"
        typer.echo(f"{key.key_id}  {key.name}  {right}  angelegt {key.created_at}  {state}")


@apikey_app.command("revoke")
def apikey_revoke(
    key_id: Annotated[str, typer.Argument(help="Die Id aus `udr apikey list`.")],
) -> None:
    """Widerruft einen Schlüssel; er wird danach mit 401 abgewiesen."""
    try:
        _key_store().revoke(key_id)
    except NotFound as exc:
        raise _fail(f"Unbekannter Schlüssel: {exc}", 1) from exc
    typer.echo(f"Widerrufen: {key_id}")


denylist_app = typer.Typer(help="Terms that must never leave this machine (PRD §3.2).")
app.add_typer(denylist_app, name="denylist")

TermsArg = Annotated[list[str], typer.Argument(help="One or more terms.")]


def _denylist_path() -> Path:
    return bootstrap.load_settings().data_dir / DENYLIST_FILE


@denylist_app.command("list")
def denylist_list() -> None:
    """Print the terms, one per line."""
    for term in Denylist.load(_denylist_path()).terms:
        typer.echo(term)


@denylist_app.command("add")
def denylist_add(terms: TermsArg) -> None:
    """Add terms. Variants of an existing term (case, accents, separators) are already covered."""
    path = _denylist_path()
    deny = Denylist.load(path)
    try:
        added = [term for term in terms if deny.add(term)]
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    for term in terms:
        typer.echo(f"added: {term}" if term in added else f"already covered: {term}", err=True)
    deny.save(path)


@denylist_app.command("remove")
def denylist_remove(terms: TermsArg) -> None:
    """Remove terms. Exit code 1 if any was not on the list."""
    path = _denylist_path()
    deny = Denylist.load(path)
    missing = [term for term in terms if not deny.remove(term)]
    for term in terms:
        typer.echo(f"not found: {term}" if term in missing else f"removed: {term}", err=True)
    deny.save(path)
    raise typer.Exit(1 if missing else 0)


def main() -> None:
    app()
