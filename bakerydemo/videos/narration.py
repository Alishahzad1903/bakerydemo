"""Build the narration script for an article's video.

The script is derived **only** from the article's own text — never sent to any
external service to be rewritten, summarised, or expanded. To honour the hard
spend ceiling (a ~10-15s clip), the narration is deliberately limited to the
article's title plus the first sentence of its introduction, capped at
:data:`MAX_NARRATION_WORDS` words. The article body is intentionally *not*
narrated: cost scales directly with clip length, and the body would blow the
ceiling.
"""

from __future__ import annotations

import re

# Hard ceiling on narration length. Cost scales with the finished clip's
# duration, so this is a spend guard, not a stylistic preference.
MAX_NARRATION_WORDS = 30

_SENTENCE_END = re.compile(r"^(.*?[.!?])(?:\s|$)", re.DOTALL)
_WHITESPACE = re.compile(r"\s+")


def first_sentence(text: str) -> str:
    """Return the first sentence of ``text`` (up to and including its terminator).

    Falls back to the whole (whitespace-collapsed) string when no sentence
    terminator is present.
    """
    collapsed = _WHITESPACE.sub(" ", (text or "").strip())
    if not collapsed:
        return ""
    match = _SENTENCE_END.match(collapsed)
    return match.group(1).strip() if match else collapsed


def _with_terminal_punctuation(segment: str) -> str:
    segment = segment.strip()
    if segment and segment[-1] not in ".!?":
        return f"{segment}."
    return segment


def _truncate_words(text: str, max_words: int) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words])


def build_narration_script(page, *, max_words: int = MAX_NARRATION_WORDS) -> str:
    """Compose the narration for ``page`` from its own title and introduction.

    Args:
        page: A Wagtail page exposing ``title`` and (optionally) ``introduction``.
        max_words: Word ceiling for the finished script.

    Returns:
        A short narration string, e.g.
        ``"Tracking Wild Yeast. Yeasts, with their single-celled growth habit, ..."``
    """
    title = _WHITESPACE.sub(" ", (getattr(page, "title", "") or "").strip())
    intro = getattr(page, "introduction", "") or ""

    segments = []
    if title:
        segments.append(_with_terminal_punctuation(title))
    intro_sentence = first_sentence(intro)
    if intro_sentence:
        segments.append(_with_terminal_punctuation(intro_sentence))

    script = " ".join(segments).strip()
    return _truncate_words(script, max_words)
