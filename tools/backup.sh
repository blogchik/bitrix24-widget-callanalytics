#!/usr/bin/env bash
# Nightly backup of the Call Analytics database.
#
#   ./tools/backup.sh /var/backups/callanalytics
#
# What is worth backing up and what is not:
#
#   portals, portal_sync   IRREPLACEABLE. Encrypted OAuth and application tokens, the
#                          sync cursors, the placement state. Losing these means every
#                          portal must be re-authorised by an administrator reopening
#                          the app, and every portal re-imports its whole history.
#   calls, employees,
#   crm_contexts           Rebuildable: the worker re-fetches them from Bitrix24. A
#                          restore without them costs a backfill, not data.
#   rest_log               Retention is 7 days by design (§6). Kept in the dump because
#                          separating it is more moving parts than it saves.
#
# The dump alone is NOT a recovery plan. TOKEN_ENC_KEYS from .env decrypts the token
# columns; without it the restored rows are unreadable and the portals table is no more
# useful than an empty one. Store the key ring separately from the dumps, and test the
# restore at least once - see docs/deployment.md.

set -euo pipefail

DEST="${1:-/var/backups/callanalytics}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
# A command plus its arguments, word-split below on purpose. The default is the production
# file set `deploy/ci-deploy.sh` runs. It used to be expanded quoted, which asked the shell
# for one program literally named "docker compose" - so this script failed on every run and
# the production host had no backup at all until 2026-09-15.
COMPOSE="${COMPOSE:-docker compose -f docker-compose.yml -f docker-compose.prod.yml}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="${DEST}/callanalytics-${STAMP}.sql.gz"

# The compose files are relative to the checkout, whatever directory cron starts in.
cd "$(dirname "$0")/.."
mkdir -p "$DEST"
# A failed dump must not leave a `.partial` behind for the next reader to mistake.
trap 'rm -f "${OUT}.partial"' EXIT

# --format=plain piped through gzip: restoring is `gunzip -c | psql`, which needs no
# tool version match. The database is small (metadata only, no audio), so the custom
# format's selective restore is not worth the coupling.
# shellcheck disable=SC2086
$COMPOSE exec -T postgres \
    pg_dump -U postgres -d callanalytics --no-owner --no-privileges \
    | gzip -9 > "${OUT}.partial"

mv "${OUT}.partial" "$OUT"
chmod 600 "$OUT"

# A zero-length or truncated dump is worse than none, because it looks like a backup.
if [ ! -s "$OUT" ] || ! gzip -t "$OUT"; then
    echo "backup verification FAILED: ${OUT}" >&2
    exit 1
fi

# The dump must actually contain the table that cannot be rebuilt.
if ! gunzip -c "$OUT" | grep -q "COPY public.portals"; then
    echo "backup has no portals data: ${OUT}" >&2
    exit 1
fi

find "$DEST" -name 'callanalytics-*.sql.gz' -mtime "+${KEEP_DAYS}" -delete

echo "$OUT"
