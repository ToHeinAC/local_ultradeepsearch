"""Near-duplicate detection with MinHash signatures (PRD M3: 128 permutations, threshold 0.6).

A source's signature is the MinHash of the word 3-grams of its normalised text. Signatures are
stored in the vault as bytes and compared directly: with at most ~150 sources per run an
all-pairs comparison is cheap and, unlike an LSH index, has no false negatives near the threshold.

The hash scheme is pinned and written into every stored signature, because datasketch 2.0 can
change its default scheme and values from different schemes cannot be compared.
"""

import re
import struct
from collections.abc import Iterable, Sequence
from typing import Protocol, cast

from datasketch import MinHash  # pyright: ignore[reportMissingTypeStubs]  # no stubs

from app.text import normalize_for_match

NUM_PERM = 128
SEED = 1
THRESHOLD = 0.6
SHINGLE_WORDS = 3
SCHEME = "affine32"
TAG = f"mh1:{SCHEME}:{NUM_PERM}:{SEED}:".encode()

_WORD = re.compile(r"[^\W_]+")


class _Array(Protocol):
    """The two numpy-array methods used on a MinHash's values (datasketch has no stubs)."""

    def astype(self, dtype: str) -> "_Array": ...
    def tobytes(self) -> bytes: ...


class _MinHash(Protocol):
    hashvalues: _Array

    def update_batch(self, items: Sequence[bytes]) -> None: ...


def _shingles(text: str) -> list[bytes]:
    words = _WORD.findall(normalize_for_match(text))
    if not words:
        return []
    size = min(SHINGLE_WORDS, len(words))
    return [" ".join(words[i : i + size]).encode() for i in range(len(words) - size + 1)]


def signature(text: str) -> bytes | None:
    """The signature of ``text``, or None if it contains no words."""
    shingles = _shingles(text)
    if not shingles:
        return None
    minhash = cast(_MinHash, MinHash(num_perm=NUM_PERM, seed=SEED, scheme=SCHEME))
    minhash.update_batch(shingles)
    return TAG + minhash.hashvalues.astype("<u4").tobytes()


def _values(raw: bytes) -> tuple[int, ...]:
    if not raw.startswith(TAG) or (len(raw) - len(TAG)) != NUM_PERM * 4:
        raise ValueError("not a MinHash signature of this scheme")
    return struct.unpack(f"<{NUM_PERM}I", raw[len(TAG) :])


def _equal_share(a: tuple[int, ...], b: tuple[int, ...]) -> float:
    return sum(x == y for x, y in zip(a, b, strict=True)) / NUM_PERM


def similarity(a: bytes, b: bytes) -> float:
    """Estimated Jaccard similarity: the share of equal hash values."""
    return _equal_share(_values(a), _values(b))


class NearDupIndex:
    """All signatures of a run's original sources, in insertion order."""

    def __init__(
        self, rows: Iterable[tuple[str, bytes]] = (), *, threshold: float = THRESHOLD
    ) -> None:
        self._threshold = threshold
        self._entries: list[tuple[str, tuple[int, ...]]] = []
        for note_id, raw in rows:
            try:
                self.add(note_id, raw)
            except ValueError:
                continue  # an unreadable stored signature is simply not compared against

    def __len__(self) -> int:
        return len(self._entries)

    def add(self, note_id: str, raw: bytes) -> None:
        self._entries.append((note_id, _values(raw)))

    def find(self, raw: bytes) -> str | None:
        """The earliest stored note whose similarity to ``raw`` reaches the threshold."""
        probe = _values(raw)
        for note_id, values in self._entries:
            if _equal_share(values, probe) >= self._threshold:
                return note_id
        return None
