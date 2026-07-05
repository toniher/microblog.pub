# CLAUDE.md

Guidance for working in this repository.

## What this is

**microblog.pub** is a self-hosted, **single-user**, ActivityPub-powered microblog
(AGPL-3.0). One instance = one actor. It federates with the fediverse (Mastodon,
Pleroma, PeerTube, PixelFed…) and doubles as an IndieWeb citizen.

### Main features

- **ActivityPub server-to-server**: full federation — follow/followers, inbox/outbox,
  notes, articles, boosts, likes, polls, media. HTTP Signatures + Linked-Data
  Signatures for authenticity.
- **Microblog UI**: author notes in Markdown (code highlighting), plus a dedicated
  articles/blog section. Mostly server-rendered HTML/CSS with minimal JS.
- **IndieWeb**: IndieAuth (OAuth2), Micropub, sends/receives Webmentions,
  Microformats markup, RSS/Atom/JSON feeds.
- **Privacy-aware**: EXIF stripped from uploads, all remote media proxied,
  outbox access controlled via HTTP signature.
- **Lightweight & backup-friendly**: SQLite; everything mutable lives in `data/`
  (config, uploads, secrets, the DB).

## Architecture

FastAPI ASGI app on async SQLAlchemy (1.4) + SQLite (via `aiosqlite`), Jinja2
templates, Alembic migrations, background workers for federation traffic.

- `app/` — the web application
  - `main.py` — the FastAPI `app`, public + AP HTTP routes, media proxy
  - `admin.py` — authenticated admin UI (`/admin`, mounted via `include_router`)
  - `config.py` — loads `data/profile.toml` / env; exposes `CONFIG`, `ME`-related settings
  - `database.py` — sync `engine` + async `async_engine`/`async_session`, `Base`
  - `templates.py` — `render_template()` helper + HTML sanitization allow-lists
  - `httpsig.py`, `ldsig.py`, `key.py` — signature signing/verification, keypair
  - `indieauth.py`, `micropub.py`, `webmentions.py`, `webfinger.py` — IndieWeb + discovery
  - `source.py` — Markdown→HTML, hashtag/mention extraction
  - `uploads.py`, `media.py` — attachment storage, thumbnails, blurhash, proxy
  - `utils/` — datetime, url, highlight, microformats, opengraph, facepile, stats…
- `activitypub/` — the AP domain library (being **modularized** out of `app/`, see below)
  - `activitypub.py` — AP constants, `RawObject`, `ME` actor object, `fetch()`/collection parsing
  - `actor.py`, `ap_object.py` — actor + object models (pydantic v2 + SQLAlchemy)
  - `boxes.py` — core inbox/outbox processing logic (large; actively refactored)
  - `incoming_activities.py` / `outgoing_activities.py` — federation worker queues
  - `models.py` — SQLAlchemy ORM models (Outbox/Inbox/Follower/Following/Upload…)
  - `tests/` — AP-focused tests + `factories.py` (factory-boy)
- `alembic/` — schema migrations (`alembic/versions/`, excluded from black/mypy)
- `tests/` — app/integration tests; `tests/conftest.py` provides the DB + TestClient fixtures
- `data/` — runtime state (gitignored); `tests.toml` is the test config
- `templates/`, `static/`, `scss/` — server-rendered UI assets
- `tasks.py` — `invoke` task runner (lint, tests, migrations, scss, workers…)

### Conventions

- Python **3.12** (min `^3.10`), Poetry for deps, **in-project `.venv/`**
  (`poetry.toml`), `package-mode = false`.
- Lint stack = `black` (24.x), `isort --sl`, `flake8` (max line 120, ignore E203),
  `mypy` (with `sqlalchemy.ext.mypy.plugin` + `pydantic.mypy`).
- pydantic **v2** idioms (`model_validate`, `model_dump`, `ConfigDict`).
- SQLAlchemy queries use the 1.4 async style (`select()` + `session.scalar/execute`).

## Common commands

Run tools from the venv (or `poetry run …`, which does the same by activating it):

```bash
# tests (needs the venv on PATH so `pytest` resolves inside invoke)
MICROBLOGPUB_CONFIG_FILE=tests.toml .venv/bin/python -m pytest -q
poetry run inv tests            # equivalent, as CI runs it

# lint / autoformat
poetry run inv lint             # black --check, isort --check, flake8, mypy
poetry run inv autoformat       # black . && isort --sl .

# DB migrations
poetry run inv generate-db-migration "message"
poetry run inv migrate-db

# run locally
poetry run inv uvicorn                        # web server
poetry run inv process-incoming-activities    # inbox worker
poetry run inv process-outgoing-activities    # outbox worker
poetry run inv compile-scss

# docker
make build        # build image (python:3.12-slim base)
make config       # configuration wizard
```

Testing notes:
- Test suite runs against an in-memory SQLite DB; ~69 tests, ~30s.
- `activitypub/tests/test_actor.py` does real-network retries (~20s) — when running
  the whole suite ad hoc, pass `--timeout=60` (pytest-timeout) so a slow/hung test
  is killed instead of stalling the run.

## Current work in progress

The `dev_ap_module` branch (where this fork's **modularization** of all ActivityPub
logic out of `app/main.py` into the standalone `activitypub/` package was developed)
has already been merged and is now part of `main` — that work is done, not in
progress. `boxes.py` is the main landing zone from that effort and may still see
churn as it gets exercised more (its formatting/lint was cleaned up as part of the
Python 3.12 work below).

## Python 3.12 modernization (2026-07)

The stack was upgraded from Python 3.10/3.11 to **Python 3.12** with current
dependencies. Key points for anyone touching related code:

**Dependency jumps** (in `pyproject.toml`): FastAPI `0.110→0.139`, Starlette `→1.3.1`,
httpx `0.25→0.28`, pydantic `1→2.9`, uvicorn/pytest/flake8/respx/mypy/isort bumped,
Pillow up, `invoke 1.x→2.x`, `bs4` shim replaced with `beautifulsoup4`. SQLAlchemy
intentionally stays on `1.4.54` (3.12-compatible); a 2.0 migration is a separate effort.

**Breaking-change fixes made during the upgrade** — watch for these patterns:
- **Starlette 1.x `Jinja2Templates`** no longer accepts Jinja env options as kwargs.
  Build with `directory=` then set `_templates.env.trim_blocks/lstrip_blocks`.
- **Starlette 1.x `TemplateResponse`** signature is `(request, name, context, …)`;
  the old `(name, context)` form silently misbinds. All rendering goes through
  `app/templates.py:render_template`.
- **httpx 0.28** removed `AsyncClient(app=…)`; use
  `httpx.AsyncClient(transport=httpx.ASGITransport(app=…))`.
- **FastAPI 0.139 lazy routing**: `app.routes` contains `_IncludedRouter` wrappers
  (no `.path`). Real sub-routes live under `route.original_router.routes` with prefix
  `route.include_context.prefix` — see the walker in `tests/test_admin.py`.
- **pydantic v2**: use `model_validate` / `model_dump` / `model_config = ConfigDict(...)`.
  Module-level dicts assembled then mutated (e.g. `ap.ME`) may need an explicit
  `: RawObject` annotation to satisfy mypy.
- **Pillow inline types**: `ImageOps.exif_transpose()` is typed `Image | None`; assert
  non-None (it only returns None for None input).
- **invoke 2.x**: the old `inspect.getargspec` monkeypatch in `tasks.py` is gone
  (it existed only for invoke 1.x on Python ≥3.11).

**Lint cleanup** (so the CI `inv lint` job is green): applied `black`/`isort`
project-wide, and set `extend-exclude = .venv` in `.flake8` (poetry's in-project
`.venv` was being scanned). Removed unused imports flagged by flake8 — mostly dead
`from app import models` leftovers from the AP refactor; safe because those modules
query only `activitypub.models` and `app.models` is registered via `app/main.py`.

**Verified**: `inv lint` (black/isort/flake8/mypy) and `inv tests` (69 passed) both
green, and the Docker image builds on `python:3.12-slim`.

**Note**: the test suite shares one in-memory SQLite DB (`cache=shared`) across the
sync `db` and async `async_db_session` fixtures, which can rarely flake under timing
variance — pre-existing, worth hardening with per-test DB isolation.

**CI hang after "N passed"**: `aiosqlite` backs each pooled connection with a
`threading.Thread` that blocks on an internal queue until `.close()` sends it a stop
sentinel; in this codebase that thread comes up non-daemon. The module-level
`async_engine` in `app/database.py` is shared for the whole pytest session and was
never disposed, so a live connection's worker thread kept the interpreter alive after
pytest printed its summary — invisible locally (the terminal just returns), but in
GitHub Actions it silently ate the full `timeout-minutes: 10` before being killed as
an orphan process. Fixed by disposing the engine in a `pytest_sessionfinish` hook in
`tests/conftest.py` (`asyncio.run(async_engine.dispose())`). Verified by dumping live
thread stacks after `pytest.main()` returned — `Thread-1 (_connection_worker_thread)`,
`daemon=False`, blocked in `tx.get()` — and confirming `poetry run inv tests` exits
with code 0 instead of needing to be killed.
