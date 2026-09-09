# VideoGen integration plan — "Article → shareable video" for Wagtail Bakery

Status: authoritative implementation contract. Facts in the **Contract sheet** come from the
`videogen` plugin SDK map + installed package (v1.0.0) and are not to be re-derived from memory.

---

## 1. Goal & flow

Add an **additive** capability to the existing Django-Ninja v3-preview API
(`wagtail.api.v3`, mounted at `/api/v3-preview/`) that turns one published blog article into a
short narrated stock-footage video and serves the finished MP4 through the site.

Endpoints (page-scoped, mirroring the pages router conventions):

- `POST /api/v3-preview/pages/{page_id}/video/` → starts production, returns immediately with
  top-level `videoJobId`. Restricted to callers permitted to **publish** that page.
- `GET  /api/v3-preview/pages/{page_id}/video/{videoJobId}/` → top-level `status`,
  `progressPercentage`, `downloadUrl`, `error`.
- `GET  /api/v3-preview/pages/{page_id}/video/{videoJobId}/download/` → streams the stored MP4
  (the target of `downloadUrl`).

Production pipeline (all VideoGen calls via the SDK):
`script_to_video` (STOCK visuals, voiceover, no avatar) → poll `get_workflow_run` until terminal →
`export_project` (quality ≤1080p) → poll `get_project_export` until terminal → download the signed
MP4 → store it in Django storage on the `ArticleVideo` row → mark `ready`.

Narration script = the article's **own text only**: `title` + `introduction` + the first three
paragraphs of `body`. No call to `text.generate_text` / assistant / any rewriting service.
`script_to_video`'s `script` is documented "used verbatim … not rewritten or expanded", which is
exactly the requirement.

---

## 2. Architecture & file layout

New isolated app `bakerydemo/videos/` (nothing existing is modified beyond settings + INSTALLED_APPS
+ one-line dev media note):

| File | Responsibility |
| --- | --- |
| `apps.py` | `VideosConfig(AppConfig)`; `ready()` imports `.api` to register the Ninja router on the shared `api` instance **before** `bakerydemo/urls.py` accesses `api.urls`. |
| `models.py` | `ArticleVideo` (OneToOne→`wagtailcore.Page`, CASCADE), status/progress/error, VideoGen ids, `FileField` for the MP4. |
| `constants.py` | `VideoStatus` values (`pending`,`processing`,`ready`,`failed`). |
| `exceptions.py` | Typed exception hierarchy for provider failures. |
| `client.py` | Long-lived module-scoped `VideogenClient` factory from Django settings; `atexit` close. |
| `narration.py` | Build the narration script from a `BlogPage` (title + intro + first 3 body paragraphs). |
| `service.py` | `produce_video(article_video)` orchestration; each SDK call wrapped in the error-translation boundary. |
| `runner.py` | `start_in_background(article_video_pk)` — daemon thread driving `service.produce_video`. |
| `schemas.py` | Ninja response `Schema`s (`StartVideoResponse`, `VideoStatusResponse`). |
| `api.py` | Ninja `Router`; POST/GET/download routes; publish-permission gating; registration. |
| `migrations/0001_initial.py` | model migration. |
| `tests/` | unit tests (stub transport), permission tests (seeded tokens), narration tests. |

Settings (in `bakerydemo/settings/base.py`, following the existing `os.environ.get` convention used
for `ADMIN_PASSWORD`/CSP — **names only, never values**):

- `VIDEOGEN_API_KEY = os.environ.get("VIDEOGEN_API_KEY")`
- `VIDEOGEN_BASE_URL = os.environ.get("VIDEOGEN_BASE_URL")` (None ⇒ SDK default `https://api.videogen.io`)
- `VIDEOGEN_EXPORT_QUALITY = os.environ.get("VIDEOGEN_EXPORT_QUALITY", "FULL_HIGH")`
- `VIDEOGEN_TIMEOUT`, `VIDEOGEN_POLL_INTERVAL`, `VIDEOGEN_MAX_WAIT_SECONDS` (numeric, with defaults)

---

## 3. Contract sheet (SDK v1.0.0 — authoritative)

**Sync or async:** Django under WSGI ⇒ **sync `VideogenClient`** (`from videogen import VideogenClient`).
Never the async client. Long-lived, module-scoped; `close()` at process exit (`atexit`). The sync
client is thread-safe to share across threads (token cache lock-guarded, pool thread-safe) — our
background thread reuses the one module client.

**Construction (keyword-only):** `VideogenClient(bearer_auth=<str>, base_url=<str|None>, timeout=<float>)`.
- `bearer_auth` is a **plain string** (not a model). Omitting it ⇒ unauthenticated ⇒ 401. We set it
  from `settings.VIDEOGEN_API_KEY` and fail fast (typed config error) if missing.
- `base_url=None` ⇒ default `https://api.videogen.io`; if `VIDEOGEN_BASE_URL` set, pass it verbatim.
- Auth scheme is Bearer only — **no OAuth2**, so there is **no `OAuthProviderError` / token-fetch
  failure mode**. Auth failures arrive as an `ApiError` with `RawError` (status 401/403).

**Response mode:** default (parsed, raises `ApiError`) everywhere, wrapped by our boundary. We use
`with_raw_response` only in the read-only credential smoke, not in production code.

**Operations in scope** (all `bearer_auth`, all **Case B** → `e.error` is always `RawError`; every
signature ends with keyword-only `request_options`):

| Operation | Signature (positional / keyword) | Returns (parsed) | Notes |
| --- | --- | --- | --- |
| `client.workflows.script_to_video` | `*, body: ScriptToVideoRequest \| dict \| None = None` | `StartWorkflowRunResponse` | POST `/v1/workflows/script-to-video` |
| `client.workflows.get_workflow_run` | `workflow_run_id: str` (positional) | `WorkflowRun` | GET `/v1/workflows/runs/{workflowRunId}` |
| `client.projects.export_project` | `project_id: str` (positional), `*, body: ExportProjectRequest \| dict \| None = None` | `ExportProjectResponse` | POST `/v1/projects/{projectId}/export` |
| `client.projects.get_project_export` | `project_id: str, export_id: str` (positional) | `ProjectExport` | GET `/v1/projects/{projectId}/exports/{exportId}` |
| `client.account.get_me` | `*` | `MeResponse` | read-only smoke only (verified 200) |

**Request models** (members we set; wire alias in parens; all others left `UNSET`/omitted):

`ScriptToVideoRequest`:
- `script: str` — **required**. The verbatim narration.
- `visual_style: WorkflowVisualStyle` — **required** (wire `visualStyle`). We set
  `WorkflowVisualStyle(type_=WorkflowVisualStyleType.STOCK)` (wire `type="STOCK"`) → stock footage,
  no AI-generated imagery.
- `aspect_ratio` (wire `aspectRatio`) = `AspectRatio(width=16, height=9)` — landscape share format.
  NB: aspect ratio is a **ratio, not pixels**; resolution is set at export.
- `actor_entity_id` — **left UNSET/omitted** ⇒ "voiceover without an avatar" (voice only, no presenter).
- Everything else (`voice_id`, `quality`, `language`, `remix_actions`, `is_output_temporary`, …) omitted → account/workflow defaults.

`WorkflowVisualStyle`: required `type_` (wire `type`), rest optional. `type_` accepts
`WorkflowVisualStyleTypeOrStr`; enum `WorkflowVisualStyleType` = {`STOCK`, `AI_IMAGE`}. Use `STOCK`.

`ExportProjectRequest`:
- `quality` = `ExportProjectQuality` member, from `settings.VIDEOGEN_EXPORT_QUALITY`.
  Enum `ExportProjectQuality` = {`STANDARD`, `HIGH`, `FULL_HIGH`, `ULTRA_HIGH`} — docstring
  "Vertical resolution tier". **Decision (§5):** default `FULL_HIGH` = Full HD = 1080p (satisfies
  "1080p or below"); `ULTRA_HIGH` is the only tier above 1080p and is not used.
- `watermark_mode` / `end_screen_mode` — **left default (AUTO)**. Setting `NONE` requires a Pro plan
  and errors otherwise; the demo account may be free-tier, so we do not touch them.

**Response models** (members we read — assert non-`UNSET`/non-null on the ones we depend on):

- `StartWorkflowRunResponse`: `workflow_run_id` (wire `workflowRunId`, **required**),
  `project_id` (wire `projectId`, **required**), `project_url`, `remix_action_ids`. We store
  `workflow_run_id` + `project_id`; guard both are present (truncated 2xx would leave them UNSET).
- `WorkflowRun`: `status: JobStatusOrStr` (**required**), `progress_percentage`
  (wire `progressPercentage`, **required**, float 0-100), `project_id`, `error: ApiErrorModel | None`
  (always present, null unless failed).
- `ExportProjectResponse`: `export_id` (wire `exportId`, **required**).
- `ProjectExport`: `status: JobStatusOrStr`, `progress_percentage`, `download_url`
  (wire `downloadUrl`, `str | None` — null until `succeeded`), `export_file_id`, `error`.

**Enums are open** (`…OrStr`) — reading `status` may yield a `JobStatus` member **or** a plain `str`.
`JobStatus` = {`PENDING`="pending", `RUNNING`="running", `SUCCEEDED`="succeeded", `FAILED`="failed",
`CANCELLED`="cancelled"}. `pending`/`running` in-progress; `succeeded`/`failed`/`cancelled` terminal.
Compare by wire string (`str(status) == "succeeded"`) so an unknown future member is handled, not crashed.
`ApiErrorModel`: `message: str` (**required**), `code`, `requirement`, `internal_error_code` (all optional/nullable).

**Error handling (the boundary in `client.py`/`service.py`):** no OAuth ⇒ ladder is:
1. `except ApiError as e:` → `e.error` is `RawError` (Case B). Map to `VideoGenApiError(status_code, message)`;
   message from `e.error.text()` (never `.json()` blindly — `RawError.json()` raises `ValueError` on non-JSON).
   Preserve `e.status_code`.
2. `except (pydantic.ValidationError, ValueError) as e:` → decode failure, bypasses both response modes;
   map to `VideoGenUnreadableResponseError` ("outcome unknown"). (ValidationError is a ValueError subclass;
   order the ValidationError arm — or a single ValueError arm — carefully; catch ValidationError before generic ValueError if split.)
3. `except httpx.HTTPError as e:` → transport failure (unwrapped by SDK); map to `VideoGenUnreachableError`.
- Truncated-but-valid 2xx: guard required members explicitly (e.g. `workflow_run_id`), raise
  `VideoGenUnreadableResponseError` if UNSET/empty.
- A workflow/export that reaches terminal `failed`/`cancelled` is **not** an exception from the SDK —
  it's a normal response; we raise `VideoGenProductionError(message from WorkflowRun.error/ProjectExport.error)`.

**No retries in the SDK.** Our polling loop is the only "retry"; we bound each call with
`request_options={"timeout": ...}` and bound the whole pipeline with `VIDEOGEN_MAX_WAIT_SECONDS`.
We deliberately do not add automatic call-level retries (kept simple; failures surface as typed errors).

**Base URL selected:** default `https://api.videogen.io` (VIDEOGEN_BASE_URL unset here — verified).

---

## 4. Design decisions

- **Public `videoJobId`:** our own `uuid4` (`ArticleVideo.job_id`), generated at POST time and returned
  immediately (before the workflow run id exists). Decouples our contract from VideoGen ids; the GET
  path validates `job_id` belongs to `page_id`.
- **Idempotency / no double-bill:** `ArticleVideo` is OneToOne on `Page`. POST uses
  `get_or_create(page=...)` inside `transaction.atomic()` + `select_for_update`. If a row exists and is
  not `failed`, return the existing `job_id` (no new thread, no new VideoGen call). If it `failed`, reset
  the same row and re-run (retry). One row per page ⇒ at most one billable production per article at a time.
- **Public `status` vocabulary:** `pending` (accepted, not yet started), `processing` (workflow or export
  running / downloading), `ready`, `failed`. `pending`+`processing` = "still being produced".
- **`progressPercentage` (0-100 composite):** pending 0; workflow phase maps VideoGen progress into 0-85;
  export phase 85-98; downloading 98; ready 100. Honest, monotonic.
- **`downloadUrl`:** absolute URL to our download route (via `reverse("wagtailapi_v3:video_download", …)`
  + `request.build_absolute_uri`), non-null only when `ready`. The site stores the MP4, so it stays
  downloadable for the life of the article (CASCADE removes it with the page).
- **`error`:** human-readable string, non-null only when `failed`.
- **Background execution:** a `threading.Thread(daemon=True)` started at POST — satisfies "does not have
  to finish before the call returns" with **no new infra** (no Celery/Redis/broker, as mandated). The
  thread reuses the module-scoped client and updates the row via the ORM. Trade-off: a process restart
  mid-run strands a `processing` row; acceptable for this demo, and the production upgrade (a durable
  queue) is noted but explicitly out of scope per the "no task queue" constraint.
- **Permission gating:** `@require_any_permission(Page, ("publish",))` + explicit
  `page.permissions_for_user(request.user).can_publish()` on POST (mirrors the pages `publish` action).
  GET status + download gated the same way (uniform, secure). Auth: `BearerTokenAuth()` (identical to the
  pages router). Only `BlogPage` instances are accepted (400/404 otherwise).
- **Storage:** `FileField(upload_to="article_videos/")` on default storage (dev media dir). Download route
  streams via `FileResponse` as `video/mp4` attachment.

---

## 5. Assumptions & Blockers

- **[Assumption — decided, not a blocker] Export quality tier ↔ resolution.** The plugin names the tiers
  `STANDARD/HIGH/FULL_HIGH/ULTRA_HIGH` ("Vertical resolution tier") but does **not** annotate pixel
  heights anywhere in the SDK, map, api-reference or raw reference (searched exhaustively). The member
  names encode the universal SD / HD / **Full HD (1080p)** / **Ultra HD (4K)** ladder, so `FULL_HIGH`
  is 1080p and satisfies "1080p or below", while `ULTRA_HIGH` is the sole tier that would exceed it.
  Decision: default `VIDEOGEN_EXPORT_QUALITY=FULL_HIGH`, configurable. This is a naming-based judgment
  call the task explicitly leaves to us — **not** a plugin gap.
- **No genuine plugin gaps found.** Every required capability (script→video with stock footage + voiceover,
  poll, export ≤1080p, signed MP4 download) is exposed by the plugin. Nothing to report under the gap rule.
- Spend guard: **at most one real video** produced during self-verification (budget 2, keeping 1 buffer).
  All other checks use the stub transport / read-only ops.

---

## 6. REQUIRED READING (load before implementing — done)

- `python-error-handling` — **MUST load** (error boundary). Loaded. Case B/RawError, decode & transport paths.
- `python-client-initialization` — **MUST load** (before constructing the client). Loaded. Sync, long-lived, close().
- `python-authentication` — **MUST load** (bearer string; no OAuth). Loaded.
- `python-calling-endpoints` — **MUST load** (positional/keyword split, body union, response modes). Loaded.
- `python-models` — **MUST load** (UNSET vs None, open enums, wire aliases, `to_dict`). Loaded.
- `python-configuration-resilience` — **MUST load** (no retries, timeout meaning, base_url). Loaded.
- `python-testing` — **MUST load** (stub transport seam) before the first test / verification script. Loaded.
