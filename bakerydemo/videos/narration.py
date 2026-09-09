"""Build the spoken narration script from an article's *own* text.

The narration is assembled only from the article's own fields — its title and
its introduction — and is never sent to any other service to be rewritten,
summarised or expanded. To keep the produced clip short (cost scales with
length), the script is capped to a small number of words: the title plus the
first sentence of the introduction, bounded by ``max_words``.
"""

from __future__ import annotations

import re

# End of a sentence: ., ! or ? followed by whitespace or end-of-text.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def first_sentence(text: str) -> str:
    """Return the first sentence of ``text`` (best effort), trimmed."""
    text = (text or "").strip()
    if not text:
        return ""
    parts = _SENTENCE_END.split(text, maxsplit=1)
    return parts[0].strip()


def _cap_words(text: str, max_words: int) -> str:
    words = text.split()
    if max_words > 0 and len(words) > max_words:
        return " ".join(words[:max_words])
    return text


def build_narration_script(page, *, max_words: int = 30) -> str:
    """Compose the narration script for ``page``.

    Uses the article's title and the first sentence of its introduction, then
    caps the result to ``max_words`` whole words. Returns a plain string.
    """
    title = (getattr(page, "title", "") or "").strip()
    introduction = (getattr(page, "introduction", "") or "").strip()

    segments = []
    if title:
        # A trailing period turns the title into its own spoken sentence.
        segments.append(title if title[-1] in ".!?" else f"{title}.")
    intro_sentence = first_sentence(introduction)
    if intro_sentence:
        segments.append(intro_sentence)

    script = " ".join(segments).strip()
    return _cap_words(script, max_words)
