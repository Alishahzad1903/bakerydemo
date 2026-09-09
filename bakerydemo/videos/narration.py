"""Build a narration script from a blog article's own text.

The script is assembled from the article's title, its introduction and the
first three paragraphs of its body — and from nothing else. The text is used
verbatim: it is never sent to any service to be rewritten, summarised or
expanded.
"""

from __future__ import annotations

from html.parser import HTMLParser
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bakerydemo.blog.models import BlogPage

# The number of leading body paragraphs to narrate, per the spend limit.
BODY_PARAGRAPH_LIMIT = 3

# Rich-text tags that delimit a paragraph-like unit of text.
_BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote"}


class _ParagraphExtractor(HTMLParser):
    """Collect the text of each block-level element in a rich-text fragment."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.paragraphs: list[str] = []
        self._buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag in _BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCK_TAGS:
            self._flush()

    def handle_data(self, data: str) -> None:
        self._buffer.append(data)

    def _flush(self) -> None:
        text = "".join(self._buffer).strip()
        self._buffer = []
        if text:
            self.paragraphs.append(text)

    def close(self) -> None:
        super().close()
        self._flush()


def _html_to_paragraphs(html: str) -> list[str]:
    parser = _ParagraphExtractor()
    parser.feed(html or "")
    parser.close()
    return parser.paragraphs


def body_paragraphs(page: BlogPage) -> list[str]:
    """Return the body's text as an ordered list of paragraph-like units.

    Walks the ``StreamField`` body in document order, extracting narratable
    text from paragraph, heading and block-quote blocks (a single rich-text
    paragraph block may contain several ``<p>``/heading elements, each its own
    paragraph). Image and embed blocks contribute no narration.
    """
    paragraphs: list[str] = []
    for block in page.body:
        block_type = block.block_type
        if block_type == "paragraph_block":
            paragraphs.extend(_html_to_paragraphs(str(block.value)))
        elif block_type == "heading_block":
            text = (block.value.get("heading_text") or "").strip()
            if text:
                paragraphs.append(text)
        elif block_type == "block_quote":
            text = (block.value.get("text") or "").strip()
            if text:
                paragraphs.append(text)
    return paragraphs


def build_narration(page: BlogPage) -> str:
    """Assemble the verbatim narration script for ``page`` (a ``BlogPage``)."""
    parts: list[str] = []
    title = (page.title or "").strip()
    if title:
        parts.append(title)
    introduction = (page.introduction or "").strip()
    if introduction:
        parts.append(introduction)
    parts.extend(body_paragraphs(page)[:BODY_PARAGRAPH_LIMIT])
    return "\n\n".join(parts).strip()
