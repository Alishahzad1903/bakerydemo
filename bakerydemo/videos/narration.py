"""
Build narration text from a blog article's own words.

The script sent to VideoGen is composed *only* from the article itself — its
title, its introduction, and the text of the first few body paragraphs. The
article is never sent to any other service to be rewritten, summarised or
expanded first.
"""

from __future__ import annotations

import html
import re

# Keep narration short (spend control): the title, the introduction, and the
# first three body paragraphs are enough.
MAX_BODY_PARAGRAPHS = 3

_TAG_RE = re.compile(r"<[^>]+>")
# Break on the boundaries of block-level elements so a heading, list item or
# quote embedded in the rich text becomes its own paragraph rather than gluing
# onto the following text.
_PARAGRAPH_SPLIT_RE = re.compile(
    r"</(?:p|h[1-6]|li|div|blockquote|figcaption)\s*>|<br\s*/?>",
    re.IGNORECASE,
)
_WHITESPACE_RE = re.compile(r"\s+")


def _html_to_paragraphs(source: str) -> list[str]:
    """Turn a rich-text HTML fragment into a list of plain-text paragraphs."""
    paragraphs = []
    for chunk in _PARAGRAPH_SPLIT_RE.split(source or ""):
        text = _TAG_RE.sub("", chunk)
        text = html.unescape(text)
        text = _WHITESPACE_RE.sub(" ", text).strip()
        if text:
            paragraphs.append(text)
    return paragraphs


def extract_body_paragraphs(page, limit: int = MAX_BODY_PARAGRAPHS) -> list[str]:
    """Return the first ``limit`` plain-text paragraphs from a page body.

    Only ``paragraph_block`` (rich text) content contributes; headings, images,
    quotes and embeds are skipped so the narration is the article's prose.
    """
    paragraphs: list[str] = []
    body = getattr(page, "body", None)
    if not body:
        return paragraphs
    for block in body:
        if block.block_type != "paragraph_block":
            continue
        # RichTextBlock values expose their stored HTML via ``.source``.
        source = getattr(block.value, "source", None)
        if source is None:
            source = str(block.value)
        for paragraph in _html_to_paragraphs(source):
            paragraphs.append(paragraph)
            if len(paragraphs) >= limit:
                return paragraphs
    return paragraphs


def build_narration(page) -> str:
    """Assemble the verbatim narration script for ``page``.

    Composed from the article's title, introduction and first three body
    paragraphs, in that order. Returns a single string used verbatim by
    VideoGen as the spoken narration.
    """
    parts: list[str] = []

    title = (getattr(page, "title", "") or "").strip()
    if title:
        parts.append(title)

    introduction = (getattr(page, "introduction", "") or "").strip()
    if introduction:
        parts.append(introduction)

    parts.extend(extract_body_paragraphs(page))

    return "\n\n".join(parts).strip()
