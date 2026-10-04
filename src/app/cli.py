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
from app.brief.errors import InvalidInput, NotFound, WrongState
from app.brief.service import BriefService
from app.brief.uploads import UploadFile
from app.calibration import CANDIDATES, CalibrationError, save_calibration
from app.doctor import evaluate, exit_code, render, render_json
from app.events import JsonlEventSink
from app.llm.errors import LLMError
from app.llm.types import Role
from app.research.console import run_plan_review
from app.research.models import RunView
from app.research.service import PreparedBrief, ResearchService

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


def _print_runs(service: ResearchService) -> None:
    rows = service.list_runs()
    if not rows:
        typer.echo("Keine Läufe.")
    for row in rows:
        typer.echo(
            f"{row.run_id}  {row.status:<22} {row.tier or '-':<6} {row.template_id or '-'}  "
            f"{row.created_at[:19]}"
        )


def _fail(message: str, code: int) -> typer.Exit:
    typer.echo(message, err=True)
    return typer.Exit(code)


def _confirm(io: ConsoleIO, prepared: PreparedBrief, yes: bool, no_input: bool) -> bool:
    """Show the brief that would be archived and ask for the go-ahead (`--yes` skips it)."""
    typer.echo(prepared.text.rstrip("\n"))
    typer.echo(f"\nsha256: {prepared.sha256}")
    if yes:
        return True
    if no_input:
        raise _fail("Mit --no-input ist --yes nötig, um den Brief freizugeben.", 2)
    return io.ask("Freigeben? [j/N]").strip().lower() in ("j", "ja", "y", "yes")


def _create_external(
    service: ResearchService,
    brief_file: Path,
    options: tuple[str | None, ...],
    flags: tuple[bool, bool],
) -> str | None:
    """A run from an external brief file; None if the owner did not approve it."""
    tier, template, fmt, language = options
    if tier == "full":
        raise _fail("Full-Tier ab M8.", 2)
    if tier != "light" or not template:
        raise _fail("--brief braucht --tier light und --template.", 2)
    if not brief_file.is_file():
        raise _fail(f"Datei nicht gefunden: {brief_file}", 2)
    text = brief_file.read_text(encoding="utf-8")
    try:
        prepared = service.prepare_external(
            text, template_id=template, response_format=fmt, report_language=language
        )
        if not _confirm(_brief_io(), prepared, *flags):
            typer.echo("Nicht freigegeben; es wurde kein Lauf angelegt.")
            return None
        row = service.create_external_run(
            prepared.text,
            tier=tier,
            template_id=prepared.template_id,
            response_format=prepared.response_format,
            report_language=prepared.report_language,
        )
    except InvalidInput as exc:
        raise _fail(f"Das ging nicht: {exc}", 2) from exc
    typer.echo(f"Lauf angelegt: {row.run_id}")
    return row.run_id


def _report(view: RunView) -> None:
    """Print where the run stands and exit with the matching code."""
    again = f"Weiter mit: udr run {view.run_id}"
    if view.status == "failed":
        raise _fail(f"Fehlgeschlagen: {view.reason}\n{again}", 1)
    if view.status == "done":
        typer.echo(f"Fertig: {view.run_dir}")
    elif view.waiting_for == "plan":
        typer.echo(f"Der Plan wartet auf die Freigabe. {again}")
    else:
        typer.echo(f"Status: {view.status}. {again}")


def _drive_run(service: ResearchService, run_id: str, no_input: bool) -> None:
    """Start or continue a run, review its plan, and report."""
    try:
        view = service.get(run_id)
        if view.status in ("queued", "failed"):
            view = service.start(run_id)
        elif view.status != "done":
            view = service.resume(run_id)
        if view.waiting_for == "plan" and not no_input:
            if run_plan_review(service, _brief_io(), run_id) == "quit":
                raise typer.Exit(0)
            view = service.get(run_id)
    except (NotFound, WrongState) as exc:
        raise _fail(f"Das ging nicht: {exc}", 1) from exc
    _report(view)


@app.command("run")
def run_cmd(
    run_id: Annotated[str | None, typer.Argument(help="Ein freigegebener Lauf.")] = None,
    brief: Annotated[Path | None, typer.Option("--brief", help="Brief aus einer Datei.")] = None,
    tier: Annotated[str | None, typer.Option("--tier", help="light (full ab M8).")] = None,
    template: Annotated[str | None, typer.Option("--template", help="Vorlagen-ID.")] = None,
    response_format: Annotated[str | None, typer.Option("--format")] = None,
    language: Annotated[str | None, typer.Option("--language")] = None,
    yes: Annotated[bool, typer.Option("--yes", help="Brief ohne Rückfrage freigeben.")] = False,
    no_input: Annotated[bool, typer.Option("--no-input", help="Keine Rückfragen.")] = False,
    list_runs: Annotated[bool, typer.Option("--list", help="Die Läufe auflisten.")] = False,
) -> None:
    """Startet oder setzt einen Recherchelauf fort (Phase 2)."""
    others = (run_id, brief, tier, template, response_format, language)
    if list_runs and any(others):
        raise _fail("--list kann nicht mit anderen Angaben kombiniert werden.", 2)
    if run_id and brief:
        raise _fail("Lauf-ID und --brief schließen sich aus.", 2)
    if not (list_runs or run_id or brief):
        raise _fail("Lauf-ID, --brief oder --list angeben.", 2)
    service = _research_service()
    if list_runs:
        _print_runs(service)
        return
    if brief:
        options = (tier, template, response_format, language)
        run_id = _create_external(service, brief, options, (yes, no_input))
        if run_id is None:
            return
    assert run_id is not None
    _drive_run(service, run_id, no_input)


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
