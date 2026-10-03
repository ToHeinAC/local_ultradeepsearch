"""Step 16 (PRD M5, D10): readability. The `summarize` role recommends layout changes per section;
code applies only the allowed categories, in the original order, and logs every decision.

`readability-recommendations.json` holds what the model recommended, `readability-decisions.json`
what code did with it. Progress is saved per section.
"""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from app.artifacts import write_json
from app.events import EventSink
from app.llm.errors import LLMOutputError, LLMTruncatedError, PromptTooLargeError
from app.llm.service import LLMService
from app.llm.types import Message, Role
from app.pipeline.profiles import ResearchBudget, RunRules
from app.prompts import research as prompts
from app.research.hunks import Recommendation, apply_recommendations
from app.research.models import ReadabilityProposal
from app.research.sections import load_section, save_section

RECOMMENDATIONS = "readability-recommendations.json"
DECISIONS = "readability-decisions.json"
_SKIPPABLE = (LLMOutputError, LLMTruncatedError, PromptTooLargeError)


def _load(path: Path, default: Any) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _empty_decisions() -> dict[str, Any]:
    return {
        "total_recommendations": 0,
        "applied": [],
        "skipped": [],
        "edit_failures": [],
        "net_char_delta_actual": 0,
        "sections_done": [],
    }


class ReadabilityAuditor:
    def __init__(
        self,
        service: LLMService,
        rules: RunRules,
        budget: ResearchBudget,
        events: EventSink,
    ) -> None:
        self._service = service
        self._rules = rules
        self._budget = budget
        self._events = events

    def audit_all(self, run_dir: Path, *, headings: Sequence[str]) -> None:
        """Audit every section not audited yet and keep both files current."""
        decisions = _load(run_dir / DECISIONS, _empty_decisions())
        done: list[int] = decisions["sections_done"]
        recs: list[dict[str, Any]] = [
            r for r in _load(run_dir / RECOMMENDATIONS, []) if r["section"] in done
        ]
        write_json(run_dir / RECOMMENDATIONS, recs)
        write_json(run_dir / DECISIONS, decisions)
        for index, heading in enumerate(headings, 1):
            if index in done:
                continue
            text = load_section(run_dir, index)
            if text:
                self._section(run_dir, decisions, recs, index, heading, text)
            done.append(index)
            write_json(run_dir / RECOMMENDATIONS, recs)
            write_json(run_dir / DECISIONS, decisions)

    def _ask(self, heading: str, text: str, room: int) -> ReadabilityProposal | None:
        system = prompts.READABILITY_SYSTEM.format(max_chars=self._rules.hunk_max_chars, cap=room)
        messages: list[Message] = [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": prompts.READABILITY_USER.format(heading=heading, text=text),
            },
        ]
        try:
            return self._service.structured(Role.SUMMARIZE, messages, ReadabilityProposal)
        except _SKIPPABLE as exc:
            self._events.emit(
                "readability_skipped", level="warning", heading=heading, error=str(exc)
            )
            return None

    def _section(
        self,
        run_dir: Path,
        decisions: dict[str, Any],
        recs: list[dict[str, Any]],
        index: int,
        heading: str,
        text: str,
    ) -> None:
        room = self._budget.readability_cap - len(recs)
        if room <= 0:
            self._events.emit("readability_cap_reached", cap=self._budget.readability_cap)
            return
        proposal = self._ask(heading, text, room)
        if proposal is None:
            return
        fresh = [
            Recommendation(
                f"r{len(recs) + n:02d}", r.category, r.current, r.recommended, r.rationale
            )
            for n, r in enumerate(proposal.recommendations[:room], 1)
        ]
        recs += [
            {
                "id": r.id,
                "section": index,
                "category": r.category,
                "current": r.current,
                "recommended": r.recommended,
                "rationale": r.rationale,
            }
            for r in fresh
        ]
        outcome = apply_recommendations(text, fresh, self._rules.hunk_max_chars)
        if outcome.text != text:
            save_section(run_dir, index, outcome.text)
        decisions["total_recommendations"] = len(recs)
        decisions["net_char_delta_actual"] += outcome.net_chars
        for decision in outcome.decisions:
            if decision.status == "applied":
                decisions["applied"].append(decision.id)
            else:
                key = "edit_failures" if decision.status == "edit_failure" else "skipped"
                decisions[key].append({"id": decision.id, "reason": decision.reason})
