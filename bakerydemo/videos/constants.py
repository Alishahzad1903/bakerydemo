"""Fixed VideoGen request parameters for article videos.

These values encode the required "cheap shape" for every video this integration
produces. They are intentionally centralised and constant — none of them is a
per-request option — so the spend profile of a produced video cannot drift.

    * Visuals: stock footage only (never AI-generated imagery).
    * Narration: a synthesized voice only (no presenter / avatar / talking head).
    * Aspect ratio: 16:9.
    * Export: a single 720p render.

VideoGen's ``script-to-video`` visual style ``{"type": "STOCK"}`` selects stock
footage; ``AI_IMAGE`` (the other documented type) would generate imagery and is
never used here. On export, ``quality`` accepts only ``STANDARD`` and ``HIGH``;
``STANDARD`` renders 720p and ``HIGH`` renders 1080p — so ``STANDARD`` is the
720p option and 4K is not reachable through this path at all.
"""

from __future__ import annotations

# script-to-video workflow parameters
STOCK_VISUAL_STYLE = {"type": "STOCK"}
ASPECT_RATIO_16_9 = {"width": 16, "height": 9}

# export parameters: STANDARD == 720p (HIGH would be 1080p).
EXPORT_QUALITY_720P = "STANDARD"

# Narration length ceiling. Cost scales with the finished video's length, so the
# script is capped to the article's title plus the first sentence of its
# introduction, never more than this many words.
MAX_NARRATION_WORDS = 30
