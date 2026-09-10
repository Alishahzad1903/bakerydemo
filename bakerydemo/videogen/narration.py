"""Build the narration script from an article's *own* words.

The script is composed solely from the article's title and the first sentence
of its introduction - no external service is asked to rewrite, summarise or
expand it. The result is hard-capped to keep the finished clip short (and cheap
to produce): a ~10-15 second, <=30-word narration.
"""

from __future__ import annotations

import re

#: Hard ceiling on narration length. The finished video's cost scales with its
#: length, so this cap is a spend control, not a stylistic choice.
MAX_WORDS = 30

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", (text or "").strip())


def first_sentence(text: str) -> str:
    """Return the first sentence of ``text`` (empty string if none)."""
    normalized = _normalize(text)
    if not normalized:
        return ""
    return _SENTENCE_SPLIT_RE.split(normalized)[0].strip()


def _cap_words(text: str, max_words: int = MAX_WORDS) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words])


def build_narration_script(page) -> str:
    """Compose the narration for ``page`` from its title and intro.

    ``page`` may be a base ``Page`` or a specific type; only ``title`` and (when
    present) ``introduction`` are read. The two are joined into a single spoken
    line and truncated to :data:`MAX_WORDS`.
    """
    title = _normalize(getattr(page, "title", ""))
    intro_sentence = first_sentence(getattr(page, "introduction", "") or "")

    if title and intro_sentence:
        # Avoid a double full stop if the title already ends a sentence.
        separator = " " if title[-1] in ".!?" else ". "
        script = f"{title}{separator}{intro_sentence}"
    else:
        script = title or intro_sentence

    return _cap_words(script)
