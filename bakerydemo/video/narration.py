"""
Build the narration script from an article's *own* text.

The narration is intentionally tiny: the article title plus the first sentence
of its introduction, capped at a hard word ceiling. Cost scales directly with
the finished clip's length, so a short script is the single most important cost
control. The body is never narrated, and the text is never sent anywhere to be
rewritten, summarised or expanded — it is used verbatim (only trimmed to the
word ceiling).
"""

from __future__ import annotations

import re

from django.utils.html import strip_tags

# Hard ceiling on narrated words. Shorter is better; this only ever trims.
MAX_SCRIPT_WORDS = 30

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_WHITESPACE = re.compile(r"\s+")


def _normalise(text: str) -> str:
    """Collapse whitespace and strip any stray markup to plain text."""
    return _WHITESPACE.sub(" ", strip_tags(text or "")).strip()


def first_sentence(text: str) -> str:
    """Return the first sentence of ``text`` (or the whole thing if unpunctuated)."""
    cleaned = _normalise(text)
    if not cleaned:
        return ""
    parts = _SENTENCE_END.split(cleaned, maxsplit=1)
    return parts[0].strip()


def build_script(title: str, introduction: str, *, max_words: int = MAX_SCRIPT_WORDS) -> str:
    """
    Compose the narration from the article's title and the first sentence of its
    introduction, then trim to at most ``max_words`` words.

    The title is spoken first (ending with a period so the narration reads as a
    heading), followed by the opening sentence of the introduction.
    """
    title_clean = _normalise(title)
    intro_sentence = first_sentence(introduction)

    pieces = []
    if title_clean:
        # Ensure the title reads as its own spoken clause.
        pieces.append(title_clean if title_clean.endswith((".", "!", "?")) else f"{title_clean}.")
    if intro_sentence:
        pieces.append(intro_sentence)

    script = " ".join(pieces).strip()

    words = script.split()
    if len(words) > max_words:
        script = " ".join(words[:max_words])
    return script
