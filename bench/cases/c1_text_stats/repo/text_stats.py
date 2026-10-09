"""Small, dependency-free statistics for plain-text documents."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

WORD_RE = re.compile(r"[A-Za-z']+")
SENTENCE_END_RE = re.compile(r"[.!?]+(?:\s|$)")
WORDS_PER_MINUTE = 200


@dataclass(frozen=True)
class TextStats:
    words: int
    unique_words: int
    sentences: int
    reading_minutes: float


def words(text: str) -> list[str]:
    """Lower-cased words; apostrophes are kept, so "don't" is one word."""
    return [word.lower() for word in WORD_RE.findall(text)]


def sentence_count(text: str) -> int:
    """Sentences ended by . ! or ?; trailing text without one still counts."""
    stripped = text.strip()
    if not stripped:
        return 0
    ends = len(SENTENCE_END_RE.findall(stripped))
    return ends if stripped[-1] in ".!?" else ends + 1


def top_words(text: str, n: int = 5) -> list[tuple[str, int]]:
    """The n most common words, ties broken alphabetically."""
    if n < 1:
        raise ValueError("n must be at least 1")
    counts = Counter(words(text))
    return sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))[:n]


def reading_minutes(text: str, wpm: int = WORDS_PER_MINUTE) -> float:
    """Estimated reading time, rounded to a tenth of a minute."""
    if wpm <= 0:
        raise ValueError("wpm must be positive")
    return round(len(words(text)) / wpm, 1)


def analyse(text: str) -> TextStats:
    """All of the above for one document."""
    all_words = words(text)
    return TextStats(
        words=len(all_words),
        unique_words=len(set(all_words)),
        sentences=sentence_count(text),
        reading_minutes=reading_minutes(text),
    )
