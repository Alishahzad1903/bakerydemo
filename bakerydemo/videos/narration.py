"""Build the narration script from an article's own words.

The narration is the article's **title and the first sentence of its
introduction only** — nothing else, and nothing is sent anywhere to be
rewritten, summarised or expanded first. It is hard-capped at 30 words to keep
the produced clip short (roughly 10-15 seconds); cost scales directly with
length, so shorter is better.
"""

from __future__ import annotations

import re
from typing import Any

# Hard ceiling on the narration length. Shorter is cheaper; this is the most
# expensive knob in the whole flow, so it is a hard cap, not a preference.
MAX_WORDS = 30

# Split on the first sentence terminator (. ! ?) followed by whitespace/end,
# or on the first line break — whichever comes first.
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+|\n")


def first_sentence(text: str) -> str:
    """Return the first sentence of ``text`` (or the whole thing if it has no
    detectable sentence boundary)."""
    stripped = (text or "").strip()
    if not stripped:
        return ""
    parts = _SENTENCE_BOUNDARY.split(stripped, maxsplit=1)
    return parts[0].strip()


def _clamp_words(text: str, max_words: int = MAX_WORDS) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text.strip()
    return " ".join(words[:max_words]).strip()


def build_script(title: str, introduction: str) -> str:
    """Compose the narration script from an article's ``title`` and the first
    sentence of its ``introduction``, capped at :data:`MAX_WORDS` words.

    Both inputs are the article's own text. The result is used verbatim as the
    spoken narration.
    """
    title = (title or "").strip()
    intro_sentence = first_sentence(introduction)

    if title and intro_sentence:
        # Avoid doubling terminal punctuation on the title.
        joiner = " " if title[-1] in ".!?" else ". "
        script = f"{title}{joiner}{intro_sentence}"
    else:
        script = title or intro_sentence

    return _clamp_words(script)


def build_script_for_page(page: Any) -> str:
    """Build the narration script for a blog article ``page``.

    Uses the page's ``title`` and ``introduction`` fields. Raises ``ValueError``
    if there is no usable text to narrate.
    """
    title = getattr(page, "title", "") or ""
    introduction = getattr(page, "introduction", "") or ""
    script = build_script(title, introduction)
    if not script:
        raise ValueError("Article has no title or introduction to narrate.")
    return script
