"""Build the narration script for an article's video.

The script is composed *only* from the article's own words - its title and the
first sentence of its introduction - and is never sent to another service to be
rewritten, summarised or expanded. It is deliberately capped hard so the
finished clip stays in the ~10-15 second range: the title plus one sentence,
truncated to at most 30 words.
"""

from __future__ import annotations

import re

MAX_WORDS = 30

# Split on the first sentence terminator (., !, ?) followed by whitespace or end.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def first_sentence(text: str) -> str:
    """Return the first sentence of ``text`` (empty string if there is none)."""
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return ""
    return _SENTENCE_SPLIT.split(cleaned, maxsplit=1)[0].strip()


def build_narration_script(page, *, max_words: int = MAX_WORDS) -> str:
    """Return the narration script for ``page``.

    ``page`` is any Wagtail page exposing ``title`` and ``introduction``
    (i.e. a ``BlogPage``). The result is ``"<title>. <first intro sentence>"``,
    normalised to single spaces and truncated to ``max_words`` words. The body
    is intentionally excluded to keep the video short and cheap.
    """
    title = " ".join((getattr(page, "title", "") or "").split()).rstrip(".!?")
    intro_sentence = first_sentence(getattr(page, "introduction", "") or "")

    if title and intro_sentence:
        script = f"{title}. {intro_sentence}"
    else:
        script = title or intro_sentence

    words = script.split()
    if len(words) > max_words:
        script = " ".join(words[:max_words])

    return script.strip()
