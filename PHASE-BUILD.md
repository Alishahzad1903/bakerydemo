# Task — Turn a published article into a shareable video on the Wagtail Bakery site

Give the Wagtail Bakery site a way to turn one of its published blog articles into a short
narrated video its marketing team can share, without anyone editing video by hand. This adds
that capability using **VideoGen** as the way of producing the video. It is an **additive**
capability — it does not replace or alter the existing pages, blog, images or admin flows.

You own the design and every implementation decision — architecture, file layout, build order,
patterns. Just honor the mandates and the details below.

---

## What to build

### Flow — Produce a video for one article

An editor picks a published blog article and asks the site for a video of it. The site turns the
article's own words into spoken narration, has the video produced, and makes the finished MP4
retrievable through the site.

- `POST /api/v3-preview/pages/{page_id}/video/` — start producing a video for that article. This
  does not have to finish before the call returns. The response identifies the work as a
  top-level field: `videoJobId`.
- `GET /api/v3-preview/pages/{page_id}/video/{videoJobId}/` — the state and outcome of one such
  request. The response must let a caller tell, without guessing, whether the video is still
  being produced, is ready to download, or failed. When it is ready it must carry where to
  download the finished MP4; when it failed it must carry what went wrong. Report these as
  top-level fields: `status`, `progressPercentage`, `downloadUrl`, `error`.
- The narration is built from the article's **own text** — its title, its introduction, and the
  text of its body — and from nothing else. Do not send the article to any other service to be
  rewritten, summarised or expanded first.
- Once a video is ready it must stay downloadable through that endpoint for as long as the
  article exists.
- Asking for a video twice in a row for the same article must not produce two videos and must
  not be billed twice.

### Where it goes

Expose this on the site's **existing HTTP API** — the one already mounted at `/api/v3-preview/`
— following that API's own conventions, and authenticate callers exactly the way that API
already authenticates them. Producing a video is an editorial action on a page: restrict it to
callers permitted to publish that page. No storefront or admin UI is required.

---

## VideoGen tooling — non-negotiable

- Use the **videogen** plugin (from the **apimatic** marketplace)
  for **every** VideoGen interaction. It is your sole reference for how to talk to VideoGen.
- **Do not** web-search or rely on general/external knowledge for VideoGen API details.
- If the plugin does not expose a capability you need, **STOP and report the gap** — do not
  invent or work around it.
---

## Spend limits — this account is really billed

Every video this task produces is produced on a real, paid account, so the shape of what you
ask for is fixed and is not a design decision:

- Visuals come from **stock footage**, never from generated imagery, and the narration is a
  **voice only** — no presenter or avatar on screen.
- Export the MP4 at **1080p or below**.
- Keep the narration short: the article's title, its introduction and the **first three
  paragraphs** of its body are enough.
- Produce **at most two** real videos in the whole run, including everything you produce while
  verifying. Check everything else without producing a video.

---

## Sandbox entities & test fixtures

The site ships its own demo content and loads it with `python manage.py load_initial_data`.
That content includes six published blog articles — `Tracking Wild Yeast` is one of them — and
API tokens for several users whose permissions differ. Verify the whole flow against a real
article from that content: start a video for it, follow the request through to a finished MP4,
and download that MP4. Use the seeded tokens to check the endpoints admit and refuse the callers
they should.

---

## Credentials

- The VideoGen API key arrives as an env var: `VIDEOGEN_API_KEY`.
- Read it from the environment through the site's own settings, and hard-code no value — the
  same build has to run against a different VideoGen account than this one.
- `VIDEOGEN_BASE_URL` is an optional override: when it is set, use it verbatim as the API base
  address for every VideoGen call instead of the default one.

---

## Environment gotchas (this machine)

- **Python 3.12.** The site pins 3.12 (`.python-version`) and Django 6 will not run on an older
  one. Create a virtual environment and install `requirements/base.txt` into it; nothing here is
  installed globally for you.
- **SQLite, and it starts empty.** With no `DATABASE_URL` set the site uses a SQLite file beside
  the project. A fresh clone has no database and no content: run `python manage.py migrate` and
  then `python manage.py load_initial_data` before anything is reachable.
- **The API and the admin need no frontend build.** `npm ci` and the static asset pipeline are
  not needed to exercise the HTTP API. Do not run them.
- **Settings module.** `manage.py` defaults to `bakerydemo.settings.dev`, which is the one to
  use. It has `DEBUG` on and accepts any host.
- **Ports:** when you run the server, bind only to your assigned block
  (`APP_PORT_BLOCK_BASE` … `+APP_PORT_BLOCK_SIZE-1`). Stop your previous instance before
  starting another — no stray processes on stale builds.

There is otherwise no infra dependency beyond Python — no Docker, no PostgreSQL, no Redis, no
broker, no task queue. Don't introduce any.

---

## Rules of engagement

- We want a **production-grade** integration — you decide what production-grade looks like.
- When done, **self-verify** that the site runs and the flow actually works — start a video for
  a real seeded article, follow it to completion, and download the MP4 it produced. Then give me
  a concise, step-by-step guide to verify the working integration myself.

---

## Constraints

- **Secrets never enter the repository.** Read the API credentials from the environment
  variables above, at run time, and never write their **values** into any file inside this
  repository — not into a settings module, not into `.env`, not into a script, a test fixture,
  a comment, or a commit message. Referencing the variable **names** is fine, the values are
  not.
- **The integration must surface provider failures as typed exceptions.**
- **Report a gap only when it is genuinely a gap.** Stop and report when the source you were
  given does not cover a capability this integration requires. A design decision being hard,
  open-ended, or left to your judgment is **not** a gap — decide it and proceed.
- **You are running headless — there is no one to answer you.** Work until the integration
  is fully complete. Never hand back, never end with a question, and never defer remaining
  work to the user: decide and proceed.

