"""Build the narration script from an article's own words.

The script is deliberately tiny: the article's **title** plus the **first
sentence of its introduction**, capped at 30 words. This is built only from the
article's own text — it is never sent to another service to be rewritten,
summarised or expanded. The article body is available as the article's own text
but is intentionally excluded to keep the produced clip short (~10-15s).
"""

from __future__ import annotations

import re

# Ceiling on narration length. Cost scales with the finished video's length, so
# this cap is a hard budget guard, not a stylistic preference.
MAX_NARRATION_WORDS = 30

# Split on a sentence terminator (. ! ?) followed by whitespace. Kept simple on
# purpose; the goal is "roughly the first sentence", not linguistic perfection.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _first_sentence(text: str) -> str:
    text = " ".join((text or "").split())
    if not text:
        return ""
    return _SENTENCE_SPLIT.split(text, maxsplit=1)[0].strip()


def _cap_words(text: str, max_words: int) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words])


def build_narration_script(*, title: str, introduction: str) -> str:
    """Return the narration script for an article.

    ``title`` and ``introduction`` are the article's own fields. The result is
    ``"<title>. <first sentence of introduction>"`` collapsed to single spaces
    and truncated to at most :data:`MAX_NARRATION_WORDS` words. The title is
    always preserved; only the introduction sentence is trimmed to fit.
    """
    title = " ".join((title or "").split()).strip()
    sentence = _first_sentence(introduction)

    if title and not title.endswith((".", "!", "?")):
        lead = f"{title}."
    else:
        lead = title

    script = f"{lead} {sentence}".strip() if sentence else lead
    return _cap_words(script, MAX_NARRATION_WORDS).strip()
