"""The ship gate G1-G12 (PRD §3.10). Pure: it judges a rendered report against the approved brief
and the run's notes, and says which checks fail and why. Fixing is `fixes.py`'s job.
"""

import hashlib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from app.brief.labels import sources_heading
from app.pipeline.profiles import RunRules
from app.pipeline.urls import dedup_key
from app.research.language import detect_code, language_samples
from app.research.markdown import (
    appendix_fence,
    body_of,
    body_words,
    citation_numbers,
    h2_list,
    outside_fences,
    sources_entries,
)
from app.research.quotes import quote_failures
from app.research.report import appendix_heading
from app.text import normalize_for_match

LIGHT_ARTIFACTS = frozenset({"polish-log.json", "readability-decisions.json"})
FULL_ARTIFACTS = LIGHT_ARTIFACTS | {
    "critic-findings-dialectic.json",
    "critic-findings-depth.json",
    "critic-findings-width.json",
    "critic-findings-instruction.json",
    "patch-log.json",
    "cite-check-pairs.json",
    "cite-check-findings.json",
    "cite-check-patch-log.json",
}
LEAKAGE = (
    r"\bLocus \d+",
    r"\bTension \d+",
    r"comparisons\.md",
    r"\binterim\b",
    r"cross-locus",
    r"scaffold",
    r"hyperresearch",
    r"\[\[",
    r"<think>",
)
SCAFFOLD_HEADERS = (
    "user prompt",
    "run config",
    "modality",
    "tier rationale",
    "wrapper requirements",
)
_LEAK = re.compile("|".join(LEAKAGE), re.IGNORECASE)
_HEADER = re.compile(r"^#{1,6}\s+(?P<title>.+?)\s*$")
_ENTRY_URL = re.compile(r"(https?://\S+?)\s+\((?:abgerufen|accessed) \d{4}-\d{2}-\d{2}\)\s*$")
_CITATION_GROUP = re.compile(r"\[\d+(?:\s*,\s*\d+)*\]")
_ACKNOWLEDGED = re.compile(r"retract|zurückgezogen", re.IGNORECASE)


@dataclass(frozen=True)
class GateNote:
    note_id: str
    urls: tuple[str, ...]  # the URL as requested, as fetched, canonical: any of them may be listed
    text: str
    retracted: bool


@dataclass(frozen=True)
class GateInput:
    report: str | None
    brief_sha256: str
    required_headings: tuple[str, ...]
    language: str
    words_range: tuple[int, int]
    tier: str
    notes: Mapping[str, GateNote]
    artifacts: frozenset[str]  # file names in the run directory
    rules: RunRules
    criticals_open: int = 0  # full tier: critic findings neither applied nor rejected (M9)


@dataclass(frozen=True)
class CheckResult:
    id: str
    name: str
    passed: bool
    detail: str = ""
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class GateResult:
    checks: tuple[CheckResult, ...] = field(default=())

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def failed(self) -> list[str]:
        return [c.id for c in self.checks if not c.passed]

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "checks": [
                {
                    "id": c.id,
                    "name": c.name,
                    "passed": c.passed,
                    "detail": c.detail,
                    "warnings": list(c.warnings),
                }
                for c in self.checks
            ],
        }


def _result(
    check_id: str, name: str, ok: bool, detail: str = "", warnings: tuple[str, ...] = ()
) -> CheckResult:
    return CheckResult(check_id, name, ok, "" if ok and not warnings else detail, warnings)


def _text(inp: GateInput) -> str:
    return inp.report or ""


def resolve_citations(report: str, notes: Mapping[str, GateNote]) -> dict[int, str]:
    """Citation number to note id, for every Sources entry whose URL is a note of the run."""
    by_url = {dedup_key(url): note.note_id for note in notes.values() for url in note.urls}
    resolved: dict[int, str] = {}
    for number, entry in sources_entries(report).items():
        found = _ENTRY_URL.search(entry)
        if found and (note_id := by_url.get(dedup_key(found[1]))):
            resolved[number] = note_id
    return resolved


def g1_exists(inp: GateInput) -> CheckResult:
    ok = bool(_text(inp).strip())
    return _result("G1", "report-exists", ok, "report.md is missing or empty")


def g2_headings(inp: GateInput) -> CheckResult:
    expected = [
        *inp.required_headings,
        sources_heading(inp.language),
        appendix_heading(inp.language),
    ]
    actual = h2_list(_text(inp))
    return _result("G2", "headings", actual == expected, f"expected {expected}, found {actual}")


def g3_length(inp: GateInput) -> CheckResult:
    words = body_words(body_of(_text(inp)))
    low, high = inp.words_range
    below, above = inp.rules.length_tolerance
    lower, upper = low * below, high * above
    return _result(
        "G3",
        "length",
        lower <= words <= upper,
        f"{words} body words, allowed {lower:g} to {upper:g}",
    )


def g4_density(inp: GateInput) -> CheckResult:
    body = body_of(_text(inp))
    words = body_words(body)
    density = len(citation_numbers(body)) / words * 1000 if words else 0.0
    return _result(
        "G4",
        "citation-density",
        density >= inp.rules.citation_density_min,
        f"{density:.1f} citations per 1000 body words, at least {inp.rules.citation_density_min:g} needed",
    )


def g5_resolve(inp: GateInput) -> CheckResult:
    text = _text(inp)
    resolved = resolve_citations(text, inp.notes)
    used = set(citation_numbers(body_of(text)))
    missing = sorted(used - resolved.keys())
    unused = sorted(sources_entries(text).keys() - used)
    return _result(
        "G5",
        "citations-resolve",
        not missing,
        f"citations without a source entry that maps to a note: {missing}",
        tuple(f"source entry [{n}] is never cited" for n in unused),
    )


def _normalized_notes(inp: GateInput) -> dict[str, str]:
    return {n.note_id: normalize_for_match(n.text) for n in inp.notes.values()}


def g6_quotes(inp: GateInput) -> CheckResult:
    text = _text(inp)
    resolved = resolve_citations(text, inp.notes)

    def cited(window: str) -> list[str]:
        return [resolved[n] for n in citation_numbers(window) if n in resolved]

    failures = quote_failures(
        body_of(text), cited, _normalized_notes(inp), inp.rules.quote_min_words
    )
    detail = "; ".join(f"{f.reason}: {f.open}{f.span}{f.close}" for f in failures)
    return _result("G6", "quote-integrity", not failures, detail)


def leaks(text: str) -> list[str]:
    """What in ``text`` (without its title line) belongs to the pipeline, not to a report."""
    found: list[str] = []
    if text.lstrip().startswith("---"):
        found.append("front matter")
    for _, line in outside_fences(text):
        header = _HEADER.match(line)
        if header and header["title"].casefold() in SCAFFOLD_HEADERS:
            found.append(f"scaffold header: {line.strip()}")
    found += [f"vocabulary: {m[0]}" for m in _LEAK.finditer(text)]
    return found


def g7_leakage(inp: GateInput) -> CheckResult:
    text = _text(inp)
    body = body_of(text)
    without_title = body.split("\n", 1)[1] if body.startswith("# ") and "\n" in body else body
    found = leaks(without_title)
    if text.lstrip().startswith("---"):
        found.insert(0, "front matter")
    return _result("G7", "no-leakage", not found, "; ".join(dict.fromkeys(found)))


def g8_appendix(inp: GateInput) -> CheckResult:
    fence = appendix_fence(_text(inp))
    digest = hashlib.sha256(fence.encode("utf-8")).hexdigest() if fence is not None else None
    return _result(
        "G8",
        "appendix-verbatim",
        digest == inp.brief_sha256,
        "the appendix is not the approved brief",
    )


def g9_retractions(inp: GateInput) -> CheckResult:
    body = body_of(_text(inp))
    resolved = resolve_citations(_text(inp), inp.notes)
    window = inp.rules.retraction_window_chars
    bare: list[int] = []
    for match in _CITATION_GROUP.finditer(body):
        numbers = [n for n in citation_numbers(match[0]) if _retracted(inp, resolved.get(n))]
        near = body[max(0, match.start() - window) : match.end() + window]
        if numbers and not _ACKNOWLEDGED.search(near):
            bare += numbers
    return _result(
        "G9",
        "retractions",
        not bare,
        f"citations of retracted sources without a notice: {sorted(set(bare))}",
    )


def _retracted(inp: GateInput, note_id: str | None) -> bool:
    return note_id is not None and inp.notes[note_id].retracted


def g10_artifacts(inp: GateInput) -> CheckResult:
    required = FULL_ARTIFACTS if inp.tier == "full" else LIGHT_ARTIFACTS
    missing = sorted(required - inp.artifacts)
    return _result("G10", "tier-artifacts", not missing, f"missing: {', '.join(missing)}")


def g11_criticals(inp: GateInput) -> CheckResult:
    open_ = inp.criticals_open if inp.tier == "full" else 0
    return _result("G11", "criticals-resolved", open_ == 0, f"{open_} critical findings are open")


def g12_language(inp: GateInput) -> CheckResult:
    samples = language_samples(body_of(_text(inp)), inp.rules.language_samples)
    codes = [detect_code(sample) for sample in samples]
    ok = bool(samples) and all(code == inp.language for code in codes)
    return _result(
        "G12", "language", ok, f"expected {inp.language}, samples read as {codes or 'no text'}"
    )


CHECKS: tuple[Callable[[GateInput], CheckResult], ...] = (
    g1_exists,
    g2_headings,
    g3_length,
    g4_density,
    g5_resolve,
    g6_quotes,
    g7_leakage,
    g8_appendix,
    g9_retractions,
    g10_artifacts,
    g11_criticals,
    g12_language,
)


def run_gate(inp: GateInput) -> GateResult:
    return GateResult(tuple(check(inp) for check in CHECKS))
