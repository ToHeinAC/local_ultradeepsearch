"""Ship-gate fixes that code makes alone (PRD §3.10, D11): nothing here asks a model, and nothing
here invents text. Functions work on section text with evidence keys (`[S3]`), the editable form.
"""

import re
from collections import Counter
from collections.abc import Mapping, Sequence

from app.research.quotes import QuoteFailure, quote_failures
from app.research.report import CITATION_GROUP, cited_keys

RETRACTION_NOTICE = {"de": "(zurückgezogen)", "en": "(retracted)"}
_ACKNOWLEDGED = re.compile(r"retract|zurückgezogen", re.IGNORECASE)
_MARKER = re.compile(r" ?\[\s*S\d+(?:\s*[,;]\s*S\d+)*\s*\]")


def keyed_quote_failures(
    text: str, keys: Mapping[str, str], note_texts: Mapping[str, str], min_words: int
) -> list[QuoteFailure]:
    """The quotes of ``text`` that are not verbatim in a note its sentence cites by key."""

    def cited(window: str) -> list[str]:
        return [keys[key] for key in cited_keys(window) if key in keys]

    return quote_failures(text, cited, note_texts, min_words)


def unquote(text: str, failures: Sequence[QuoteFailure]) -> str:
    """``text`` with the quotation marks of the failed spans removed: the wording stays, as a
    paraphrase with its citation, and nothing is made up (G6)."""
    for failure in failures:
        text = text.replace(f"{failure.open}{failure.span}{failure.close}", failure.span)
    return text


def acknowledge_retractions(text: str, retracted: set[str], notice: str, window: int) -> str:
    """A notice right after every citation of a retracted source that has none within ``window``
    characters (G9)."""
    result = text
    for match in reversed(list(CITATION_GROUP.finditer(text))):
        if not retracted & set(cited_keys(match[2])):
            continue
        near = text[max(0, match.start() - window) : match.end() + window]
        if not _ACKNOWLEDGED.search(near):
            result = f"{result[: match.end()]} {notice}{result[match.end() :]}"
    return result


def _bare(text: str) -> str:
    return " ".join(_MARKER.sub("", text).split())


def citations_only_change(old: str, new: str, known: set[str]) -> bool:
    """True if ``new`` is ``old`` plus citation markers of known keys, and nothing else (G4)."""
    kept = not Counter(cited_keys(old)) - Counter(cited_keys(new))
    return _bare(old) == _bare(new) and set(cited_keys(new)) <= known and kept


def strip_markers(text: str) -> str:
    """``text`` without its citation markers and the space before each."""
    return _MARKER.sub("", text)


def section_words(text: str) -> int:
    """Words of a section's text, without its citation markers."""
    return len(strip_markers(text).split())


def citation_density(text: str) -> float:
    """Citations per 1000 words of ``text``, counting the keys in its markers."""
    words = section_words(text)
    return len(cited_keys(text)) / words * 1000 if words else 0.0
