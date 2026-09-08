#!/usr/bin/env bash
# Generate the secrets a deployment needs, in the exact format `app/config.py` parses.
#
#   ./tools/gen-secrets.sh            # print them
#   ./tools/gen-secrets.sh >> .env    # append to an env file you are assembling
#
# Nothing here talks to the server or reads existing values: it only prints fresh
# random material. Copy what you need.
#
# TOKEN_ENC_KEYS is the one that matters. It encrypts every portal's OAuth and
# application tokens (§3). Lose it and every installed portal must be re-authorised by
# its administrator opening the app again - the rows survive but nothing can decrypt
# them. Back it up somewhere other than the box it runs on, before the first install.

set -euo pipefail

if ! command -v openssl >/dev/null 2>&1; then
    echo "openssl is required" >&2
    exit 1
fi

key_id="${1:-1}"

cat <<EOF
# --- generated $(date -u +%Y-%m-%dT%H:%M:%SZ) by tools/gen-secrets.sh ---

# AES-256-GCM key ring for tokens at rest. Format: <id>:<base64 32 bytes>[,<id>:<b64>].
# To rotate: add a second entry with a new id, point TOKEN_ENC_ACTIVE_KEY_ID at it, and
# keep the old entry until the background re-encrypt has run. Never remove a key whose
# ciphertext still exists.
TOKEN_ENC_KEYS=${key_id}:$(openssl rand -base64 32)
TOKEN_ENC_ACTIVE_KEY_ID=${key_id}

# HS256 signing key for the session and playback tokens (§4.6). Rotating it invalidates
# every open session; users recover by reopening the app from Bitrix24.
SESSION_SECRET=$(openssl rand -base64 48 | tr -d '\n=' | tr '+/' '-_')

# PostgreSQL. The two role passwords must match the ones embedded in DATABASE_URL and
# DATABASE_URL_MIGRATIONS; init.sql reads them on first boot only, so changing them
# later means an ALTER ROLE as well as an edit here.
POSTGRES_PASSWORD=$(openssl rand -base64 24 | tr -d '\n=' | tr '+/' '-_')
POSTGRES_CA_OWNER_PASSWORD=$(openssl rand -base64 24 | tr -d '\n=' | tr '+/' '-_')
POSTGRES_CA_APP_PASSWORD=$(openssl rand -base64 24 | tr -d '\n=' | tr '+/' '-_')
EOF

cat >&2 <<'EOF'

Reminder: after pasting the three POSTGRES_* values, rewrite DATABASE_URL and
DATABASE_URL_MIGRATIONS so their passwords match, or the stack starts and then fails
every query with an authentication error.
EOF
