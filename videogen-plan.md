# VideoGen integration plan — Article → shareable video (Wagtail Bakery)

## Goal

Add an **additive** capability to the bakerydemo site: turn one published blog article into a
short narrated MP4 via VideoGen, exposed on the existing Wagtail **v3-preview** HTTP API.

- `POST /api/v3-preview/pages/{page_id}/video/` → `{ "videoJobId": "..." }` (async start)
- `GET  /api/v3-preview/pages/{page_id}/video/{videoJobId}/` →
  `{ "status", "progressPercentage", "downloadUrl", "error", "videoJobId" }`

## Host survey (decides the shape)

- **Framework**: Django 6.0 + Wagtail 8.0, WSGI/**sync**. Settings: `bakerydemo.settings.dev`.
  SQLite, no broker/queue permitted.
- **v3 API is Django-Ninja**, not DRF. Shared object: `wagtail.api.v3.api.api` (a `NinjaAPI`),
  re-exported by `wagtail.api.v3.urls`. Mounted in `bakerydemo/urls.py` at `api/v3-preview/`.
  - Exemplar for a page **action** route (auth + per-page permission):
    `wagtail/api/v3/routers/pages.py` — `actions_router = Router(auth=BearerTokenAuth())`,
    `@actions_router.post("/{page_id}/actions/publish/", ...)`,
    `@require_any_permission(Page, ("publish",))`, then
    `page.permissions_for_user(request.user).can_publish()`.
  - Exemplar for a simple typed route + schema: `wagtail/api/v3/routers/whoami.py`.
  - **Registration ordering**: routers must be added to `api` **before** `api.urls` is
    evaluated (Ninja raises `ConfigError` otherwise). So our router module must be imported at
    the top of `bakerydemo/urls.py`, before `path("api/v3-preview/", api.urls)`.
- **Auth**: `wagtail.api.v3.auth.BearerTokenAuth` resolves `Authorization: Bearer <token>`
  against `wagtail.models.APIToken`, sets `request.user`. We reuse it verbatim.
- **Permission**: "callers permitted to publish that page" → `require_any_permission(Page,
  ("publish",))` gate **plus** the object-level `permissions_for_user(user).can_publish()`
  check (matches the `publish` action exemplar). Model-level gate alone is not per-page.
- **Blog article model**: `bakerydemo.blog.models.BlogPage` (subclass of `Page`) with
  `title` (Page), `introduction` (TextField), `body` (StreamField). Six published in the
  fixture incl. *Tracking Wild Yeast*.
- **Response field naming**: v3 schemas are snake_case, but the task demands exact camelCase
  keys. Use pydantic `Field(alias=...)` on Ninja `Schema` + `by_alias=True` on the operation.
- **Toolchain**: `pip` + venv at `.venv` (Python 3.12). Deps from `requirements/base.txt`;
  `videogen-apimatic` installed. Tests: `DJANGO_SETTINGS_MODULE=bakerydemo.settings.test
  ./manage.py test`. Type check: install `mypy`, run `mypy --strict` on touched integration
  files (SDK is generated under that setting).
- **Baseline**: fresh tree, no DB. `migrate` + `load_initial_data` required before endpoints
  are reachable. (Baseline test run captured after venv ready.)

## Credentials (verified)

- `VIDEOGEN_API_KEY` present in env (len 81). `VIDEOGEN_BASE_URL` **unset** → SDK default
  host `https://api.videogen.io` selected. Read both from env **through Django settings**;
  never hard-code values. Read-only smoke `client.account.get_me()` → `MeResponse` OK
  (auth + host confirmed, non-billable).

## Architecture (new app `bakerydemo.videos`)

- `models.py` — `ArticleVideo`: `page` OneToOne→Page (unique ⇒ idempotency), `job_id`
  (uuid, our opaque videoJobId), `status`, `progress` (0–100), `error` (text),
  provider ids (`workflow_run_id`, `project_id`, `export_id`), `script`, `mp4` FileField,
  timestamps.
- `narration.py` — `build_script(page)`: title + **first sentence of introduction only**,
  ≤30 words (hard clamp), from the article's own text. No external rewrite service.
- `videogen_client.py` — thin integration layer over the SDK:
  - `get_client()` factory from Django settings (`bearer_auth`, optional `base_url`).
  - `produce_video(...)`: script_to_video → poll workflow run → export_project(720p) →
    poll export → return signed MP4 url. **Surfaces provider failures as typed exceptions**
    (`VideoGenError` hierarchy). SDK does **no retries**; we own the polling loop + deadline.
- `service.py` — orchestration: create/lookup `ArticleVideo`, `transaction.on_commit`
  spawns a **daemon thread** (no broker allowed) running `produce_video`, downloads the MP4
  into Django storage, updates status/progress/error. Idempotent: a second request for the
  same page returns the existing job — **no second production, no second bill**.
- `api.py` — Ninja router with the two routes; registers onto shared `api`. Serves the MP4
  through a third authenticated route (`downloadUrl` points at the site, so the clip stays
  downloadable for as long as the article exists — independent of VideoGen URL expiry).
- `schemas.py` — camelCase response schema.
- `apps.py`, `migrations/`, `tests/` (SDK fully mocked — real API hit exactly once, in the
  final manual end-to-end verification).

## Spend shape (hard limits → fixed request)

- **script_to_video** body: `script` (≤30 words), `aspectRatio={16,9}`,
  `visualStyle={type:"STOCK"}` (stock footage only; no AI image/avatar), **omit**
  `actorEntityId` (voice only, no avatar), **omit** `voiceId` (default voice), **omit**
  `remixActions` (no captions/transitions/logo/image-to-video), **omit** `quality`
  (only affects AI images; we use STOCK). Leave `isOutputTemporary`/`hideFromUi` default.
- **export_project** body: `quality=HIGH` (see decision below). Leave `watermarkMode` /
  `endScreenMode` default `AUTO` (setting `NONE` needs Pro and errors). **Exactly one export.**
- Total billable in the whole run: **one** workflow run + **one** 720p export = one video.
- Tests never call the real API. Only the single manual verification produces the one video.

### Decision — export resolution tier = `HIGH`  (design decision, not a gap)

`ExportProjectQuality` = `STANDARD | HIGH | FULL_HIGH | ULTRA_HIGH`, documented only as
"Vertical resolution tier for the rendered MP4" — the plugin gives named tiers, not pixels.
Mapping the ordered ladder onto the conventional consumer ladder SD/HD/FHD/UHD:
`STANDARD`=480p, **`HIGH`=720p**, `FULL_HIGH`=1080p, `ULTRA_HIGH`=4K. `HIGH` matches the
required 720p and is safely far from the forbidden 4K (`ULTRA_HIGH`). The export capability
itself is exposed, so this is a naming interpretation left to judgment, not a missing capability.

---

## Contract sheet (VideoGen Python SDK — no open lookups)

Sync/async: **SYNC** — host is WSGI/threads. Use `VideogenClient` (`from videogen import
VideogenClient`). Never mix with `AsyncVideogenClient`. Must `client.close()` (or `with`).
Client is short-lived per production run (built in the worker thread, closed in `finally`) —
acceptable here; not per-HTTP-request. Keyword-only ctor: `bearer_auth=<str>`,
`base_url=<str|None>`, `timeout=30.0` default.

Base URL: omitting `base_url` ⇒ `https://api.videogen.io` (confirmed). Pass `base_url` verbatim
only when `VIDEOGEN_BASE_URL` is set.

Response modes: use the **plain (raising)** call — raises `ApiError` on error status. All 4 ops
below are **Case B** ⇒ `ApiError.error` is always `RawError` (no typed union). Decode failure
raises `pydantic.ValidationError`/`ValueError`, **not** `ApiError`, and bypasses both modes —
must be caught too. `httpx` transport exceptions arrive unwrapped — catch `httpx.HTTPError`.
No operation returns `None`, so `.with_raw_response` not needed.

Keyword-only boundary: every op splits positional path params from a keyword-only tail after
`*`; `body` and `request_options` are keyword-only with real defaults (no defensive `None`s).

### Operations in scope

1. `client.workflows.script_to_video(*, body: ScriptToVideoRequest|dict|None=None,
   request_options=None) -> StartWorkflowRunResponse`
   - Route `POST /v1/workflows/script-to-video`. Error: `RawError` (Case B).
   - **`StartWorkflowRunResponse`** required members to read (assert on):
     `workflow_run_id` (alias `workflowRunId`), `project_id` (`projectId`),
     `project_url` (`projectUrl`), `remix_action_ids` (`remixActionIds`).

2. `client.workflows.get_workflow_run(workflow_run_id: str, *, request_options=None)
   -> WorkflowRun`
   - Route `GET /v1/workflows/runs/{workflowRunId}`. Error: `RawError` (Case B).
   - **`WorkflowRun`** required members: `workflow_run_id`, `status` (`JobStatusOrStr`),
     `workflow_type` (`workflowType`), `progress_percentage` (`progressPercentage`, float),
     `attempt_index`, `project_id`, `project_url`, `error` (`ApiErrorModel | None`).

3. `client.projects.export_project(project_id: str, *, body: ExportProjectRequest|dict|None
   =None, request_options=None) -> ExportProjectResponse`
   - Route `POST /v1/projects/{projectId}/export`. Error: `RawError` (Case B).
   - **`ExportProjectResponse`** required member: `export_id` (alias `exportId`).

4. `client.projects.get_project_export(project_id: str, export_id: str, *,
   request_options=None) -> ProjectExport`
   - Route `GET /v1/projects/{projectId}/exports/{exportId}`. Error: `RawError` (Case B).
   - **`ProjectExport`** members: `export_id`, `project_id`, `status` (`JobStatusOrStr`),
     `progress_percentage` (float), `attempt_index`, `download_url` (`downloadUrl`, `str|None`,
     signed MP4 URL — `null` until `succeeded`), `download_url_expires_at`, `thumbnail_url`,
     `export_file_id`, `file` (`FileInfo|None`), `error` (`ApiErrorModel|None`).

### Request models (members we set; `Optional[T]` here = `T | UNSET`, **not** None)

- **`ScriptToVideoRequest`** (`from videogen.models import ScriptToVideoRequest`):
  - `script: str` (required) — narration, verbatim.
  - `visual_style: WorkflowVisualStyle` (required, alias `visualStyle`).
  - `aspect_ratio: Optional[AspectRatio]` (alias `aspectRatio`) — set `{width:16, height:9}`.
  - Leave all other members `UNSET` (voice-only default voice; STOCK; no remix; no avatar).
- **`WorkflowVisualStyle`** (`from videogen.models import WorkflowVisualStyle`):
  - `type_: WorkflowVisualStyleTypeOrStr` (required, alias `type`) → `WorkflowVisualStyleType.STOCK`.
- **`AspectRatio`** (`from videogen.models import AspectRatio`): `width:int`, `height:int` (both required).
- **`ExportProjectRequest`** (`from videogen.models import ExportProjectRequest`):
  - `quality: Optional[ExportProjectQualityOrStr]` → `ExportProjectQuality.HIGH`. Rest `UNSET`.
- **mypy note (`[call-arg]`)**: constructing a model with Python member names is correct at
  runtime but plain mypy flags it. If it fires, pass the **`…Dict`** companion (e.g.
  `ScriptToVideoRequestDict`), never the alias spelling, never `# type: ignore`.

### Enums (wire values, from source)

- `JobStatus`: `pending`, `running`, `succeeded`, `failed`, `cancelled`
  (`from videogen.models.enums.job_status import JobStatus`). In-progress: pending/running;
  terminal: succeeded/failed/cancelled. `progress_percentage` == 100 when succeeded.
  Enums are **open** (`…OrStr`) — an unknown wire value passes through as `str`; compare by value.
- `ExportProjectQuality`: `STANDARD, HIGH, FULL_HIGH, ULTRA_HIGH`
  (`from videogen.models.enums.export_project_quality import ExportProjectQuality`).
- `WorkflowVisualStyleType`: `STOCK, AI_IMAGE`
  (`from videogen.models.enums.workflow_visual_style_type import WorkflowVisualStyleType`).

### Error surface

- `from videogen.core import ApiError, RawError`. `ApiError.error: RawError`
  (`.status_code`, `.text()`, `.json()`). `ApiErrorModel` (on `WorkflowRun.error` /
  `ProjectExport.error`) has `message` (required), `code`, `requirement`,
  `internal_error_code` — use `message`/`code` for the failure text we surface.
- SDK performs **no retries** — our polling loop owns interval + max-deadline; we deliberately
  add bounded polling, and do not add call-level retries (kept simple; documented).

### Assumptions & blockers

- Minor assumptions only (export tier = HIGH=720p; default voice acceptable; daemon-thread
  worker acceptable given no-broker constraint). No blockers. Proceed.

## REQUIRED READING (load before coding the governed step)

- `MUST load videogen:python-error-handling` — error boundary + typed exceptions (always).
- `MUST load videogen:python-client-initialization` — before constructing `VideogenClient`.
- `MUST load videogen:python-calling-endpoints` — before first `client.*` call.
- `MUST load videogen:python-models` — building request models / `…Dict` companions / open enums.
- `MUST load videogen:python-testing` — before the first test file / fake-transport script.
- `MUST load videogen:python-configuration-resilience` — base_url/timeout/no-retry polling.
</content>
