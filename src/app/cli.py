"""The `udr` command line. Thin: it wires `bootstrap`, calls pure logic and prints."""

from typing import Annotated

import typer

from app import bootstrap
from app.adapters.ollama_instance import InstanceState
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


def main() -> None:
    app()
