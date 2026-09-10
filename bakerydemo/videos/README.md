# Article videos (VideoGen integration)

Turn a **published blog article** into a short, narrated MP4 using
[VideoGen](https://videogen.io), and retrieve it through the site. This is an
**additive** capability: it does not change the existing pages, blog, images, or
admin flows.

## Endpoints

All three live on the existing Wagtail v3 HTTP API (`/api/v3-preview/`), use its
bearer-token authentication, and are restricted to callers permitted to
**publish** the target page.

| Method & path | Purpose |
| --- | --- |
| `POST /api/v3-preview/pages/{page_id}/video/` | Start producing a video for the article. Returns `202` with `{"videoJobId": ...}` on first request; `200` with the same id on repeats (idempotent — never a second render/bill). |
| `GET /api/v3-preview/pages/{page_id}/video/{videoJobId}/` | Report the job: `status` (`pending`/`processing`/`succeeded`/`failed`), `progressPercentage`, `downloadUrl` (set when ready), `error` (set when failed). |
| `GET /api/v3-preview/pages/{page_id}/video/{videoJobId}/download/` | Stream the finished MP4 through the site. |

Example:

```bash
BASE=http://localhost:8000/api/v3-preview
TOKEN=wagtail_...            # a token whose user may publish the page

# Start (page 62 is "Tracking Wild Yeast" in the demo data)
curl -X POST "$BASE/pages/62/video/" -H "Authorization: Bearer $TOKEN"
# -> {"videoJobId": "…", "status": "pending", "progressPercentage": 0}

# Poll
curl "$BASE/pages/62/video/<videoJobId>/" -H "Authorization: Bearer $TOKEN"
# -> {"videoJobId":"…","status":"succeeded","progressPercentage":100,
#     "downloadUrl":"…/download/","error":null}

# Download
curl -L -o article.mp4 "$BASE/pages/62/video/<videoJobId>/download/" \
  -H "Authorization: Bearer $TOKEN"
```

## Narration

The script is built **only from the article's own words** — its title followed
by the first sentence of its introduction — and is narrated verbatim. The
article is never sent to another service to be rewritten or summarised. The
script is hard-capped at `VIDEOGEN_NARRATION_MAX_WORDS` (30) words to keep the
clip short (~10–15 s). `narration.extract_body_text` shows the body is part of
the article's "own words", but the body is intentionally not narrated (cost).

## Video shape (cost controls)

Fixed, conservative "cheap shape", configured in settings:

- **Stock footage** visuals only — never AI-generated imagery.
- **Voiceover only** — no avatar/presenter (no `actorEntityId`).
- **One 720p export** (`STANDARD` tier — the ladder is STANDARD 720p, HIGH
  1080p, FULL_HIGH 1440p, ULTRA_HIGH 4K), **16:9**, never resized or re-exported.
- No remix actions, upscaling, image-to-video, or standalone media generation.

## Configuration

Read from the environment at run time (never hard-coded, never committed):

- `VIDEOGEN_API_KEY` — **required**. Bearer key for the VideoGen account.
- `VIDEOGEN_BASE_URL` — optional. Used verbatim as the API base when set;
  defaults to `https://api.videogen.io`.

Other tunables (see `bakerydemo/settings/base.py`): `VIDEOGEN_VISUAL_STYLE`,
`VIDEOGEN_EXPORT_QUALITY`, `VIDEOGEN_ASPECT_RATIO`, `VIDEOGEN_NARRATION_MAX_WORDS`,
`VIDEOGEN_POLL_INTERVAL_SECONDS`, `VIDEOGEN_RENDER_TIMEOUT_SECONDS`,
`VIDEOGEN_HTTP_TIMEOUT_SECONDS`.

## How it works

1. `POST` validates the page is a live `BlogPage`, checks publish permission,
   builds the narration, and (idempotently, one row per page) creates an
   `ArticleVideo`. A new job launches a background daemon thread and the request
   returns immediately with `202`.
2. The pipeline (`services.run_pipeline`) calls VideoGen: script-to-video render
   → poll → single 720p export → poll → download the MP4 into local storage.
   Progress is written to the row throughout.
3. The finished MP4 is served through the `download/` endpoint and stays
   available for as long as the article exists (`ArticleVideo` is deleted with
   the page via `on_delete=CASCADE`).

No task queue/broker is used (per the environment constraints); the pipeline
runs on a background thread within the Django process.

## Failure handling

Every provider failure surfaces as a typed exception
(`bakerydemo.videos.exceptions`): configuration, API/auth/credits, connection,
timeout, and terminal job-failed errors. The pipeline records the failure on the
`ArticleVideo` row, and `GET` returns it as `status: "failed"` with `error`.

## Tests

```bash
env DJANGO_SETTINGS_MODULE=bakerydemo.settings.test ./manage.py test bakerydemo.videos
```

Tests use a fake VideoGen client and never hit the network.
