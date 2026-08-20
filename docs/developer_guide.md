# Developer's guide

This guide assumes you have some knowledge of [ActivityPub](https://activitypub.rocks/).

## Architecture

Microblog.pub is a "modern" Python application with "old-school" server-rendered templates.

 - [Poetry](https://python-poetry.org/) is used for dependency management.
 - Most of the code is asynchronous, using [asyncio](https://docs.python.org/3/library/asyncio.html).
 - SQLite3 for data storage

The server has 4 components:

 - The web server (powered by [FastAPI](https://fastapi.tiangolo.com/) and [Jinja2](https://jinja.palletsprojects.com/en/3.1.x/) templates)
 - One process that takes care of sending "outgoing activities"
 - One process that takes care of processing "incoming activities"
 - One process that delivers Web Push notifications (`app/push_notifications.py`)

The Mastodon streaming API (`app/mastodon/streaming.py`) adds no fifth process:
it's a background `asyncio` task inside the web server, since that's the only
component with an open WebSocket to push events to. It learns about activity
from the other processes by polling committed rows (SQLite is WAL, so this
never blocks a writer) rather than through any direct signalling between
processes — see the module docstring for the full rationale.

### Tasks

The project uses [Invoke](https://www.pyinvoke.org/) to manage tasks (a Python powered Makefile).

You can find the tasks definition in `tasks.py` and list the tasks using:

```bash
inv -l
```

### Media storage

The uploads are stored in the `data/` directory, using a simple content-addressed storage system (file contents hash is BLOB filename).
Files metadata are stored in the database.

`{content_hash}_resized` is always a webp: for an image it's a thumbnail of the original; for
video it's a poster frame extracted with `ffmpeg` (see below) and thumbnailed the same way, so
`Upload.has_thumbnail` and the `/attachments/thumbnails/...` route work identically for both —
no separate "poster" concept.

#### Video and audio uploads

`app/ffmpeg.py` is a thin subprocess wrapper (argv-only, `-protocol_whitelist file`, explicit
timeouts) over the `ffprobe`/`ffmpeg` binaries — never a Python binding, so there's no new
dependency and `shutil.which`-based degradation is free. It does three things, all read-only
(no transcoding):

- **Probe** (`ffmpeg.probe`) — duration, width/height (rotation-corrected), whether a real
  video/audio stream is present (guarding against an MP3's embedded cover art, which ffprobe
  reports as an `attached_pic` video stream), and a compatibility verdict.
- **Poster extraction** (`ffmpeg.extract_poster`) — a single PNG frame from partway through the
  clip, later re-encoded to the same webp thumbnail format as images.
- **Compatibility classification** (`ffmpeg.classify_compatibility`) — rejects a file only on
  confident, well-understood incompatibilities (HEVC and other non-`{h264,vp8,vp9,av1}` codecs,
  4:4:4/4:2:2 chroma, the QuickTime `.mov` container brand). Everything else — including
  "verdict unavailable" (no `ffmpeg`, probe failure, timeout) — is accepted. This fail-open rule
  is deliberate: a false-positive rejection blocks a legitimate post, so the classifier only
  refuses what it's sure about.

`ffmpeg` is an optional runtime dependency (`app.ffmpeg.is_available()`); without it, video/audio
uploads still work, they just get no duration, no poster/blurhash, and no compatibility
rejection. `save_upload` (`app/uploads.py`) enforces size limits (`max_image_upload_size`/
`max_video_upload_size` in `data/profile.toml`) before any byte is written to disk, and unlinks
a written-then-rejected file so an incompatible upload never leaves an orphaned row or file
behind.

### Mastodon client API

`app/mastodon/` implements a subset of the [Mastodon client REST
API](https://docs.joinmastodon.org/client/intro/) (OAuth, timelines, statuses,
notifications, conversations, accounts/social graph, search, media) on top of
the same ActivityPub data — no separate data model. It's mounted unconditionally
in `app/main.py`. See the [user-facing docs](mastodon_api.md) for what's
supported.

### Database migrations

Schema changes are managed with [Alembic](https://alembic.sqlalchemy.org/) migrations
under `alembic/versions/`. This fork's history includes all migrations from
[upstream tinyBlogPub/microblog.pub](https://github.com/tinyBlogPub/microblog.pub)
up to `a209f0333f5a` (*Add oauth refresh token support*, 2022-12-18), plus the
migrations below, which exist only in this fork.

They are listed in dependency order — the order `inv migrate-db` applies them, and
so also the order of the `alembic_version` values a database passes through. The
last row is the current head.

**Whenever a new migration is added to `alembic/versions/` (see the autogenerate
caveat below), add a matching row to this table in the same change** — revision id,
date, and a one-line description of what it does and why. This table is the only
place that history is summarized; a migration without a row here is invisible to
anyone reading this guide instead of grepping the directory.

| Revision | Date | Description |
| --- | --- | --- |
| `6aafc8f7dd54` | 2026-07-11 | Add `upload.description` (alt text for uploaded media). |
| `bd38c89e83de` | 2026-07-15 | Add `actor.outbox_backfilled_at`, tracking when a remote actor's outbox was last backfilled on demand. |
| `33d3ae2dedac` | 2026-07-15 | Add `actor.followers_count`, `actor.following_count`, `actor.statuses_count`, and `actor.counts_refreshed_at`, caching remote actor counts instead of re-fetching them on every request. |
| `4c9a1f7b2e58` | 2026-08-01 | Add `actor.is_muted`, `actor.muted_until` and `actor.are_notifications_muted` (Mastodon-style account mutes). |
| `420fe79fe4da` | 2026-08-01 | Add the `marker` table backing `GET/POST /api/v1/markers` (cross-device read positions). |
| `9f1c3d7a5b21` | 2026-08-08 | Add `actor.note`, the owner's private per-account note. |
| `b3e7a1f0c9d4` | 2026-08-08 | Add the `muted_conversation` table (muted threads, keyed on `conversation`). |
| `2e529833b5cb` | 2026-08-10 | Add the timeline indexes `ix_inbox_ap_published_at`, `ix_inbox_stream`, `ix_outbox_ap_published_at` and `ix_outbox_homepage`. |
| `7c4a1d8e3f02` | 2026-08-10 | Rebuild `ix_outbox_homepage` without `visibility` leading, so the Mastodon outbox timeline (which never constrains it) can use the index too. |
| `5eabb060f447` | 2026-08-13 | Add `ix_outgoing_activity_queue` and `ix_incoming_activity_queue`; the federation workers poll those tables every 2 seconds and had nothing but the PK. |
| `b05a3893306a` | 2026-08-14 | Index the foreign-key and `conversation` columns on `inbox`/`outbox` (SQLite does not index FK columns on its own). |
| `43e8f29aa190` | 2026-08-14 | Add `upload.duration` and `upload.has_audio`, populated from ffmpeg/ffprobe for video/audio uploads (see [Video and audio uploads](#video-and-audio-uploads)). |
| `0a7b9bc9538e` | 2026-08-17 | Add the `push_subscription` table (Web Push delivery state and per-alert preferences). |
| `c8d2f4a71e63` | 2026-08-18 | Add the `scheduled_status` table (statuses queued for later publication). |
| `cc8f551f6765` | 2026-08-19 | Add `upload.focus_x` / `upload.focus_y` (media focal point / crop hint). |
| `a3f61c9d20b7` | 2026-08-19 | Add the expression indexes `ix_inbox_in_reply_to` / `ix_outbox_in_reply_to` on `json_extract(ap_object, '$.inReplyTo')`, so reply lookups (the AP `replies` collection and the reply counters) stop scanning both tables. |
| `f4c2a7e8b910` | 2026-08-20 | Add quote-post support (FEP-044f): `inbox`/`outbox.quote_ap_id`, `quote_authorization_ap_id`; `outbox.quote_state`, `quotes_count`; `inbox.quote_is_verified`; and `ix_inbox_quote_ap_id`. |

Running `poetry run inv migrate-db` (or `inv update`, see [Updating](install.md#updating))
applies any migration not yet present in your local database, regardless of
whether it originated upstream or in this fork. To see where a database stands
before upgrading it:

```bash
poetry run alembic current   # the revision this database is at
poetry run alembic heads     # the revision the code expects
poetry run alembic history   # the full chain, newest first
```

If `current` is behind `heads`, the rows between them in the table above are the
migrations `inv migrate-db` will apply. If you ever move a `data/` SQLite file
between an upstream checkout and this fork (or vice versa), check `alembic_version`
in the database against the table above to confirm the schema is compatible before
running the app.

Two conventions this fork's migrations follow, both learned the hard way:

- **Write them by hand, and never commit an autogenerated body unread.**
  `alembic/env.py` imports `Base` but none of the model modules, so
  `Base.metadata` is empty when it runs. Autogenerate therefore compares the live
  database against *nothing* and confidently emits `op.drop_table()` for every
  table in the schema. `inv generate-db-migration "message"` is still the right
  way to get a revision file with the correct `down_revision` — just delete the
  generated `upgrade()`/`downgrade()` bodies and write the real ones.
- **Use plain `op.create_index` for expression indexes**, not
  `batch_alter_table`. Batch mode recreates the table and reflects its indexes,
  and expression indexes do not survive that reflection — SQLAlchemy skips them
  with `SAWarning: Skipped unsupported reflection of expression-based index`,
  which in batch mode means the index is silently dropped.

### Emoji assets

Standard unicode emoji are rendered as [Twemoji](https://github.com/jdecked/twemoji)
SVGs served from `app/static/twemoji/`. These are **not** checked into the repo (the
directory ships with only a `.gitignore`), so a fresh clone starts without them.

They are downloaded automatically during setup — the `download-twemoji` task is a
dependency of `configuration-wizard`, so `poetry run inv configuration-wizard`
(Python) or `make config` (Docker) fetches them. For Docker, the entrypoint
(`misc/docker_start.sh`) also re-runs `download-twemoji` on **every** container
start, so the `microblogpub_static` volume always ends up with the full, current
set even if a previous boot left it empty or partially populated (see
[Installing](install.md#docker-edition)). To force a redownload on demand
without restarting the container, run `make download-twemoji` (Docker) or
`poetry run inv download-twemoji` (non-Docker installs, or after bumping the
pinned version).

Under the hood the task downloads a release tarball and extracts `assets/svg/`. The
source is [jdecked/twemoji](https://github.com/jdecked/twemoji), the maintained
continuation of the original `twitter/twemoji` (abandoned after the Twitter/X
acquisition). The release tag is pinned in `tasks.py:download_twemoji` — bump it there
when a newer release is needed.

### Translations (i18n)

The UI (public pages and the admin UI) uses [gettext](https://www.gnu.org/software/gettext/)
via [Babel](https://babel.pocoo.org/) for translations. Catalogs live under
`app/translations/<locale>/LC_MESSAGES/messages.po`, with the extraction template at
`app/translations/messages.pot`. Which language is shown is controlled by the
`language_code` setting in `data/profile.toml` (see [Installation](install.md)):
both public pages and the admin UI (`/admin`) negotiate the visitor's
`Accept-Language` header against the locales available on the instance, falling
back to `language_code` when no match is found (e.g. no header sent, or none of
the requested languages are available).

Bundled locales: `en` (source strings), `ca` (Catalan), `es` (Spanish), `fr` (French),
`it` (Italian), and `ro` (Romanian). Corrections and new locales are welcome — see
below.

To add or update a translation:

```bash
poetry run inv extract-messages          # (re)generate app/translations/messages.pot
poetry run inv init-translation <locale>  # create a new app/translations/<locale>/LC_MESSAGES/messages.po
poetry run inv update-translations        # merge new/changed msgids into all existing .po files
poetry run inv compile-translations       # compile .po -> .mo (also runs automatically as part of `inv update`)
```

Edit the generated `.po` file's `msgstr` entries with a gettext-aware editor (e.g.
[Poedit](https://poedit.net/)) or by hand, then run `compile-translations` to produce
the `.mo` file the app actually loads at runtime (`.mo` files are build artifacts and
are gitignored). A `data/translations/<locale>/LC_MESSAGES/messages.mo` — following the
same `data/`-over-`app/` override convention used for templates — takes precedence over
the bundled one, letting an instance ship a custom or newer translation without
touching the checkout.

## Installation

Running a local version requires:

 - Python 3.10+ (3.12 recommended — it's what the project is developed and tested against)
 - SQLite 3.35+

You can follow the [Python developer version of the install instructions](install.md#python-developer-edition).

## Documentation

The documentation is a set of Markdown files in `docs/`, built into a static
website with [Sphinx](https://www.sphinx-doc.org/) using the
[MyST](https://myst-parser.readthedocs.io/) Markdown parser and the
[Furo](https://pradyunsg.me/furo/) theme. The online documentation is published
to GitHub Pages automatically by the `.github/workflows/pages.yml` workflow on
every push to `main` that touches `docs/`.

Install the documentation dependencies (ideally in a dedicated virtualenv):

```bash
pip install -r docs/requirements.txt
```

Then build the documentation locally by running:

```bash
inv build-docs
```

The rendered HTML lands in `docs/_build/html`. Check out the result by starting a
static server using the Python standard library:

```bash
cd docs/_build/html
python -m http.server 8001
```

## Contributing

Contributions/patches are welcome, but please start a discussion in an [issue](https://github.com/toniher/microblog.pub/issues) before working on anything consequent.

### Patches

Please ensure your code passes the code quality checks:

```bash
inv autoformat
inv lint
```

And that the tests suite is passing:

```bash
inv tests
```

Please also consider adding new test cases if needed.
