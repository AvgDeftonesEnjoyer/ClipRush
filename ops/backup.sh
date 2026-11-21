#!/bin/sh
# Run only using the ops profile; writes a private, versioned PostgreSQL archive.
set -eu
umask 077
mkdir -p /backups
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
target="/backups/cliprush-${stamp}.dump"
tmp="${target}.partial"
trap 'rm -f "$tmp"' EXIT HUP INT TERM
pg_dump --no-owner --no-acl --format=custom --compress=6 --file="$tmp"
# Verify archive structure before declaring the snapshot complete.
pg_restore --list "$tmp" > /dev/null
mv "$tmp" "$target"
sha256sum "$target" > "${target}.sha256"
chmod 600 "$target" "${target}.sha256"
echo "Backup verified: $target"
