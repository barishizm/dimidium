#!/bin/sh
# Runs the app as the unprivileged "app" user (uid 10001).
# As root: make the data volume writable for that user, then drop root for good.
set -eu

if [ "$(id -u)" = "0" ]; then
    mkdir -p /srv/data
    chown app:app /srv/data
    exec setpriv --reuid=app --regid=app --init-groups --inh-caps=-all -- "$@"
fi
exec "$@"
