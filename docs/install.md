# Installing

## Docker edition

Assuming Docker and [Docker Compose](https://docs.docker.com/compose/install/) are already installed.

No image is published on Docker Hub yet, so you have to build the image locally.

Clone the repository, replace `your-domain.tld` by your own domain.

If you want to serve static assets via your reverse proxy (like nginx), clone it in a place
your reverse proxy user can access.

```bash
git clone https://github.com/toniher/microblog.pub your-domain.tld
```

Build the Docker image locally.

```bash
make build
```

Run the configuration wizard.

```bash
make config
```

Update `data/profile.toml` and add this line in order to process headers from the reverse proxy:

```toml
trusted_hosts = ["*"]
```

Start the app with Docker Compose. It listens on port 8000 by default;
you can change the port in the `docker-compose.yml` file.

```bash
docker compose up -d
```

Setup a reverse proxy (see the [Reverse Proxy section](#reverse-proxy)).

### What runs inside the container

The image is built from `python:3.12-slim` and a single container runs **four
processes** under [supervisord](http://supervisord.org/) (see `misc/docker-supervisord.conf`):

 - `uvicorn`: the web server, listening on `0.0.0.0:8000`
 - `incoming_worker`: processes incoming federation activities (your inbox)
 - `outgoing_worker`: delivers your outgoing activities to other servers
 - `push_worker`: delivers Web Push notifications to subscribed Mastodon
   client apps (see `docs/mastodon_api.md`)

On every start, the entrypoint (`misc/docker_start.sh`) first runs `inv update
--no-update-deps`, which recompiles the CSS, compiles the translation catalogs
(`.po` → `.mo`, see [Translations / i18n](developer_guide.md#translations-i18n)),
and applies any pending database migrations before launching supervisord. You
don't need to run migrations by hand after pulling a new version: restarting the
container is enough.

The container runs as the unprivileged user `1000:1000` (see the `user:` line in
`docker-compose.yml`), and its state lives in two volumes so it survives image
rebuilds:

 - `./data` → `/app/data`: a host bind mount holding your config (`profile.toml`),
   secrets (`key.pem`, the actor signing key, and `vapid_key.pem`, the Web Push
   key), the SQLite database, uploads and logs. Keep this backed up.
 - `microblogpub_static` → `/app/app/static`: a **named Docker volume** for
   generated assets: compiled CSS, the favicon, the downloaded Twemoji set and
   custom emoji.

Using a named volume (rather than a host bind mount) for `app/static` means the
image ships a pristine copy of the static assets, and the entrypoint
(`misc/docker_start.sh`) repopulates the volume from that copy whenever it is
empty, restoring the base assets and recompiling the CSS. If you remove the volume,
the next start rebuilds it with no manual step:

```bash
docker compose down
docker volume rm microblogpub_static   # recreated & repopulated on next up
docker compose up -d
```

The Twemoji set is handled separately: the entrypoint re-downloads it on **every**
container start (needs network access), so it's complete and current regardless
of the volume's state, including after a wipe. The ~4,000-file Twemoji download
makes every boot slightly slower.

### Managing the app

```bash
docker compose ps          # show the container status
docker compose stop        # stop the app
docker compose up -d       # (re)start the app in the background
docker compose restart     # restart (e.g. after editing data/profile.toml)
```

Most configuration changes (anything in `data/profile.toml`) take effect only
after a restart.

### Viewing logs

supervisord writes each process' output to a file under `data/`, which you can tail
from the host:

```bash
tail -f data/uvicorn.log     # web server
tail -f data/incoming.log    # incoming federation worker
tail -f data/outgoing.log    # outgoing federation worker
tail -f data/push.log        # Web Push delivery worker
```

supervisord rotates each of these once it hits `stdout_logfile_maxbytes` (see
`misc/*supervisord.conf`), producing `data/*.log.1`, `data/*.log.2`, etc., but it
never compresses the backups. `misc/gzip_rotated_logs.sh` gzips those rotated
files; run it periodically from the host's crontab (or a systemd timer) against
the `data/` directory, e.g.:

```
0 3 * * * /path/to/repo/misc/gzip_rotated_logs.sh /path/to/data
```

The container's own stdout/stderr is also available via Docker:

```bash
docker compose logs -f
```

### Running maintenance tasks

Administrative tasks (checking the config, resetting the password, pruning old data,
moving instances, importing follows, …) are exposed as `make` targets that each spin
up a throwaway container sharing your `data/` and `app/static/` volumes. For example:

```bash
make check-config                                   # validate data/profile.toml
make reset-password                                 # set a new admin password
make account=user@other.tld webfinger              # resolve a remote actor URL
```

See the [User's guide](user_guide.md) for the full list and the details of each
task (each one documents its "Docker edition" invocation).

### Updating 

To update microblogpub, pull the latest changes, rebuild the Docker image and restart the process with `docker compose`.

```bash
git pull
make build
docker compose stop
docker compose up -d
```

The entrypoint applies migrations on start. To run that step (`inv update
--no-update-deps`) on its own in a throwaway container, use `make update`.

Docker can (and will) eat a lot of disk space, so when updating, [prune old images](https://docs.docker.com/config/pruning/#prune-images) from time to time:

```bash
docker image prune -a --filter "until=24h"
```

### Troubleshooting: `PermissionError` on `app/static/` or `data/` after updating

`docker-compose.yml` runs the container as an unprivileged user (`user: 1000:1000`).
If your `data/` and `app/static/` directories were created (or previously written to)
by a container running as `root` (e.g. an older setup without the `user:` line), the
container's uid `1000` can no longer write to them. Startup tasks like `compile_scss`
(which regenerates `app/static/favicon.ico` and the compiled CSS) then fail with a
traceback ending in `PermissionError: [Errno 13] Permission denied`, and
`uvicorn`/the worker processes crash-loop under supervisord.

Check ownership:

```bash
stat -c '%u:%g %n' app/static data
```

If it doesn't match the uid:gid in `docker-compose.yml`'s `user:` line (default `1000:1000`),
fix it:

```bash
docker compose down
sudo chown -R 1000:1000 ./data ./app/static
docker compose up -d
```

## Python developer edition

Assuming you have a working **Python 3.10+** environment (Python **3.12** is
recommended: the project is developed and tested against it, and the Docker image
ships it).

Setup [Poetry](https://python-poetry.org/docs/master/#installing-with-the-official-installer).

```bash
curl -sSL https://install.python-poetry.org | python3 -
```

Clone the repository.

```bash
git clone https://github.com/toniher/microblog.pub testing.microblog.pub
```

Install deps.

```bash
poetry install --no-root
```

**Recommended**: install `ffmpeg` (e.g. `apt install ffmpeg`). It's an optional runtime
dependency used only to read video/audio metadata, extract a poster frame, and check that an
uploaded video/audio file will play in mainstream browsers (see [Video and audio
uploads](developer_guide.md#video-and-audio-uploads)). Without it, video/audio still uploads,
but with no duration, no poster/blurhash, and **no compatibility checking at all** (nothing is
ever rejected). The Docker image already includes it.

Setup config.

```bash
poetry run inv configuration-wizard
```

Setup the database.

```bash
poetry run inv migrate-db
```

Run the configured processes (web server + workers, see `misc/supervisord.conf`)
with supervisord. `poetry.toml` makes Poetry create the virtualenv inside the
checkout, so `VENV_DIR` is the `.venv/` directory at the repository root.

```bash
VENV_DIR=$PWD/.venv poetry run supervisord -c misc/supervisord.conf -n
```

supervisord writes each process' output to a log file relative to the directory
you start it from: `uvicorn.log`, `incoming_worker.log`, `outgoing_worker.log`
and `push_worker.log`.

Setup a reverse proxy (see the next section).

### Updating 

To update microblogpub locally, pull the remote changes and run the `update` task to regenerate the CSS, compile the translation catalogs, and run any DB migrations.

```bash
git pull
poetry run inv update
```

`inv update` does not touch the Twemoji assets. When an update bumps the pinned
Twemoji release (`tasks.py:download_twemoji`), run `poetry run inv
download-twemoji` as well.

If your install predates 2.5.2, your supervisord config has no Web Push worker.
Add the `[program:push_worker]` section (`inv process-push-notifications`) from
`misc/supervisord.conf`, then restart supervisord.

This fork carries a number of Alembic migrations beyond upstream: new tables
(scheduled statuses, Web Push subscriptions, muted threads, read markers,
Mastodon lists), extra columns (media alt text and focal point, cached remote
actor counts, account mutes, per-follow boost/notify options) and several
indexes. The
[developer guide](developer_guide.md#database-migrations) lists every one of them
in the order they are applied, and shows how to check which are still pending on
a given database (`alembic current` vs. `alembic heads`). Check it if you're moving
a database between this fork and upstream, or want to know what changed in the
schema.

## Reverse proxy

Set up a reverse proxy like NGINX (see the [uvicorn documentation](https://www.uvicorn.org/deployment/#running-behind-nginx)):

If you don't have a reverse proxy setup yet, [NGINX + certbot](https://www.nginx.com/blog/using-free-ssltls-certificates-from-lets-encrypt-with-nginx/) is recommended.

```nginx
server {
    # nginx's own cap needs to be at or above the app-level upload limits
    # (max_image_upload_size/max_video_upload_size in data/profile.toml,
    # 10 MiB/40 MiB by default). Otherwise nginx rejects a large-but-valid
    # upload before the app ever sees it.
    client_max_body_size 4G;

    location / {
      # HTTP/1.1 is required for the Upgrade/Connection headers below to
      # turn a request into a WebSocket (nginx defaults to 1.0 upstream).
      proxy_http_version 1.1;
      proxy_set_header Host $http_host;
      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
      proxy_set_header X-Forwarded-Proto $scheme;
      proxy_set_header Upgrade $http_upgrade;
      proxy_set_header Connection $connection_upgrade;
      proxy_redirect off;
      proxy_buffering off;
      # Needed for the Mastodon streaming API (wss://…/api/v1/streaming,
      # app/mastodon/streaming.py). The default 60s would kill an idle
      # WebSocket, which the client sees as an unexplained disconnect.
      proxy_read_timeout 3600s;
      proxy_send_timeout 3600s;
      proxy_pass http://localhost:8000;
    }

    # [...]
}

# This should be outside the `server` block
map $http_upgrade $connection_upgrade {
  default upgrade;
  # NOT `close`: that forces `Connection: close` on every ordinary
  # (non-upgrade) request too, since $http_upgrade is empty for those.
  '' '';
}
```

The streaming endpoint has its own settings (`streaming_enabled`,
`streaming_max_connections`, `streaming_poll_interval`); see
[Streaming API](user_guide.md#streaming-api) in the user's guide.

Run a single uvicorn process (no `--workers`). The streaming hub and its poll
loop live inside the web process and assume they are the only copy: each extra
worker would run its own poll loop against the database and enforce its own
`streaming_max_connections` cap.

Optionally, you can serve static files from NGINX directly with an additional `location` block.
The NGINX user then needs access to the `static/` directory.

```nginx
server {
    # [...]

    location / {
        # [...]
    }

    location /static {
       # path for static files
       rewrite ^/static/(.*) /$1 break;
       root /path/to/your-domain.tld/app/static/;
       expires 1y;
    }

    # [...]
}
```

### NGINX config tips

Enable HTTP2 (which is disabled by default):

```nginx
server {
    # [...]
    listen [::]:443 ssl http2;
}
```

Tweak `/etc/nginx/nginx.conf` and add gzip compression for ActivityPub responses:

```nginx
http {
    # [...]
    gzip_types text/plain text/css application/json application/javascript application/activity+json application/octet-stream;
}
```


## (Advanced) Running on a subdomain

You can run microblogpub on a subdomain (`sub.domain.tld`) and still be reachable from the root domain (`domain.tld`) using the `name@domain.tld` handle.

This requires forwarding/proxying requests from the root domain to the subdomain, for example using NGINX:

```nginx
location /.well-known/webfinger {
  add_header Access-Control-Allow-Origin '*';
  return 301 https://sub.domain.tld$request_uri;
}
```

And updating `data/profile.toml` to specify the root domain as the webfinger domain:

```toml
webfinger_domain = "domain.tld"
```

Once configured, people can follow you using `name@domain.tld`, while you use `sub.domain.tld` for the web interface.


## (Advanced) Running from subpath

You can configure microblogpub to run from a subpath.
Do the following configuration _between_ the config and start steps,
i.e. _after_ you run `make config` or `poetry run inv configuration-wizard`,
but _before_ you run `docker compose up` or `poetry run supervisord`.
Changing these settings on an instance that has posts or was seen by other instances will likely break links to those posts or federation (i.e. links to your instance, posts and profile from other instances).

The following steps configure an instance to be available at `https://example.com/subdir`.
Change them to your actual domain and subdir.

* Edit `data/profile.toml` file, add this line:

		id = "https://example.com/subdir"

* Edit the `misc/*-supervisord.conf` file relevant to you (it depends on how you start microblogpub; if in doubt, make the same change in all of them). In the `[program:uvicorn]` section, in the line that starts with `command`, add this argument at the very end: ` --root-path /subdir`

These two steps configure microblogpub.
Next, configure the reverse proxy.
The setup might differ slightly if you plan to run other services on the same domain, but for the [NGINX config shown above](#reverse-proxy), the following changes are enough:

* Add subdir to location, so location block starts like this:

		location /subdir {

* Add `/` at the end of `proxy_pass` directive, like this:

		proxy_pass http://localhost:8000/;

With these two changes, NGINX forwards requests sent to `https://example.com/subdir/...` to `http://localhost:8000/...`.

* Inside `server` block, add redirects for well-known URLs (add these lines after `client_max_body_size`, remember to replace `subdir` with your actual subdir!):

		location /.well-known/webfinger { return 301 /subdir$request_uri; }
		location /.well-known/nodeinfo  { return 301 /subdir$request_uri; }
		location /.well-known/oauth-authorization-server  { return 301 /subdir$request_uri; }

* The instance advertises the streaming API as `wss://example.com` with no path
  (`streaming_base_url()` in `app/mastodon/streaming.py`), and clients append
  `/api/v1/streaming` to it. Either proxy `/api/v1/streaming` at the domain root
  to `http://localhost:8000/api/v1/streaming` (with the WebSocket settings from
  the `location /` block above), or set `streaming_enabled = false` in
  `data/profile.toml` so clients fall back to polling.

* Optionally, [check robots.txt from a running microblogpub instance](https://microblog.pub/robots.txt) and integrate it into robots.txt file in the root of your server - remember to prepend `subdir` to URLs, so for example `Disallow: /admin` becomes `Disallow: /subdir/admin`.

## Available tutorial/guides

 - [Opalstack](https://community.opalstack.com/d/1055-howto-install-and-run-microblogpub-on-opalstack), thanks to [@defulmere@mastodon.social](https://mastodon.online/@defulmere).
