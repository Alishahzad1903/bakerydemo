"""Build narration scripts from an article's own words.

The narration is composed *only* from the article's own text — never sent to
any other service to be rewritten, summarised or expanded. To keep the produced
clip short (and therefore cheap), the script is the article's title plus the
first sentence of its introduction, hard-capped at a small word budget.
"""

from __future__ import annotations

import re

from django.conf import settings

#: Absolute ceiling on the narration length. Cost scales with the finished
#: video's length, so the script is capped well under a spoken-minute.
DEFAULT_MAX_SCRIPT_WORDS = 30

_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")
_WHITESPACE = re.compile(r"\s+")


def first_sentence(text: str) -> str:
    """Return the first sentence of ``text`` (or the whole thing if it is one)."""
    normalised = _WHITESPACE.sub(" ", (text or "").strip())
    if not normalised:
        return ""
    return _SENTENCE_BOUNDARY.split(normalised, maxsplit=1)[0].strip()


def _max_words() -> int:
    value = getattr(settings, "VIDEOGEN_MAX_SCRIPT_WORDS", DEFAULT_MAX_SCRIPT_WORDS)
    try:
        value = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_SCRIPT_WORDS
    return max(1, value)


def cap_words(text: str, max_words: int) -> str:
    """Trim ``text`` to at most ``max_words`` words."""
    words = _WHITESPACE.sub(" ", text.strip()).split(" ")
    words = [word for word in words if word]
    if len(words) <= max_words:
        return " ".join(words)
    return " ".join(words[:max_words])


def build_script(title: str, introduction: str, *, max_words: int | None = None) -> str:
    """Compose the narration script from an article's title and introduction.

    The result is ``"<title>. <first sentence of introduction>"``, normalised
    and capped at the configured word budget. Only the article's own text is
    used.
    """
    limit = max_words if max_words is not None else _max_words()

    clean_title = _WHITESPACE.sub(" ", (title or "").strip()).rstrip(".!?")
    lead = first_sentence(introduction)

    if clean_title and lead:
        script = f"{clean_title}. {lead}"
    else:
        script = clean_title or lead

    script = cap_words(script, limit)
    if not script:
        raise ValueError("Cannot build a narration script: the article has no text.")
    return script
