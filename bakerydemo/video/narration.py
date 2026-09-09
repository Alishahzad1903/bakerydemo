"""
Build the narration script from a blog article's *own* text.

The script is exactly the article's title, its introduction and the first three
paragraphs of its body. Nothing is sent anywhere to be rewritten, summarised or
expanded first — VideoGen receives this text verbatim and narrates it.
"""

import html
import re

from django.utils.html import strip_tags

# Matches the inner HTML of each <p>...</p> in a rich text block, in order.
_PARAGRAPH_RE = re.compile(r"<p\b[^>]*>(.*?)</p>", re.IGNORECASE | re.DOTALL)

# Blog body StreamField block that holds prose (see base.blocks.BaseStreamBlock).
_PARAGRAPH_BLOCK = "paragraph_block"

DEFAULT_MAX_BODY_PARAGRAPHS = 3


def _clean(fragment_html):
    """Turn a fragment of rich-text HTML into a single line of plain text."""
    text = strip_tags(fragment_html)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def extract_body_paragraphs(page, limit=DEFAULT_MAX_BODY_PARAGRAPHS):
    """
    Return up to ``limit`` plain-text paragraphs from the article body, taken
    in document order from the rich-text (paragraph) blocks.
    """
    paragraphs = []
    for block in page.body:
        if block.block_type != _PARAGRAPH_BLOCK:
            continue
        for fragment in _PARAGRAPH_RE.findall(str(block.value)):
            cleaned = _clean(fragment)
            if not cleaned:
                continue
            paragraphs.append(cleaned)
            if len(paragraphs) >= limit:
                return paragraphs
    return paragraphs


def build_narration(page, *, max_body_paragraphs=DEFAULT_MAX_BODY_PARAGRAPHS):
    """
    Assemble the narration for ``page`` from its title, introduction and the
    first ``max_body_paragraphs`` body paragraphs. Returns a single string with
    blank lines between parts.
    """
    parts = []

    title = (page.title or "").strip()
    if title:
        parts.append(title)

    introduction = (getattr(page, "introduction", "") or "").strip()
    if introduction:
        parts.append(introduction)

    parts.extend(extract_body_paragraphs(page, limit=max_body_paragraphs))

    return "\n\n".join(parts)
