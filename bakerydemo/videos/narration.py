"""Build narration text from an article's own words.

The narration is derived *only* from the article's own fields (its title and its
introduction) — it is never sent to another service to be rewritten, summarised
or expanded first.

The spend policy caps what we narrate to the article's **title and the first
sentence of its introduction**, and no more than :data:`MAX_WORDS` words. The
article body is deliberately *not* narrated: cost scales with the finished
video's length, so a longer script is the single most expensive mistake here.
"""

from __future__ import annotations

import re

#: Hard ceiling on narration length (words). Shorter is cheaper; this is a cap.
MAX_WORDS = 30

_SENTENCE_END = re.compile(r"[.!?]")


def first_sentence(text: str) -> str:
    """Return the first sentence of ``text`` (up to the first ``.``/``!``/``?``)."""
    text = (text or "").strip()
    if not text:
        return ""
    match = _SENTENCE_END.search(text)
    if match:
        return text[: match.end()].strip()
    return text


def build_narration_script(
    *, title: str, introduction: str, max_words: int = MAX_WORDS
) -> str:
    """Compose the narration script from the article's title and introduction.

    Returns ``"<title>. <first sentence of introduction>"``, truncated to at most
    ``max_words`` words. The body is intentionally excluded (see module docstring).
    """
    title = (title or "").strip()
    intro_sentence = first_sentence(introduction)

    parts: list[str] = []
    if title:
        # End the title with punctuation so the two clauses read as separate
        # spoken sentences rather than running together.
        parts.append(title if title[-1] in ".!?" else f"{title}.")
    if intro_sentence:
        parts.append(intro_sentence)

    script = " ".join(parts).strip()

    words = script.split()
    if len(words) > max_words:
        script = " ".join(words[:max_words])
    return script
