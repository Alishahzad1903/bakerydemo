"""
Build the spoken-narration script for an article's video.

The script is derived *only* from the article's own words – never sent to any
third party to be rewritten, summarised or expanded. To keep the rendered clip
short and cheap (the target is roughly a 10–15 second video), the narration is
limited to the article's **title** and the **first sentence of its
introduction**, and is hard-capped at :data:`MAX_WORDS` words.
"""

from __future__ import annotations

import re

#: Hard ceiling on narration length. Cost scales with the finished video's
#: length, so the script is never allowed past this many words.
MAX_WORDS = 30

#: Matches the end of the first sentence (``.``/``!``/``?`` + whitespace).
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def first_sentence(text: str) -> str:
    """Return the first sentence of ``text`` (trailing terminator stripped)."""
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return ""
    sentence = _SENTENCE_END.split(cleaned, maxsplit=1)[0]
    return sentence.strip().rstrip(".!?").strip()


def _cap_words(text: str, max_words: int) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words])


def build_narration_script(*, title: str, introduction: str) -> str:
    """Compose the narration from an article's title and introduction.

    Produces e.g. ``"Tracking Wild Yeast. Yeasts, with their single-celled
    growth habit, can be contrasted with molds, which grow hyphae."`` and never
    exceeds :data:`MAX_WORDS` words.
    """
    title_part = (title or "").strip().rstrip(".!?").strip()
    sentence_part = first_sentence(introduction)

    segments = [segment for segment in (title_part, sentence_part) if segment]
    script = ". ".join(segments)
    if script:
        script += "."

    script = _cap_words(script, MAX_WORDS)
    return script.strip()
