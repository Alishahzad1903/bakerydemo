"""Build the narration script from an article's own words.

The script is the article's **title** followed by the **first sentence of its
introduction**, and nothing else. It is capped at a small word count so the
finished clip stays short (~10-15s). Nothing here contacts any external
service: the narration is derived purely from the article's own text.
"""

from __future__ import annotations

import re

# The maximum number of words we will narrate. Cost scales with clip length, so
# the script is kept deliberately short — title + first sentence of the intro.
MAX_SCRIPT_WORDS = 30

_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")


def first_sentence(text: str) -> str:
    """Return the first sentence of ``text`` (empty string when blank)."""
    text = (text or "").strip()
    if not text:
        return ""
    return _SENTENCE_BOUNDARY.split(text, maxsplit=1)[0].strip()


def build_narration_script(
    title: str,
    introduction: str,
    *,
    max_words: int = MAX_SCRIPT_WORDS,
) -> str:
    """Compose the narration from the article's title and intro first sentence.

    The two are joined into one short piece of speech, then truncated to
    ``max_words`` words as a hard ceiling.
    """
    title = (title or "").strip()
    if title and title[-1] not in ".!?":
        title = f"{title}."

    intro_sentence = first_sentence(introduction)
    script = " ".join(part for part in (title, intro_sentence) if part).strip()

    words = script.split()
    if len(words) > max_words:
        script = " ".join(words[:max_words])
    return script
