"""Build the narration script from an article's own words.

The script is deliberately tiny - the article's **title** plus the **first
sentence of its introduction**, and nothing else - and is hard-capped at
``MAX_WORDS`` words. Keeping the script short is the single most important cost
control: VideoGen bills by the finished clip's length, so a ~10-15 second clip is
the target. The body is never used, and the text is taken verbatim from the page;
it is never sent elsewhere to be rewritten, summarised or expanded.
"""

from __future__ import annotations

import re

from django.utils.html import strip_tags

# Hard ceiling from the spend rules. Shorter is better.
MAX_WORDS = 30

# Split on the first sentence terminator (., !, ?) followed by whitespace or end.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s")


def _first_sentence(text: str) -> str:
    """Return the first sentence of ``text`` (plain text, whitespace-collapsed)."""
    cleaned = re.sub(r"\s+", " ", strip_tags(text or "")).strip()
    if not cleaned:
        return ""
    parts = _SENTENCE_END.split(cleaned, maxsplit=1)
    return parts[0].strip()


def _cap_words(text: str, max_words: int = MAX_WORDS) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words])


def build_script(*, title: str, introduction: str) -> str:
    """Compose the narration from a title and an introduction.

    Returns ``"{title}. {first sentence of introduction}"``, capped at
    ``MAX_WORDS`` words. If the introduction is empty, only the title is used.
    """
    title = re.sub(r"\s+", " ", strip_tags(title or "")).strip()
    sentence = _first_sentence(introduction)

    if title and not re.search(r"[.!?]$", title):
        title = f"{title}."

    script = f"{title} {sentence}".strip() if sentence else title
    return _cap_words(script)


def build_script_for_page(page) -> str:
    """Build the narration script for a Wagtail ``BlogPage``-like object."""
    return build_script(
        title=getattr(page, "title", "") or "",
        introduction=getattr(page, "introduction", "") or "",
    )
