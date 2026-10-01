"""Read-only questions about this machine: GPUs, free disk, binaries, the Ollama model store."""

import shutil
import subprocess
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

Runner = Callable[[Sequence[str]], str | None]

DEFAULT_STORES = (
    Path.home() / ".ollama" / "models",
    Path("/usr/share/ollama/.ollama/models"),
    Path("/var/lib/ollama/.ollama/models"),
)


@dataclass(frozen=True)
class Gpu:
    index: int
    name: str
    total_mib: int
    used_mib: int


def run_command(argv: Sequence[str], timeout_s: float = 5.0) -> str | None:
    """stdout of ``argv``, or None if it cannot run, fails or times out."""
    try:
        proc = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout_s)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def parse_gpus(csv_text: str) -> list[Gpu]:
    """Parse `nvidia-smi --format=csv,noheader,nounits` rows: index, name, total, used."""
    found: list[Gpu] = []
    for line in csv_text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 4:
            continue
        try:
            found.append(Gpu(int(parts[0]), parts[1], int(parts[2]), int(parts[3])))
        except ValueError:
            continue
    return found


def list_gpus(run: Runner = run_command) -> list[Gpu] | None:
    """The NVIDIA cards, or None when nvidia-smi is unavailable (no driver, no card)."""
    out = run(
        [
            "nvidia-smi",
            "--query-gpu=index,name,memory.total,memory.used",
            "--format=csv,noheader,nounits",
        ]
    )
    return None if out is None else parse_gpus(out)


def free_disk_bytes(path: Path) -> int:
    """Free bytes on the filesystem that holds (or would hold) ``path``."""
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


def find_binary(name: str) -> str | None:
    return shutil.which(name)


def _manifest_count(store: Path) -> int:
    manifests = store / "manifests"
    if not manifests.is_dir():
        return 0
    try:
        return sum(1 for p in manifests.rglob("*") if p.is_file())
    except OSError:
        return 0


def find_models_dir(
    preferred: Path | None, candidates: Iterable[Path] = DEFAULT_STORES
) -> Path | None:
    """The explicitly configured store, else the candidate holding the most manifests.

    Not "the first that exists": `~/.ollama/models` can exist but be empty while every model
    lives in the system daemon's store, so picking by existence would start a daemon that sees
    no models.
    """
    if preferred is not None and _manifest_count(preferred) > 0:
        return preferred
    best, best_count = None, 0
    for candidate in candidates:
        count = _manifest_count(candidate)
        if count > best_count:
            best, best_count = candidate, count
    return best
