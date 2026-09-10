# VideoGen integration plan — Article → shareable video (Wagtail Bakery)

## Goal

Add an **additive** capability to the Wagtail Bakery v3 HTTP API: turn one published blog
article into a short narrated MP4 via **VideoGen**, retrievable through the site.

- `POST /api/v3-preview/pages/{page_id}/video/` → starts production, returns `videoJobId`.
- `GET /api/v3-preview/pages/{page_id}/video/{videoJobId}/` → `status`, `progressPercentage`,
  `downloadUrl`, `error` (top-level).

## Repo survey (conventions + exemplar files)

- **v3 API is Django Ninja**, mounted at `/api/v3-preview/` from `wagtail.api.v3.urls.api`
  (a `NinjaAPI`). Routers are added with `api.add_router(prefix, router)` and MUST be added
  **before** `api.urls` is accessed (else `ConfigError`). Exemplar: `wagtail/api/v3/api.py`.
  Adding a *new* Router instance at the existing `/pages/` prefix is allowed (the duplicate
  check only fires for the same router object).
- **Auth**: `BearerTokenAuth()` resolves `Authorization: Bearer <token>` → `APIToken` → sets
  `request.user`; returns `None` (→ Ninja 401) when unauthenticated. Exemplar:
  `wagtail/api/v3/auth.py`. Invalid / revoked / inactive-user tokens all become 401 at the auth
  layer.
- **Permissions**: per-page `page.permissions_for_user(user).can_publish()` is the authoritative
  "may publish this page" gate (used by the `publish` action in
  `wagtail/api/v3/routers/pages.py`). `require_any_permission(Page, ("publish",))` is the coarse
  policy gate. Raise `django.core.exceptions.PermissionDenied` → handled as 403 (authenticated)
  / 401 (anonymous) by `wagtail/api/v3/errors.py`.
- **Error handling**: `register_exception_handlers` maps `PermissionDenied`→403/401,
  `Http404`→404, ninja `HttpError`→its status, validation→422. Use `ninja.errors.HttpError` for
  400/409/503. Exemplar: `wagtail/api/v3/errors.py`.
- **Router exemplars**: `wagtail/api/v3/routers/whoami.py` (simple), `.../routers/pages.py`
  (`publish` action + per-page permission check).
- **Blog model** `bakerydemo/blog/models.py::BlogPage(Page)`: narration source fields are
  `title` (from `Page`), `introduction` (TextField), `body` (StreamField). Article is
  "published" ⇔ `page.live`.
- **Settings**: `bakerydemo/settings/base.py` (INSTALLED_APPS, os.environ reads). `.env` auto
  loaded by `manage.py`/`wsgi.py` via `python-dotenv`. Add settings + new app here.
- **Toolchain**: `pip` + venv at `.venv` (Python 3.12). `videogen-apimatic` installed and
  importable (`from videogen import VideogenClient` OK). Tests: Django test runner
  (`DJANGO_SETTINGS_MODULE=bakerydemo.settings.test ./manage.py test`). No mypy configured →
  will run `mypy --strict` on touched integration files from the venv.
- **Host app is SYNC** (WSGI/Django/Ninja views) → use the **sync** `VideogenClient`. Never the
  async client. No broker/queue allowed → background work via an in-process `threading.Thread`.

## Seeded verification matrix (from `readme.md` + fixture `bakerydemo.json`)

| User | Token (README) | Auth result | Publish? | Expected |
| --- | --- | --- | --- | --- |
| admin (superuser) | `wagtail_C3qh…` | ok | yes | 202 / 200 |
| moderator (Moderators: has `publish_page`) | `wagtail_XeNI…` | ok | yes | 202 / 200 |
| editor (Editors: add+change, **no publish**) | `wagtail_KbX5…` | ok | no | **403** |
| german (token **revoked**) | `wagtail_AXzz…` | fail | — | **401** |
| inactive (user `is_active=False`) | `wagtail_3Oo2…` | fail→anon | — | **401** |

Target article: **"Tracking Wild Yeast"** (a live `BlogPage` from the fixture; resolve its
`page_id` at runtime).

---

## VideoGen flow (grounded in the SDK map + source; sync client)

Two async provider jobs chained: **script→video build**, then **export to MP4**.

1. `client.workflows.script_to_video(body=…)` → `StartWorkflowRunResponse`
   (`workflow_run_id`, `project_id`). **BILLED — call once.**
2. Poll `client.workflows.get_workflow_run(workflow_run_id)` → `WorkflowRun`
   (`status`, `progress_percentage`, `error`) until terminal.
3. On workflow `succeeded`: `client.projects.export_project(project_id, body=…)` →
   `ExportProjectResponse` (`export_id`). **BILLED — call once.**
4. Poll `client.projects.get_project_export(project_id, export_id)` → `ProjectExport`
   (`status`, `progress_percentage`, `download_url`, `error`) until terminal.
5. On export `succeeded`: `download_url` is the signed MP4 URL (valid 7 days, auto re-signed by
   `get_project_export` within 1h of expiry). Re-fetch live on each GET so the link stays valid
   "as long as the article exists".

### Cheap-shape decisions (hard spend limits)

- **script** = article **title + first sentence of `introduction`**, verbatim, **capped at 30
  words** (word-count hard cap in code). Body NOT narrated. No external rewrite service.
- **Stock footage only**: `visual_style = WorkflowVisualStyle(type_="STOCK")` (enum
  `WorkflowVisualStyleType.STOCK`). No `ai_style`/`entity_id`.
- **Voice only, no avatar**: omit `actor_entity_id` (→ voiceover without avatar). Omit
  `voice_id` (default voice). No avatar/presenter fields.
- **16:9**: `aspect_ratio = AspectRatio(width=16, height=9)`.
- **No remix actions** (avoid `CONVERT_IMAGES_TO_VIDEOS`, captions, transitions, logo — each is
  extra billed work). `remix_actions` omitted.
- **Export once at 720p**: `quality = ExportProjectQuality.STANDARD`. **Decision (corrected
  against a real export):** the plugin documents the tiers only as a "vertical resolution"
  ladder, not by pixel size. The one real export produced at `HIGH` measured **1920×1080
  (1080p)**, so `HIGH` is the 1080p tier and the 720p tier is the one below it, `STANDARD`
  (with `ULTRA_HIGH` the 4K tier to avoid). Not a gap (the enum covers the capability); a
  judgment call, now grounded in the measured output. Note: the single video produced during
  verification came out at 1080p because the run initially used `HIGH`; it was **not** re-exported
  at `STANDARD` because a second export is a hard-forbidden spend, and 1080p still honors "never
  4K". The code requests `STANDARD` for all subsequent runs.
- **Watermark/end-screen**: leave `watermark_mode`/`end_screen_mode` UNSET (default AUTO) —
  setting `NONE` requires Pro and errors otherwise. No second export at any quality.
- `is_output_temporary` left UNSET (default false) so assets are retained (durable download).

### Idempotency (no double video / double bill)

`ArticleVideo` is `OneToOneField(Page)`. `POST` is idempotent per page: if a job already exists
and is not `failed`, return its existing `videoJobId` without any provider call. The worker also
guards each billed call (`script_to_video` only when `workflow_run_id` empty; `export_project`
only when `export_id` empty).

---

## Contract sheet (authoritative — from map + source lookups)

**Client** (`videogen`): sync `VideogenClient(bearer_auth=…, base_url=… )`, keyword-only.
`base_url` default `https://api.videogen.io`; pass `settings.VIDEOGEN_BASE_URL` verbatim only
when set. `timeout` default 30.0. App-scoped singleton (httpx pool, thread-safe); `close()` on
process teardown (best-effort). **No SDK retries** — polling/backoff is ours to build.

**Operations** (all Case B → `.error` is `RawError`; parsed call raises `ApiError`; every op also
has a `with_raw_response` peer; every op returns a payload — none return `None`):

| Op | Signature (positional, then `*` keyword-only) | Returns | Key response members |
| --- | --- | --- | --- |
| `client.workflows.script_to_video` | `*, body: ScriptToVideoRequest\|…Dict\|None=None` | `StartWorkflowRunResponse` | `workflow_run_id`, `project_id` |
| `client.workflows.get_workflow_run` | `workflow_run_id: str, *` | `WorkflowRun` | `status`, `progress_percentage`, `project_id`, `error` |
| `client.projects.export_project` | `project_id: str, *, body: ExportProjectRequest\|…Dict\|None=None` | `ExportProjectResponse` | `export_id` |
| `client.projects.get_project_export` | `project_id: str, export_id: str, *` | `ProjectExport` | `status`, `progress_percentage`, `download_url`, `download_url_expires_at`, `export_file_id`, `error` |

**Models — members we set / read** (`Optional[T]` here = `T | UNSET`, NOT `None`; construct via
the `…Dict` companion to keep plain `mypy` happy on member names — see python-models):

- `ScriptToVideoRequest` (required: `script: str`, `visual_style: WorkflowVisualStyle`):
  set `script`, `visual_style`, `aspect_ratio` (alias `aspectRatio`). Leave voice/avatar/remix
  UNSET.
- `WorkflowVisualStyle` (required: `type_` alias `type`): set `type_="STOCK"`.
- `AspectRatio` (required `width:int`, `height:int`): `16`, `9`.
- `ExportProjectRequest`: set `quality="HIGH"`. Others UNSET.
- Read models (`WorkflowRun`, `ProjectExport`): members via wire aliases `status`,
  `progressPercentage`, `downloadUrl`, `error` — read Python names
  (`progress_percentage`, `download_url`).
- `JobStatus` enum (wire values): `pending`, `running`, `succeeded`, `failed`, `cancelled`.
  Terminal = {succeeded, failed, cancelled}. Open enum → compare against `.value` strings
  defensively.
- `ApiErrorModel` (on `WorkflowRun.error`/`ProjectExport.error`): `message: str`,
  `code: str|None`.

**Decode note:** a truncated/garbage 2xx body raises `pydantic.ValidationError`/`ValueError`,
NOT `ApiError`, and bypasses both response modes → the error boundary must catch it separately.
`httpx` transport errors arrive unwrapped.

## Typed exception boundary (`bakerydemo/videos/exceptions.py`)

`VideoGenError` (base) → `VideoGenConfigError` (missing key), `VideoGenAPIError`
(wraps `ApiError`: `status_code`, `code`, `message`), `VideoGenResponseError` (wraps
`ValidationError`/`ValueError` decode failures), `VideoGenTransportError` (wraps `httpx` errors),
`VideoGenTimeoutError` (our polling budget exhausted), `VideoGenJobFailedError` (provider job
reached `failed`/`cancelled`). The provider wrapper translates every SDK failure kind into these
— nothing SDK-specific leaks past the boundary. (Satisfies "surface provider failures as typed
exceptions".)

## Architecture / file layout — new app `bakerydemo/videos/`

- `apps.py` — `VideosConfig.ready()` registers the ninja router onto `wagtail.api.v3.urls.api`.
- `models.py` — `ArticleVideo` (OneToOne page, `job_id` UUID = videoJobId, `status`,
  `progress_percentage`, `workflow_run_id`, `project_id`, `export_id`, `download_url`, `error`,
  `script`, timestamps).
- `migrations/` — initial migration.
- `narration.py` — `build_narration_script(page)`: title + first intro sentence, ≤30 words.
- `provider.py` — settings-based client singleton + `VideoGenService` wrapping the 4 ops with
  error translation; cheap-shape request builders.
- `service.py` — orchestration: `request_video(page, user)` (idempotent, launches worker via
  `transaction.on_commit`), `_run_job(pk)` (build→poll→export→poll, updates DB, never retries
  billed calls), `get_job_state(page, job_id)`, `fresh_download_url(av)`.
- `api.py` — Ninja `Router` + Schemas (`StartVideoResponse{videoJobId}`,
  `VideoJobStateResponse{videoJobId,status,progressPercentage,downloadUrl,error}`); POST + GET
  handlers gated by per-page `can_publish()`.
- `tests/` — permission matrix, idempotency, narration cap, provider error translation
  (faked transport), all with the SDK mocked (NO real video in tests).
- Settings: add `"bakerydemo.videos"` to INSTALLED_APPS; add `VIDEOGEN_API_KEY`,
  `VIDEOGEN_BASE_URL` reads (os.environ, no hardcoded values).

## Background execution (no broker)

`threading.Thread(daemon=True)` started via `transaction.on_commit`. Worker opens its own DB
connection, keeps transactions short (`.update()` writes), closes the connection in `finally`.
Poll interval 5s; per-phase budget cap (fail with `VideoGenTimeoutError`, never re-bill). Poll
GETs tolerate a few consecutive transient errors before giving up; billed POSTs never retried.
Documented as the deliberate no-queue design.

## Assumptions & Blockers

- **720p = `ExportProjectQuality.HIGH`** — judgment call (see above), not a gap. Proceed.
- **Threading for async work** — required because brokers are forbidden; acceptable for the demo
  single-process server. Minor assumption. Proceed.
- No blockers. The plugin exposes every capability needed (`script_to_video`, `get_workflow_run`,
  `export_project`, `get_project_export`).

## REQUIRED READING (load before implementing the governed step)

- `python-client-initialization` — MUST load before building the client singleton (`provider.py`).
- `python-authentication` — MUST load before wiring `bearer_auth` from settings.
- `python-calling-endpoints` — MUST load before the first `client.<ctrl>.<op>()` call.
- `python-models` — MUST load before constructing request models (`…Dict` companions, UNSET,
  open enums, wire aliases).
- `python-error-handling` — MUST load before the try/except boundary in `provider.py`.
- `python-configuration-resilience` — MUST load before base_url/timeout/polling (no SDK retries).
- `python-testing` — MUST load before writing the test files / faked-transport verification.
