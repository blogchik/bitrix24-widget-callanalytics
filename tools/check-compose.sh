#!/usr/bin/env bash
#
# The production compose stack has properties that are load-bearing rather than tidy, and
# they are the kind that break silently. This asserts them against the merged
# configuration Compose actually produces, not against the files by eye.
#
#   ./tools/check-compose.sh
#
# Requires a `.env` (any values; `tools/make-env.sh --ci` writes a throwaway one) because
# `env_file:` makes Compose refuse to render a config without it.
#
# What each check is protecting, and what broke to earn it:
#
#  1. **Nothing is published.** The app is reachable only through its own Cloudflare
#     Tunnel; the base file publishes 127.0.0.1:8000 and :3000 for local work and
#     `ports` MERGES BY APPEND, so the production override cannot drop them by restating
#     the key - it needs `!reset []`. That was a deployment blocker once already: the
#     override looked correct and the loopback binds survived it.
#  2. **The source tree is never mounted over an image.** `docker-compose.dev.yml` mounts
#     `./web` at `/app` and runs `next dev`; layering it on a server would serve an
#     unbuilt frontend out of whatever the checkout happens to contain. Config files
#     mounted read-only (the Caddyfile, `init.sql`) are expected and are not this.
#  3. **The images are tagged, not `:local`.** `IMAGE_TAG` unset resolves to `:local`,
#     which does not exist on the host, and the error names a missing image rather than
#     the missing variable. A rollback is a tag change, so the tag has to be real.
#  4. **The path split still splits.** `/settings` must reach `web` and `/api/` must
#     reach `api`; routing `/settings` to the API returned a raw JSON error inside the
#     iframe once, which is a moderation failure rather than a bug report.
set -euo pipefail

cd "$(dirname "$0")/.."

# Values only need to exist: this renders the config, it does not run anything.
export TUNNEL_TOKEN="${TUNNEL_TOKEN:-render-only}"
export IMAGE_TAG="${IMAGE_TAG:-0000000}"
export IMAGE_REGISTRY="${IMAGE_REGISTRY:-ghcr.io/example/}"

PROD=(-f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.tunnel.yml)

fail() { echo "FAIL: $*" >&2; exit 1; }

# Checks 1-3 read the rendered document rather than grepping YAML: `ports` and `volumes`
# each have three surface forms, and a grep that matches one of them is a check that
# passes for the wrong reason.
docker compose "${PROD[@]}" config --format json | python3 -c '
import json, os, sys

doc = json.load(sys.stdin)
problems = []

for name, svc in sorted(doc.get("services", {}).items()):
    # 1
    for port in svc.get("ports", []) or []:
        published = port.get("published") if isinstance(port, dict) else port
        if published:
            problems.append(
                f"{name} publishes {published} - the production stack must publish nothing "
                f"(see docker-compose.prod.yml: ports: !reset [])"
            )
    # 2
    for vol in svc.get("volumes", []) or []:
        if not isinstance(vol, dict) or vol.get("type") != "bind":
            continue
        target = vol.get("target", "")
        source = vol.get("source", "?")
        if target == "/app" or target.startswith("/app/"):
            problems.append(
                f"{name} bind-mounts {source} at {target} - the source tree is over the "
                f"image, so a dev override has leaked into the production stack"
            )
    # 3
    image = svc.get("image", "")
    if name in ("api", "worker", "web"):
        want = ":" + os.environ["IMAGE_TAG"]
        if not image:
            problems.append(f"{name} has no image")
        elif not image.endswith(want):
            problems.append(
                f"{name} resolves to {image}, not to {want} - either IMAGE_TAG did not "
                f"reach the shell (it falls back to :local) or an override pinned a tag "
                f"of its own, and a rollback that changes IMAGE_TAG would move nothing"
            )
        else:
            print(f"ok: {name} -> {image}")

if problems:
    print("", file=sys.stderr)
    for p in problems:
        print(f"FAIL: {p}", file=sys.stderr)
    sys.exit(1)

print("ok: no service publishes a port")
print("ok: no service has the source tree mounted over its image")
'

# 4 -----------------------------------------------------------------------------------
# The routing lives in docker/Caddyfile.tunnel. api/tests/test_routing.py parses the
# shared snippet in depth; this is the cheap version that runs without the test image,
# so a hand-edit to the tunnel Caddyfile fails here in seconds rather than in production.
caddyfile=docker/Caddyfile.tunnel
grep -q 'reverse_proxy[[:space:]]\+web:3000' "$caddyfile" \
  || fail "$caddyfile no longer proxies anything to web:3000"
grep -q 'reverse_proxy[[:space:]]\+api:8000' "$caddyfile" \
  || fail "$caddyfile no longer proxies anything to api:8000"
grep -qE '^[[:space:]]*:80[[:space:]]*\{' "$caddyfile" \
  || fail "$caddyfile must use the ':80' site address; a hostname turns Caddy's automatic HTTPS back on"
echo "ok: ingress still splits api and web, and Caddy still has no hostname to certify"

echo "compose invariants hold"
