"""Build the narration script from an article's own words.

The script is assembled *locally* from the page's own fields - its title and
the first sentence of its introduction - and from nothing else. The article is
never sent to another service to be rewritten, summarised or expanded.

Only the title and first intro sentence are narrated (never the body), and the
result is capped to a hard word ceiling so the finished clip stays short
(~10-15 seconds).
"""

from __future__ import annotations

import re

from django.conf import settings

# Split on the first sentence terminator followed by whitespace. Good enough for
# the editorial prose in blog introductions; we only ever take the first piece.
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")
_SENTENCE_ENDINGS = (".", "!", "?")


def first_sentence(text: str) -> str:
    """Return the first sentence of ``text`` (whitespace-normalised)."""
    normalised = " ".join((text or "").split())
    if not normalised:
        return ""
    return _SENTENCE_BOUNDARY.split(normalised, maxsplit=1)[0].strip()


def build_narration_script(page, *, max_words: int | None = None) -> str:
    """Compose the narration for ``page``.

    ``page`` only needs a ``title`` and an ``introduction`` attribute, so any
    article-like page works. The returned string is ``"<title>. <first intro
    sentence>"`` capped to ``max_words`` words.
    """
    if max_words is None:
        max_words = getattr(settings, "VIDEOGEN_NARRATION_MAX_WORDS", 30)

    title = " ".join((getattr(page, "title", "") or "").split())
    intro_sentence = first_sentence(getattr(page, "introduction", "") or "")

    parts: list[str] = []
    if title:
        # End the title with punctuation so TTS pauses naturally before the
        # introduction.
        parts.append(title if title.endswith(_SENTENCE_ENDINGS) else f"{title}.")
    if intro_sentence:
        parts.append(intro_sentence)

    script = " ".join(parts).strip()

    # Hard safety net: never exceed the word ceiling, whatever the source text.
    words = script.split()
    if max_words and len(words) > max_words:
        script = " ".join(words[:max_words])
    return script
