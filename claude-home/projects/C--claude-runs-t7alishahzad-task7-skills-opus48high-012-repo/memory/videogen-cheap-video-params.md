---
name: videogen-cheap-video-params
description: Exact VideoGen params for a cheap stock-footage 720p 16:9 video (not in the api skill docs)
metadata:
  type: reference
---

The `api` skill's local files only document `script-to-video` with
`visualStyle: {type: "AI_IMAGE"}` and don't list the stock-footage type or any
export resolution field. Web lookups for VideoGen are blocked in this workspace,
so these were discovered by probing the live API with validation-error requests
that create no billable work (invalid enum / non-existent project id):

- **Stock footage** (never AI imagery): `script-to-video` `visualStyle: {"type": "STOCK"}`. `STOCK` is the only valid stock option (`STOCK_VIDEO`, `STOCK_IMAGE`, etc. are all rejected). `AI_IMAGE` is the only other type.
- **Aspect ratio**: `script-to-video` `aspectRatio: {"width": 16, "height": 9}` (a real field; 16:9 is already the default).
- **720p export**: `POST /v1/projects/{id}/export` has ONE knob, `quality`, accepting only `STANDARD` and `HIGH`. `STANDARD` = 720p (1280×720), `HIGH` = 1080p (1920×1080). Unspecified defaults to 1080p. 4K is not reachable through this export path at all — no export field selects resolution beyond `quality`.

Used by `bakerydemo.videos` — see [[bakerydemo-article-videos]].
