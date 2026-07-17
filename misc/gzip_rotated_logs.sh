#!/bin/sh
# Gzip supervisord's rotated worker/uvicorn logs (data/*.log.1, data/*.log.2, ...).
# Supervisord's own stdout_logfile_maxbytes rotation (see misc/*supervisord.conf)
# only renames these backups, it never compresses them.
#
# Meant to be run periodically from the host's crontab (or a systemd timer)
# against the data/ directory, e.g.:
#   0 3 * * * /path/to/repo/misc/gzip_rotated_logs.sh /path/to/data
set -eu

DATA_DIR="${1:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)/data}"

find "$DATA_DIR" -maxdepth 1 -type f -name '*.log.[0-9]*' ! -name '*.gz' -print0 |
    xargs -0 -r gzip -f
