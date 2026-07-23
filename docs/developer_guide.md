# Developer's guide

This guide assumes you have some knowledge of [ActivityPub](https://activitypub.rocks/).

## Architecture

Microblog.pub is a "modern" Python application with "old-school" server-rendered templates.

 - [Poetry](https://python-poetry.org/) is used for dependency management.
 - Most of the code is asynchronous, using [asyncio](https://docs.python.org/3/library/asyncio.html).
 - SQLite3 for data storage

The server has 3 components:

 - The web server (powered by [FastAPI](https://fastapi.tiangolo.com/) and [Jinja2](https://jinja.palletsprojects.com/en/3.1.x/) templates)
 - One process that takes care of sending "outgoing activities" 
 - One process that takes care of processing "incoming activities" 

### Tasks

The project uses [Invoke](https://www.pyinvoke.org/) to manage tasks (a Python powered Makefile).

You can find the tasks definition in `tasks.py` and list the tasks using:

```bash
inv -l
```

### Media storage

The uploads are stored in the `data/` directory, using a simple content-addressed storage system (file contents hash is BLOB filename).
Files metadata are stored in the database.

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
following migrations added only in this fork:

| Revision | Date | Description |
| --- | --- | --- |
| `6aafc8f7dd54` | 2026-07-11 | Add `upload.description` (alt text for uploaded media). |
| `bd38c89e83de` | 2026-07-15 | Add `actor.outbox_backfilled_at`, tracking when a remote actor's outbox was last backfilled on demand. |
| `33d3ae2dedac` | 2026-07-15 | Add `actor.followers_count`, `actor.following_count`, `actor.statuses_count`, and `actor.counts_refreshed_at`, caching remote actor counts instead of re-fetching them on every request. |

Running `poetry run inv migrate-db` (or `inv update`, see [Updating](install.md#updating))
applies any migration not yet present in your local database, regardless of
whether it originated upstream or in this fork. If you ever move a `data/` SQLite
file between an upstream checkout and this fork (or vice versa), check `alembic_version`
in the database against the table above to confirm the schema is compatible before
running the app.

### Emoji assets

Standard unicode emoji are rendered as [Twemoji](https://github.com/jdecked/twemoji)
SVGs served from `app/static/twemoji/`. These are **not** checked into the repo (the
directory ships with only a `.gitignore`), so a fresh clone starts without them.

They are downloaded automatically during setup — the `download-twemoji` task is a
dependency of `configuration-wizard`, so `poetry run inv configuration-wizard`
(Python) or `make config` (Docker) fetches them. They are fetched **once** and are
*not* refreshed on a normal start; the Docker entrypoint only re-downloads them when
the `microblogpub_static` volume is empty (see [Installing](install.md#docker-edition)).
To refresh manually (e.g. after bumping the pinned version), run `poetry run inv
download-twemoji` — for Docker, a `docker run` of the same task mounting
`--volume microblogpub_static:/app/app/static`.

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
