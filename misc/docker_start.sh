#!/bin/sh
# Fail loudly: if any bootstrap step (asset copy, Twemoji download, migrations)
# fails, abort before starting supervisord rather than serving a broken instance.
set -e

# app/static is a Docker volume so generated assets (compiled CSS, the favicon,
# downloaded Twemoji and custom emoji) survive container restarts and rebuilds.
# When that volume is removed it comes back empty, so repopulate it here from the
# pristine copy baked into the image at build time (see Dockerfile).
if [ -z "$(ls -A /app/app/static 2>/dev/null)" ]; then
    echo "=====> app/static volume is empty, populating it from the image"
    cp -a /app/app/static.dist/. /app/app/static/
fi

# The Twemoji SVGs are not bundled in the image, so (re-)fetch them into the
# volume on every start (needs network access). Unconditional — rather than
# gating on the SVGs being present — so a stale/partial set from a previous
# boot (e.g. an interrupted download, or an outdated pinned version) can never
# linger silently; every start ends up with the full, current set.
echo "=====> downloading Twemoji into the app/static volume"
inv download-twemoji

inv update --no-update-deps
exec supervisord -n -c misc/docker-supervisord.conf
