# VideoGen integration plan — "Article → shareable video" for Wagtail Bakery

Turn one published blog article into a short narrated MP4, exposed on the existing Wagtail v3
HTTP API (`/api/v3-preview/`). Additive only. Produced with the **VideoGen** SDK
(`videogen-apimatic`, import root `videogen`, version `1.0.0`), which is the sole reference for
every VideoGen interaction.

Sync vs async: **SYNC**. The host is a Django WSGI app whose entire v3 API is sync `def` Ninja
views (see `wagtail/api/v3/routers/pages.py`). Use `videogen.VideogenClient`, close with
`close()`. The sync/async clients do not mix.

---

## Repo conventions to imitate (pattern → exemplar)

- **v3 API routes** = Django Ninja `Router`, `@router.post(...)`, `auth=BearerTokenAuth()` →
  `wagtail/api/v3/routers/pages.py`.
- **Coarse permission gate** = `@require_any_permission(Page, ("publish",))` →
  `wagtail/api/v3/permissions.py` + its use on `publish()` in `routers/pages.py`.
- **Fine per-page publish check** = `page.permissions_for_user(request.user).can_publish()` →
  `publish()` and `_check_can_view_revisions()` in `routers/pages.py`.
- **Errors** = raise `PermissionDenied` / `Http404` / `pydantic ValidationError`; the registered
  handlers render RFC7807 `application/problem+json` (401 when unauthenticated, 403 when
  authenticated-but-forbidden, 404, 422) → `wagtail/api/v3/errors.py`.
- **Settings env reads** = `os.environ.get(...)` in `bakerydemo/settings/base.py`.
- **App extending the API** = `bakerydemo/base/api.py` + registration in `bakerydemo/api.py`
  (that is the v2 pattern; v3 has no project hook yet, so we register in `AppConfig.ready()`).
- **Blog article fields** = `BlogPage.title`, `BlogPage.introduction` (TextField),
  `BlogPage.body` (StreamField), `.live` → `bakerydemo/blog/models.py`.

Ninja serializes responses with `by_alias=False` by default (`ninja/operation.py`), so routes
that must emit camelCase set `by_alias=True` on the decorator.

## Toolchain

- Env manager: **pip + venv** (`.venv/`, Python 3.12 via `py -3.12`). `videogen-apimatic`
  installed into `.venv`. Requirements: `requirements/base.txt`.
- Tests: `./.venv/Scripts/python.exe manage.py test` (Django test runner) with
  `DJANGO_SETTINGS_MODULE=bakerydemo.settings.dev` (or `.test`). No pytest config ships; use the
  Django runner for app tests.
- Type check: no mypy configured in-repo. Will `pip install mypy` into `.venv` and run
  `mypy --strict` on the files touched (SDK ships `py.typed`, generated under `mypy --strict`).
- Lint: `ruff` (config `ruff.toml`; select B,BLE,C4,E,F,I,RUF100,T20,UP,W — **N is not
  selected**, but we still use snake_case + aliases for cleanliness). `ruff check <paths>`.

## Baseline (untouched tree)

- `migrate` + `load_initial_data` succeed. Article **`Tracking Wild Yeast` = page_id 62**, live.
  Intro first sentence: "Yeasts, with their single-celled growth habit, can be contrasted with
  molds, which grow hyphae."
- Credential smoke (read-only, no billable ops): `client.account.get_me()` → `MeResponse` OK;
  `client.workflows.list_workflow_runs(limit=1)` OK. Base URL = default `https://api.videogen.io`
  (no `VIDEOGEN_BASE_URL` set). Auth works.

---

## Design

### Flow (exactly ONE billable video + ONE export for the whole run)

1. `POST /api/v3-preview/pages/{page_id}/video/` — resolve page (must be a live `BlogPage`; caller
   must be able to publish it). Idempotent per page: `get_or_create` a `VideoJob` keyed uniquely on
   the page. Only the creating request calls `client.workflows.script_to_video(...)` (the single
   video build) and stores `workflow_run_id` + `project_id`. A repeat POST returns the existing job
   — never a second workflow, never a second bill. Returns `202` (new) / `200` (existing) with
   top-level `videoJobId` (plus the full status object).
2. `GET /api/v3-preview/pages/{page_id}/video/{videoJobId}/` — reports `status`,
   `progressPercentage`, `downloadUrl`, `error`. Drives the state machine lazily (reconcile):
   - workflow phase → `client.workflows.get_workflow_run(workflow_run_id)`;
     `succeeded` ⇒ claim-once and call `client.projects.export_project(project_id, quality=HIGH)`
     (the single 720p export); `failed`/`cancelled` ⇒ FAILED with the workflow's error.
   - export phase → `client.projects.get_project_export(project_id, export_id)`;
     `succeeded` ⇒ download the MP4 once into Django storage, status READY; else FAILED.
3. `GET /api/v3-preview/pages/{page_id}/video/{videoJobId}/download/` — streams the stored MP4
   (302-free, served by the site). `downloadUrl` in the status response points here (absolute).
   Storing the MP4 locally makes it downloadable "for as long as the article exists", independent
   of VideoGen's 7-day signed-URL expiry. `VideoJob.page` FK is `on_delete=CASCADE` so the job (and
   its stored MP4) disappear when the article does.

No task queue / broker / thread: `script_to_video` returns immediately (async on VideoGen's side);
progress is driven by the caller polling GET. Matches "does not have to finish before the call
returns" and "read the provider's own reported state".

### Narration script (from the article's OWN text only; never sent elsewhere to be rewritten)

`build_narration_script(page)` = `page.title` + first sentence of `page.introduction`, hard-capped
to **30 words** (`VIDEOGEN_NARRATION_MAX_WORDS`). Body is deliberately NOT narrated (spend limit).
For page 62 → "Tracking Wild Yeast. Yeasts, with their single-celled growth habit, can be
contrasted with molds, which grow hyphae." (~18 words, ~10–15s). The script is assembled locally by
string ops — no external summarization/rewrite service.

### Cheap-shape request (hard spend limits — requirements, not preferences)

`ScriptToVideoRequest`:
- `script` = the ≤30-word narration.
- `visual_style` = `WorkflowVisualStyle(type_="STOCK")` → **stock footage only**, never AI imagery.
- `aspect_ratio` = `AspectRatio(width=16, height=9)` → **16:9**.
- `actor_entity_id` **omitted** → voiceover only, **no avatar/presenter**.
- `remix_actions` **omitted** → no image-to-video, no captions/logo/extra billed items.
- `quality` omitted (only affects AI_IMAGE; STOCK unaffected).
- `is_output_temporary` omitted (defaults false) → keep files retained for durability.

Export (`ExportProjectRequest`): `quality = "STANDARD"` (720p tier) via `VIDEOGEN_EXPORT_QUALITY`.
**Never `ULTRA_HIGH` (4K).** `watermark_mode`/`end_screen_mode` left at default (AUTO) — setting
`NONE` requires a Pro plan and would error. Export is called **exactly once** per job (claim guard).

720p → tier mapping: the SDK does NOT publish a tier→pixel mapping (only "vertical resolution
tier"). Empirically confirmed during self-verify that `HIGH` renders **1080p**, so with the ladder
STANDARD < HIGH < FULL_HIGH < ULTRA_HIGH the 720p (HD) tier is **`STANDARD`**, and the default is
`STANDARD`. `ULTRA_HIGH`/4K is refused outright by the client. (Note: the one verification clip was
produced under the initial `HIGH` assumption and therefore came out at 1080p; the one-export limit
forbade re-exporting it at STANDARD — see the final report.)

### Provider failures → typed exceptions (constraint)

Error-translation boundary in the service catches SDK failure kinds and re-raises our own types:
- `videogen.core.ApiError` (status ≥400) → `VideoGenAPIError(status_code, code, message)`
  (reads `ApiError.error` = `RawError` Case B; `.json()` → `ApiErrorModel`-shaped `code`/`message`).
- `pydantic.ValidationError` / `ValueError` (decode failure — bypasses both response modes) →
  `VideoGenProtocolError`.
- `httpx.HTTPError` (transport, arrives unwrapped) → `VideoGenUnavailableError`.
- Missing `VIDEOGEN_API_KEY` → `VideoGenConfigurationError` (raised at client build).
All subclass `VideoGenError`. On a job, a caught provider failure marks the job FAILED and records
code/message so the GET endpoint's `error` field carries what went wrong.

### Settings (env at runtime; NO secret values in the repo)

`bakerydemo/settings/base.py` (reference variable NAMES only):
- `VIDEOGEN_API_KEY = os.environ.get("VIDEOGEN_API_KEY")`
- `VIDEOGEN_BASE_URL = os.environ.get("VIDEOGEN_BASE_URL")` — optional; falsy ⇒ SDK default host,
  else passed verbatim as `base_url`.
- `VIDEOGEN_EXPORT_QUALITY = os.environ.get("VIDEOGEN_EXPORT_QUALITY", "HIGH")`
- `VIDEOGEN_NARRATION_MAX_WORDS = 30`
- `VIDEOGEN_HTTP_TIMEOUT = float(os.environ.get("VIDEOGEN_HTTP_TIMEOUT", "60"))`
- add `"bakerydemo.videos"` to `INSTALLED_APPS`.

### Client lifetime

Module-level lazy singleton `get_client()` in `bakerydemo/videos/videogen_client.py`:
`VideogenClient(bearer_auth=settings.VIDEOGEN_API_KEY, base_url=<override or None>,
timeout=settings.VIDEOGEN_HTTP_TIMEOUT)`. Long-lived (owns httpx pool); `atexit`-close. Raise
`VideoGenConfigurationError` if key missing (else requests go out unauthenticated silently).

### File layout (`bakerydemo/videos/`)

`apps.py` (ready() registers router), `models.py` (VideoJob), `migrations/0001`, `narration.py`,
`videogen_client.py`, `exceptions.py`, `service.py` (create_job/reconcile/mp4 download),
`api.py` (Ninja schemas + routes), `tests/` (fakes + unit tests).

---

## CONTRACT SHEET (no open lookups)

**Client** (keyword-only ctor): `VideogenClient(base_url: str|None=None, timeout: float=30.0,
bearer_auth: str|None=None, custom_http_client: HttpClient|None=None)`. `close()` obligation.
Async twin `AsyncVideogenClient` uses `custom_async_http_client` + `aclose()` — NOT used here.
Base URL default `https://api.videogen.io`. Every credentials kw optional ⇒ unset = unauthenticated.
Imports: `from videogen import VideogenClient`; `from videogen.core import ApiError, RawError`;
models from `videogen.models`.

**Operations in scope** (all Case B → `.error` is `RawError`; all raise `ApiError` on parsed call;
the SDK performs **no retries** — none needed here beyond marking FAILED; every op ends with
keyword-only `request_options`; params behind `*` are keyword-only with real defaults):

| Op | Signature (positional / keyword) | Returns | Notes |
|---|---|---|---|
| `client.workflows.script_to_video` | `(*, body: ScriptToVideoRequest\|dict\|None=None)` | `StartWorkflowRunResponse` | body keyword-only |
| `client.workflows.get_workflow_run` | `(workflow_run_id: str, *)` | `WorkflowRun` | id positional |
| `client.projects.export_project` | `(project_id: str, *, body: ExportProjectRequest\|dict\|None=None)` | `ExportProjectResponse` | id positional, body kw |
| `client.projects.get_project_export` | `(project_id: str, export_id: str, *)` | `ProjectExport` | both positional |

**Model members used** (`Optional[T]` here = `T | UNSET`, NOT `typing.Optional`; never pass `None`
to an `Optional[T]`; wire aliases shown; construct via the model or its `…Dict` companion — if
plain-mypy `[call-arg]` fires on Python member names, switch that call to the `…Dict` companion,
never the alias spelling, never `# type: ignore`):

- `ScriptToVideoRequest`: `script: str` (required); `visual_style: WorkflowVisualStyle` (required,
  alias `visualStyle`); `aspect_ratio: Optional[AspectRatio]` (alias `aspectRatio`). Others omitted.
- `WorkflowVisualStyle`: `type_: WorkflowVisualStyleTypeOrStr` (required, alias `type`) = `"STOCK"`.
- `AspectRatio`: `width: int`, `height: int` (both required) = 16, 9.
- `StartWorkflowRunResponse` (all required): `workflow_run_id` (alias `workflowRunId`),
  `project_id` (`projectId`), `project_url` (`projectUrl`), `remix_action_ids` (`remixActionIds`).
  → assert `workflow_run_id`, `project_id` on the 2xx body.
- `WorkflowRun` (all required): `workflow_run_id`, `status: JobStatusOrStr`, `workflow_type`,
  `progress_percentage: float` (alias `progressPercentage`), `attempt_index`, `project_id`,
  `project_url`, `error: ApiErrorModel|None`. → assert `status`, `progress_percentage`.
- `ExportProjectRequest`: `quality: Optional[ExportProjectQualityOrStr]` = `"HIGH"`. Others omitted.
- `ExportProjectResponse`: `export_id: str` (required, alias `exportId`). → assert `export_id`.
- `ProjectExport` (required): `export_id`, `project_id`, `status: JobStatusOrStr`,
  `progress_percentage`, `attempt_index`; nullable: `download_url: str|None` (alias `downloadUrl`),
  `download_url_expires_at`, `thumbnail_url`, `export_file_id` (`exportFileId`), `file: FileInfo|None`,
  `error: ApiErrorModel|None`. → on `succeeded`, `download_url` is the signed MP4 URL (auto re-signed
  on each call; valid 7 days). We fetch the bytes once and store locally.
- `ApiErrorModel`: `message: str` (required); `code: OptionalNullable[str]`;
  `internal_error_code` (alias `internalErrorCode`). Used to fill our FAILED error/code.

**Enums** (open, `str`-valued; wire values):
- `JobStatus`: `pending`, `running`, `succeeded`, `failed`, `cancelled` (lowercase wire values).
  Terminal = succeeded/failed/cancelled.
- `ExportProjectQuality`: `STANDARD`, `HIGH`, `FULL_HIGH`, `ULTRA_HIGH`. Use `STANDARD` (720p;
  `HIGH` empirically = 1080p). Never `ULTRA_HIGH` (4K).
- `WorkflowVisualStyleType`: `STOCK`, `AI_IMAGE`. Use `STOCK`.

**Errors**: single `ApiError`; `.error` is `RawError` (Case B) with `.status_code`, `.text()`,
`.json()`. Decode failure raises `ValidationError`/`ValueError` (NOT `ApiError`, bypasses both
response modes) — catch separately. `httpx` transport errors arrive unwrapped — catch separately.

**Response mode**: use the plain parsed call (raises `ApiError`) for every op; none returns `None`,
so `with_raw_response` is not needed.

---

## REQUIRED READING (load every one BEFORE implementing the step it governs)

- `MUST load videogen:python-client-initialization` — before building `VideogenClient` (keyword-only,
  close() obligation, module-scoped singleton, sync≠async).
- `MUST load videogen:python-authentication` — before setting `bearer_auth` (optional kw ⇒ silent
  unauth; load secret from env).
- `MUST load videogen:python-calling-endpoints` — before the first `client.<ctrl>.<op>(...)` call
  (positional/keyword-only split; two response modes).
- `MUST load videogen:python-models` — before constructing `ScriptToVideoRequest` etc. (`Optional`=
  `T|UNSET`; `…Dict` companions; open enums; wire aliases; the plain-mypy `[call-arg]` trap).
- `MUST load videogen:python-error-handling` — before any try/except around an SDK call (single
  `ApiError`, `.error` union, decode failures + transport errors bypass it).
- `MUST load videogen:python-configuration-resilience` — before setting base_url/timeout (no retries;
  timeout semantics; base_url override).
- `MUST load videogen:python-testing` — before any test/verification script that fakes the SDK
  (fake the transport protocol `HttpClient`, or respx at httpx layer; assert on the built request).

## Assumptions & Blockers

- **No blockers.** Credential valid; all in-scope ops reachable (write ops NOT smoked to avoid
  cost). Decisions made (not gaps): 720p→`HIGH`; lazy reconcile-on-GET instead of a worker
  (no queue allowed); local MP4 storage for durability; idempotent one-job-per-page (no auto-retry
  after failure, honoring "exactly ONE video"). Minor assumptions only ⇒ proceed.
