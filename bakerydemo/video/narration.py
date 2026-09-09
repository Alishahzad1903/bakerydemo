"""Build the narration script from an article's *own* text.

The script is deliberately tiny: the article's title plus the first sentence of
its introduction, capped at :data:`MAX_SCRIPT_WORDS` words. This is both an
editorial choice (a short, shareable teaser) and a hard cost control — the
finished video's length, and therefore its price, scales directly with the
narration length, so we never narrate the body.

The text is taken verbatim from the page; it is never sent to another service to
be rewritten, summarised or expanded.
"""

from __future__ import annotations

import re

# Hard ceiling on the narration length. ~30 words of speech is roughly a
# 10-15 second clip.
MAX_SCRIPT_WORDS = 30

_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")


def first_sentence(text: str) -> str:
    """Return the first sentence of ``text`` (empty string if there is none)."""
    text = (text or "").strip()
    if not text:
        return ""
    parts = _SENTENCE_BOUNDARY.split(text, maxsplit=1)
    return parts[0].strip()


def _cap_words(text: str, max_words: int) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words])


def build_script(title: str, introduction: str = "") -> str:
    """Compose the narration from a title and an introduction.

    Produces ``"<title>. <first sentence of introduction>"`` and then enforces
    the word cap. If there is no introduction, the title alone is narrated.
    """
    title = (title or "").strip()
    intro_sentence = first_sentence(introduction)

    if title and title[-1:] not in ".!?":
        title = f"{title}."

    if intro_sentence:
        script = f"{title} {intro_sentence}".strip()
    else:
        script = title

    return _cap_words(script, MAX_SCRIPT_WORDS).strip()


def build_script_for_page(page) -> str:
    """Build the narration for a Wagtail page.

    Uses ``title`` (always present) and ``introduction`` when the page type has
    one (blog articles do). No other field — notably not the body — is read.
    """
    title = getattr(page, "title", "") or ""
    introduction = getattr(page, "introduction", "") or ""
    return build_script(title, introduction)
