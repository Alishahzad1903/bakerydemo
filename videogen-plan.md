# VideoGen integration plan — "Article → shareable video" for Wagtail Bakery

## Goal (from PHASE-BUILD.md)

Add an **additive** capability to the Wagtail Bakery site: turn one published blog article into a
short narrated MP4 via **VideoGen**, exposed on the existing Wagtail **v3 API**
(`/api/v3-preview/`), authenticated the same way, restricted to callers who may **publish** the page.

Two endpoints:

- `POST /api/v3-preview/pages/{page_id}/video/` → start production; returns top-level `videoJobId`.
  Async — returns before the video is finished.
- `GET  /api/v3-preview/pages/{page_id}/video/{videoJobId}/` → state/outcome; top-level
  `status`, `progressPercentage`, `downloadUrl`, `error`.

## Hard constraints that drive design (non-negotiable spend shape)

- **Produce exactly ONE video in the whole run.** No retry of billable calls. `script_to_video` and
  `export_project` each run **at most once per job**, no automatic re-attempt on failure.
- Narration = **article title + first sentence of its introduction**, ≤ **30 words**, target ~10–15s.
  Built only from the article's own text; never sent to another service to rewrite/expand.
- Cheap shape only: **stock footage** (no AI imagery), **voice only** (no avatar/presenter),
  **one export at 720p**, **16:9**. No image-to-video, no regen/upscale, no standalone media gen,
  no second export.
- Secrets: read `VIDEOGEN_API_KEY` (and optional `VIDEOGEN_BASE_URL`) from env via Django settings.
  **Never write the value into any repo file.**
- Provider failures surfaced as **typed exceptions**.

## Repo conventions (patterns + one exemplar each)

- **v3 API is Django-Ninja**, singleton `NinjaAPI` at `wagtail.api.v3.api:api`, mounted in
  `bakerydemo/urls.py:28` as `path("api/v3-preview/", api.urls)`.
- **Router + endpoint pattern** — exemplar: `.venv/.../wagtail/api/v3/routers/pages.py`
  (`actions_router = Router(auth=BearerTokenAuth())`, `@router.post("/{page_id}/actions/publish/", ...)`,
  `router.add_router("/", actions_router)`). Routes register on the singleton via `api.add_router(prefix, router)`.
- **Auth** — `wagtail.api.v3.auth.BearerTokenAuth` resolves `Authorization: Bearer <token>` to an
  `APIToken`/user and sets `request.user`. Reuse it verbatim on my router.
- **Publish permission** — exemplar `pages.py:publish`: `@require_any_permission(Page, ("publish",))`
  gate + per-page `page.permissions_for_user(user).can_publish()`. I use the per-page check (precise).
- **Errors** — `wagtail.api.v3.errors` registers RFC7807 `application/problem+json` handlers:
  `PermissionDenied`→403 (401 if unauthenticated), `Http404`→404, `ValidationError`→422. Raise those
  Django/Ninja exceptions and the shared handlers format them. Verified counts confirmed empirically.
- **Adding a second router at the same `/pages/` prefix is allowed** by Ninja
  (`NinjaAPI.add_router` only requires `url_name_prefix` when the *same router object* is mounted twice;
  a distinct router at the same prefix is fine — `.venv/.../ninja/main.py:429`).
- **Register on `ready()`** — an AppConfig `ready()` runs during `django.setup()`, before ROOT_URLCONF
  is resolved, so `api.add_router(...)` there lands before `api.urls` is accessed.
- **Response field casing** — Ninja `by_alias` defaults to `False` (`ninja/operation.py:133`), so JSON
  keys = Schema attribute names. Ruff does NOT select `N` (naming) rules (`ruff.toml`), so I use
  **explicit camelCase attribute names** on response schemas to emit `videoJobId`/`progressPercentage`/
  `downloadUrl` exactly.
- **Settings** read env with `os.environ.get(...)` in `bakerydemo/settings/base.py`. `manage.py` loads `.env`.
- **Blog article model** — `bakerydemo/blog/models.py:BlogPage`: `title` (Page), `introduction`
  (TextField), `body` (StreamField). Narration source.

## Toolchain

- `pip` + venv at `.venv/` (Python 3.12.10). `videogen-apimatic==1.0.0` installed → import root `videogen`.
- No test runner configured for the app; Django tests via `manage.py test`. Type checker: none configured
  → run `mypy` on touched files (install into venv). SDK ships `py.typed` under `mypy --strict`.
- Run: `DJANGO_SETTINGS_MODULE=bakerydemo.settings.dev manage.py runserver 0.0.0.0:31140` (port block base).

## Sync vs async — **SYNC**

Host is **Django under WSGI** (dev server / gunicorn). Use `VideogenClient` (sync). Background work runs
in a `threading.Thread` (sync context). Teardown obligation: `close()` (via `with` context manager per
pipeline run). The two client classes do not mix; never use `AsyncVideogenClient` here.

## No infra beyond Python

No Celery/Redis/broker allowed. Async production runs in a **daemon `threading.Thread`** spawned from the
POST handler. The thread owns its own DB connection (`connection.close()` in `finally`) and its own
short-lived `VideogenClient` (context-managed for the whole pipeline, which is one logical unit of work).

---

## Architecture / file layout — new app `bakerydemo.videos`

```
bakerydemo/videos/
  __init__.py
  apps.py              # VideosConfig.ready() -> api.add_router("/pages/", router)
  exceptions.py        # typed exception hierarchy (provider failures)
  narration.py         # build_narration_script(page) -> str  (title + 1st sentence, <=30 words)
  videogen_client.py   # settings-driven client factory + VideoGenService (thin, typed wrapper over SDK)
  models.py            # VideoJob model + JobStatus choices
  service.py           # start_or_get_job(page, user); _run_pipeline(job_id); refresh_download_url(job)
  api.py               # Ninja router: POST + GET, request/response Schemas, permission + validation
  migrations/0001_initial.py
  tests/               # unit tests (stub transport — NO real API), permission tests, narration tests
```
Register `"bakerydemo.videos"` in `INSTALLED_APPS`. Settings get `VIDEOGEN_API_KEY`, `VIDEOGEN_BASE_URL`.

### Data model — `VideoJob`
- `id: UUIDField(primary_key)` → the opaque `videoJobId`.
- `page: FK(wagtailcore.Page, on_delete=CASCADE)` → job dies with the article; downloadable while it exists.
- `status: CharField(choices=JobStatus)` — `PENDING`, `PROCESSING`, `SUCCEEDED`, `FAILED`.
- `progress_percentage: FloatField(default=0)`.
- `script: TextField` — narration actually sent (audit).
- `workflow_run_id`, `project_id`, `export_id`: CharField(blank) — provider handles for polling/refresh.
- `download_url: TextField(blank)`, `download_url_expires_at: BigIntegerField(null)` — cached signed URL.
- `error_message: TextField(blank)`, `error_code: CharField(blank)` — populated on FAILED.
- `created_at`, `updated_at`.
- **Idempotency:** partial unique constraint — unique on `page` where `status != FAILED`
  (`UniqueConstraint(fields=["page"], condition=~Q(status="failed"), name="unique_active_job_per_page")`).
  On POST, `transaction.atomic` + create; on `IntegrityError` fetch the existing active job and return it.
  Guarantees one billable production per article ⇒ "not produced twice / not billed twice".

### Narration (`narration.py`)
`build_narration_script(page)`:
1. title = `page.title`; intro = `page.introduction` (BlogPage).
2. first sentence of intro via regex split on sentence terminators.
3. `script = f"{title}. {first_sentence}"`; collapse whitespace.
4. **Hard cap 30 words** (truncate at word boundary; keep ≥ title). Return script.
Body is available as the article's own text but is deliberately excluded to honor the spend cap
("do not narrate the body"). Verified for pg 62 "Tracking Wild Yeast": 17 words.

### Orchestration (`service.py`) — the pipeline (runs in the thread)
1. `PROCESSING`.
2. `script_to_video(...)` **once** → store `workflow_run_id`, `project_id`.
3. Poll `get_workflow_run(workflow_run_id)` every ~6s; mirror `progress_percentage` (scaled to 0–80%);
   until terminal. `failed`/`cancelled` → typed failure, stop (no re-attempt).
4. `export_project(project_id, HIGH)` **once** → store `export_id`.
5. Poll `get_project_export(project_id, export_id)` every ~6s (scale 80–100%); until terminal.
   `succeeded` → store `download_url`(+expiry) → `SUCCEEDED`. `failed`/`cancelled` → typed failure.
6. Overall wall-clock cap (~30 min) → FAILED("timed out"). Any typed provider exception → FAILED with
   its message/code. `finally: connection.close()`.

Reads (`get_workflow_run`, `get_project_export`) are idempotent GETs; a transient transport error while
polling is retried a bounded number of times **within the poll loop** (never re-issues a billable call).

### Download URL longevity
Provider signed URL is valid 7 days and `get_project_export` re-signs when near expiry. GET endpoint:
if job `SUCCEEDED` and stored URL missing or within 1h of `download_url_expires_at`, call
`get_project_export` to refresh (a read — not billed media work), persist, return. Else return stored URL.
⇒ "stays downloadable as long as the article exists" without hammering the provider.

### Endpoints (`api.py`)
`router = Router(tags=["videos"], auth=BearerTokenAuth())`; `api.add_router("/pages/", router)` in ready().
- `POST /{page_id}/video/` → resolve `page.specific`; require `isinstance(page, BlogPage)` and `page.live`
  (else `Http404`/`ValidationError`); `page.permissions_for_user(request.user).can_publish()` else
  `PermissionDenied`; `service.start_or_get_job(page, user)`; return **202** `{videoJobId, status, ...}`.
- `GET /{page_id}/video/{video_job_id}/` → `get_object_or_404(VideoJob, pk=job_id, page_id=page_id)`;
  same publish-permission gate; refresh URL if needed; return `{videoJobId, pageId, status,
  progressPercentage, downloadUrl, error}`.

Response schemas use camelCase attributes (see casing note). `downloadUrl`/`error` are `str | None`.

---

## CONTRACT SHEET — VideoGen Python SDK (`videogen==1.0.0`, import root `videogen`)

**Client:** `from videogen import VideogenClient`. Keyword-only ctor. Single server / one environment ⇒
`base_url: str | None = None` (default **`https://api.videogen.io`** — silent). Auth = **plain bearer
string**: `bearer_auth=<key>` (NOT OAuth2 → no lazy token fetch, no `OAuthProviderError`, no token-source).
`timeout: float = 30.0`. Sync transport override kw `custom_http_client` (async peer differs — unused).
Teardown `client.close()` / `with`. **SDK performs NO retries** — all retry/backoff is mine.

**All operations are Case B** (map: 0 typed error unions of 59). `e.error` is **always `RawError`**
(`videogen.core.RawError`: `status_code:int`, `text()`, `json()` [raises ValueError if not JSON], `content`,
`response`). `ApiError` (`videogen.core`): `.error`, `.status_code`, `.response`. Raw peer
`.with_raw_response` → `ApiResult` (`Success`/`Failure`). Decode failure raises `pydantic.ValidationError`/
`ValueError` and **bypasses both modes**. Transport errors arrive as **`httpx.*`** unwrapped.

Keyword-only boundary: for these ops the only positional args are the path ids; everything else is
keyword-only with real defaults (no defensive `None` needed). No op returns `None` (raw peer unneeded).

### Operations in scope (one pass)

| Op | Call | Returns | Key result members (assert) |
|---|---|---|---|
| workflows.script_to_video | `client.workflows.script_to_video(body=ScriptToVideoRequest(...))` | `StartWorkflowRunResponse` | `workflow_run_id` (wire `workflowRunId`), `project_id` (`projectId`) |
| workflows.get_workflow_run | `client.workflows.get_workflow_run(workflow_run_id)` [positional] | `WorkflowRun` | `status` (JobStatusOrStr), `progress_percentage` (`progressPercentage`), `error` (ApiErrorModel\|None) |
| projects.export_project | `client.projects.export_project(project_id, body=ExportProjectRequest(...))` [project_id positional] | `ExportProjectResponse` | `export_id` (`exportId`) |
| projects.get_project_export | `client.projects.get_project_export(project_id, export_id)` [both positional] | `ProjectExport` | `status`, `progress_percentage`, `download_url` (`downloadUrl`, str\|None), `download_url_expires_at` (`downloadUrlExpiresAt`, int\|None), `error` (ApiErrorModel\|None) |
| account.get_me | `client.account.get_me()` | `MeResponse` | (smoke only — already verified live) |

### Request models

`ScriptToVideoRequest` (`videogen.models`) — build via the **`...Dict` companion** to satisfy plain mypy
(constructor-alias trap); keys are the **Python names**:
- `script: str` **(required)** — narration verbatim (title + 1st sentence, ≤30 words).
- `visual_style: WorkflowVisualStyle` **(required)** = `{"type_": "STOCK"}` (wire `visualStyle.type`;
  `WorkflowVisualStyleType.STOCK`). STOCK ⇒ stock footage, ignores AI quality/style fields.
- `aspect_ratio: {"width": 16, "height": 9}` (wire `aspectRatio`) — 16:9.
- Omit everything else. Specifically **omit** `actor_entity_id`/`avatar_quality` (⇒ voiceover, NO avatar),
  `remix_actions` (⇒ no captions/image-to-video/transitions), `voice_id` (default voice), `quality`,
  `scenes`, `featured_b_roll_file_ids`, `is_output_temporary` (default false ⇒ output retained),
  `hide_from_ui`. `Optional[T]` here = `T | UNSET` (no None arm) — omit, never pass None.

`ExportProjectRequest` (`videogen.models`) — via `...Dict`:
- `quality: "HIGH"` (`ExportProjectQualityOrStr`; enum `ExportProjectQuality.HIGH`). **720p decision:**
  the tier enum is `STANDARD < HIGH < FULL_HIGH < ULTRA_HIGH` mirroring HD / Full-HD / Ultra-HD; the
  docstring says "Vertical resolution tier". **HIGH = 720p (HD)**; FULL_HIGH = 1080p; **ULTRA_HIGH = 4K
  (the banned tier)**. Choosing HIGH satisfies "720p, never 4K" with margin. (Design decision from the
  enum's own naming — the export-quality capability IS exposed; not a gap.)
- Omit `watermark_mode`/`end_screen_mode` (free-plan default: watermark + end screen appended — acceptable,
  and setting NONE would error without Pro). Omit `delivery_destinations`.

### Enums (`videogen.models.enums`) — open (`...OrStr`), unknown wire value passes through as str
- `JobStatus`: `PENDING="pending"`, `RUNNING="running"`, `SUCCEEDED="succeeded"`, `FAILED="failed"`,
  `CANCELLED="cancelled"`. Terminal = succeeded/failed/cancelled. Compare against `.value` strings and
  handle the open `str` arm (unknown ⇒ treat as non-terminal/keep polling until timeout).
- `WorkflowVisualStyleType.STOCK="STOCK"`; `ExportProjectQuality.HIGH="HIGH"`.

### `ApiErrorModel` (on WorkflowRun.error / ProjectExport.error)
`message: str`, `code: OptionalNullable[str]`, `internal_error_code` (wire `internalErrorCode`),
`requirement`. Use `message`+`code` to fill the job's `error_message`/`error_code` when production fails.

### Error boundary (in `videogen_client.py`) — translate to typed exceptions (one place)
```
try: <sdk call>
except ApiError as e:            -> VideoGenRequestError(status_code=e.status_code, detail=e.error.text())
except pydantic.ValidationError  -> VideoGenUnreadableError("unreadable response; outcome unknown")
except httpx.HTTPError as e:      -> VideoGenUnavailableError("provider unreachable; outcome unknown")
```
Plus `VideoGenConfigError` (missing key) at client build; `VideoGenProductionError(code,message)` when a
WorkflowRun/ProjectExport reports terminal `failed`/`cancelled` (carry `ApiErrorModel.message`/`code`).
Base `VideoGenError`. `raise ... from e` always. Never surface `str(e)`/raw body to the API caller.

---

## Assumptions & blockers
- **Minor assumption:** HIGH = 720p (justified above from enum naming; ULTRA_HIGH=4K is what's banned, avoided).
  Proceed.
- **Minor assumption:** narration = title + first sentence of intro (spend cap dominates the "title/intro/
  body" phrasing). Proceed.
- **Minor:** downloadUrl = the provider's signed MP4 URL returned by the GET (‑"where to download");
  the site makes it retrievable through the GET endpoint. Proceed.
- No blockers. Live credential verified (`get_me` OK, default base URL). Proceed to implement.

## REQUIRED READING (loaded before coding)
- `MUST load python-error-handling` — error boundary (loaded). Case B ⇒ RawError only; decode/transport not ApiError.
- `MUST load python-client-initialization` — sync client, close obligation, thread placement (loaded).
- `MUST load python-authentication` — bearer string; omission ⇒ unauthenticated (loaded).
- `MUST load python-calling-endpoints` — positional path ids vs kw-only body; parsed vs raw (loaded).
- `MUST load python-models` — `...Dict` companion to satisfy mypy; `Optional`=UNSET not None; open enums (loaded).
- `MUST load python-configuration-resilience` — NO retries; base_url default silent; timeout (loaded).
- `MUST load python-testing` — stub transport seam; token-first N/A (no OAuth); assert lowercase headers (loaded).
```
