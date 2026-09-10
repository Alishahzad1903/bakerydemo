"""Build the narration script from an article's own text.

The script is assembled locally from the article's title and the first sentence of its
introduction — nothing is sent to any other service to be rewritten, summarised or
expanded. The result is hard-capped to a small number of words so the produced clip stays
short (~10-15s), which is the single biggest cost lever.
"""

from __future__ import annotations

import re

from django.conf import settings

DEFAULT_MAX_WORDS = 30

# First run of characters ending at a sentence terminator (., !, ?).
_SENTENCE_RE = re.compile(r"(.+?[.!?])(?:\s|$)", re.DOTALL)
_SENTENCE_ENDINGS = (".", "!", "?")


def first_sentence(text: str | None) -> str:
    """Return the first sentence of ``text`` (or the whole thing if it has no terminator)."""
    text = (text or "").strip()
    if not text:
        return ""
    match = _SENTENCE_RE.match(text)
    return match.group(1).strip() if match else text


def build_narration_script(page, max_words: int | None = None) -> str:
    """Compose the narration for ``page``: its title + the first sentence of its intro.

    The body is deliberately not narrated (spend limit). The result is capped to
    ``max_words`` (default ``VIDEOGEN_NARRATION_MAX_WORDS``).
    """
    if max_words is None:
        max_words = getattr(settings, "VIDEOGEN_NARRATION_MAX_WORDS", DEFAULT_MAX_WORDS)

    title = (getattr(page, "title", "") or "").strip()
    if title and not title.endswith(_SENTENCE_ENDINGS):
        title = f"{title}."

    intro_sentence = first_sentence(getattr(page, "introduction", "") or "")

    parts = [part for part in (title, intro_sentence) if part]
    script = " ".join(parts).strip()

    words = script.split()
    if len(words) > max_words:
        script = " ".join(words[:max_words])
    return script
