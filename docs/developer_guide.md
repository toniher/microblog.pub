# Developer's guide

This guide assumes you have some knowledge of [ActivityPub](https://activitypub.rocks/).

[TOC]

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

### Emoji assets

Standard unicode emoji are rendered as [Twemoji](https://github.com/jdecked/twemoji)
SVGs served from `app/static/twemoji/`. These SVGs are **not** checked into the repo
(the directory ships with only a `.gitignore`), so a fresh clone starts without them.

**They are downloaded automatically during initial setup.** The `download-twemoji`
task is a dependency of `configuration-wizard`, so:

 - **Python edition:** `poetry run inv configuration-wizard` fetches the SVGs straight
   to `app/static/twemoji/` on disk.
 - **Docker edition:** `make config` does the same — its `docker run` mounts both
   `data/` and the `microblogpub_static` named volume, so the emoji downloaded by the
   wizard land in that volume and are then served by the running `docker compose`
   container (which mounts the same volume at `/app/app/static`).

You don't need to download them by hand. They are fetched **once** and are *not*
refreshed on a normal start — `inv update` / `make update` do not re-run the download.
The Docker entrypoint (`misc/docker_start.sh`) only re-downloads them when the
`microblogpub_static` volume is empty (i.e. on first boot or after the volume has been
removed), so a wiped volume rebuilds itself automatically. To refresh or re-fetch them
manually (e.g. after bumping the pinned version), run `poetry run inv download-twemoji`
(or, for Docker, a `docker run` invocation of `inv download-twemoji` that mounts
the static volume — i.e. `--volume microblogpub_static:/app/app/static`).

Under the hood the task downloads a release tarball and extracts `assets/svg/` into
`app/static/twemoji/`. The source is [jdecked/twemoji](https://github.com/jdecked/twemoji),
the actively maintained continuation of the original `twitter/twemoji` project
(abandoned after the Twitter/X acquisition). The release tag is pinned in
`tasks.py:download_twemoji` — bump it there when a newer Twemoji release is needed.

## Installation

Running a local version requires:

 - Python 3.10+ (3.12 recommended — it's what the project is developed and tested against)
 - SQLite 3.35+

You can follow the [Python developer version of the install instructions](https://docs.microblog.pub/installing.html#python-developer-edition).

## Documentation

The documention is managed as Markdown files in `docs/` and the online documentation is built using a homegrown Python script (`scripts/build_docs.py`).

You can build the documentation locally by running:

```bash
inv build-docs
```

And check out the result by starting a static server using Python standard library:

```bash
cd docs/dist
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
