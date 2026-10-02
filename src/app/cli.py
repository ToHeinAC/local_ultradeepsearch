"""The `udr` command line. Thin: it wires `bootstrap`, calls pure logic and prints."""

from pathlib import Path
from typing import Annotated

import click
import typer

from app import bootstrap
from app.adapters.ollama_instance import InstanceState
from app.adapters.outbound.denylist import Denylist
from app.bootstrap import DENYLIST_FILE
from app.brief.console import ConsoleIO, run_session
from app.brief.errors import InvalidInput, NotFound
from app.brief.service import BriefService
from app.brief.uploads import UploadFile
from app.calibration import CANDIDATES, CalibrationError, save_calibration
from app.doctor import evaluate, exit_code, render, render_json
from app.events import JsonlEventSink
from app.llm.errors import LLMError
from app.llm.types import Role

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
