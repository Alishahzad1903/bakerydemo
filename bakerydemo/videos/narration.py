"""Build the narration script from an article's own words.

The narration is assembled *only* from the article's own text — its title, its
introduction, and (available but intentionally unused, see below) the text of
its body. The article is never sent to another service to be rewritten,
summarised, or expanded first.

Spend policy: cost scales directly with the finished video's length, so the
emitted script is deliberately tiny — the article title followed by the first
sentence of its introduction, hard-capped at ``max_words`` words
(``settings.VIDEOGEN_NARRATION_MAX_WORDS``, 30 by default). The body is not
narrated. :func:`extract_body_text` exists so the "own words" source clearly
includes the body, but it is excluded from the produced narration on purpose.
"""

from __future__ import annotations

import re

# A sentence ends at . ! or ? followed by whitespace or end-of-string. This is
# a pragmatic splitter (not linguistically perfect), which is fine because we
# only ever take the first sentence of short editorial intro copy.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_WHITESPACE = re.compile(r"\s+")


def _normalise(text: str) -> str:
    return _WHITESPACE.sub(" ", (text or "").strip())


def first_sentence(text: str) -> str:
    """Return the first sentence of ``text`` (empty string if there is none)."""
    normalised = _normalise(text)
    if not normalised:
        return ""
    return _SENTENCE_END.split(normalised, maxsplit=1)[0].strip()


def extract_body_text(page) -> str:
    """Return the plain text of a BlogPage body StreamField.

    Part of the article's "own words". Not used in the produced narration under
    the current spend policy, but kept so the source of truth is unambiguous.
    """
    parts: list[str] = []
    for block in getattr(page, "body", []) or []:
        value = block.value
        # RichText / text blocks stringify to their (HTML) content; strip tags.
        text = re.sub(r"<[^>]+>", " ", str(value))
        text = _normalise(text)
        if text:
            parts.append(text)
    return " ".join(parts)


def _cap_words(text: str, max_words: int) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words])


def build_narration_script(page, *, max_words: int) -> str:
    """Compose the narration for ``page`` from its own title and introduction.

    Returns ``"<title>. <first sentence of introduction>"``, normalised and
    capped to ``max_words`` words, ending with terminal punctuation. Raises
    :class:`ValueError` if there is no usable text at all.
    """
    title = _normalise(getattr(page, "title", ""))
    intro_sentence = first_sentence(getattr(page, "introduction", ""))

    segments = [seg for seg in (title, intro_sentence) if seg]
    if not segments:
        raise ValueError("Article has no title or introduction to narrate.")

    # Join with sentence punctuation so the title reads as its own sentence.
    script = ". ".join(seg.rstrip(".!?") for seg in segments)
    script = _cap_words(script, max_words).rstrip(",;: ")
    if script and script[-1] not in ".!?":
        script += "."
    return script
