"""Text normalisation for verbatim checks. Pure; shared by claim extraction and the ship gate."""

import re
import unicodedata

# Code points are spelled out (hex) on purpose: these characters look alike, and formatters
# rewrite escapes into literals that linters then flag as ambiguous.
_DOUBLE_QUOTES = (0x201C, 0x201D, 0x201E, 0x201F, 0x00AB, 0x00BB, 0x2039, 0x203A)
_SINGLE_QUOTES = (0x2019, 0x2018, 0x201A)
_DASHES = (0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2015)  # NFKC maps U+2011 to U+2010
_SOFT_HYPHEN = 0x00AD
_QUOTES = {
    **dict.fromkeys(_DOUBLE_QUOTES, '"'),
    **dict.fromkeys(_SINGLE_QUOTES, "'"),
    **dict.fromkeys(_DASHES, "-"),
    _SOFT_HYPHEN: None,
}
_SPACE = re.compile(r"\s+")


def normalize_for_match(text: str) -> str:
    """NFKC, uniform quotes and dashes, no soft hyphens, single spaces, casefolded."""
    folded = unicodedata.normalize("NFKC", text).translate(_QUOTES)
    return _SPACE.sub(" ", folded).strip().casefold()


# (opening, closing) pairs a model may wrap a quote in, spelled as code points
_WRAPPERS = (
    (0x0022, 0x0022),
    (0x0027, 0x0027),
    (0x201C, 0x201D),
    (0x201E, 0x201C),
    (0x00AB, 0x00BB),
    (0x00BB, 0x00AB),
    (0x2018, 0x2019),
    (0x201A, 0x2018),
    (0x2039, 0x203A),
)


def strip_wrapping_quotes(text: str) -> str:
    """``text`` without surrounding whitespace and without quotation marks that wrap all of it."""
    result = text.strip()
    while len(result) >= 2 and any(
        result[0] == chr(open_) and result[-1] == chr(close) for open_, close in _WRAPPERS
    ):
        result = result[1:-1].strip()
    return result


def _bare(quote: str) -> str:
    """The normalised quote without wrapping quotation marks the model may have added."""
    text = normalize_for_match(quote)
    while len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return text.strip('"').strip()


def contains_quote(normalized_text: str, quote: str) -> bool:
    """True if ``quote`` occurs verbatim in text that is already `normalize_for_match`-ed.

    The match must start and end on word boundaries: a quote that begins or ends in the middle of
    a word (`ckbau kerntechnischer`) is not a faithful quote.
    """
    needle = _bare(quote)
    if not needle:
        return False
    start = normalized_text.find(needle)
    while start != -1:
        end = start + len(needle)
        before = normalized_text[start - 1] if start > 0 else " "
        after = normalized_text[end] if end < len(normalized_text) else " "
        starts_ok = not (needle[0].isalnum() and before.isalnum())
        ends_ok = not (needle[-1].isalnum() and after.isalnum())
        if starts_ok and ends_ok:
            return True
        start = normalized_text.find(needle, start + 1)
    return False


def quote_in_text(quote: str, text: str) -> bool:
    return contains_quote(normalize_for_match(text), quote)
