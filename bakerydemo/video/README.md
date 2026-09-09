# Article-to-video (VideoGen)

Turns a **published blog article** into a short, narrated MP4 using
[VideoGen](https://videogen.io). It is purely additive: it adds two API
endpoints and one model, and does not change any existing page, blog, image or
admin behaviour.

## Endpoints

Mounted on the existing Wagtail v3 preview API and authenticated exactly like
the rest of it (a bearer API token). Producing a video is an editorial action,
so both endpoints require the caller to be permitted to **publish that page**.

| Method & path | Purpose |
| --- | --- |
| `POST /api/v3-preview/pages/{page_id}/video/` | Start producing a video. Returns `202 {"videoJobId": …}`. Returns immediately; production continues in the background. |
| `GET /api/v3-preview/pages/{page_id}/video/{videoJobId}/` | Report job state: `{"videoJobId", "status", "progressPercentage", "downloadUrl", "error"}`. |

`status` is one of `pending`, `processing`, `ready`, `failed`. When `ready`,
`downloadUrl` points at the finished MP4; when `failed`, `error` says why.

## How it works

1. **Narration** (`narration.py`) is built from the article's *own* text only —
   its title, its introduction, and the first three paragraphs of its body.
   Nothing is sent anywhere to be rewritten or summarised first.
2. **Production** (`service.py`) drives VideoGen through its documented flow via
   the typed client (`client.py`):
   - `POST /v1/workflows/script-to-video` with `visualStyle: {type: STOCK}` and
     no actor — stock footage, voice-only.
   - poll `GET /v1/workflows/runs/{id}` to completion.
   - `POST /v1/projects/{id}/export` (quality `STANDARD`, so the MP4 stays at or
     below 1080p), then poll `GET /v1/projects/{id}/exports/{id}`.
3. The finished export's signed `downloadUrl` (and its expiry) are stored on the
   `VideoJob`. On each status read the URL is re-signed when close to expiry, so
   the MP4 stays retrievable for as long as the article exists.

Production runs in a background daemon thread — no broker, queue or extra infra.
Set `VIDEOGEN_EXECUTE_INLINE=1` to run it synchronously (used by tests), or run a
job by hand with `manage.py run_video_job <videoJobId>`.

## Idempotency & spend safety

At most one *active* job (pending/processing/ready) can exist per page — enforced
by a database unique constraint. A repeated request returns the existing job, so
a video is never produced or billed twice. A failed job may be retried.

## Failure handling

Every provider failure surfaces as a typed `VideoGenError` subclass
(`exceptions.py`): auth, not-found, rate-limit, bad-request, server, connection,
timeout, and terminal production failures. Rate limits (429) are retried with
backoff. A failed job records the provider's message in `error`.

## Configuration

All read from the environment via Django settings; no value is ever hard-coded.

| Setting | Default | Purpose |
| --- | --- | --- |
| `VIDEOGEN_API_KEY` | – | Bearer credential (required). |
| `VIDEOGEN_BASE_URL` | `https://api.videogen.io` | Used **verbatim** as the API base when set. |
| `VIDEOGEN_EXPORT_QUALITY` | `STANDARD` | Export tier; keep at/below 1080p. |
| `VIDEOGEN_VOICE_ID` | – | Narration voice; VideoGen default when unset. |
| `VIDEOGEN_ASPECT_RATIO` | `16:9` | Sent as `{width, height}`. |
| `VIDEOGEN_POLL_INTERVAL` / `VIDEOGEN_POLL_TIMEOUT` / `VIDEOGEN_HTTP_TIMEOUT` | `5` / `1800` / `30` | Polling and HTTP tuning (seconds). |
