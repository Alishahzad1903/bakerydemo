# VideoGen integration plan — "Article → shareable video" for Wagtail Bakery

## Goal (restated)

Additive capability on the existing Wagtail v3 HTTP API (`/api/v3-preview/`, Django-Ninja):
turn one **published blog article** into one short narrated MP4 via **VideoGen**, retrievable through
the site. Two endpoints:

- `POST /api/v3-preview/pages/{page_id}/video/` → start; returns `202` with top-level `videoJobId`.
- `GET  /api/v3-preview/pages/{page_id}/video/{videoJobId}/` → `status`, `progressPercentage`,
  `downloadUrl`, `error` (top-level).

Narration = the article's **own** title + **first sentence of its introduction**, ≤ 30 words
(shorter better; target 10–15 s). No body. No external rewriting.

## Sync vs async — SYNC

The v3 API views are plain `def` functions (WSGI Django + Ninja). Use the **sync** `VideogenClient`.
`custom_http_client=` (sync), teardown `close()`. Client held as a lazily-built module-level singleton
in the videos app, closed via `atexit`. (python-client-initialization: WSGI → module-level lazy global.)

## Auth / config

- Bearer token: `bearer_auth=settings.VIDEOGEN_API_KEY` (plain string; **not OAuth** → no token-fetch /
  `OAuthProviderError` failure mode).
- `base_url`: pass `settings.VIDEOGEN_BASE_URL` **verbatim** only when set; otherwise omit → SDK default
  `https://api.videogen.io`. **Confirmed default host** by read-only smoke (`account.get_me()` → 200,
  `MeResponse`, VIDEOGEN_BASE_URL unset in this env).
- Secrets read from env **through Django settings** only; **no values in the repo**. `settings.py`
  references `os.environ.get("VIDEOGEN_API_KEY")` / `("VIDEOGEN_BASE_URL")` — names only.
- timeout: 30.0 s default is fine (all calls return immediately — async workflow/export jobs).

## Flow / architecture (no broker, no queue — allowed only Python)

VideoGen has **no** single "make an MP4" call. The MP4 comes from **two** async jobs:
`script_to_video` (builds a project) then `export_project` (renders the project to MP4). We orchestrate
with a **persisted state machine advanced on demand** (no background worker — none permitted here):

States: `PENDING → BUILDING → EXPORTING → READY` (terminal), and `FAILED` (terminal).

- **POST** resolves/authorizes the page, `get_or_create`s the one `VideoJob` for that page, then calls
  `advance()` once. On a fresh job `advance()` builds the script and calls **`script_to_video`**
  (the single billable production), stores `workflow_run_id`+`project_id`, → `BUILDING`. Returns
  `videoJobId` (202; 200 if the job already existed → **idempotent, never a 2nd video / 2nd bill**).
- **GET** returns current state and calls `advance()` to move it forward:
  - `BUILDING`: `get_workflow_run` → `succeeded` ⇒ trigger export (see below); `failed`/`cancelled` ⇒
    `FAILED`; else update progress.
  - `EXPORTING`: `get_project_export` → `succeeded` ⇒ store `download_url`, → `READY`; `failed` ⇒
    `FAILED`; else progress.
  - `READY`: re-fetch `get_project_export` (read, **not** a re-export → not billed) to return a
    freshly re-signed `download_url`, so the MP4 stays downloadable for as long as the article exists.

**Single-export / single-video safety under concurrent GETs:** each provider-triggering transition is a
compare-and-set DB update (`filter(pk=, status=EXPECTED, <trigger-field>__isnull=True).update(...)`);
only the winner calls the provider. `script_to_video` guarded by `status==PENDING & workflow_run_id
null`; `export_project` guarded by `status==BUILDING & export_id null` (SQLite serializes writes;
`.update()` rowcount decides the winner). `VideoJob` is `OneToOneField(Page)` (unique) → one job/article.

**No SDK retries exist** (by design). We add none around the billable `script_to_video` (a write with
**no idempotency-key field** on `ScriptToVideoRequest` — confirmed — so a blind retry risks a double
bill). If `script_to_video` fails, the job stays `PENDING` (no ids stored) and the error surfaces; a
later POST may deliberately retry. Poll reads that fail transiently surface as `502/503` and leave
state intact for the next poll (that polling *is* the recovery path). Residual at-least-once risk
(response lost after the server accepted the run) is inherent without a provider idempotency key — noted.

`page` FK is `CASCADE` from `wagtailcore.Page` → job (and its downloadability) lives exactly as long as
the article. Additive: no existing model/route/flow changed.

## Permission model

"Permitted to publish that page." Idiom from `wagtail/api/v3/routers/pages.py`: per-page tester
`page.permissions_for_user(request.user).can_publish()` (used by the publish action). Gate **both**
endpoints on it (only someone who could produce it may see it). Auth via `BearerTokenAuth()` on the
router (401 when no/invalid token via Ninja; PermissionDenied → 401 if unauthenticated else 403, per
existing `errors.py` handlers). Page must exist (`404`), be a `BlogPage` (`404` — capability is
blog-only), and be `live` (`422` — "requires a published article").

## Export shape (the cheap, mandated shape)

- Visuals **stock only**: `WorkflowVisualStyle.type_ = WorkflowVisualStyleType.STOCK` (no AI image, no
  avatar → omit `actor_entity_id` ⇒ voiceover **voice only**, no presenter).
- Aspect ratio **16:9**: `AspectRatio(width=16, height=9)`.
- Export **once, 720p**: `export_project` with `quality = ExportProjectQuality.STANDARD`.
  **720p decision (empirically anchored):** the plugin does not document each tier's pixel
  resolution. The one permitted export, run at `HIGH`, was observed to render **1920×1080 (1080p)**
  — so the ladder is `STANDARD`=720p < `HIGH`=1080p < `FULL_HIGH` < `ULTRA_HIGH`=4K, and `STANDARD`
  is the 720p tier. (The already-produced clip is therefore 1080p; the spend rule forbids a second
  export to re-render it at 720p, so the code is corrected to `STANDARD` for real runs and not
  re-exported.) `ULTRA_HIGH` (4K) is the forbidden tier. Watermark/end-screen left at `AUTO`.
- **Not used at all** (banned/billable): AI imagery, image-to-video / `CONVERT_IMAGES_TO_VIDEOS`,
  regeneration/upscaling/restyle, avatar/presenter, standalone media gen, any 2nd export. No
  `remix_actions`. `is_output_temporary` left **false** (default) so outputs persist (durability).

---

## CONTRACT SHEET (grounded in SDK map + source; all in scope)

Sync client `VideogenClient(bearer_auth=<str>, base_url=<str?>, timeout=30.0)`; alias `Client`.
Everything after `*` is keyword-only with real defaults. **All 4 ops are Case B** → on non-2xx raise
`ApiError` whose `.error` is always `RawError` (`status_code`,`content`,`text()`,`json()`); no typed
error arm anywhere in this SDK. Bodies passed as **`…Dict` companions keyed by Python names** (checked
under `mypy --strict`; the constructor's alias-keyword form is not). Import models from
`videogen.models`, enums from `videogen.models.enums`, core from `videogen.core`.

### 1. `client.workflows.script_to_video(*, body=ScriptToVideoRequest|ScriptToVideoRequestDict|None)`  → `StartWorkflowRunResponse`
- Route `POST /v1/workflows/script-to-video`. **THE single billable production call.**
- `ScriptToVideoRequest` members we set (rest left `UNSET`/omitted):
  - `script: str` (required) — narration verbatim (title + 1st intro sentence, ≤30 words).
  - `visual_style: WorkflowVisualStyle` (required; wire `visualStyle`) — `{"type_": WorkflowVisualStyleType.STOCK}`
    (`type_` wire `type`).
  - `aspect_ratio` (opt; wire `aspectRatio`) — `{"width":16,"height":9}`.
  - Omit `voice_id`,`actor_entity_id`,`quality`,`scenes`,`remix_actions`,`is_output_temporary`(→false).
- Dict form: `ScriptToVideoRequestDict = {"script":…, "visual_style":{"type_":WorkflowVisualStyleType.STOCK}, "aspect_ratio":{"width":16,"height":9}}`.
- Returns `StartWorkflowRunResponse` (all **required**): `workflow_run_id`(wire `workflowRunId`),
  `project_id`(`projectId`), `project_url`(`projectUrl`), `remix_action_ids`(`remixActionIds`). Guard:
  assert `workflow_run_id` and `project_id` present after the call.

### 2. `client.workflows.get_workflow_run(workflow_run_id: str, *)`  → `WorkflowRun`
- Route `GET /v1/workflows/runs/{workflowRunId}` (read; not billed).
- `WorkflowRun` (all required except): `workflow_run_id`, `status: JobStatusOrStr`, `workflow_type`,
  `progress_percentage: float`(`progressPercentage`, always 100 when succeeded), `attempt_index`,
  `project_id`, `project_url`, `error: ApiErrorModel | None` (null unless failed).

### 3. `client.projects.export_project(project_id: str, *, body=ExportProjectRequest|…Dict|None)`  → `ExportProjectResponse`
- Route `POST /v1/projects/{projectId}/export`. **The single 720p export.**
- Body: `ExportProjectRequestDict = {"quality": ExportProjectQuality.STANDARD}` (720p; `HIGH`
  empirically = 1080p). `quality` opt; leave `watermark_mode`/`end_screen_mode`/
  `delivery_destinations` UNSET → AUTO/none.
- Returns `ExportProjectResponse`: `export_id: str` (required; wire `exportId`). Guard: assert present.

### 4. `client.projects.get_project_export(project_id: str, export_id: str, *)`  → `ProjectExport`
- Route `GET /v1/projects/{projectId}/exports/{exportId}` (read; not billed; auto re-signs URL).
- `ProjectExport`: `export_id`, `project_id`, `status: JobStatusOrStr`,
  `progress_percentage`(`progressPercentage`), `attempt_index`,
  `download_url: str | None`(`downloadUrl`; null until succeeded; signed 7 d, auto re-signed),
  `download_url_expires_at: int | None`, `thumbnail_url`, `export_file_id`(`exportFileId`), `file`,
  `error: ApiErrorModel | None`. Guard: if `status==succeeded` but `download_url` falsy → treat as
  not-ready-yet (keep polling), never as READY.

### Enums (`videogen.models.enums`) — wire values
- `JobStatus`: `PENDING="pending"`,`RUNNING="running"`,`SUCCEEDED="succeeded"`,`FAILED="failed"`,
  `CANCELLED="cancelled"`. **Open** (`JobStatusOrStr` may be a plain `str`) → match known members;
  unknown ⇒ treat as in-progress. Terminal-success = `succeeded`; terminal-fail = `failed`/`cancelled`.
- `WorkflowVisualStyleType`: `STOCK="STOCK"`, `AI_IMAGE="AI_IMAGE"` → use `STOCK`.
- `ExportProjectQuality`: `STANDARD`,`HIGH`,`FULL_HIGH`,`ULTRA_HIGH` → use `STANDARD` (720p;
  `HIGH` was observed to render 1080p).
- `ApiErrorModel`: `message: str` (required), `code: OptionalNullable[str]` — surface `message` (+`code`)
  as the job `error`.

### Read-only smoke used for the plan (no side effects)
- `client.account.get_me()` → `MeResponse` (keys apiKeyId/apiKeyNickname/email/displayName/teamId).
  ✅ 200 with the provided key at the default host. `script_to_video`/`export_project` were **not**
  smoked (billable side effects — that is the one real video).

### Error boundary (typed exceptions — required)
Wrap every provider call in a service seam translating to our own hierarchy (`videos/exceptions.py`):
- `except ApiError as e` (`.error` is `RawError`) → `VideoGenAPIError(status_code, e.error.text())`.
- `except (pydantic.ValidationError, ValueError)` (decode failure — bypasses both response modes;
  success-body truncation reads back `UNSET`, so also assert required members) →
  `VideoGenResponseError` ("outcome unknown").
- `except httpx.HTTPError` (transport, unwrapped) → `VideoGenUnavailableError` ("outcome unknown").
Base `VideoGenError`. Router maps: `VideoGenAPIError`→502, `VideoGenResponseError`→502,
`VideoGenUnavailableError`→503, via `ninja.errors.HttpError` (existing handler → RFC7807 problem+json).
No `OAuthProviderError` (bearer, not OAuth). SDK does no retries — we add none on the billable write.

---

## Response schemas (Ninja) — exact top-level field names via alias + `by_alias=True`
- Start (`202`/`200`): `{"videoJobId": str}` — `video_job_id: str = Field(alias="videoJobId")`.
- Status (`200`): `status: str` ∈ {`processing`,`succeeded`,`failed`};
  `progressPercentage: float` (build 0–90, export 90–100, 100 when READY);
  `downloadUrl: str | None` (only when READY); `error: str | None` (only when FAILED).
  Emit exactly those keys with `@router.<m>(..., by_alias=True)` (Ninja serializes `by_alias`).

## File layout (`bakerydemo/videos/`)
`apps.py` (`VideosConfig.ready()` → `api.add_router("/pages/", video_router)` on
`wagtail.api.v3.api.api`), `models.py` (`VideoJob`), `videogen_client.py` (settings→singleton client),
`exceptions.py`, `service.py` (script build + state machine + error boundary), `schemas.py`, `api.py`
(2 endpoints + permission gate + exception→HttpError mapping), `migrations/`, `tests/`.
Settings: add `"bakerydemo.videos"` to `INSTALLED_APPS`; add `VIDEOGEN_API_KEY`/`VIDEOGEN_BASE_URL`
(env, names only) to `settings/base.py`.

## Test matrix (seeded tokens; verified from fixture + GroupPagePermission)
- **Allow** (can_publish): `admin` (superuser), `moderator` (Moderators have `publish_page`), `arabic`
  (superuser).
- **403** (authenticated, no publish): `editor` (Editors have add/change but **no** publish).
- **401**: `inactive` (user `is_active=False`), `german` (token `revoked_at` set), no token.
Unit tests use the **stub transport** seam (success / RawError / decode-failure / transport-failure),
assert our boundary's mapping and the outgoing request body (`visualStyle.type=STOCK`, `aspectRatio`,
export `quality=HIGH`). `mypy --strict` on touched files + `pytest`.

## E2E self-verify (ONE video only)
Against article **id 62 "Tracking Wild Yeast"** (live). Script ≈ "Tracking Wild Yeast. Yeasts, with
their single-celled growth habit, can be contrasted with molds, which grow hyphae." (≈17 words).
POST once → poll GET until `succeeded` → download the MP4 from `downloadUrl`. Diagnose any failure from
provider-reported state; **never** produce a second video.

## Assumptions & Blockers
- **No blockers.** Minor decisions (720p=`HIGH`; advance-on-read since no broker allowed; gate GET on
  publish too; blog-only) are design calls, taken above. No open lookups remain.

## REQUIRED READING (all loaded before implementing — Step 1c)
- `python-error-handling` — MUST load (error boundary). ✅
- `python-client-initialization` — MUST load (client construction/lifetime). ✅
- `python-testing` — MUST load (stub-transport tests + verification script). ✅
- `python-calling-endpoints` — MUST load (op signatures / body / response modes). ✅
- `python-models` — MUST load (Dict companions, UNSET, open enums, wire aliases). ✅
- `python-authentication` — MUST load (bearer). ✅
- `python-configuration-resilience` — MUST load (base_url, timeout, no-retry). ✅
