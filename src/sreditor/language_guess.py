"""A rough guess at the language a speech is written in, from its most common short words.

It only has to catch the obvious case before credits are spent: a script written in English but
set to speak Brazilian Portuguese, or the other way round. It answers None when unsure.
"""

from __future__ import annotations

import re

# The commonest words of each language that the other two rarely use.
MARKERS: dict[str, frozenset[str]] = {
    "en": frozenset(
        [
            "the",
            "and",
            "of",
            "is",
            "it",
            "to",
            "this",
            "that",
            "would",
            "what",
            "when",
            "with",
            "you",
            "your",
            "are",
            "was",
            "be",
            "they",
            "their",
            "there",
            "which",
            "have",
            "has",
            "will",
        ]
    ),
    "pt": frozenset(
        [
            "o",
            "os",
            "as",
            "é",
            "não",
            "um",
            "uma",
            "do",
            "da",
            "dos",
            "das",
            "em",
            "no",
            "na",
            "nos",
            "para",
            "com",
            "que",
            "você",
            "isso",
            "ele",
            "ela",
            "são",
            "está",
            "mais",
            "mas",
            "também",
        ]
    ),
    "es": frozenset(
        [
            "el",
            "los",
            "las",
            "es",
            "y",
            "del",
            "al",
            "en",
            "un",
            "una",
            "por",
            "con",
            "que",
            "usted",
            "eso",
            "él",
            "ella",
            "son",
            "está",
            "más",
            "pero",
            "también",
            "muy",
        ]
    ),
}
# Fewer marker words than this tells too little to guess.
MIN_MARKERS = 4
# The winner needs this share of all marker words found.
MIN_SHARE = 0.6
WORD = re.compile(r"[^\W\d_]+")


def guess_language(text: str) -> str | None:
    """The primary language subtag (en, pt, es) the text most likely is in, or None."""
    words = [word.casefold() for word in WORD.findall(text)]
    counts = {language: sum(word in found for word in words) for language, found in MARKERS.items()}
    total = sum(counts.values())
    if total < MIN_MARKERS:
        return None
    language, best = max(counts.items(), key=lambda pair: pair[1])
    return language if best / total >= MIN_SHARE else None


def primary_subtag(language: str) -> str:
    """The language part of a tag: pt for pt-BR."""
    return language.split("-")[0].casefold()
