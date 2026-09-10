"""Build the narration script from an article's own words.

The script is derived *only* from the article's title and the first sentence of
its introduction — no external service rewrites, summarises or expands it. A
hard 30-word ceiling keeps the produced clip short (~10-15 seconds), which is
what actually drives VideoGen billing.
"""

from __future__ import annotations

import re

# Ceiling on the narration length. The clip's cost scales with its duration, so
# this is a hard limit, not a preference.
MAX_WORDS = 30

# Split on the boundary *after* sentence-ending punctuation followed by
# whitespace, so the first sentence keeps its trailing period.
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")


def first_sentence(text: str | None) -> str:
    """Return the first sentence of ``text`` (empty string if none)."""
    text = (text or "").strip()
    if not text:
        return ""
    return _SENTENCE_BOUNDARY.split(text, maxsplit=1)[0].strip()


def build_narration_script(
    title: str | None,
    introduction: str | None,
    *,
    max_words: int = MAX_WORDS,
) -> str:
    """Compose the narration from the title and first introduction sentence.

    The title is spoken first (as its own sentence), then the first sentence of
    the introduction. The result is truncated to ``max_words`` words so it can
    never exceed the ceiling, whatever the article contains.
    """
    title = (title or "").strip()
    sentence = first_sentence(introduction)

    pieces: list[str] = []
    if title:
        pieces.append(title if title[-1] in ".!?" else f"{title}.")
    if sentence:
        pieces.append(sentence)

    script = " ".join(pieces).strip()

    words = script.split()
    if len(words) > max_words:
        script = " ".join(words[:max_words])
    return script
