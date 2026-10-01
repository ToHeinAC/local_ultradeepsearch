"""The denylist: the only hard guarantee that a confidential term never leaves the machine.

Matching is deliberately variant-tolerant but word-bounded:

- Every text is folded two ways after NFKC and casefolding: German umlauts to `ae/oe/ue`, and
  all diacritics stripped. So `Müller` matches `Mueller` and `Muller`.
- Both are split into words of letters and digits. A term matches when its words, joined
  together, equal the joined words of any contiguous run of text words. So `Müller-Werke`
  matches `mueller werke` and `MuellerWerke`, but never a substring inside a longer word: `AG`
  does not match `Tagung`.
- Percent-encoded text (URLs, form data) is checked decoded as well.

Known limit: a term entered without its umlaut (`Muller`) does not match the `ue` spelling
(`Mueller`). Enter terms in their proper spelling.
"""

import re
import unicodedata
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import unquote_plus

from app.artifacts import write_text

_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue"})
_NOT_DECOMPOSABLE = str.maketrans(
    {"æ": "ae", "œ": "oe", "ø": "o", "ł": "l", "đ": "d", "ð": "d", "þ": "th", "\u0131": "i"}
)
_WORD = re.compile(r"[^\W_]+")
_HEADER = "# Denylist: one term per line. Matching ignores case, accents and separators.\n"


def _strip_marks(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def fold_variants(text: str) -> tuple[str, str]:
    """(umlauts as ae/oe/ue, diacritics stripped), both NFKC-normalised and casefolded."""
    base = unicodedata.normalize("NFC", unicodedata.normalize("NFKC", text).casefold())
    umlaut = _strip_marks(base.translate(_UMLAUTS)).translate(_NOT_DECOMPOSABLE)
    stripped = _strip_marks(base).translate(_NOT_DECOMPOSABLE)
    return umlaut, stripped


def tokens(text: str) -> list[str]:
    """Words of letters and digits, casefolded. Everything else separates."""
    return _WORD.findall(text.casefold())


def _joined_variants(term: str) -> tuple[str, str]:
    umlaut, stripped = fold_variants(term)
    return "".join(tokens(umlaut)), "".join(tokens(stripped))


def _run_matches(joined: str, words: list[str]) -> bool:
    """True if some contiguous run of ``words``, concatenated, equals ``joined``."""
    for start in range(len(words)):
        acc = ""
        for word in words[start:]:
            acc += word
            if acc == joined:
                return True
            if not joined.startswith(acc):
                break
    return False


class Denylist:
    def __init__(self, terms: Iterable[str]) -> None:
        self._terms: list[str] = []
        self._joined: list[tuple[str, str]] = []
        for term in terms:
            self.add(term)

    @property
    def terms(self) -> tuple[str, ...]:
        return tuple(self._terms)

    def add(self, term: str) -> bool:
        """Add ``term``; False if an equivalent term is already present."""
        cleaned = term.strip()
        joined = _joined_variants(cleaned)
        if not joined[0]:
            raise ValueError(f"denylist term needs letters or digits: {term!r}")
        if self._index_of(joined) is not None:
            return False
        self._terms.append(cleaned)
        self._joined.append(joined)
        return True

    def remove(self, term: str) -> bool:
        """Remove the term equivalent to ``term``; False if there is none."""
        index = self._index_of(_joined_variants(term.strip()))
        if index is None:
            return False
        del self._terms[index], self._joined[index]
        return True

    def _index_of(self, joined: tuple[str, str]) -> int | None:
        """Position of an equivalent term: equal in either folded form."""
        for index, (umlaut, stripped) in enumerate(self._joined):
            if umlaut == joined[0] or stripped == joined[1]:
                return index
        return None

    def find(self, text: str) -> list[str]:
        """Every term that occurs in ``text`` (or its percent-decoded form), in list order."""
        texts = {text, unquote_plus(text)}
        variants = [fold_variants(t) for t in texts]
        words = [(tokens(umlaut), tokens(stripped)) for umlaut, stripped in variants]
        return [
            term
            for term, (j_umlaut, j_stripped) in zip(self._terms, self._joined, strict=True)
            if any(
                _run_matches(j_umlaut, w_umlaut) or _run_matches(j_stripped, w_stripped)
                for w_umlaut, w_stripped in words
            )
        ]

    def contains_any(self, text: str) -> bool:
        return bool(self.find(text))

    @classmethod
    def load(cls, path: Path) -> "Denylist":
        """Read ``path``; a missing file is an empty list. `#` starts a comment line."""
        if not path.exists():
            return cls([])
        lines = path.read_text(encoding="utf-8").splitlines()
        return cls(line for line in lines if line.strip() and not line.lstrip().startswith("#"))

    def save(self, path: Path) -> None:
        body = "".join(f"{term}\n" for term in self._terms)
        write_text(path, _HEADER + body, scrub=False)
