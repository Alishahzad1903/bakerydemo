"""Build the narration script from an article's own words.

The script is composed *only* from the article's own text — its title and the
first sentence of its introduction — and is never sent to another service to be
rewritten, summarised, or expanded. Cost scales with narration length, so the
body is deliberately not narrated and the result is capped at
``MAX_NARRATION_WORDS`` words.
"""

from __future__ import annotations

import re

from .constants import MAX_NARRATION_WORDS

# First run of characters up to (and including) a sentence terminator.
_FIRST_SENTENCE_RE = re.compile(r"^\s*(.+?[.!?])(\s|$)", re.DOTALL)


def first_sentence(text: str) -> str:
    """Return the first sentence of ``text`` (or the whole thing if it has no
    sentence terminator). Whitespace/newlines are collapsed to single spaces."""
    collapsed = re.sub(r"\s+", " ", (text or "").strip())
    if not collapsed:
        return ""
    match = _FIRST_SENTENCE_RE.match(collapsed)
    return match.group(1).strip() if match else collapsed


def _cap_words(text: str, max_words: int) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words])


def build_narration_script(title: str, introduction: str) -> str:
    """Compose the narration from ``title`` + the first sentence of
    ``introduction``, capped at ``MAX_NARRATION_WORDS`` words."""
    title = re.sub(r"\s+", " ", (title or "").strip())
    intro_sentence = first_sentence(introduction)

    parts = []
    if title:
        # End the title as its own spoken sentence.
        parts.append(title if title[-1:] in ".!?" else f"{title}.")
    if intro_sentence:
        parts.append(intro_sentence)

    script = " ".join(parts).strip()
    return _cap_words(script, MAX_NARRATION_WORDS)


def build_narration_for_page(page) -> str:
    """Build the narration script for a blog ``page`` from its own fields."""
    return build_narration_script(
        title=getattr(page, "title", "") or "",
        introduction=getattr(page, "introduction", "") or "",
    )
