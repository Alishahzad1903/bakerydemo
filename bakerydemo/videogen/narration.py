"""Build the spoken narration from an article's own words.

The narration is composed *only* from the article's own text — its title and
the first sentence of its introduction. The article is never sent to any other
service to be rewritten, summarised or expanded first; we read the model
fields directly and hand the resulting text to VideoGen verbatim.

The script is deliberately capped to a small number of words (see
``settings.VIDEOGEN_MAX_SCRIPT_WORDS``). A short script keeps the finished clip
around 10-15 seconds, which is both what the marketing team wants and what
keeps the render cheap.
"""

from __future__ import annotations

import re

from django.conf import settings

# Splits text into sentences on ``.``/``!``/``?`` followed by whitespace.
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")


def first_sentence(text: str) -> str:
    """Return the first sentence of ``text`` (empty string if there is none)."""
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return ""
    return _SENTENCE_BOUNDARY.split(cleaned, maxsplit=1)[0].strip()


def _truncate_words(text: str, max_words: int) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    truncated = " ".join(words[:max_words]).rstrip(",;:-— ")
    if truncated and truncated[-1] not in ".!?":
        truncated += "."
    return truncated


def article_title(page) -> str:
    return (getattr(page, "title", "") or "").strip()


def article_introduction(page) -> str:
    return (getattr(page, "introduction", "") or "").strip()


def build_narration_script(page, max_words: int | None = None) -> str:
    """Compose the narration script for ``page``.

    ``page`` should be a specific page instance (e.g. a ``BlogPage``) exposing
    ``title`` and ``introduction``. The result is ``"<title>. <first sentence
    of the introduction>"``, normalised and clamped to ``max_words`` words.
    """
    if max_words is None:
        max_words = getattr(settings, "VIDEOGEN_MAX_SCRIPT_WORDS", 30)

    title = article_title(page)
    intro_sentence = first_sentence(article_introduction(page))

    parts = []
    if title:
        # Guarantee the title is spoken as its own sentence.
        parts.append(title if title[-1] in ".!?" else f"{title}.")
    if intro_sentence:
        parts.append(intro_sentence)

    script = " ".join(parts).strip()
    return _truncate_words(script, max_words)
