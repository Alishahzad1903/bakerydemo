# VideoGen integration plan — "article → shareable video" for Wagtail Bakery

## Goal (from PHASE-BUILD.md)

Add an **additive** capability to the existing Wagtail v3 HTTP API (`/api/v3-preview/`):
turn one published blog article into a short narrated MP4, produced with VideoGen.

- `POST /api/v3-preview/pages/{page_id}/video/` → start production, returns `videoJobId` (async, need not finish before returning).
- `GET  /api/v3-preview/pages/{page_id}/video/{videoJobId}/` → `status`, `progressPercentage`, `downloadUrl`, `error` (top-level).
- Narration = article's **own text** only (title + first sentence of introduction). No third-party rewriting.
- Downloadable for as long as the article exists.
- Idempotent per article: asking twice must not produce/​bill a second video.
- Auth exactly like the existing API (`BearerTokenAuth`); restrict to callers who may **publish** the page.

## Spend limits (HARD — not design choices)

- **Exactly ONE video** in the whole run. No retries-to-see-what-happens.
- Script = **title + first sentence of introduction**, ≤ 30 words, target 10–15s. **Body not narrated.**
- Cheap shape: **stock footage** visuals, **voice only** (no avatar/presenter), export **once at 720p**, **16:9**.
- Forbidden: image→video/animate-stills, regen/restyle/upscale, avatars, standalone media gen, any 2nd export.

## Repo survey — conventions & exemplars (pattern → the one file to imitate)

- **API framework**: Django Ninja. v3 API singleton `api = NinjaAPI(...)` in `.venv/.../wagtail/api/v3/api.py`; mounted in `bakerydemo/urls.py` via `path("api/v3-preview/", api.urls)`. Routers must be registered on `api` **before** `api.urls` is accessed (ConfigError otherwise).
- **Router + page-action pattern (EXEMPLAR)**: `.venv/.../wagtail/api/v3/routers/pages.py` — `actions_router = Router(auth=BearerTokenAuth())`, endpoints decorated `@actions_router.post("/{page_id}/actions/publish/")` + `@require_any_permission(Page, ("publish",))`, and per-page enforcement via `page.permissions_for_user(request.user).can_publish()` (see `publish()` at pages.py:399).
- **Auth (EXEMPLAR)**: `.venv/.../wagtail/api/v3/auth.py` — `BearerTokenAuth()` resolves `Authorization: Bearer <token>` → sets `request.user`, returns the `APIToken` as `request.auth`. Invalid/inactive/revoked → returns None → 401.
- **Model-level permission gate (EXEMPLAR)**: `.venv/.../wagtail/api/v3/permissions.py::require_any_permission`.
- **Blog model (EXEMPLAR)**: `bakerydemo/blog/models.py::BlogPage` — fields `title` (from Page), `introduction` (TextField), `body` (StreamField). Target article: "Tracking Wild Yeast" (live BlogPage).
- **Settings**: `bakerydemo/settings/base.py` reads env via `os.environ.get(...)`. Add `VIDEOGEN_API_KEY` / `VIDEOGEN_BASE_URL` here (names only, never values).
- **App layout**: feature apps under `bakerydemo/<app>/` listed in `INSTALLED_APPS`. No `apps.py` in `base/` today; I add a new app `bakerydemo/video/` with an explicit `AppConfig`.
- **Sync or async**: the whole stack is **SYNC** (Ninja views are `def`, Django WSGI). → use the **sync** `VideogenClient`. Background work uses a `threading.Thread` (no Celery/broker — forbidden).
- **Lint/format**: ruff (line-length 88; selected rules `B,BLE,C4,E,F,I,RUF100,T20,UP,W`; `N`/`D` mostly off → camelCase schema fields OK). `make lint` / `ruff`.
- **Tests**: `DJANGO_SETTINGS_MODULE=bakerydemo.settings.test ./manage.py test`. Type checker: none configured → run `mypy` on touched files as the SDK gate.

## Toolchain

- Env: `pip` + venv at `.venv/` (created with `py -3.12`). `videogen-apimatic==1.0.0` installed and import-verified (`from videogen import VideogenClient` OK). Wagtail 8.0, Django 6.
- Tests: `env DJANGO_SETTINGS_MODULE=bakerydemo.settings.test .venv/Scripts/python.exe manage.py test bakerydemo.video`.
- Type check: `.venv/Scripts/python.exe -m mypy` (install into venv) on `bakerydemo/video/`.

## Credentials / config

- `VIDEOGEN_API_KEY` (required), `VIDEOGEN_BASE_URL` (optional; if set, pass **verbatim** as `base_url`). Read via Django settings at runtime. base_url omitted ⇒ SDK default `https://api.videogen.io` (silent). Confirmed env: `VIDEOGEN_API_KEY` set, `VIDEOGEN_BASE_URL` unset.

---

## CONTRACT SHEET — VideoGen Python SDK (import root `videogen`, dist `videogen-apimatic` 1.0.0)

**Sync/async**: SYNC — `from videogen import VideogenClient`. `AsyncVideogenClient` NOT used (would block/deadlock in this sync stack). Client owns an httpx pool → **must** `close()` (or use as context manager). Held per background-job run (one client per pipeline, closed at end) — not per HTTP request, not per SDK call.

**Client construction** (keyword-only): `VideogenClient(bearer_auth=<str>, base_url=<str|None>, timeout=<float=30.0>)`. Omitting `bearer_auth` ⇒ unauthenticated (no error at construction) → guard that the key is set. Omitting `base_url` ⇒ default host silently.

**Operations in scope** (all `client.<ctrl>.<op>`, keyword-only tail after `*`, every kw has a real default, trailing `request_options`; parsed call **raises `ApiError`** on error status; every error is **Case B → `.error` is `RawError`**; async/​raw peers exist but unused):

1. `client.workflows.script_to_video(*, body: ScriptToVideoRequest | ...Dict | None = None, request_options=None) -> StartWorkflowRunResponse`
   - Route `POST /v1/workflows/script-to-video`.
   - **Body `ScriptToVideoRequest`** (`videogen/models/script_to_video_request.py`) — members I set:
     - `script: str` (**required**) — verbatim narration; used as-is, not rewritten. ← title + 1st intro sentence, ≤30 words.
     - `visual_style: WorkflowVisualStyle` (**required**, wire `visualStyle`) → `WorkflowVisualStyle(type_=WorkflowVisualStyleType.STOCK)` (wire `type`). STOCK = stock footage/images (the required cheap shape). NOT AI_IMAGE.
     - `aspect_ratio: Optional[AspectRatio]` (wire `aspectRatio`) → `AspectRatio(width=16, height=9)`. (16:9 as width:height pair, not pixels.)
     - **Omit everything else** → default voiceover (no avatar: omit `actor_entity_id`), no `remix_actions` (no captions/animate/transitions/logo), default voice, `is_output_temporary` defaults false (VideoGen retains output).
   - **Returns `StartWorkflowRunResponse`** (`.../start_workflow_run_response.py`): required members `workflow_run_id` (wire `workflowRunId`), `project_id` (`projectId`), `project_url` (`projectUrl`), `remix_action_ids` (`remixActionIds`). ← truncated body would fail to decode (ValidationError). Store `workflow_run_id`, `project_id`.
2. `client.workflows.get_workflow_run(workflow_run_id: str, *, request_options=None) -> WorkflowRun`
   - Route `GET /v1/workflows/runs/{workflowRunId}`. Positional `workflow_run_id`.
   - **Returns `WorkflowRun`** (`.../workflow_run.py`): `workflow_run_id`, `status: JobStatusOrStr`, `progress_percentage: float` (wire `progressPercentage`, 0–100; =100 when succeeded), `project_id`, `error: ApiErrorModel | None` (null unless failed). Poll until `status` terminal.
3. `client.projects.export_project(project_id: str, *, body: ExportProjectRequest | ...Dict | None = None, request_options=None) -> ExportProjectResponse`
   - Route `POST /v1/projects/{projectId}/export`. Positional `project_id`.
   - **Body `ExportProjectRequest`** (`.../export_project_request.py`): set `quality = ExportProjectQuality.HIGH`. Leave `watermark_mode`/`end_screen_mode` UNSET (default AUTO; NONE needs Pro and errors). **Called exactly once per project.**
   - **Returns `ExportProjectResponse`**: `export_id` (wire `exportId`, required). Store it.
4. `client.projects.get_project_export(project_id: str, export_id: str, *, request_options=None) -> ProjectExport`
   - Route `GET /v1/projects/{projectId}/exports/{exportId}`. Positional `project_id`, `export_id`.
   - **Returns `ProjectExport`** (`.../project_export.py`): `export_id`, `project_id`, `status: JobStatusOrStr`, `progress_percentage: float`, `download_url: str | None` (wire `downloadUrl`; null until succeeded; signed MP4 URL valid 7d), `download_url_expires_at: int | None`, `export_file_id: str | None`, `file: FileInfo | None`, `error: ApiErrorModel | None`. Poll until terminal; on succeeded use `download_url` to fetch bytes.

**Enums** (open `...OrStr`; `videogen/models/enums/`):
- `JobStatus`: `PENDING="pending"`, `RUNNING="running"`, `SUCCEEDED="succeeded"`, `FAILED="failed"`, `CANCELLED="cancelled"`. In-progress = pending/running; terminal = succeeded/failed/cancelled.
- `ExportProjectQuality`: `STANDARD`, `HIGH`, `FULL_HIGH`, `ULTRA_HIGH` — "vertical resolution tier". Standard HD ladder → **HIGH = 720p** (HD); FULL_HIGH = 1080p (Full HD); ULTRA_HIGH = 4K (Ultra HD). Use **HIGH** (720p). Never ULTRA_HIGH (4K).
- `WorkflowVisualStyleType`: `STOCK="STOCK"`, `AI_IMAGE="AI_IMAGE"`. Use **STOCK**.

**Provider error model** `ApiErrorModel` (`.../api_error_model.py`): `message: str` (required), `code: OptionalNullable[str]`, `internal_error_code`, `requirement`. Branch on `code`, display `message`.

**Optionality**: `Optional[T]` here = `T | UNSET` (from `videogen.core`), **not** `typing.Optional`; default `UNSET`; never pass `None` to it. `OptionalNullable[T]` may be null. Build request models via the `...Dict` companions where mypy plain would flag member-name call-args (see REQUIRED READING → python-models).

**Errors** (`videogen.core`): single `ApiError` (`.error: RawError`, `.status_code`, `.response`). **Decode failure → `pydantic.ValidationError`/`ValueError`, NOT ApiError**, bypassing both response modes. httpx transport exceptions (`httpx.HTTPError`) arrive **unwrapped**. **SDK does NO retries** — polling/backoff is mine to build.

**Response mode**: use the **parsed** (raising) call for every op — I inspect decoded payloads, never a bare status code. No `with_raw_response` needed.

### Integration exceptions (typed, surface provider failures) — `bakerydemo/video/exceptions.py`
- `VideoGenError` (base) → `VideoGenConfigError` (missing key), `VideoGenAPIError` (wraps `ApiError`; carries status_code + provider message/code from `RawError.json()`), `VideoGenTransportError` (wraps `httpx.HTTPError`), `VideoGenResponseError` (wraps decode `ValidationError`/`ValueError`), `VideoGenProductionError` (workflow/export reached failed/cancelled; carries provider `ApiErrorModel.message`), `VideoGenTimeoutError` (polling exceeded budget).

---

## Architecture — new app `bakerydemo/video/`

- `exceptions.py` — typed exception hierarchy above.
- `narration.py` — `build_narration_script(page) -> str`: `f"{title}. {first_sentence(introduction)}"`, collapse whitespace, hard-cap 30 words. Body intentionally excluded (cost ceiling). Uses only the article's own text.
- `models.py` — `ArticleVideo`:
  - `job_id: UUIDField(unique, default uuid4)` = `videoJobId`.
  - `page = OneToOneField("wagtailcore.Page", on_delete=CASCADE, related_name="article_video")` → one video per article (idempotency) + deleted with the article (durability requirement).
  - `status` TextChoices `PENDING/PROCESSING/READY/FAILED`; `progress_percentage` PositiveSmallInteger; `script` TextField; `error` TextField(blank); `video_file = FileField(upload_to="article_videos/", blank, null)`.
  - provider bookkeeping: `provider_project_id`, `provider_workflow_run_id`, `provider_export_id` (CharField blank).
  - `created_at`/`updated_at`. Helper `download_available` etc.
- `provider.py` — `VideoGenGateway`: builds sync `VideogenClient` from settings (raises `VideoGenConfigError` if key blank); context-managed; methods `start_script_to_video(script)`, `get_workflow_run(id)`, `export_project(project_id)`, `get_project_export(project_id, export_id)`, `download(url)->bytes`. One private `_translate` wraps `ApiError`/`httpx.HTTPError`/`ValidationError|ValueError` → typed exceptions. `QUALITY_720P = ExportProjectQuality.HIGH`, `ASPECT_16_9`, `VISUAL_STYLE_STOCK` constants.
- `worker.py` — `produce_video(job_id)`: state machine PENDING→PROCESSING→(workflow poll)→export→(export poll)→download→READY, updating `progress_percentage` (workflow 0–80, export 80–95, download 95, done 100). Typed exception → FAILED + `error`. `start_in_background(job)` spawns a daemon `threading.Thread`. Polling: fixed interval (5s) with a max wall-clock budget (~15 min) → `VideoGenTimeoutError`. Guards so start/export happen at most once (resume-safe, never double-bills).
- `api.py` — `Router` (`auth=BearerTokenAuth()`); schemas with exact camelCase keys:
  - `POST /{page_id}/video/` → `@require_any_permission(Page,("publish",))` + per-page `can_publish()`. Resolve live `BlogPage` (else 404). `get_or_create(ArticleVideo, page=...)`; if created, build script + spawn thread. Return **202** `{videoJobId,status,progressPercentage,downloadUrl,error}`; if it already exists, return it unchanged (idempotent, no new thread).
  - `GET /{page_id}/video/{job_id}/` → same gate. Return `{videoJobId,status,progressPercentage,downloadUrl,error}`; `downloadUrl` = absolute URL to the download route, non-null only when READY.
  - `GET /{page_id}/video/{job_id}/download/` → same gate. `FileResponse` streaming the stored MP4 (durable, VideoGen-independent).
- `apps.py` — `VideoConfig.ready()` registers the router on the v3 `api` singleton (`api.add_router("/pages/", router)`) with an idempotency guard, before `api.urls` is accessed.
- `settings/base.py` — add `VIDEOGEN_API_KEY` / `VIDEOGEN_BASE_URL` from env; add `"bakerydemo.video"` to `INSTALLED_APPS`.
- `migrations/0001_initial.py`.
- `tests/` — permission matrix + state machine, using a **faked transport** (`custom_http_client`) / mocked gateway; no real VideoGen calls.

### Idempotency & "exactly one video"
OneToOne(page) + `get_or_create` ⇒ one `ArticleVideo` per article; thread spawned only on create; export called once. A 2nd POST returns the existing job (no workflow, no export, no bill). For the whole run I create exactly one job (one article) → one workflow → one export → one MP4.

### Download durability
On export success, the worker downloads the signed MP4 **once** into Django storage (`video_file`) and serves it from my own `.../download/` route. Durable for the article's lifetime without depending on VideoGen's 7-day signed URL and without extra provider calls.

---

## Verification plan (ONE real video only)

1. Setup: venv, `migrate`, `load_initial_data`. Find "Tracking Wild Yeast" page_id + confirm live BlogPage.
2. **Refusals (free, rejected before any provider call)** against that page: no token→401, bad token→401, `german` (revoked)→401, `inactive` (user inactive)→401, `editor` (no publish)→403.
3. **The one video**: POST with `admin` token → 202 + `videoJobId`. Poll GET until `status=ready` (watch `progressPercentage`). GET download route → save MP4; verify it's a real MP4 (~10–15s).
4. **Idempotency + moderator admit (no 2nd video)**: POST again with `moderator` token to same page → returns the SAME `videoJobId`, no new workflow/export.
5. If the one attempt fails: read provider `error` (from GET), diagnose from it. Do **not** produce another.
6. Automated tests (faked transport) for the full matrix + state machine.

## REQUIRED READING (load before implementing — Step 1c)
- `python-error-handling` — **MUST load** (error boundary / typed-exception translation; ApiError vs decode-failure vs httpx). ALWAYS.
- `python-client-initialization` — **MUST load** before constructing `VideogenClient` (keyword-only, close(), lifetime).
- `python-calling-endpoints` — **MUST load** before first `client.workflows/projects.*` call (positional/keyword split, parsed vs raw).
- `python-models` — **MUST load** before building `ScriptToVideoRequest`/`ExportProjectRequest` (Optional=UNSET, `...Dict` companion for mypy call-arg, wire aliases, open enums).
- `python-authentication` — **MUST load** before setting `bearer_auth` (optional-cred trap; load secret from env).
- `python-configuration-resilience` — **MUST load** before setting base_url/timeout (no retries → I build polling; timeout semantics).
- `python-testing` — **MUST load** before the first test / throwaway verification script (fake the transport protocol; assert built request; cover error+decode paths).

## Assumptions & Blockers
- **No blockers.** SDK covers every needed capability (script→video with STOCK+voiceover, 720p export, status/download polling). No gap to report.
- Assumption (minor, decided): `ExportProjectQuality.HIGH` = 720p per the HD-ladder naming; `WorkflowVisualStyleType.STOCK` = stock footage. Both grounded in the plugin's own enum docstrings.
- Assumption (minor, decided): background `threading.Thread` is the no-broker async mechanism; a crash mid-run leaves the job PROCESSING (not auto-retried, honoring the one-video limit).
