#!/usr/bin/env bash
#
# The only thing the CI deploy key can run.
#
# It is installed on the production host as /usr/local/bin/callanalytics-deploy, root
# owned and root writable, and the key in /root/.ssh/authorized_keys is pinned to it:
#
#   command="/usr/local/bin/callanalytics-deploy",no-agent-forwarding,no-port-forwarding,
#   no-pty,no-user-rc,no-X11-forwarding ssh-ed25519 AAAA... github-actions-cd
#
# so the key gets no shell, no port forward and no pty. Whatever the client asks for
# arrives as $SSH_ORIGINAL_COMMAND and anything not matched below is refused.
#
#   deploy <40-hex-sha>   pull that tag, migrate, restart, verify, record it
#   rollback              go back to the previously recorded tag; no build, no migration
#   status                what is running, and what rollback would go back to
#
# **Why it lives OUTSIDE /opt/callanalytics.** `deploy` checks the working tree out at
# the requested commit. If this script lived in that tree, a deploy would rewrite the
# very thing the forced command points at, and the key's restriction would only hold
# until the first deployment. The canonical copy is `deploy/ci-deploy.sh` in the
# repository so it can be reviewed in a pull request; installing a new version on the
# host is a deliberate, manual, root action. Those two facts are the whole design.
#
# **What it deliberately does not do.** It never builds. Images come from GHCR already
# built and scanned; the host is shared production with five other projects and two
# cores, and a build there is felt by all of them.
set -euo pipefail
umask 022

APP_DIR=/opt/callanalytics
STATE_DIR=/var/lib/callanalytics
REGISTRY=ghcr.io/blogchik/
COMPOSE=(-f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.tunnel.yml)

log()    { printf '[%s] %s\n' "$(date -Is)" "$*"; }
die()    { printf 'error: %s\n' "$*" >&2; exit 1; }
refuse() { printf 'refused: %s\n' "$*" >&2; exit 64; }

dc() { docker compose "${COMPOSE[@]}" "$@"; }

# --- parse, without a shell ------------------------------------------------------------
# Deliberately not `eval` and not `bash -c`: the request is split on whitespace and each
# field is validated against a pattern. An argument that is not 40 lowercase hex
# characters never reaches a command line.
read -r -a request <<<"${SSH_ORIGINAL_COMMAND:-}"
action="${request[0]:-}"
argument="${request[1]:-}"
[ "${#request[@]}" -le 2 ] || refuse "too many arguments"

is_sha() { [[ "$1" =~ ^[0-9a-f]{40}$ ]]; }

mkdir -p "$STATE_DIR"
current_file="$STATE_DIR/current"
previous_file="$STATE_DIR/previous"

# --- the one deployment routine --------------------------------------------------------
apply() {
  local sha="$1" reason="$2"
  is_sha "$sha" || die "not a commit sha: $sha"

  cd "$APP_DIR"

  log "$reason $sha"

  # The tree is still needed even though nothing is built from it: the compose files,
  # the Caddyfile and the alembic revisions all come from the checkout.
  git fetch --quiet --prune origin
  git cat-file -e "${sha}^{commit}" 2>/dev/null || die "commit $sha is not in this repository"
  git checkout --quiet --detach "$sha"

  export IMAGE_REGISTRY="$REGISTRY"
  export IMAGE_TAG="$sha"

  # Only the two images that change. caddy and cloudflared are pinned upstream images
  # and pulling them would restart the ingress for no reason - the tunnel stays up
  # across an app deployment precisely because nothing here touches it.
  log 'pulling api and web'
  dc pull --quiet api web worker

  # Before the new code runs, never after: a migration is additive by convention, so old
  # code tolerates a new column and new code does not tolerate a missing one.
  log 'applying migrations'
  dc run --rm -T api python -m alembic upgrade head

  log 'starting'
  dc up -d --wait

  # Inside first, so a failure says where it is. If this passes and the public URL does
  # not, the fault is the tunnel or the zone, not the stack.
  log 'verifying from inside the network'
  dc exec -T web wget -qO- http://api:8000/healthz | grep -q '"status":"ok"' \
    || die 'the api container is up but not healthy'
  dc exec -T web wget -qO- http://caddy/healthz | grep -q '"status":"ok"' \
    || die 'the api is healthy but our caddy does not reach it'

  log 'deployed'
}

case "$action" in
  deploy)
    is_sha "$argument" || refuse "deploy needs a 40-character commit sha"
    # Recorded only after the new one is verified, so a failed deploy leaves the
    # rollback target pointing at the last version that actually worked.
    previous="$(cat "$current_file" 2>/dev/null || true)"
    apply "$argument" 'deploying'
    [ -n "$previous" ] && printf '%s\n' "$previous" > "$previous_file"
    printf '%s\n' "$argument" > "$current_file"
    ;;

  rollback)
    [ -z "$argument" ] || refuse "rollback takes no argument"
    target="$(cat "$previous_file" 2>/dev/null || true)"
    [ -n "$target" ] || die 'nothing to roll back to: no previous deployment recorded'
    apply "$target" 'rolling back to'
    # The two swap. Rolling back twice returns to where you started rather than walking
    # backwards through history one deployment at a time, which is almost never what
    # somebody typing `rollback` a second time in an incident actually wants.
    printf '%s\n' "$(cat "$current_file" 2>/dev/null || true)" > "$previous_file"
    printf '%s\n' "$target" > "$current_file"
    ;;

  status)
    [ -z "$argument" ] || refuse "status takes no argument"
    cd "$APP_DIR"
    echo "current:  $(cat "$current_file" 2>/dev/null || echo 'unrecorded')"
    echo "previous: $(cat "$previous_file" 2>/dev/null || echo 'none')"
    echo "checkout: $(git rev-parse HEAD)"
    echo
    IMAGE_REGISTRY="$REGISTRY" IMAGE_TAG="$(cat "$current_file" 2>/dev/null || echo local)" \
      dc ps --format '{{.Service}}\t{{.Image}}\t{{.Status}}'
    ;;

  *)
    refuse "expected 'deploy <sha>', 'rollback' or 'status'; got '${SSH_ORIGINAL_COMMAND:-}'"
    ;;
esac
