# Article videos (VideoGen integration)

An **additive** capability that turns a published blog article into a short,
narrated MP4 the marketing team can share. It changes none of the existing
pages, blog, images or admin flows — it only adds two endpoints to the existing
Wagtail v3 (preview) HTTP API and a `VideoJob` record behind them.

Videos are produced by [VideoGen](https://videogen.io). All knowledge of
VideoGen's wire protocol lives in `bakerydemo/videos/videogen/`.

## Endpoints

Both are mounted under the existing `/api/v3-preview/` API, use the same
bearer-token authentication, and are restricted to callers permitted to
**publish** the target page.

### Start production

```
POST /api/v3-preview/pages/{page_id}/video/
```

Starts producing a video for the article and returns immediately (production
continues in the background):

```json
202 Accepted
{ "videoJobId": "bac7d410-630a-4f2f-9724-fc6e5288b7ef" }
```

Idempotent: a page has at most one `VideoJob`. Asking again returns the same
`videoJobId` and never starts a second production run, so the account is never
billed twice for the same article.

### Check status / get the download

```
GET /api/v3-preview/pages/{page_id}/video/{videoJobId}/
```

```json
200 OK
{
  "videoJobId": "bac7d410-...",
  "pageId": 62,
  "status": "processing | ready | failed",
  "progressPercentage": 0-100,
  "downloadUrl": "https://...mp4 | null",
  "error": "reason | null"
}
```

- `status` tells the caller unambiguously whether the video is still being
  produced, is ready, or failed.
- When `ready`, `downloadUrl` carries a freshly signed link to the MP4. The URL
  is re-signed on each read, so the finished video stays downloadable for as
  long as the article exists.
- When `failed`, `error` carries what the provider reported.

## How the narration is built

The script is derived **only** from the article's own words — its title and the
first sentence of its introduction — capped at 30 words (see
`narration.py`). Nothing is sent to any other service to be rewritten or
summarised. The short script keeps the produced clip to ~10-15 seconds.

## What is asked of VideoGen (and what is not)

The pipeline (`services.run_pipeline`) makes the cheapest documented shape:

1. `POST /v1/workflows/script-to-video` — stock footage visuals, voice
   narration only, `aspectRatio` `{width: 16, height: 9}`.
2. Poll the workflow run to completion.
3. `POST /v1/projects/{id}/export` — a single export at `STANDARD` quality
   (720p), default watermark/end-screen handling.
4. Poll the export to a downloadable MP4.

It never requests AI imagery, avatars/presenters, image-to-video, upscaling or
regeneration, standalone media, or a second export.

## Configuration

Read from the environment via Django settings (`settings/base.py`); no secret is
ever hard-coded:

- `VIDEOGEN_API_KEY` (required) — bearer token for the VideoGen account.
- `VIDEOGEN_BASE_URL` (optional) — overrides the default `https://api.videogen.io`.

## Error handling

Every provider failure surfaces as a typed exception from
`bakerydemo.videos.videogen.exceptions` (`VideoGenAuthError`,
`VideoGenRateLimitError`, `VideoGenJobFailedError`, …), never a raw HTTP error.
Failures during background production are recorded on the job as
`status = "failed"` with the provider's message in `error`.

## Verifying it works

With the site set up (venv, `migrate`, `load_initial_data`) and
`VIDEOGEN_API_KEY` exported, run the server and, using the seeded `admin` token:

```bash
# 1. Start a video for "Tracking Wild Yeast" (page 62 in the demo data)
curl -s -X POST \
  -H "Authorization: Bearer $WAGTAIL_CLI_TOKEN" \
  http://localhost:8000/api/v3-preview/pages/62/video/
# -> {"videoJobId": "..."}

# 2. Poll until status is "ready"
curl -s -H "Authorization: Bearer $WAGTAIL_CLI_TOKEN" \
  http://localhost:8000/api/v3-preview/pages/62/video/<videoJobId>/

# 3. Download the MP4 from the returned downloadUrl
curl -L -o article.mp4 "<downloadUrl>"
```

The seeded tokens exercise access control: `admin`, `moderator` and `arabic`
are admitted; `editor` is refused with `403` (no publish permission); the
`inactive` user and the revoked `german` token are refused with `401`.

## Tests

```bash
env DJANGO_SETTINGS_MODULE=bakerydemo.settings.test ./manage.py test bakerydemo.videos
```

The tests mock VideoGen, so they never contact the provider or incur cost.
