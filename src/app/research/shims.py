"""The four role shims of a run (upstream `levers.compose_shims`, ported byte for byte).
Composition is additive: one register block, one domain block, one inference block per file."""

from pathlib import Path

from app.artifacts import write_text
from app.prompts.shims import (
    INFERENCE_CRITICS,
    INFERENCE_DRAFTING,
    INFERENCE_RESEARCH,
    REGISTER_CRITICS,
    REGISTER_DRAFTING,
    REGISTER_POLISH,
    SHIM_HEADER,
)
from app.research.schemas import Levers

ROLES = ("research", "drafting", "critics", "polish")


def _domain_block(notes: str) -> str:
    return f"\n### Domain notes\n\n{notes.strip()}\n" if notes.strip() else ""


def compose_shims(levers: Levers) -> dict[str, str]:
    reg, depth = levers.register_, levers.inference_depth
    header = SHIM_HEADER.format(register=reg, inference_depth=depth)
    domain = _domain_block(levers.domain_notes)
    return {
        "research": header + domain + f"\n### Inference depth\n\n{INFERENCE_RESEARCH[depth]}\n",
        "drafting": header
        + f"\n### Register posture\n\n{REGISTER_DRAFTING[reg]}\n"
        + domain
        + f"\n### Inference depth\n\n{INFERENCE_DRAFTING[depth]}\n",
        "critics": header
        + f"\n### Register posture\n\n{REGISTER_CRITICS[reg]}\n"
        + f"\n### Inference depth\n\n{INFERENCE_CRITICS[depth]}\n",
        "polish": header + f"\n### Register posture\n\n{REGISTER_POLISH[reg]}\n",
    }


def write_shims(run_dir: Path, shims: dict[str, str]) -> None:
    for role in ROLES:
        write_text(run_dir / "shims" / f"{role}.md", shims[role], scrub=False)


def read_shim(run_dir: Path, role: str) -> str:
    return (run_dir / "shims" / f"{role}.md").read_text(encoding="utf-8")
