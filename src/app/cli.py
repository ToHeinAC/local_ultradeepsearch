"""The `udr` command line. Thin: it wires `bootstrap`, calls pure logic and prints."""

from pathlib import Path
from typing import Annotated

import typer

from app import bootstrap
from app.adapters.ollama_instance import InstanceState
from app.adapters.outbound.denylist import Denylist
from app.bootstrap import DENYLIST_FILE
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
