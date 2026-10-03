"""Ship-gate fixes that need a model (PRD §3.10, D11): shorten or extend a section, add missing
citations, reword pipeline vocabulary. A model proposes, code checks, and a proposal that breaks a
rule is rejected loudly (a `fix_rejected` event) instead of being applied.
"""

from collections.abc import Mapping
from pathlib import Path

from app.brief.labels import language_name
from app.events import EventSink
from app.llm.errors import LLMOutputError, LLMTruncatedError, PromptTooLargeError
from app.llm.service import LLMService
from app.llm.types import Message, Role
from app.pipeline.profiles import RunRules
from app.prompts import research as prompts
from app.research.draft import DraftPlan, clean_section, pack_budget
from app.research.evidence import EvidenceKeys, PackBuilder, section_query
from app.research.fixes_code import citations_only_change, section_words
from app.research.hunks import Hunk, apply_hunk, check_hunk
from app.research.leakage import leak_sentences, leaks
from app.research.models import CitationProposal, LeakProposal, PolishProposal
from app.research.report import cited_keys
from app.research.sections import load_section, save_section

_SKIPPABLE = (LLMOutputError, LLMTruncatedError, PromptTooLargeError)


class ModelFixes:
    def __init__(
        self,
        service: LLMService,
        packs: PackBuilder,
        keys: EvidenceKeys,
        rules: RunRules,
        events: EventSink,
        *,
        prompt_chars: int,
        condense_chars: int,
    ) -> None:
        self._service = service
        self._packs = packs
        self._keys = keys
        self._rules = rules
        self._events = events
        self._prompt_chars = prompt_chars
        self._condense_chars = condense_chars

    def _rejected(self, fix: str, index: int, reason: str) -> None:
        self._events.emit("fix_rejected", level="warning", fix=fix, section=index, reason=reason)

    def _text(self, system: str, user: str, fix: str, index: int) -> str | None:
        messages: list[Message] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        try:
            return clean_section(self._service.text(Role.REASON, messages))
        except _SKIPPABLE as exc:
            self._rejected(fix, index, type(exc).__name__)
            return None

    # ---- length (G3) ----------------------------------------------------------------------

    def compress(self, run_dir: Path, index: int, heading: str, words: int) -> bool:
        """Shorten section ``index`` to about ``words`` words; True if its text was replaced."""
        text = load_section(run_dir, index) or ""
        user = prompts.COMPRESS_USER.format(words=words, heading=heading, text=text)
        new = self._text(prompts.COMPRESS_SYSTEM, user, "compress", index)
        if not new:
            return False
        if section_words(new) >= section_words(text):
            self._rejected("compress", index, "not_shorter")
        elif not set(cited_keys(new)) <= set(cited_keys(text)):
            self._rejected("compress", index, "new_citation")
        else:
            save_section(run_dir, index, new)
            return True
        return False

    def expand(self, run_dir: Path, plan: DraftPlan, index: int, words: int) -> bool:
        """Extend section ``index`` towards ``words`` words from evidence; True if replaced."""
        section = plan.sections[index - 1]
        text = load_section(run_dir, index) or ""
        user = prompts.EXPAND_USER.format(
            language=language_name(plan.language, "en"),
            shim=plan.shim.strip(),
            heading=section.heading,
            instructions=section.instructions or prompts.NO_INSTRUCTIONS,
            text=text,
            words=words,
            pack="{pack}",
        )
        budget = pack_budget(self._prompt_chars, self._rules, len(prompts.DRAFT_SYSTEM) + len(user))
        pack = self._packs.build(
            section=section.heading,
            query=section_query(section.heading, section.instructions, plan.questions),
            must_read=plan.must_read,
            budget_chars=budget,
            condense_chars=self._condense_chars,
        )
        if not pack.text:
            self._rejected("expand", index, "no_evidence")
            return False
        new = self._text(prompts.DRAFT_SYSTEM, user.replace("{pack}", pack.text), "expand", index)
        if not new:
            return False
        if section_words(new) <= section_words(text):
            self._rejected("expand", index, "not_longer")
        elif not set(cited_keys(new)) <= set(self._keys.mapping()):
            self._rejected("expand", index, "unknown_citation")
        else:
            save_section(run_dir, index, new)
            return True
        return False

    # ---- citations (G4) -------------------------------------------------------------------

    def add_citations(
        self, run_dir: Path, index: int, heading: str, sources: Mapping[str, str]
    ) -> int:
        """Insert evidence keys the model proposes, as far as they only add known keys; returns
        how many hunks were applied. ``sources`` maps each usable key to `source: summary`."""
        text = load_section(run_dir, index) or ""
        known = set(sources)
        listed = "\n".join(f"{key}: {line}" for key, line in sources.items())
        system = prompts.CITE_SYSTEM.format(max_chars=self._rules.hunk_max_chars)
        user = prompts.CITE_USER.format(heading=heading, keys=listed, text=text)
        proposal = self._hunks(CitationProposal, system, user, "cite", index)
        applied = 0
        for hunk in proposal:
            reason = check_hunk(text, hunk, self._rules.hunk_max_chars)
            if reason is None and not citations_only_change(hunk.old, hunk.new, known):
                reason = "not_only_citations"
            if reason is None:
                text = apply_hunk(text, hunk)
                applied += 1
            else:
                self._rejected("cite", index, reason)
        if applied:
            save_section(run_dir, index, text)
        return applied

    # ---- vocabulary (G7) ------------------------------------------------------------------

    def reword_leaks(self, run_dir: Path, index: int, heading: str) -> int:
        """Reword (or delete) the sentences with pipeline vocabulary; returns how many hunks were
        applied. A reworded sentence may not contain the vocabulary or a new citation key."""
        text = load_section(run_dir, index) or ""
        sentences = leak_sentences(text)
        if not sentences:
            return 0
        user = prompts.LEAK_USER.format(
            heading=heading, sentences="\n".join(f"- {s}" for s in sentences), text=text
        )
        proposal = self._hunks(LeakProposal, prompts.LEAK_SYSTEM, user, "leak", index)
        applied = 0
        for hunk in proposal:
            reason = check_hunk(text, hunk, self._rules.hunk_max_chars)
            if reason is None and leaks(hunk.new):
                reason = "still_leaks"
            elif reason is None and not set(cited_keys(hunk.new)) <= set(cited_keys(hunk.old)):
                reason = "new_citation"
            if reason is None:
                text = _replace_once(text, hunk)
                applied += 1
            else:
                self._rejected("leak", index, reason)
        if applied:
            save_section(run_dir, index, text)
        return applied

    def _hunks(
        self, schema: type[PolishProposal], system: str, user: str, fix: str, index: int
    ) -> list[Hunk]:
        messages: list[Message] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        try:
            proposal = self._service.structured(Role.REASON, messages, schema)
        except _SKIPPABLE as exc:
            self._rejected(fix, index, type(exc).__name__)
            return []
        return [Hunk(h.old, h.new) for h in proposal.hunks]


def _replace_once(text: str, hunk: Hunk) -> str:
    """Apply ``hunk``; deleting a sentence also deletes the space after it."""
    if not hunk.new and text.count(hunk.old + " ") == 1:
        return text.replace(hunk.old + " ", "", 1)
    return apply_hunk(text, hunk)
