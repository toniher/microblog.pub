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
- **Mastodon client API**: a subset of the Mastodon client REST API (`app/mastodon/`)
  layered on the same ActivityPub data — OAuth2 app registration/login, timelines,
  statuses (read/write/interactions), notifications, conversations (DMs), social
  graph, search, media uploads. Lets existing Mastodon apps (Tusky, Fedilab…) act
  as another client for the same single actor. See `docs/mastodon_api.md` for the
  user-facing feature matrix.
- **Privacy-aware**: EXIF stripped from uploads, all remote media proxied,
  outbox access controlled via HTTP signature.
- **Lightweight & backup-friendly**: SQLite; everything mutable lives in `data/`
  (config, uploads, secrets, the DB).
- **Localizable interface**: gettext-based i18n. Both public and `/admin` pages
  negotiate the visitor's `Accept-Language` against the available locales, falling
  back to the instance's configured `language_code`. Bundled locales: `en`, `ca`,
  `es`, `fr`, `it`, `ro`. See `app/i18n.py` and "Translations / i18n" in
  `docs/developer_guide.md`.

## Architecture

FastAPI ASGI app on async SQLAlchemy (1.4) + SQLite (via `aiosqlite`), Jinja2
templates, Alembic migrations, background workers for federation traffic.

- `app/` — the web application
  - `main.py` — the FastAPI `app`, public + AP HTTP routes, media proxy
  - `admin.py` — authenticated admin UI (`/admin`, mounted via `include_router`)
  - `config.py` — loads `data/profile.toml` / env; exposes `CONFIG`, `ME`-related settings
  - `database.py` — sync `engine` + async `async_engine`/`async_session`, `Base`
  - `templates.py` — `render_template()` helper (incl. per-request locale/gettext
    wiring), HTML sanitization allow-lists, locale-aware date filters
  - `i18n.py` — gettext locale resolution/negotiation (`Accept-Language`, for both
    public and `/admin` pages, falling back to the instance `language_code`),
    catalog loading; catalogs live under `app/translations/<locale>/LC_MESSAGES/`
    (`.po` tracked, `.mo` gitignored, compiled via `inv compile-translations`)
  - `httpsig.py`, `ldsig.py`, `key.py` — signature signing/verification, keypair
  - `indieauth.py`, `micropub.py`, `webmentions.py`, `webfinger.py` — IndieWeb + discovery
  - `source.py` — Markdown→HTML, hashtag/mention extraction
  - `uploads.py`, `media.py` — attachment storage, thumbnails, blurhash, proxy
  - `utils/` — datetime, url, highlight, microformats, opengraph, facepile, stats…
  - `mastodon/` — Mastodon-compatible client REST API, mounted unconditionally in
    `main.py`: `router.py` (timelines/statuses/notifications/conversations/social
    graph/search/media), `oauth.py` (adapts `indieauth.py`'s OAuth2 server rather
    than forking one), `serializers.py`/`entities.py` (AP object → Mastodon JSON),
    `ids.py` (timestamp-prefixed numeric ids so Mastodon's id-ordering contract
    holds across the merged inbox/outbox), `pagination.py` (`Link` header +
    `max_id`/`since_id`/`min_id`), `scopes.py`, `errors.py`
- `activitypub/` — the AP domain library (being **modularized** out of `app/`, see below)
  - `activitypub.py` — AP constants, `RawObject`, `ME` actor object, `fetch()`/collection parsing
  - `actor.py`, `ap_object.py` — actor + object models (pydantic v2 + SQLAlchemy)
  - `boxes.py` — core inbox/outbox processing logic (large; actively refactored)
  - `incoming_activities.py` / `outgoing_activities.py` — federation worker queues
  - `models.py` — SQLAlchemy ORM models (Outbox/Inbox/Follower/Following/Upload…)
  - `tests/` — AP-focused tests + `factories.py` (factory-boy)
- `alembic/` — schema migrations (`alembic/versions/`, excluded from black/mypy)
- `tests/` — app/integration tests; `tests/conftest.py` provides the DB + TestClient
  fixtures; `tests/mastodon/` covers the Mastodon client API surface
- `data/` — runtime state (gitignored); `tests.toml` is the test config
- `templates/`, `static/`, `scss/` — server-rendered UI assets
- `tasks.py` — `invoke` task runner (lint, tests, migrations, scss, i18n, workers…)

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

# translations (i18n) — see "Translations / i18n" in docs/developer_guide.md
poetry run inv extract-messages               # regenerate app/translations/messages.pot
poetry run inv init-translation <locale>      # scaffold a new locale's .po
poetry run inv update-translations            # merge new msgids into existing .po files
poetry run inv compile-translations           # .po -> .mo (also runs as part of `inv update`)

# docker
make build        # build image (python:3.12-slim base)
make config       # configuration wizard
```

Testing notes:
- Test suite runs against an in-memory SQLite DB; ~286 tests.
- `activitypub/tests/test_actor.py` does real-network retries (~20s) — when running
  the whole suite ad hoc, pass `--timeout=60` (pytest-timeout) so a slow/hung test
  is killed instead of stalling the run.

### Local development setup

The project uses an **in-project virtualenv** (`poetry.toml` → `virtualenvs.in-project`),
so `.venv/` lives at the repo root and CI runs `poetry run inv lint` / `inv tests`.

```bash
poetry install --no-root      # populate ./.venv with runtime + dev deps (what CI does)
```

To catch the CI lint (`black --check`, `isort --check`, `flake8`, `mypy`) *before*
pushing, a committed **`.pre-commit-config.yaml`** runs those same checks on every commit.
It's meant to run with [prek](https://prek.j178.dev/) — a fast, drop-in replacement for the
`pre-commit` framework that reads the same config (plain `pre-commit` works too):

```bash
pipx install prek        # or: uv tool install prek / cargo install prek
poetry install --no-root # populate ./.venv with the pinned tools
prek install             # wire up .git/hooks/pre-commit (per clone)
prek run --all-files     # run everything on demand
```

- The hooks are `repo: local`, `language: system` and call the tools from `./.venv/bin`,
  so **tool versions stay single-sourced in `pyproject.toml`** — nothing to pin in the YAML.
  Requires `poetry install` first (the in-project `.venv`).
- Each hook runs on the whole tree (`pass_filenames: false`), mirroring CI exactly and
  letting mypy load its sqlalchemy/pydantic plugins and resolve project imports.
- `.git/hooks/` is not version-controlled, so each clone runs `prek install` once.
  Bypass a single commit with `git commit --no-verify`.

When a hook fails, run `inv autoformat` (black + isort) then fix any flake8/mypy issues.

