# VideoGen integration plan — "Article → shareable video" for Wagtail Bakery

## Goal

Add an **additive** capability to the Wagtail bakerydemo: turn a published blog article into a
short narrated MP4 via VideoGen, exposed on the existing `/api/v3-preview/` HTTP API. Nothing
existing is altered.

- `POST /api/v3-preview/pages/{page_id}/video/` → start production, returns `{ "videoJobId": ... }` (202).
- `GET  /api/v3-preview/pages/{page_id}/video/{videoJobId}/` → `{ status, progressPercentage, downloadUrl, error }`.

## Repo conventions (survey results — pattern + exemplar to imitate)

- **API framework**: `/api/v3-preview/` is **Django Ninja** (django-ninja 1.7.0), NOT DRF. The
  singleton `api = NinjaAPI(...)` lives in `wagtail/api/v3/api.py`; `bakerydemo/urls.py` mounts it
  via `from wagtail.api.v3.urls import api` → `path("api/v3-preview/", api.urls)`.
- **Routers**: registered with `api.add_router("/pages/", <Router>)`. Multiple distinct `Router`
  objects may share the `/pages/` prefix (verified: `add_router` appends `(prefix, router)`; the
  duplicate check is on the *router object*, not the prefix). Routers must be added **before**
  `api.urls` is accessed → register in an AppConfig `ready()` (blessed pattern per
  `wagtail/api/v3/registry.py` docstring: "Registrations run from ready() so other apps can
  register ... from their own ready() hooks"). All models are loaded before any `ready()`, so
  app ordering does not affect page-model discovery.
- **Auth**: `from wagtail.api.v3.auth import BearerTokenAuth`; used per-operation as
  `auth=BearerTokenAuth()` (exemplar: `wagtail/api/v3/routers/pages.py` write ops). It resolves
  `Authorization: Bearer <token>` → `APIToken` → `request.user`, and normalizes to
  AnonymousUser on invalid/inactive/revoked tokens.
- **Permission gate**: exemplar is the `publish` view in `routers/pages.py`:
  `@require_any_permission(Page, ("publish",))` (broad model gate) **plus** a per-page check
  `page.permissions_for_user(request.user).can_publish()`. Raise `django.core.exceptions.PermissionDenied`
  → the v3 error handler returns 401 if unauthenticated, else 403.
- **Errors**: v3 registers RFC7807 `application/problem+json` handlers for `Http404`,
  `PermissionDenied`, `HttpError`, validation errors (`wagtail/api/v3/errors.py`). So my views
  raise `Http404` / `PermissionDenied` / `ninja.errors.HttpError` and get correct responses free.
- **App layout**: project apps live at `bakerydemo/<app>/` and are dotted in
  `INSTALLED_APPS` in `bakerydemo/settings/base.py`. Custom API viewset precedent:
  `bakerydemo/base/api.py`. New app → `bakerydemo/videos/`.
- **Blog model**: `bakerydemo/blog/models.py::BlogPage(Page)` — `title` (from Page),
  `introduction = TextField` (plain text), `body = StreamField`. Pages keyed by integer `Page.pk`.
  Resolve with `Page.objects.get(pk=...).specific`.
- **Settings**: `bakerydemo/settings/base.py` reads env via `os.environ`. Uses python-dotenv
  (loaded in `manage.py`). `dev` settings have DEBUG on, accept any host.
- **Lint**: ruff (line 88; rules include `BLE` blind-except and `T20` no-print). Use logging, not
  print; avoid bare `except Exception` (or justify with a comment). Baseline `ruff check` clean.
- **Sync**: Django Ninja views are **synchronous** here → use the **sync** `VideogenClient`.

## Toolchain

- Env manager: **pip + venv**. `.venv/` created with Python 3.12.10. Installed
  `requirements/base.txt` + `requirements/development.txt` + `videogen-apimatic==1.0.0`.
- Type checker: none shipped → installed `mypy==1.19.1` (pinned <2 to keep `pathspec<1` for
  curlylint). Run `mypy` on touched files (SDK ships `py.typed`, generated under `--strict`).
- Tests: Django test runner — `python manage.py test`. Settings module `bakerydemo.settings.dev`
  (or `.test`).
- DB: SQLite; `migrate` + `load_initial_data` already run. Baseline ruff clean.

## Credentials / environment verification

- `VIDEOGEN_API_KEY` present in env (len 81). `VIDEOGEN_BASE_URL` **unset** → SDK default
  `https://api.videogen.io` (confirmed via smoke). Both read at runtime through Django settings;
  **values never written to any repo file**.
- Read-only smoke (scratchpad, real key) succeeded: `account.get_me()` → MeResponse;
  `workflows.list_workflow_runs(limit=1)` and `projects.list_projects(limit=1)` → 200. So auth,
  base URL, and entitlement to the workflows+projects controllers are all confirmed. The
  cost-bearing ops (`script_to_video`, `export_project`) were deliberately **not** smoked.

---

## Production flow (design)

`script_to_video` builds a *project* (stock b-roll + TTS narration); it does not itself yield an
MP4. Rendering an MP4 is a *project export*. So one "video" = one `script_to_video` + one
`export_project`. Steps the background producer runs:

1. `workflows.script_to_video(body=ScriptToVideoRequest(...))` → `StartWorkflowRunResponse`
   (`workflow_run_id`, `project_id`).
2. Poll `workflows.get_workflow_run(workflow_run_id)` → `WorkflowRun.status`/`progress_percentage`
   until terminal.
3. On `succeeded`: `projects.export_project(project_id, body=ExportProjectRequest(quality=HIGH))`
   → `ExportProjectResponse.export_id`.
4. Poll `projects.get_project_export(project_id, export_id)` → `ProjectExport.status`/`download_url`
   until terminal.
5. On `succeeded`: video is ready. `download_url` is a 7-day signed URL that the endpoint
   re-signs on read, so the GET endpoint re-fetches `get_project_export` to always return a fresh
   URL → durable "as long as the article exists".

### The cheap shape (spend limits — hard requirements)

- `script`: **title + first sentence of the introduction only**, ≤ 30 words (built locally from the
  article's own text; nothing sent elsewhere to rewrite). Target 10–15s clip.
- `visual_style = WorkflowVisualStyle(type_="STOCK")` → stock footage/images only, **no AI images**.
- `actor_entity_id` **omitted** → voiceover only, **no avatar/presenter**.
- `voice_id` omitted → default voice (no voice generation billed).
- `aspect_ratio = AspectRatio(width=16, height=9)`.
- `remix_actions` omitted (no captions/convert-to-video/transitions/logo — each bills).
- `quality` (AI-image tier) omitted — irrelevant to STOCK.
- `is_output_temporary` omitted (default false) → outputs retained (durability).
- Export exactly **once**, `quality = ExportProjectQuality.HIGH` (= 720p; tiers are
  STANDARD/HIGH/FULL_HIGH/ULTRA_HIGH ⇒ SD/**HD 720**/FHD 1080/UHD 4K by standard naming;
  ULTRA_HIGH is the 4K one to avoid). No second export at any quality.
- **Exactly ONE video for the whole run.** Idempotency below guarantees a re-POST never produces
  or bills a second.

### Idempotency

One `ArticleVideo` row per page. On POST: if a non-failed row exists for the page → return its
existing `videoJobId` (no new SDK call). A `failed` row may be re-driven (no video was produced).

### Async without a queue

No broker/queue allowed. Producer runs in a `threading.Thread(daemon=True)`; it calls
`django.db.close_old_connections()` at start and in `finally`, reloads the row by id, and updates
`status`/`progress_percentage`/`error` as it advances. Polling loop with sleep + overall timeout
(no per-request retry — the SDK does none; stated, not built).

### API status vocabulary (top-level `status`)

`processing` | `ready` | `failed` — a 1:1 map to the spec's "still being produced / ready to
download / failed". `progressPercentage` (0–100), `downloadUrl` (string when ready else null),
`error` (string when failed else null).

---

## CONTRACT SHEET — VideoGen Python SDK (videogen-apimatic 1.0.0)

**Sync vs async**: use **sync** `VideogenClient` (Django Ninja views are sync). The sync and async
clients do not mix. Client owns an httpx pool → must be long-lived (module singleton) and
`.close()`d at teardown. Base URL: `base_url` omitted ⇒ `https://api.videogen.io`; pass
`VIDEOGEN_BASE_URL` verbatim when set. Auth: `bearer_auth=<VIDEOGEN_API_KEY>`.

**Keyword-only boundary**: every op below takes its path params positionally and everything after
`*` (incl. `body`, `request_options`) by keyword. Every keyword-only param has a real default — no
"must pass None" hazard. **No op returns None** — `with_raw_response` needed only where the status
code itself matters (not needed here; parsed calls suffice).

**Error model**: all ops are **Case B** → on error the parsed call raises `ApiError` with
`.error: RawError` (`.status_code:int`, `.text()`, `.json()`, `.content`, `.response`) and
`.status_code:int`. A **decode failure raises `pydantic.ValidationError`/`ValueError`, NOT
`ApiError`**, and bypasses both response modes. `httpx` transport exceptions
(`httpx.HTTPError` subclasses, e.g. `TimeoutException`, `ConnectError`) arrive **unwrapped**. **No
retries** are performed by the SDK. Import `ApiError`, `RawError` from `videogen.core`.

### Operations in scope (from `map/operations/*.md`, verified against installed 1.0.0 source)

| Op | Signature (sync, parsed) | Returns | Notes |
|---|---|---|---|
| `client.workflows.script_to_video` | `(*, body: ScriptToVideoRequest \| ...Dict \| None = None, request_options=None)` | `StartWorkflowRunResponse` | POST /v1/workflows/script-to-video |
| `client.workflows.get_workflow_run` | `(workflow_run_id: str, *, request_options=None)` | `WorkflowRun` | GET /v1/workflows/runs/{id} |
| `client.projects.export_project` | `(project_id: str, *, body: ExportProjectRequest \| ...Dict \| None = None, request_options=None)` | `ExportProjectResponse` | POST /v1/projects/{id}/export |
| `client.projects.get_project_export` | `(project_id: str, export_id: str, *, request_options=None)` | `ProjectExport` | GET /v1/projects/{id}/exports/{exportId} |

### Model members I set / read (required vs UNSET; `Optional[T]` here = `T | UnsetType`, NOT None)

**`ScriptToVideoRequest`** (`models/script_to_video_request.py`) — build via the **`...Dict`**
companion to keep mypy happy on Python member names (per python-models `[call-arg]` note), OR the
model with member names. Members I set:
- `script: str` (**required**) — narrated verbatim.
- `visual_style: WorkflowVisualStyle` (**required**, wire `visualStyle`) — set `type_="STOCK"`
  (wire `type`; `WorkflowVisualStyleTypeOrStr`, member `WorkflowVisualStyleType.STOCK`).
- `aspect_ratio` (wire `aspectRatio`) — `AspectRatio(width=16, height=9)` (both `int`, required in AspectRatio).
- Everything else (`voice_id`, `actor_entity_id`, `quality`, `visual_pacing`, `remix_actions`,
  `is_output_temporary`, ...) → leave UNSET (omit).

**`StartWorkflowRunResponse`** (required members to read): `workflow_run_id` (wire `workflowRunId`),
`project_id` (`projectId`), `project_url` (`projectUrl`), `remix_action_ids` (`remixActionIds`).

**`WorkflowRun`** (all required): `workflow_run_id`, `status: JobStatusOrStr`, `workflow_type`,
`progress_percentage: float` (wire `progressPercentage`), `attempt_index`, `project_id`,
`project_url`, `error: ApiErrorModel | None`.

**`ExportProjectRequest`** — set `quality = ExportProjectQuality.HIGH` (`ExportProjectQualityOrStr`).
All members optional/UNSET; watermark/end-screen left default (AUTO; NONE would require Pro).

**`ExportProjectResponse`**: `export_id: str` (wire `exportId`) — required.

**`ProjectExport`** (all required as fields, several nullable):
`export_id`, `project_id`, `status: JobStatusOrStr`, `progress_percentage: float`,
`attempt_index`, `download_url: str|None` (wire `downloadUrl`; null until succeeded),
`download_url_expires_at: int|None`, `thumbnail_url`, `thumbnail_url_expires_at`,
`export_file_id: str|None`, `file: FileInfo|None`, `error: ApiErrorModel|None`.

### Enums (member → wire)
- `JobStatus` (`models/enums/job_status.py`): PENDING=`pending`, RUNNING=`running`,
  SUCCEEDED=`succeeded`, FAILED=`failed`, CANCELLED=`cancelled`. Terminal: succeeded/failed/cancelled.
  **Open enum** — compare by wire string value to be safe (`str(status) == "succeeded"` /
  `JobStatus.SUCCEEDED.value`).
- `WorkflowVisualStyleType`: STOCK=`STOCK`, AI_IMAGE=`AI_IMAGE`.
- `ExportProjectQuality`: STANDARD, HIGH, FULL_HIGH, ULTRA_HIGH. Use **HIGH** (720p).

### Decode-failure exposure
`script_to_video`/`export_project` responses have single required members (`workflow_run_id` /
`export_id`); `WorkflowRun`/`ProjectExport` have several required. A truncated 2xx body would raise
`ValidationError` there → my service treats `ValidationError`/`ValueError` around SDK calls as a
provider failure (typed `VideoGenError`), distinct from `ApiError`.

---

## Assumptions & Blockers

- **720p = `ExportProjectQuality.HIGH`** — a judgment from standard resolution naming (HD 720 /
  FHD 1080 / UHD 4K); the enum docstring says "vertical resolution tier" without pixel numbers.
  Not a gap: decided. ULTRA_HIGH is the 4K value the task forbids.
- **Permission model**: "permitted to publish that page" ⇒ `can_publish()` per-page, gated on both
  POST and GET (both are the editorial video resource). Decided.
- **Status vocabulary** (`processing`/`ready`/`failed`) is my own, mapping the spec's three
  outcomes. Decided.
- No genuine SDK capability gap found: the whole flow (script→project→export→download) is covered
  by `workflows.script_to_video`, `workflows.get_workflow_run`, `projects.export_project`,
  `projects.get_project_export`. Proceeding.

## Implementation order

1. Settings: `VIDEOGEN_API_KEY`, `VIDEOGEN_BASE_URL` in `settings/base.py`; add
   `"bakerydemo.videos"` to INSTALLED_APPS (last).
2. `bakerydemo/videos/` app: `apps.py` (ready→register router), `exceptions.py` (typed),
   `client.py` (singleton factory), `service.py` (typed-exception wrapper over the 4 ops),
   `narration.py` (title + first sentence, ≤30 words), `models.py` (`ArticleVideo`),
   `producer.py` (threaded flow), `api.py` (Ninja router: POST + GET), `migrations/`.
3. Tests (mock transport / service — never real API).
4. mypy + ruff + Django tests.
5. One real end-to-end video + MP4 download; token admit/refuse checks.

## REQUIRED READING (load before implementing)

- `python-error-handling` — MUST load (error boundary; ApiError vs ValidationError vs httpx). **Floor.**
- `python-client-initialization` — MUST load before constructing the client (keyword-only,
  singleton lifetime, close()). **Floor.**
- `python-calling-endpoints` — MUST load before first `client.<ctrl>.<op>()` call (positional/kw
  split, response modes).
- `python-models` — MUST load before building `ScriptToVideoRequest`/`ExportProjectRequest`
  (UNSET vs Optional, `...Dict` companion for mypy, open enums, wire aliases).
- `python-testing` — MUST load before writing any test/verification script that fakes transport. **Floor.**
