---
name: bakerydemo-article-videos
description: The bakerydemo.videos app adds VideoGen article-to-video endpoints on the v3 API
metadata:
  type: project
---

`bakerydemo.videos` is an additive app that turns a published blog article into a
short narrated MP4 via VideoGen, exposed on the existing v3 API
(`/api/v3-preview/`, Django Ninja, `BearerTokenAuth`):

- `POST /pages/{id}/video/` (start, idempotent per page → `videoJobId`), `GET .../{jobId}/` (status), `GET .../{jobId}/download/` (302 to signed MP4).
- Gated on per-page publish permission: `page.permissions_for_user(user).can_publish()` → `PermissionDenied` (v3 maps to 403, or 401 if the token didn't resolve).
- Narration = title + first sentence of introduction, ≤30 words, from the article's own text only. VideoGen params in [[videogen-cheap-video-params]].
- `VideoJob` model (FK to Page, cascade); partial unique constraint `~Q(status="failed")` enforces one active job per page. Production runs in a daemon thread (no task queue allowed). Provider failures surface as typed exceptions in `exceptions.py`.
- Credentials: settings `VIDEOGEN_API_KEY` / `VIDEOGEN_BASE_URL` read from env; never hard-coded.

**Why:** encodes the task's fixed "cheap shape" and spend limits so a produced
video's cost can't drift.

**How to apply:** run against seeded article page 62 ("Tracking Wild Yeast");
demo publisher token `admin`, non-publisher `editor` (403), revoked `german` (401).
