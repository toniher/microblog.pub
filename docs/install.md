# Installing

[TOC]

## Docker edition

Assuming Docker and [Docker Compose](https://docs.docker.com/compose/install/) are already installed.

For now, there's no image published on Docker Hub, this means you will have to build the image locally.

Clone the repository, replace `you-domain.tld` by your own domain.

Note that if you want to serve static assets via your reverse proxy (like nginx), clone it in a place
where it is accessible by your reverse proxy user.

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

Start the app with Docker Compose, it will listen on port 8000 by default.
The port can be tweaked in the `docker-compose.yml` file.

```bash
docker compose up -d
```

Setup a reverse proxy (see the [Reverse Proxy section](/installing.html#reverse-proxy)).

### What runs inside the container

The image is built from `python:3.12-slim` and a single container runs **three
processes** under [supervisord](http://supervisord.org/) (see `misc/docker-supervisord.conf`):

 - `uvicorn` — the web server, listening on `0.0.0.0:8000`
 - `incoming_worker` — processes incoming federation activities (your inbox)
 - `outgoing_worker` — delivers your outgoing activities to other servers

On every start, the entrypoint (`misc/docker_start.sh`) first runs `inv update
--no-update-deps`, which recompiles the CSS and applies any pending database
migrations before launching supervisord. You therefore don't need to run migrations
by hand after pulling a new version — restarting the container is enough.

The container runs as the unprivileged user `1000:1000` (see the `user:` line in
`docker-compose.yml`), and two host directories are bind-mounted so your data
survives image rebuilds:

 - `./data` → `/app/data` — config (`profile.toml`), secrets, the SQLite database, uploads, logs
 - `./app/static` → `/app/app/static` — compiled CSS, favicon and emoji assets

### Managing the app

```bash
docker compose ps          # show the container status
docker compose stop        # stop the app
docker compose up -d       # (re)start the app in the background
docker compose restart     # restart (e.g. after editing data/profile.toml)
```

Note that most configuration changes (anything in `data/profile.toml`) only take
effect after a restart.

### Viewing logs

supervisord writes each process' output to a file under `data/`, which you can tail
from the host:

```bash
tail -f data/uvicorn.log     # web server
tail -f data/incoming.log    # incoming federation worker
tail -f data/outgoing.log    # outgoing federation worker
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

See the [User's guide](/user_guide.html) for the full list and the details of each
task (each one documents its "Docker edition" invocation).

### Updating 

To update microblogpub, pull the latest changes, rebuild the Docker image and restart the process with `docker compose`.

```bash
git pull
make build
docker compose stop
docker compose up -d
```

As you probably already know, Docker can (and will) eat a lot of disk space, when updating you should [prune old images](https://docs.docker.com/config/pruning/#prune-images) from time to time:

```bash
docker image prune -a --filter "until=24h"
```

### Troubleshooting: `PermissionError` on `app/static/` or `data/` after updating

`docker-compose.yml` runs the container as an unprivileged user (`user: 1000:1000`).
If your `data/` and `app/static/` directories were created (or previously written to)
by a container running as `root` — e.g. an older setup without the `user:` line — the
container's uid `1000` will no longer be able to write to them, and startup tasks like
`compile_scss` (which regenerates `app/static/favicon.ico` and the compiled CSS) will
fail with a traceback ending in `PermissionError: [Errno 13] Permission denied`, and
`uvicorn`/the worker processes will crash-loop under supervisord.

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
recommended — it's what the project is developed and tested against, and what the
Docker image ships).

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
poetry install
```

Setup config.

```bash
poetry run inv configuration-wizard
```

Setup the database.

```bash
poetry run inv migrate-db
```

Grab your virtualenv path.

```bash
poetry env info
```

Run the two processes with supervisord.

```bash
VENV_DIR=/home/ubuntu/.cache/pypoetry/virtualenvs/microblogpub-chx-y1oE-py3.12 poetry run supervisord -c misc/supervisord.conf -n
```

Setup a reverse proxy (see the next section).

### Updating 

To update microblogpub locally, pull the remote changes and run the `update` task to regenerate the CSS and run any DB migrations.

```bash
git pull
poetry run inv update
```

## Reverse proxy

You will also want to setup a reverse proxy like NGINX, see [uvicorn documentation](https://www.uvicorn.org/deployment/#running-behind-nginx):

If you don't have a reverse proxy setup yet, [NGINX + certbot](https://www.nginx.com/blog/using-free-ssltls-certificates-from-lets-encrypt-with-nginx/) is recommended.

```nginx
server {
    client_max_body_size 4G;

    location / {
      proxy_set_header Host $http_host;
      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
      proxy_set_header X-Forwarded-Proto $scheme;
      proxy_set_header Upgrade $http_upgrade;
      proxy_set_header Connection $connection_upgrade;
      proxy_redirect off;
      proxy_buffering off;
      proxy_pass http://localhost:8000;
    }

    # [...]
}

# This should be outside the `server` block
map $http_upgrade $connection_upgrade {
  default upgrade;
  '' close;
}
```

Optionally, you can serve static files using NGINX directly, with an additional `location` block.
This will require the NGINX user to have access to the `static/` directory.

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

It is possible to run microblogpub on a subdomain (`sub.domain.tld`) while being reachable from the root root domain (`domain.tld`) using the `name@domain.tld` handle.

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

Once configured correctly, people will be able to follow you using `name@domain.tld`, while using `sub.domain.tld` for the web interface.


## (Advanced) Running from subpath

It is possible to configure microblogpub to run from subpath.
To achieve this, do the following configuration _between_ config and start steps.
i.e. _after_ you run `make config` or `poetry run inv configuration-wizard`,
but _before_ you run `docker compose up` or `poetry run supervisord`.
Changing this settings on an instance which has some posts or was seen by other instances will likely break links to these posts or federation (i.e. links to your instance, posts and profile from other instances).

The following steps will explain how to configure instance to be available at `https://example.com/subdir`.
Change them to your actual domain and subdir.

* Edit `data/profile.toml` file, add this line:

		id = "https://example.com/subdir"

* Edit `misc/*-supervisord.conf` file which is relevant to you (it depends on how you start microblogpub - if in doubt, do the same change in all of them) - in `[program:uvicorn]` section, in the line which starts with `command`, add this argument at the very end: ` --root-path /subdir`

Above two steps are enough to configure microblogpub.
Next, you also need to configure reverse proxy.
It might slightly differ if you plan to have other services running on the same domain, but for [NGINX config shown above](#reverse-proxy), the following changes are enough:

* Add subdir to location, so location block starts like this:

		location /subdir {

* Add `/` at the end of `proxy_pass` directive, like this:

		proxy_pass http://localhost:8000/;

These two changes will instruct NGINX that requests sent to `https://example.com/subdir/...` should be forwarded to `http://localhost:8000/...`.

* Inside `server` block, add redirects for well-known URLs (add these lines after `client_max_body_size`, remember to replace `subdir` with your actual subdir!):

		location /.well-known/webfinger { return 301 /subdir$request_uri; }
		location /.well-known/nodeinfo  { return 301 /subdir$request_uri; }
		location /.well-known/oauth-authorization-server  { return 301 /subdir$request_uri; }

* Optionally, [check robots.txt from a running microblogpub instance](https://microblog.pub/robots.txt) and integrate it into robots.txt file in the root of your server - remember to prepend `subdir` to URLs, so for example `Disallow: /admin` becomes `Disallow: /subdir/admin`.

## Available tutorial/guides

 - [Opalstack](https://community.opalstack.com/d/1055-howto-install-and-run-microblogpub-on-opalstack), thanks to [@defulmere@mastodon.social](https://mastodon.online/@defulmere).
