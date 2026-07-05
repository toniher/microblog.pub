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

# The Twemoji SVGs are not bundled in the image, so fetch them into the volume
# (needs network access). Gated on the SVGs being absent — not on the volume
# being empty — so that a download which failed on a previous (partial) boot is
# retried on the next start instead of leaving the volume silently incomplete.
if ! ls /app/app/static/twemoji/*.svg >/dev/null 2>&1; then
    echo "=====> downloading Twemoji into the app/static volume"
    inv download-twemoji
fi

inv update --no-update-deps
exec supervisord -n -c misc/docker-supervisord.conf
