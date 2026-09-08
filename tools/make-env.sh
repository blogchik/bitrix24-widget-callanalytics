#!/usr/bin/env bash
# Assemble a working .env from .env.example plus freshly generated secrets.
#
#   ./tools/make-env.sh              # writes ./.env, refuses to clobber an existing one
#   ./tools/make-env.sh --force      # overwrite (rotates every secret - see the warning)
#   ./tools/make-env.sh --ci         # non-interactive: placeholder vendor credentials
#
# What it fills in: the encryption key ring, the session secret, the three PostgreSQL
# passwords, and the two DATABASE_URLs that must carry the same passwords. What it
# cannot fill in: B24_CLIENT_ID and B24_CLIENT_SECRET, which come from the Bitrix24
# vendor cabinet, and APP_BASE_URL. Those are left as loud placeholders and the script
# tells you so at the end.
#
# --force rotates TOKEN_ENC_KEYS, which makes every stored portal token undecryptable.
# On a live deployment that means every portal must be re-authorised. Do not use it to
# "refresh" an existing .env.

set -euo pipefail

cd "$(dirname "$0")/.."

FORCE=0
CI=0
for arg in "$@"; do
    case "$arg" in
        --force) FORCE=1 ;;
        --ci) CI=1 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

if [ -f .env ] && [ "$FORCE" -eq 0 ]; then
    cat >&2 <<'EOF'
.env already exists and was not touched.

If you meant to rotate its secrets, read the warning at the top of this script first:
regenerating TOKEN_ENC_KEYS orphans every token already stored, and every portal will
have to be re-authorised by an administrator reopening the app.
EOF
    exit 1
fi

command -v openssl >/dev/null 2>&1 || { echo "openssl is required" >&2; exit 1; }

enc_key="$(openssl rand -base64 32)"
session_secret="$(openssl rand -base64 48 | tr -d '\n=' | tr '+/' '-_')"
su_pw="$(openssl rand -base64 24 | tr -d '\n=' | tr '+/' '-_')"
owner_pw="$(openssl rand -base64 24 | tr -d '\n=' | tr '+/' '-_')"
app_pw="$(openssl rand -base64 24 | tr -d '\n=' | tr '+/' '-_')"

if [ "$CI" -eq 1 ]; then
    client_id="local.ci.00000000"
    client_secret="ci-not-a-real-secret"
    base_url="https://localhost"
else
    client_id="REPLACE-with-the-vendor-cabinet-client-id"
    client_secret="REPLACE-with-the-vendor-cabinet-client-secret"
    base_url="https://b24.texnobus.uz"
fi

# Rewrite only the keys we own; every other line, and every comment, survives from
# .env.example so a new setting added there is not silently dropped here.
python3 - "$enc_key" "$session_secret" "$su_pw" "$owner_pw" "$app_pw" \
         "$client_id" "$client_secret" "$base_url" <<'PY'
import io
import re
import sys

enc, session, su, owner, app, cid, csecret, base = sys.argv[1:9]

values = {
    "TOKEN_ENC_KEYS": f"1:{enc}",
    "TOKEN_ENC_ACTIVE_KEY_ID": "1",
    "SESSION_SECRET": session,
    "POSTGRES_PASSWORD": su,
    "POSTGRES_CA_OWNER_PASSWORD": owner,
    "POSTGRES_CA_APP_PASSWORD": app,
    "DATABASE_URL": f"postgresql+asyncpg://ca_app:{app}@postgres:5432/callanalytics",
    "DATABASE_URL_MIGRATIONS": f"postgresql+psycopg://ca_owner:{owner}@postgres:5432/callanalytics",
    "B24_CLIENT_ID": cid,
    "B24_CLIENT_SECRET": csecret,
    "APP_BASE_URL": base,
}

out = []
seen = set()
for line in io.open(".env.example", encoding="utf-8").read().splitlines():
    match = re.match(r"^([A-Z0-9_]+)=", line)
    if match and match.group(1) in values:
        name = match.group(1)
        out.append(f"{name}={values[name]}")
        seen.add(name)
    else:
        out.append(line)

missing = [k for k in values if k not in seen]
if missing:
    out.append("")
    out.append("# Added by tools/make-env.sh: not present in .env.example.")
    out.extend(f"{k}={values[k]}" for k in missing)

io.open(".env", "w", encoding="utf-8").write("\n".join(out) + "\n")
PY

chmod 600 .env
echo "wrote .env"

if [ "$CI" -eq 0 ]; then
    cat >&2 <<'EOF'

Two values are still placeholders and the app will not start until you set them:

  B24_CLIENT_ID       from the Bitrix24 vendor cabinet
  B24_CLIENT_SECRET   from the Bitrix24 vendor cabinet

And one thing to do before the first portal installs:

  Copy TOKEN_ENC_KEYS somewhere other than this machine. It decrypts every stored
  portal token; a database backup without it restores rows nothing can read.
EOF
fi
