# Article videos (`bakerydemo.video`)

An **additive** capability: turn a *published blog article* into a short,
narrated MP4 the marketing team can share — no manual video editing. It is
produced with [VideoGen](https://videogen.io) and exposed on the site's
existing Wagtail v3 HTTP API (`/api/v3-preview/`). It does not change any
existing page, blog, image, or admin flow.

## Endpoints

All three live under the existing v3 API and use the **same bearer-token
authentication** as the rest of that API. Producing a video is an editorial
action, so every endpoint is restricted to callers who may **publish that
page**.

| Method & path | Purpose |
| --- | --- |
| `POST /api/v3-preview/pages/{page_id}/video/` | Start producing a video for the article. Returns immediately with the top-level `videoJobId`. |
| `GET /api/v3-preview/pages/{page_id}/video/{videoJobId}/` | State and outcome: top-level `status`, `progressPercentage`, `downloadUrl`, `error`. |
| `GET /api/v3-preview/pages/{page_id}/video/{videoJobId}/download/` | Stream the finished MP4 (same auth + publish gate). |

`status` is one of `pending`, `processing`, `ready`, `failed`. `downloadUrl` is
populated once the MP4 is `ready`; `error` once it has `failed`. A `POST` for an
article that already has a job returns the **same** `videoJobId` and never
starts (or bills for) a second video.

### Behaviour details

- **Narration comes only from the article's own words**: the title plus the
  first sentence of the introduction, capped at 30 words. The body is never
  narrated and the text is never sent anywhere to be rewritten or summarised.
- **Cheap shape only**: one script-to-video workflow (stock footage, a voice
  only, default 16:9), then exactly one MP4 export. No AI imagery,
  image-to-video, upscaling, avatars, extra media, or second export are ever
  requested.
- **Asynchronous, no task queue**: production runs on a daemon thread, so the
  `POST` returns straight away. There is no broker/queue dependency.
- **Provider failures are typed exceptions** (`bakerydemo/video/exceptions.py`)
  and are surfaced to the caller via the `error` field.
- The finished MP4 is stored on the site (`MEDIA_ROOT/article_videos/`) so it
  stays downloadable for as long as the article exists (the job row is deleted
  with the page via `on_delete=CASCADE`).

## Configuration

Read from the environment at run time (via Django settings); **no secret is
ever stored in the repository**:

| Env var | Required | Meaning |
| --- | --- | --- |
| `VIDEOGEN_API_KEY` | yes | VideoGen API key. |
| `VIDEOGEN_BASE_URL` | no | Override the VideoGen API base address; used verbatim when set. |
| `VIDEOGEN_VISUAL_STYLE_TYPE` | yes, to produce | The stock-footage visual style (see the gap note below). |
| `VIDEOGEN_JOB_TIMEOUT_SECONDS` | no | Max seconds a job may run before it times out (default 1800). |

## Known gap — stock-footage visual style

VideoGen's `script-to-video` workflow **requires** a `visualStyle`, but the
VideoGen `api` skill that is this integration's *sole* reference documents only
the AI-image style (`AI_IMAGE`), which this site must not use. The value that
selects **stock footage** is not in the skill, not in the SDK, is not
enumerated by the provider's validation error, and VideoGen web/docs lookups are
blocked in this workspace.

Rather than invent an enum value or fall back to the forbidden AI style, the
value is supplied as operator configuration via `VIDEOGEN_VISUAL_STYLE_TYPE`.
Until it is set, a production run fails fast with a clear, typed configuration
error (visible in the job's `error` field) instead of ever requesting AI
imagery. Set it to the stock-footage visual style accepted by your VideoGen
account and video production works end to end with no code change.

## Verifying it yourself

```bash
# 0. Environment (Python 3.12).
python -m venv .venv && . .venv/Scripts/activate   # or source .venv/bin/activate
pip install -r requirements/base.txt
python manage.py migrate
python manage.py load_initial_data

# 1. Credentials (values come from your environment; never commit them).
export VIDEOGEN_API_KEY=...            # your key
# export VIDEOGEN_BASE_URL=...         # optional
export VIDEOGEN_VISUAL_STYLE_TYPE=...  # the stock-footage visual style (see gap note)

# 2. Run the server.
python manage.py runserver 127.0.0.1:8000

# 3. Find a published article id (e.g. "Tracking Wild Yeast").
TOKEN=wagtail_C3qhlJUUj75vWvK5bbcb73bZ4JC4cQKWt   # seeded admin (a publisher)

# 4. Start a video (returns {"videoJobId": "..."}).
curl -X POST -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8000/api/v3-preview/pages/62/video/

# 5. Poll until status is "ready" (or "failed").
curl -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8000/api/v3-preview/pages/62/video/<videoJobId>/

# 6. Download the MP4 from the downloadUrl in the status response.
curl -H "Authorization: Bearer $TOKEN" -L -o article.mp4 \
  "http://127.0.0.1:8000/api/v3-preview/pages/62/video/<videoJobId>/download/"
```

Permission checks with the seeded tokens: `admin` / `moderator` / `arabic` are
admitted (they may publish); `editor` is refused with **403** (cannot publish);
a missing or `inactive` token is **401**; a non-blog or unpublished page is
**404**.

## Tests

```bash
python manage.py test bakerydemo.video
```

The suite mocks the VideoGen client, so it never contacts the provider and never
spends money.
