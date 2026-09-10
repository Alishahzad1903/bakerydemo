"""Build the spoken narration for an article's video.

The narration is composed *only* from the article's own text - never sent to
another service to be rewritten, summarised or expanded. To keep the produced
clip short (the provider bills by finished length), the script is deliberately
limited to the article's **title** and the **first sentence of its
introduction**, and then hard-capped at :data:`MAX_WORDS` words. The body is
intentionally excluded to hold the clip to the ~10-15s / <=30-word target.
"""

from __future__ import annotations

import re

# Hard ceiling on the narration length. Cost scales with the finished video's
# duration, so this cap is a spend guardrail, not a stylistic choice.
MAX_WORDS = 30

# Split on sentence-ending punctuation followed by whitespace. Good enough for
# the demo's prose; keeps the first sentence and discards the rest.
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")


def first_sentence(text: str) -> str:
    """Return the first sentence of ``text`` (or the whole thing if it has one)."""
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return ""
    return _SENTENCE_BOUNDARY.split(cleaned, maxsplit=1)[0].strip()


def _cap_words(text: str, max_words: int = MAX_WORDS) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words])


def build_narration_script(*, title: str, introduction: str) -> str:
    """Compose the narration from an article's ``title`` and ``introduction``.

    The result is ``"<title>. <first sentence of introduction>"``, normalised
    and capped at :data:`MAX_WORDS` words. If the article has no introduction,
    the title alone is used - the body is never narrated.
    """
    title = " ".join((title or "").split()).strip()
    intro_sentence = first_sentence(introduction)

    if title and not title.endswith((".", "!", "?")):
        title_part = f"{title}."
    else:
        title_part = title

    script = " ".join(part for part in (title_part, intro_sentence) if part).strip()
    return _cap_words(script)
