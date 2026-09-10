# Article videos (VideoGen integration)

Turn a **published blog article** into a short, narrated MP4 the marketing team
can share — no manual video editing. This is an **additive** capability: it does
not touch the existing pages, blog, images or admin flows.

## What it does

An editor picks a published article and asks the site for a video of it. The
site builds spoken narration from the article's **own words**, has
[VideoGen](https://videogen.io) produce the video with stock footage and a
voice, downloads the finished MP4 into the site's own storage, and serves it
back through the API.

## Endpoints

All endpoints live on the site's existing v3 API (`/api/v3-preview/`), use its
`Authorization: Bearer <token>` authentication, and return RFC 7807
`application/problem+json` errors — exactly like the rest of that API.

| Method & path | Purpose |
| --- | --- |
| `POST /api/v3-preview/pages/{page_id}/video/` | Start producing a video for the article. Returns immediately. |
| `GET /api/v3-preview/pages/{page_id}/video/{videoJobId}/` | State and outcome of a request. |
| `GET /api/v3-preview/pages/{page_id}/video/{videoJobId}/download/` | Stream the finished MP4. |

**Start response** carries the job id as a top-level field:

```json
{ "videoJobId": "…", "status": "processing", "progressPercentage": 0 }
```

`POST` returns **202** when it starts a new video and **200** when it returns an
existing (in-progress or finished) one.

**Status response** lets a caller tell, without guessing, whether the video is
still being produced, is ready, or failed — as top-level fields:

```json
{
  "videoJobId": "…",
  "status": "processing | ready | failed",
  "progressPercentage": 0-100,
  "downloadUrl": "…/download/  (present only when ready)",
  "error": "…  (present only when failed)"
}
```

## Authorization

Producing a video is an editorial action on a page, so every endpoint is
restricted to callers **permitted to publish that page**
(`page.permissions_for_user(user).can_publish()`). With the demo data:

| User | Result |
| --- | --- |
| `admin`, `moderator`, `arabic` (publishers) | allowed |
| `editor` (Editors group — no publish right) | `403 Forbidden` |
| inactive user / revoked or missing token | `401 Unauthorized` |

## The narration

Built **only** from the article's own text — never sent to another service to be
rewritten, summarised or expanded. To keep the clip short (and cheap) the script
is the article's **title + the first sentence of its introduction**, hard-capped
at 30 words (`VIDEOGEN_MAX_SCRIPT_WORDS`). The body is not narrated.

## Cost guardrails

Every video is billed on a real account, so the request is pinned to the
cheapest safe shape and there are no code paths that spend more:

- **Stock footage only** — `visualStyle: { type: "STOCK" }`. Never AI imagery.
- **A voice only** — script-to-video narrates with a TTS voice; no avatar or
  presenter fields are ever sent.
- **One export, at 720p, 16:9** — a single `STANDARD` export (the export API
  offers only `STANDARD` and `HIGH`; there is no 4K option), aspect ratio left
  at the 16:9 default, never resized.
- **Nothing else** — no remix actions (captions, image-to-video, transitions),
  no upscales/regenerations, no standalone media generation, no second export.
- **Idempotent** — at most one non-failed job per page (a DB constraint), so
  asking twice in a row never produces or bills for a second video.

## Durability

When the export finishes, the MP4 is downloaded into the site's own storage
(`FileField`), so it stays retrievable through the download endpoint for as long
as the article exists — independent of VideoGen's expiring signed URLs. The job
is a cascade child of the page, so deleting the article removes its videos.

## How it runs without a task queue

`POST` creates the job and starts the pipeline on a background **daemon thread**,
then returns. There is deliberately no broker, Celery, or Redis. Run the dev
server with `--noreload` so the worker thread is not killed by the autoreloader.

## Configuration

Read from the environment at run time (never committed):

| Variable | Required | Purpose |
| --- | --- | --- |
| `VIDEOGEN_API_KEY` | yes | VideoGen API key. |
| `VIDEOGEN_BASE_URL` | no | Override the API base URL verbatim (else the SDK default). |
| `VIDEOGEN_VISUAL_STYLE_TYPE` | no | Visual-style type (default `STOCK`). |
| `VIDEOGEN_WORKFLOW_QUALITY` | no | Workflow quality tier (default: provider default). |
| `VIDEOGEN_EXPORT_QUALITY` | no | Export tier, `STANDARD` (720p, default) or `HIGH`. |
| `VIDEOGEN_MAX_SCRIPT_WORDS` | no | Narration word cap (default 30). |

## Layout

```
bakerydemo/videos/
├── api.py          # Ninja router: the three endpoints, mounted on the v3 API
├── apps.py         # registers the router on Wagtail's shared v3 API instance
├── constants.py    # job statuses; public status mapping
├── models.py       # VideoJob (owns the stored MP4; idempotency constraint)
├── narration.py    # build the script from the article's own words
├── services.py     # resolve article, idempotent job creation, pipeline
└── videogen/       # the VideoGen integration
    ├── client.py       # cost-guarded wrapper over the official SDK
    └── exceptions.py   # typed provider-failure exceptions
```

## VideoGen interaction

Every VideoGen call goes through `videogen/client.py`, which wraps the official
`videogen` Python SDK. Provider failures are surfaced as typed exceptions
(`VideoGenAPIError`, `VideoGenAuthError`, `VideoGenProductionError`,
`VideoGenTimeoutError`, `VideoGenConfigurationError`). The pipeline is:
`script-to-video` → poll the run → export once → poll the export → download the
file.
