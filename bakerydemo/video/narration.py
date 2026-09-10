"""Build the narration script from an article's own words.

The narration is deliberately built from **only** the article's own text — its
title and the first sentence of its introduction — and nothing else. The body is
intentionally excluded: the finished clip is billed per second, so the script is
kept to a hard ceiling of :data:`MAX_WORDS` words (a ~10-15s clip). Nothing here
sends the article to any other service to be rewritten or summarised; the text is
used verbatim.
"""

from __future__ import annotations

import re

# Hard ceiling on the narration length. Cost scales directly with the finished
# video's length, so this cap is a spend control, not a stylistic choice.
MAX_WORDS = 30

# Sentence terminators used to isolate the first sentence of the introduction.
_SENTENCE_END = re.compile(r"[.!?]")


def first_sentence(text: str) -> str:
    """Return the first sentence of ``text`` (without its trailing terminator)."""
    collapsed = " ".join(text.split())
    if not collapsed:
        return ""
    match = _SENTENCE_END.search(collapsed)
    if match:
        return collapsed[: match.start()].strip()
    return collapsed


def cap_words(text: str, max_words: int = MAX_WORDS) -> str:
    """Collapse whitespace in ``text`` and truncate to ``max_words`` words."""
    words = text.split()
    return " ".join(words[:max_words])


def build_narration_script(title: str, introduction: str) -> str:
    """Compose the narration from an article title and introduction.

    The script is ``"<title>. <first sentence of introduction>"``, collapsed to
    single spaces and truncated to :data:`MAX_WORDS` words. The body is never
    used.
    """
    title = " ".join((title or "").split())
    intro_sentence = first_sentence(introduction or "")

    if title and intro_sentence:
        script = f"{title}. {intro_sentence}"
    else:
        script = title or intro_sentence

    return cap_words(script)


def build_narration_for_page(page) -> str:
    """Build the narration script for a Wagtail ``BlogPage``-like object."""
    return build_narration_script(
        title=getattr(page, "title", "") or "",
        introduction=getattr(page, "introduction", "") or "",
    )
