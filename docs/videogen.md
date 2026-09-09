# Article videos (VideoGen integration)

The `bakerydemo.videos` app adds an **additive** capability to the Wagtail Bakery
site: turn one published blog article into a short, narrated MP4 that the
marketing team can share — without anyone editing video by hand. It does not
change any existing page, blog, image or admin flow.

The video is produced with [VideoGen](https://videogen.io). The narration is
built **only** from the article's own words (its title and the first sentence of
its introduction) — the article is never sent to any other service to be
rewritten, summarised or expanded.

## API

The feature is exposed on the site's existing Wagtail **v3 API**
(`/api/v3-preview/`), follows that API's conventions, and authenticates callers
exactly the way that API does: a bearer API token
(`Authorization: Bearer wagtail_…`). Producing a video is an editorial action, so
it is restricted to callers permitted to **publish that page**.

| Method & path | Purpose |
| --- | --- |
| `POST /api/v3-preview/pages/{page_id}/video/` | Start producing a video for the article. Returns without waiting. |
| `GET  /api/v3-preview/pages/{page_id}/video/{videoJobId}/` | State and outcome of one request. |
| `GET  /api/v3-preview/pages/{page_id}/video/{videoJobId}/download/` | Download the finished MP4. |

### POST — start producing

Idempotent. Starting a video for an article that already has one in flight (or
already produced) returns the **same** `videoJobId` and does **not** produce or
bill for a second video.

* `202 Accepted` — a new video was started. Body: `{ "videoJobId": "<uuid>" }`
* `200 OK` — a video is already in flight / produced. Body: `{ "videoJobId": "<uuid>" }`
* `401` no/invalid/revoked token or inactive user · `403` token holder cannot
  publish this page · `404` not a published blog article.

### GET — state and outcome

Top-level fields let a caller tell, without guessing, whether the video is still
being produced, is ready to download, or failed:

```json
{
  "videoJobId": "…",
  "status": "pending | processing | ready | failed",
  "progressPercentage": 0-100,
  "downloadUrl": "https://…/download/  (present only when status == ready)",
  "error": "what went wrong  (present only when status == failed)"
}
```

When `ready`, `downloadUrl` points at the download endpoint above, which streams
the MP4 (`Content-Type: video/mp4`). The finished MP4 is stored on the site (in
media storage), so it stays downloadable for as long as the article exists — it
does not depend on VideoGen's expiring signed URLs. The download endpoint
requires the same publish permission as the rest of the feature.

## How it works

* A `VideoJob` row (UUID primary key = `videoJobId`) is the single source of
  truth for a request's state. It has a `CASCADE` foreign key to the page, so a
  produced video lives exactly as long as its article.
* Production runs in a background daemon thread (there is no task queue in this
  project and none is introduced). The pipeline is:
  `script-to-video workflow → poll → export to MP4 → poll → download → store`.
* Idempotency is enforced by a partial unique constraint (one active/ready job
  per page), so even concurrent requests cannot produce (or bill for) two.
* Every provider failure is raised as a **typed exception**
  (`bakerydemo.videos.exceptions`) and recorded on the job's `error` field.

## Configuration (environment variables)

Credentials are read from the environment at run time and are **never** written
into the repository. The same build runs against a different VideoGen account by
changing only these variables.

| Variable | Required | Meaning |
| --- | --- | --- |
| `VIDEOGEN_API_KEY` | yes (to produce) | VideoGen API bearer token. |
| `VIDEOGEN_BASE_URL` | no | Overrides the VideoGen API base address verbatim when set; otherwise the default is used. |
| `VIDEOGEN_VISUAL_STYLE` | yes (to produce) | JSON object for the workflow's required `visualStyle` — see the gap note below. |
| `VIDEOGEN_MAX_SCRIPT_WORDS` | no (default 30) | Word cap on the narration script. |
| `VIDEOGEN_POLL_INTERVAL_SECONDS` / `VIDEOGEN_TIMEOUT_SECONDS` / `VIDEOGEN_HTTP_TIMEOUT_SECONDS` | no | Polling / timeout tuning. |

`VIDEOGEN_SCRIPT_PARAMS` / `VIDEOGEN_EXPORT_PARAMS` (Python settings) are optional
passthroughs for a specific account/plan (e.g. a narration voice, or an export
resolution).

## Cost shape

The integration only ever asks for the cheap shape: one `script-to-video`
workflow with a ~10–15s script (title + first sentence, ≤30 words), a voice-only
narration (no avatar/presenter), the documented 16:9 default, and exactly **one**
export at default (non-4K) quality. It never converts images to video, upscales,
regenerates media, or exports twice.

## ⚠️ Known gap: the stock-footage `visualStyle` value

VideoGen's `script-to-video` workflow **requires** a `visualStyle` object, and
this task mandates **stock footage only — never AI-generated imagery**. The
VideoGen `api` skill that is this integration's sole reference documents only
`visualStyle.type == "AI_IMAGE"` (which is forbidden here) and does **not**
publish the value that selects stock footage. The provider's own validation
confirms `visualStyle.type` must be one of an allowed set but does not disclose
the members, and the skill's pointer to the OpenAPI spec is a web lookup that is
not permitted in this workspace.

Rather than guess an enum value — which would either request AI imagery or spend
the single, really-billed video on an unverified configuration — no default is
hard-coded. **Supply the correct stock-footage value for your account** and the
flow runs end to end with no code change, e.g.:

```bash
export VIDEOGEN_VISUAL_STYLE='{"type": "<the stock-footage visualStyle type for your account>"}'
```

Until it is set, a start request is accepted (202) but the job fails fast with a
clear, typed configuration error and **never calls the paid API**.

## Verify it yourself

Prerequisites (see the repo README for full setup):

```bash
python -m venv .venv && .venv/Scripts/activate        # Windows; use source .venv/bin/activate on *nix
pip install -r requirements/base.txt
export DJANGO_SETTINGS_MODULE=bakerydemo.settings.dev
python manage.py migrate
python manage.py load_initial_data                    # seeds articles + API tokens
export VIDEOGEN_API_KEY='…'                            # your key (never commit it)
export VIDEOGEN_VISUAL_STYLE='{"type": "…"}'           # your account's stock-footage value
python manage.py runserver 127.0.0.1:8000
```

Seeded tokens (from the README) and their expected result for `POST …/video/`:

| Token user | Result |
| --- | --- |
| `admin` (superuser) | `202` — admitted |
| `moderator` (Moderators, can publish) | `202` — admitted |
| `arabic` (superuser) | `202` — admitted |
| `editor` (Editors, cannot publish) | `403` — refused |
| `inactive` (inactive user) | `401` — refused |
| `german` (revoked token) | `401` — refused |
| _no token_ | `401` — refused |

`Tracking Wild Yeast` is page **62** in the seeded data.

```bash
BASE=http://127.0.0.1:8000/api/v3-preview
ADMIN=wagtail_C3qhlJUUj75vWvK5bbcb73bZ4JC4cQKWt

# 1. Refuse checks (no video is produced by any of these)
curl -s -o /dev/null -w "no token: %{http_code}\n" -X POST $BASE/pages/62/video/
curl -s -o /dev/null -w "editor:   %{http_code}\n" -X POST -H "Authorization: Bearer wagtail_KbX51h5BjfoVDtzQZSziixrFR2R02g9vI" $BASE/pages/62/video/

# 2. Start ONE video as a publisher
JOB=$(curl -s -X POST -H "Authorization: Bearer $ADMIN" $BASE/pages/62/video/ | python -c "import sys,json;print(json.load(sys.stdin)['videoJobId'])")
echo "job=$JOB"

# 3. Poll until ready (or failed)
while true; do
  curl -s -H "Authorization: Bearer $ADMIN" $BASE/pages/62/video/$JOB/ ; echo
  sleep 5
done

# 4. When status == ready, download the MP4
curl -s -L -H "Authorization: Bearer $ADMIN" -o wild-yeast.mp4 $BASE/pages/62/video/$JOB/download/
```

Running the app's automated tests (mocked provider — no cost) exercises the same
flow, including auth, permission, idempotency, the full success pipeline, and
provider-failure handling:

```bash
python manage.py test bakerydemo.videos
```
