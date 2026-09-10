"""Build the narration script from an article's own words.

The script is composed of *only* the article's title and the first sentence of
its introduction — the article's own text, used verbatim. Nothing is sent to any
other service to be rewritten, summarised or expanded, and the body is not
narrated. The result is capped at :data:`MAX_SCRIPT_WORDS` words to keep the
finished clip short (~10-15s); shorter is better.
"""

from __future__ import annotations

import re

#: Hard ceiling on narration length. Cost scales with the finished video's
#: length, so the script is trimmed to at most this many words.
MAX_SCRIPT_WORDS = 30

# Matches the end of the first sentence: a ., ! or ? followed by whitespace or
# end-of-string. Kept intentionally simple — we only need the first sentence.
_SENTENCE_END = re.compile(r"[.!?](?:\s|$)")


def first_sentence(text: str) -> str:
    """Return the first sentence of ``text`` (without a trailing newline run)."""
    text = (text or "").strip()
    if not text:
        return ""
    match = _SENTENCE_END.search(text)
    if match:
        # Include the terminating punctuation mark itself.
        return text[: match.start() + 1].strip()
    return text


def _cap_words(text: str, max_words: int) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words])


def build_narration_script(
    title: str, introduction: str, *, max_words: int = MAX_SCRIPT_WORDS
) -> str:
    """Compose the narration script from ``title`` + first intro sentence.

    Only the article's own words are used. The combined text is capped at
    ``max_words`` words.
    """
    title = (title or "").strip()
    intro_sentence = first_sentence(introduction)

    if title and intro_sentence:
        # A period after the title gives the narrator a natural pause.
        separator = " " if title.endswith((".", "!", "?")) else ". "
        script = f"{title}{separator}{intro_sentence}"
    else:
        script = title or intro_sentence

    return _cap_words(script.strip(), max_words)
