"""Step 15 (PRD M5, AD5): polish. A model proposes cut-only hunks per section; code decides.

`polish-log.json` records what was applied, what was rejected and why, what the model could not
fix by cutting, and the net change in characters. Progress is saved per section, so a resumed run
continues with the first section not done; polishing an already polished section only cuts more.
"""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from app.artifacts import write_json
from app.brief.labels import language_name
from app.events import EventSink
from app.llm.errors import LLMOutputError, LLMTruncatedError, PromptTooLargeError
from app.llm.service import LLMService
from app.llm.types import Message, Role
from app.pipeline.profiles import RunRules
from app.prompts import research as prompts
from app.research.hunks import Hunk, apply_polish
from app.research.models import PolishProposal
from app.research.sections import load_section, save_section

POLISH_LOG = "polish-log.json"
# What a model can get wrong without the run being at fault: the section is skipped, not retried.
_SKIPPABLE = (LLMOutputError, LLMTruncatedError, PromptTooLargeError)


def _empty_log() -> dict[str, Any]:
    return {
        "applied": [],
        "rejected": [],
        "skipped": [],
        "escalations": [],
        "net_chars": 0,
        "sections_done": [],
    }


def load_log(run_dir: Path) -> dict[str, Any]:
    path = run_dir / POLISH_LOG
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else _empty_log()


class Polisher:
    def __init__(self, service: LLMService, rules: RunRules, events: EventSink) -> None:
        self._service = service
        self._rules = rules
        self._events = events

    def polish_all(
        self, run_dir: Path, *, headings: Sequence[str], language: str, shim: str
    ) -> None:
        """Polish every section not polished yet and keep `polish-log.json` current."""
        log = load_log(run_dir)
        write_json(run_dir / POLISH_LOG, log)
        for index, heading in enumerate(headings, 1):
            if index in log["sections_done"]:
                continue
            text = load_section(run_dir, index)
            if text:
                self._section(run_dir, log, index, heading, text, language, shim)
            log["sections_done"].append(index)
            write_json(run_dir / POLISH_LOG, log)

    def _section(
        self,
        run_dir: Path,
        log: dict[str, Any],
        index: int,
        heading: str,
        text: str,
        language: str,
        shim: str,
    ) -> None:
        system = prompts.POLISH_SYSTEM.format(max_chars=self._rules.hunk_max_chars)
        user = prompts.POLISH_USER.format(
            language=language_name(language, "en"), shim=shim.strip(), heading=heading, text=text
        )
        messages: list[Message] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        try:
            proposal = self._service.structured(Role.REASON, messages, PolishProposal)
        except _SKIPPABLE as exc:
            self._events.emit("polish_skipped", level="warning", section=index, error=str(exc))
            log["skipped"].append({"section": index, "reason": type(exc).__name__})
            return
        hunks = [Hunk(h.old, h.new) for h in proposal.hunks]
        outcome = apply_polish(text, hunks, self._rules.hunk_max_chars)
        if outcome.text != text:
            save_section(run_dir, index, outcome.text)
        log["applied"] += [{"section": index, "old": h.old, "new": h.new} for h in outcome.applied]
        log["rejected"] += [
            {"section": index, "old": r.hunk.old, "new": r.hunk.new, "reason": r.reason}
            for r in outcome.rejected
        ]
        log["escalations"] += [{"section": index, "text": e} for e in proposal.escalations]
        log["net_chars"] += outcome.net_chars
