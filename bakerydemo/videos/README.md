# Article videos

Turn one published blog article into a short, narrated MP4 the marketing team
can share — no manual video editing. This is an **additive** capability: it does
not change the existing pages, blog, images or admin flows. Videos are produced
with [VideoGen](https://videogen.io) (stock footage + voice narration).

## Endpoints

Exposed on the site's existing v3 HTTP API (`/api/v3-preview/`), using the same
bearer-token authentication. Producing or inspecting a video is an editorial
action, so it is restricted to callers **permitted to publish that page**.

| Method & path | Purpose |
| --- | --- |
| `POST /api/v3-preview/pages/{page_id}/video/` | Start producing a video for the article. Returns `{ "videoJobId": "…" }`. Returns before production finishes (`202`; `200` if a video already exists for the article — see idempotency). |
| `GET /api/v3-preview/pages/{page_id}/video/{videoJobId}/` | State and outcome: `{ videoJobId, status, progressPercentage, downloadUrl, error }`. |
| `GET /api/v3-preview/pages/{page_id}/video/{videoJobId}/download/` | Stream the finished MP4 (`Content-Disposition: attachment`). |

### `status` values

`ready` and `failed` are terminal; anything else (`pending`, `processing`) means
the video is still being produced. When `ready`, `downloadUrl` points at the
download endpoint above; when `failed`, `error` carries the reason.

### Authentication & permissions

* No / invalid / revoked token, or an inactive user → `401`.
* Authenticated but not permitted to publish the page → `403`.
* The target page must be a **published** `BlogPage`, otherwise → `422`.

## How the narration is built

The spoken narration is assembled **only** from the article's own words — its
title, its introduction, and the text of the first three body paragraphs — and
from nothing else. The article is never sent to any service to be rewritten,
summarised or expanded. See `narration.py`.

## What is asked of VideoGen (fixed spend policy)

Set in the client (`videogen_client.py`), not per request:

* **Stock footage** visuals (`visual_style={"type": "STOCK"}`), never generated imagery.
* **Voice only** — no `actor_entity_id` is sent, so no presenter/avatar appears.
* Export at **1080p or below** — `FULL_HIGH` (Full HD, 1080p) by default; the
  client refuses `ULTRA_HIGH` (above 1080p).

Provider (VideoGen) failures surface as the typed exceptions in
`exceptions.py` (`VideoGenConfigurationError`, `VideoGenProviderError`,
`VideoGenTimeoutError`).

## Idempotency

Asking for a video twice in a row for the same article does not produce two
videos and is not billed twice: while a video for the article is pending,
processing or ready, the same `videoJobId` is returned. A **failed** video does
not block a fresh attempt. A partial unique constraint on `ArticleVideo`
(`page` where `status != "failed"`) is the race-proof backstop.

## Durability

The finished MP4 is downloaded and stored locally (`MEDIA_ROOT/article_videos/`),
and `downloadUrl` points at this site — not at a provider URL that would expire —
so it stays downloadable for as long as the article (and thus the `ArticleVideo`
row, `on_delete=CASCADE`) exists.

## Configuration (environment variables)

Read at run time through Django settings (`settings/base.py`); values are never
written into the repository.

| Variable | Required | Meaning |
| --- | --- | --- |
| `VIDEOGEN_API_KEY` | yes | VideoGen API key. |
| `VIDEOGEN_BASE_URL` | no | Overrides the API base address verbatim when set. |
| `VIDEOGEN_EXPORT_QUALITY` | no | `STANDARD` / `HIGH` / `FULL_HIGH` (default `FULL_HIGH`). Values above 1080p are rejected. |
| `VIDEOGEN_VOICE_ID` | no | Catalog voice `displayName` or id; account default when unset. |

## Architecture notes

Production runs off the request cycle on a background daemon thread
(`production.py`) — the environment ships no task queue/broker and forbids
adding one. The orchestration is isolated behind `start_production_async()` so
it can be swapped for Celery/RQ later without touching the API layer. Trade-off:
a server restart mid-production leaves a job in `processing`; a durable queue
would reconcile such jobs.

## Tests

```bash
python manage.py test bakerydemo.videos
```

All tests mock VideoGen — they never hit the network and never produce a video.
