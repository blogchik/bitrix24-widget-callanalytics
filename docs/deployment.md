# Deployment

How to put Call Analytics on `b24.texnobus.uz` and keep it there. Written to be followed
top to bottom the first time and used as a reference afterwards.

Read [architecture.md](architecture.md) §2 and §4.10 first if you want to know why the
routing looks the way it does; this document assumes the design and only tells you what
to run.

**This runbook is written for one specific machine.** `b24.texnobus.uz` runs on
`89.167.6.101` (hostname `karimov-academy`), which is *shared production*: five other
projects live on it and none of them may be disturbed. Several instructions below exist
only because of that, and they are marked. Read [The host you are standing
on](#the-host-you-are-standing-on) before you type anything. A dedicated host is a
different and easier deployment; where the two diverge the dedicated-host form is in
[Appendix: a dedicated host](#appendix-a-dedicated-host) rather than inline, so nobody
follows the wrong one by accident.

---

## The host you are standing on

| Fact | Consequence for you |
| --- | --- |
| 2 CPU cores, 3.7 GiB RAM (~2.2 GiB available), 4 GiB swap | A build competes with five live projects; see [step 5](#5-build-the-images) |
| 38 G disk, 34% used, ~24 GiB free | A build costs 2–4 GiB. Filling `/` takes down four PostgreSQL instances at once |
| 20 containers in five compose projects, under `/opt` | `docker system prune -a` here is destructive; see [The neighbours](#the-neighbours) |
| **`make` is not installed** | Every command below is raw `docker compose`. Do not translate them back into `make` |
| `karimov-academy-caddy` publishes 80 and 443 — the only listener on the box | We publish nothing, and we never touch its config |
| Four projects already reach the internet over their own Cloudflare Tunnel | So do we. It is the house style here and it needs no ports |
| Docker Engine 29.7.2, Compose v5.5.0 | `ports: !reset []` in `docker-compose.prod.yml` needs Compose ≥ 2.24, so this is fine |

### Our topology

```text
   Cloudflare edge ── TLS, WAF, and the DNS for b24.texnobus.uz
          │
          │  outbound-only tunnel; no inbound port anywhere on this box
          ▼
   cloudflared ──── our container, our .env, our TUNNEL_TOKEN
          │
          │  http://caddy:80        (the compose network of project `callanalytics`)
          ▼
        caddy ──── our container, docker/Caddyfile.tunnel, plain HTTP, no ACME
          ├──▶ api:8000    /install/ /app/ /settings/ /events/ /api/* /healthz
          └──▶ web:3000    everything else
```

Four properties hold this together, and each one was a deployment blocker before it did:

1. **No service publishes a port.** `docker-compose.prod.yml` clears the loopback
   publications the base file declares (`ports: !reset []`). Nothing reaches us from
   outside; cloudflared dials *out*.
2. **Cloudflare terminates TLS and our Caddy never tries to.** `docker/Caddyfile.tunnel`
   uses the site address `:80` with no hostname, which is what keeps Caddy's automatic
   HTTPS switched off. Give that block a hostname and Caddy starts an ACME order for a
   name whose A record belongs to Cloudflare, fails the HTTP-01 challenge forever, and
   installs a `:80` → `:443` redirect that the tunnel follows straight back into itself.
3. **The path split is ours, inside our project.** The tunnel's dashboard route is a
   single catch-all to `http://caddy:80`; our Caddy decides what is `api` and what is
   `web`. No part of our routing lives in someone else's file, and none of it can be
   changed from the Cloudflare dashboard by a person who does not know the
   trailing-slash rule.
4. **Nothing outside `/opt/callanalytics` is touched.** In particular
   `/opt/karimov-academy/caddy/Caddyfile` is *not* ours. It is the config of the
   container that owns 80/443 and it also serves that project's media host; editing it
   and reloading would put someone else's sites at risk for our benefit. There is no
   host Caddy on this machine and we are not introducing one.

### The neighbours

You are root on a machine other people's production depends on. What else is here:

| Compose project | What it is | Notes |
| --- | --- | --- |
| `karimov-academy` | Frontend, backend, an Instagram token refresher, **and the Caddy that owns 80/443** | Its Caddyfile also serves a media host. Never edit, never reload |
| `kc-analytics` | App, worker, PostgreSQL, Redis, cloudflared | |
| `kc-homepage` | App, cloudflared | |
| `karimov-capital-mycrmstats` | App, sync worker, export worker, PostgreSQL, cloudflared; lives in `/opt/mycrmstats` | Its **PostgreSQL has restart policy `no`** — see below |
| `workly` | API, worker, PostgreSQL, cloudflared | |

Three things about that list matter more than the rest.

- **There are no database backups on this host at all.** `/var/backups` holds dpkg and
  apt metadata and nothing else, the root crontab is empty, and no project keeps a dump
  anywhere on this disk. Our `tools/backup.sh` covers **our** database and only ours. If
  you break a neighbour's PostgreSQL there is nothing to restore it from — so treat the
  shared resources (the disk, memory, the Docker daemon) as things whose failure is
  unrecoverable for somebody else.
- **`karimov-capital-mycrmstats-postgres-1` has restart policy `no`.** Every other
  container on the box is `unless-stopped`. If that daemon is OOM-killed, or the Docker
  daemon is restarted under it, it stays down until a human notices — and nobody is
  watching for it. It is the most expensive thing you can knock over here.
- **Never run `docker system prune -a` on this host.** It deletes every image no
  container references, and several of those are project images built here that exist in
  no registry; rebuilding one means finding the right source revision on someone else's
  schedule. At the time of writing there are five (`kc-analytics-api`,
  `kc-analytics-migrate`, `karimov-capital-mycrmstats-migrate`,
  `karimov-capital-mycrmstats:09cfcd890cc7`,
  `karimov-academy-instagram-token-refresher`). List them yourself before you believe
  any tool that offers to reclaim space:

  ```bash
  for i in $(docker images -q | sort -u); do
    printf '%s  %s\n' "$(docker ps -a --filter ancestor=$i -q | wc -l)" \
                      "$(docker inspect -f '{{join .RepoTags ","}}' $i)"
  done | sort            # a leading 0 means `system prune -a` would delete it
  ```

  The safe reclaim is `docker builder prune -af`, which touches build cache only. See
  [Reclaiming disk](#reclaiming-disk).

---

## What you need before you start

| Thing | Why |
| --- | --- |
| Root on `89.167.6.101`, with Docker Engine and the compose plugin | Already there; nothing to install |
| A Cloudflare Tunnel **token** for a remotely-managed tunnel in the `texnobus.uz` account | The connector runs as `cloudflared tunnel --no-autoupdate run --token <TUNNEL_TOKEN>`, so the token is the whole credential |
| `b24.texnobus.uz` configured as a **public hostname of that tunnel** | Not an A record to this box. The tunnel owns the name; see [step 8](#8-two-things-only-you-can-do-in-cloudflare) |
| `B24_CLIENT_ID` and `B24_CLIENT_SECRET` from the Bitrix24 vendor cabinet | The app cannot exchange a token without them |
| Somewhere off this machine to store one secret | See [Back up the key ring](#3-back-up-the-key-ring-before-the-first-install) |
| ~6 GiB free on `/`, and an hour when the box is quiet | The build is the only heavy thing we ever do here |

What you explicitly do **not** need, and must not arrange: ports 80/443, a DNS A record
pointing at this host, a host Caddy, a Let's Encrypt certificate, or any change to
another project's files.

The database stores call metadata only, never audio: a portal with 500,000 calls costs on
the order of 200 MB.

---

## First deploy

### 0. The command prefix, and the two ways to get it wrong

There is no `make` on this host. Every command is `docker compose` with all three files
named explicitly, in this order:

```bash
cd /opt/callanalytics
export IMAGE_TAG=$(git rev-parse --short HEAD)

docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.tunnel.yml \
  ps
```

- `docker-compose.yml` — the four services.
- `docker-compose.prod.yml` — tagged images, healthchecks, log caps, memory and CPU
  limits sized for *this* box, and `ports: !reset []`, which is what removes the base
  file's loopback publications.
- `docker-compose.tunnel.yml` — `caddy` and `cloudflared`, our ingress.

Two failure modes are worth spelling out, because both are silent.

- **A bare `docker compose <anything>` in this directory is not the production stack.**
  With no `-f` flags Compose reads `docker-compose.yml` alone: no memory limits, no
  healthchecks, no ingress, `:local` image tags, and the loopback ports the production
  override exists to remove. If a `docker-compose.override.yml` ever appears beside it,
  Compose silently layers that too. Never omit the flags — and **never pass
  `-f docker-compose.dev.yml` on a server**: the dev override bind-mounts the working
  tree over the image and runs `next dev`, so you would be serving an unbuilt,
  watch-mode frontend out of whatever the checkout happens to contain, with the source
  tree live under it.
- **`IMAGE_TAG` must be set in the shell you are typing in.** `docker-compose.prod.yml`
  pins `callanalytics-api:${IMAGE_TAG:-local}`; a shell that has lost the export
  resolves to `:local`, which does not exist on this host, and the error names a missing
  image rather than the missing variable. Re-export it after every new SSH session,
  `su`, or tmux pane.

If you are typing a lot, define the prefix once per shell — but keep the full form in
anything you paste into a ticket, a cron entry or a runbook, so the next reader can see
which files are in play:

```bash
dc() { docker compose -f docker-compose.yml -f docker-compose.prod.yml \
                      -f docker-compose.tunnel.yml "$@"; }
```

### 1. Get the code onto the host

```bash
git clone https://github.com/blogchik/bitrix24-widget-callanalytics /opt/callanalytics
cd /opt/callanalytics
```

`/opt/callanalytics` and the compose project name `callanalytics` are fixed: every other
project here follows the same convention and the container names read from it.

### 2. Build the environment file

```bash
./tools/make-env.sh
```

This writes `.env` with a fresh encryption key ring, a session secret and three
PostgreSQL passwords, and rewrites both `DATABASE_URL`s so their passwords match. It
leaves the values only you have as loud placeholders:

```bash
$EDITOR .env      # set B24_CLIENT_ID, B24_CLIENT_SECRET and TUNNEL_TOKEN
```

`TUNNEL_TOKEN` is the credential of the remotely-managed tunnel;
`docker-compose.tunnel.yml` hands it to `cloudflared tunnel --no-autoupdate run --token
…`. Anyone holding it can publish traffic as `b24.texnobus.uz`, so it belongs in the
same place as the client secret — and never on a command line, where it would land in
`~/.bash_history`.

Confirm `APP_BASE_URL=https://b24.texnobus.uz`. The app builds the placement handler URLs
from it, so a wrong value binds every portal's CRM tabs to the wrong address. It is
`https://` even though our Caddy speaks plain HTTP: the scheme describes what the
browser sees, which is Cloudflare's TLS.

### 3. Back up the key ring before the first install

`TOKEN_ENC_KEYS` decrypts every portal's OAuth and application tokens. A database backup
without it restores rows that nothing can read, and every portal would have to be
re-authorised by an administrator opening the app again.

```bash
grep '^TOKEN_ENC_KEYS=' .env      # copy this into your password manager, now
```

Do this before the first portal installs, not after. On this host it carries extra
weight: there is nothing else here to fall back on.

### 4. Check there is room, and pick the hour

**Host-specific; do not skip.** A build writes two images plus BuildKit cache — 2 to
4 GiB — into `/var/lib/docker`, on the same 38 G filesystem as everything else on the
box, including four PostgreSQL data directories. A PostgreSQL that cannot write its WAL
does not degrade, it stops. Filling `/` here takes down four databases at once, one of
which will not come back on its own.

```bash
df -h /                      # want ≥ 6 GiB free before you start
docker system df             # where it went, if you do not have it
```

If you need room, reclaim build cache — never images:

```bash
docker builder prune -af     # safe: cache only; costs neighbours rebuild time, nothing else
```

That one command is what took this host from 83% used to 34%. It is the routine reclaim
here, and it is worth running *before* a build as well as after.

### 5. Build the images

**Host-specific.** Two cores are shared with five live projects, and `next build` is the
memory-hungry step of the whole deployment. If the box runs out of memory during it the
kernel's OOM killer picks a victim across *every* cgroup on the machine, not just ours,
and the cheapest-looking victim may be a neighbour's PostgreSQL — one of which
(`karimov-capital-mycrmstats-postgres-1`) has restart policy `no` and would stay down
until somebody noticed. Our own memory caps protect the box from a leak in our running
containers; they do not cover a build, which is the daemon's work, not ours.

So: build at a quiet hour, and build one service at a time.

```bash
export IMAGE_TAG=$(git rev-parse --short HEAD)

nice -n 19 ionice -c3 docker compose \
  -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.tunnel.yml \
  build api

nice -n 19 ionice -c3 docker compose \
  -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.tunnel.yml \
  build web
```

Naming the service is the part that matters: `build` with no argument builds services in
parallel, which is exactly the both-cores-at-once situation to avoid. `nice`/`ionice`
lower the priority of the client and of host-side work only — the compilation itself runs
inside the Docker daemon, which does not inherit them — so treat the quiet hour and the
one-at-a-time rule as the real protection and the prefix as a courtesy.

Watch it from a second session while it runs, and stop the build rather than find out
afterwards:

```bash
free -h                                  # swap climbing steadily is the warning sign
docker stats --no-stream --format '{{.Name}}\t{{.MemUsage}}'
dmesg -T | tail                          # "Out of memory: Killed process" names the victim
```

Then give the disk back:

```bash
docker builder prune -af
df -h /
```

### 6. Start the stack

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.tunnel.yml \
  up -d --wait
```

`--wait` blocks until every healthcheck is green, so when it returns the API is answering
and the database is reachable. If it times out:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.tunnel.yml \
  logs --tail=200
```

Then confirm we published nothing. This must list the neighbour's Caddy and nothing of
ours:

```bash
docker ps --format '{{.Names}}\t{{.Ports}}' | grep -E '0\.0\.0\.0|::'
```

### 7. Apply the schema

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.tunnel.yml \
  run --rm api alembic upgrade head
```

The roles `ca_owner` and `ca_app` were created by `docker/postgres/init.sql` on the
database's first boot. Migrations run as `ca_owner`; the application runs as `ca_app`,
which cannot bypass row-level security. Verify both:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.tunnel.yml \
  exec -T postgres psql -U postgres -tAc \
  "SELECT rolname, rolbypassrls FROM pg_roles WHERE rolname LIKE 'ca_%';"
# ca_owner | f
# ca_app   | f
```

### 8. Two things only you can do, in Cloudflare

Neither can be done from the server, and both fail *silently*: the stack looks healthy
and the install simply does not work.

**a. The public hostname must point at `http://caddy:80` — HTTP, not HTTPS.**

In Zero Trust → Networks → Tunnels → *our tunnel* → Public Hostname:

| Field | Value |
| --- | --- |
| Subdomain / Domain | `b24` / `texnobus.uz` |
| Path | *empty* — one catch-all rule; the path split is our Caddy's job |
| Type | **HTTP** |
| URL | `caddy:80` |

`caddy` is the service name on our compose network, which is where cloudflared runs.
Choosing **HTTPS** is the mistake to expect: our Caddy has no certificate and no TLS
listener by design (see [Our topology](#our-topology)), so cloudflared gets a connection
error and the edge answers 502 for a stack that is entirely healthy. Do not switch on
"No TLS Verify" to make an HTTPS setting work — fix the scheme.

Adding path rules in the dashboard is the other tempting mistake. The trailing-slash
split between `/settings/` (the Bitrix24 handler, FastAPI) and `/settings` (the SPA page)
is documented, tested and enforced in `docker/Caddyfile.tunnel`; a second, undocumented
copy of that rule in a web UI is how it drifts.

**b. Bot Fight Mode and the WAF must not challenge the Bitrix24 handler paths.**

`/install/`, `/app/`, `/settings/` and `/events/` are POSTed to by Bitrix24 — some by the
portal's own servers, some by an auto-submitting iframe form that carries no cookies, no
referrer and runs no JavaScript. Every one of those traits is what a bot heuristic exists
to catch. A challenge or interstitial answers with an HTML page where Bitrix24 expected
our response, so the portal reports nothing useful and the install never completes.

In the zone, add a WAF **Skip** rule (Security → WAF → Custom rules) matching

```text
http.request.uri.path in {"/install/" "/app/" "/settings/" "/events/"}
```

and skip Super Bot Fight Mode, the Browser Integrity Check and the managed rulesets for
it. Check Security → Bots as well: Bot Fight Mode is zone-wide and on some plans is not
covered by a skip rule at all — if it cannot be excepted, it has to be off.

**c. Nothing may add `X-Frame-Options` at the edge.**

The app renders inside a Bitrix24 iframe. `X-Frame-Options` has no origin granularity, so
any value breaks every portal; framing is controlled exclusively by the per-request
`Content-Security-Policy: frame-ancestors` the app emits from the validated `DOMAIN`. Our
Caddy strips the header, but it can only strip what an *upstream* sets — a Cloudflare
Transform Rule or a "security headers" managed rule adds it **after** us, and the origin
has no way to remove it. If `curl -sSI https://b24.texnobus.uz/` shows the header, it is
coming from the edge and it must be turned off there.

### 9. Smoke-test before you tell Bitrix24 about it

Inside first, so that a failure tells you *where* it is:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.tunnel.yml \
  exec -T web wget -qO- http://api:8000/healthz        # {"status":"ok"} — the app is up
docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.tunnel.yml \
  exec -T web wget -qO- http://caddy/healthz           # same answer — our routing is right
```

If those two pass and the public URL does not, the fault is in the tunnel or the zone,
not in the stack. Then, from anywhere:

```bash
curl -sS https://b24.texnobus.uz/healthz                  # {"status":"ok"}
curl -sS -o /dev/null -w '%{http_code}\n' \
     https://b24.texnobus.uz/                              # 200, the SPA
curl -sSI https://b24.texnobus.uz/ | grep -i x-frame       # must print nothing
curl -sS "https://b24.texnobus.uz/settings?DOMAIN=x.bitrix24.kz&PROTOCOL=1" \
  | head -c 40                                             # HTML, never JSON
```

The last one is the check worth keeping: it is the exact path a moderator walks, and
routing it to the wrong upstream produced a raw error inside the iframe once already.

One more, specific to sitting behind Cloudflare — a challenge page on a handler path is
step 8b, not a bug in here:

```bash
curl -sS -X POST -o /tmp/install.out -w '%{http_code}\n' https://b24.texnobus.uz/install/
grep -qi 'cloudflare\|cf-chl\|challenge' /tmp/install.out && echo 'WAF is in the way'
```

### 10. Point the vendor cabinet at it

Handler `https://b24.texnobus.uz/app/`, install `https://b24.texnobus.uz/install/`,
settings `https://b24.texnobus.uz/settings/`, and — if the cabinet offers the field —
the event handler `https://b24.texnobus.uz/events/`.

Then install on a development portal and walk
[the moderation checklist](moderation-checklist.md) end to end.

---

## Upgrading

**Normally you do not.** [`.github/workflows/cd.yml`](../.github/workflows/cd.yml) does
this, and it is the only path anything should take:

```
merge to main -> CD builds both images on the runner
              -> pushes them to ghcr.io/blogchik/callanalytics-{api,web}:<full-sha>
              -> Trivy scans them
              -> the `production` environment waits for a human to approve
              -> ssh <host> "deploy <sha>"   (the key can run nothing else)
              -> smoke test from outside, through Cloudflare
              -> on failure: ssh <host> "rollback"
```

Watch it in the repository's **Actions** tab. Approving the deployment is a button on the
run; declining it costs nothing, because the images are already published and a later run
can deploy the same sha.

The three commands the deploy key may run, which are also the three you can run yourself
from any machine holding a key that is *not* restricted:

| Command | What it does |
| --- | --- |
| `deploy <40-hex-sha>` | Fetch, check the tree out at that commit, pull those image tags, migrate, `up -d --wait`, verify from inside. Records the previous tag first. |
| `rollback` | Re-run the above against the previously recorded tag. No build, no migration. |
| `status` | What is running, what `rollback` would return to, and the checkout's HEAD. |

The script behind them is [`deploy/ci-deploy.sh`](../deploy/ci-deploy.sh) in this
repository, installed on the host as `/usr/local/bin/callanalytics-deploy`. **Editing the
repository copy does not change the host.** That is deliberate: a deployment checks the
tree out at the requested commit, so a script living inside `/opt/callanalytics` would
rewrite the very thing `authorized_keys` pins the key to. Updating it is a manual root
action:

```bash
scp deploy/ci-deploy.sh root@89.167.6.101:/tmp/cd.sh
ssh root@89.167.6.101 'install -m 755 -o root -g root /tmp/cd.sh /usr/local/bin/callanalytics-deploy && rm /tmp/cd.sh'
```

### Rolling back

```bash
ssh root@89.167.6.101 'callanalytics-deploy' <<< 'rollback'   # or, with the CI key:
ssh -i <cd-key> root@89.167.6.101 rollback
```

Images are tagged, so this is a tag change: no CPU, no disk, no risk to a neighbour. On
this box, reach for it before you reach for a rebuild.

A schema rollback is a different question and usually the wrong move. `downgrade()` exists
in every revision and CI proves it still runs, but executing it discards whatever the
newer columns hold. Prefer rolling the images back and leaving the schema forward.

---

## Break glass: deploying by hand

For when GitHub is down, the tunnel is down, or you are debugging the pipeline itself.
This is the old procedure and it still works, with one difference: **it builds on the
host**, which costs both cores and 2-4 GiB of disk while five other projects are serving.
Check the disk and pick a quiet hour.

```bash
cd /opt/callanalytics
git fetch && git checkout <tag-or-sha>
export IMAGE_TAG=$(git rev-parse --short HEAD)
unset IMAGE_REGISTRY                     # build locally, do not look for a GHCR tag

df -h /                                  # >= 6 GiB free, or prune the builder first

nice -n 19 ionice -c3 docker compose \
  -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.tunnel.yml build api
nice -n 19 ionice -c3 docker compose \
  -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.tunnel.yml build web

# migrations before the new code runs, so old code never sees a new schema
docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.tunnel.yml \
  run --rm api python -m alembic upgrade head

docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.tunnel.yml \
  up -d --wait                           # recreates only what changed, waits for health

docker builder prune -af                 # give the disk back
```

If you do this, **tell the pipeline afterwards** — write the sha into
`/var/lib/callanalytics/current` — or the next `rollback` will target whatever the last
CD run recorded rather than what is actually running:

```bash
git rev-parse HEAD > /var/lib/callanalytics/current
```

Two things make either path safe to run during the day:

- The worker holds a lease on each portal it is syncing and is given two minutes to
  finish. A visit cut short costs one re-fetched batch, never data: rows and cursor commit
  in the same transaction.
- The API is stateless. The session token is a signed JWT, so a restart does not log
  anyone out; the browser simply retries.

Run migrations **before** the new containers start. A migration is additive by convention,
so old code tolerates a new column; the reverse is not true.

`caddy` and `cloudflared` are untouched by an app upgrade — `up -d` recreates only the
services whose definition or image changed — so the tunnel never drops and the public
hostname never goes away mid-deploy.

## Reclaiming disk

**Host-specific, and the most dangerous routine task here.** `/` is 38 G and holds every
project on the box, including four PostgreSQL data directories.

| Command | Verdict |
| --- | --- |
| `docker builder prune -af` | **The routine reclaim.** BuildKit cache only. The cost to a neighbour is a slower next build and nothing else |
| `docker image prune` (no `-a`) | Fine. Dangling layers only |
| `docker system prune` (no `-a`, no `--volumes`) | Fine — stopped containers, unused networks, dangling images |
| `docker system prune -a` | **Never.** It deletes locally-built images that exist in no registry; see [The neighbours](#the-neighbours) |
| `docker volume prune` | **Never.** A neighbour's stopped-container volume is somebody's database |
| `docker network prune` | **Never.** It removes the network of any project that is scaled to zero |

Where the space actually is:

```bash
docker system df
du -sh /var/lib/docker/* 2>/dev/null | sort -h | tail
```

Our own growth is bounded and needs no attention: `docker-compose.prod.yml` caps every
service's logs at 50 MB × 5 files, and `rest_log` retention is seven days.

---

## Backups

```bash
./tools/backup.sh /var/backups/callanalytics
```

Add it to cron, daily, and keep the output somewhere other than this host:

```cron
17 3 * * * cd /opt/callanalytics && ./tools/backup.sh /var/backups/callanalytics >> /var/log/callanalytics-backup.log 2>&1
```

The odd hour is deliberate: off the hour, when the box is idle. The dump is small
(metadata only), but it is still disk on a shared filesystem, so copy it off and let the
script rotate what stays.

**This backs up our database and nothing else on this machine.** There are no backups of
the other five projects here, by anyone, and a green log from our script says nothing
about them. Do not read it as "the host is backed up".

The script verifies the dump is readable and actually contains the `portals` table before
it rotates old files, because a truncated backup that looks like a backup is worse than
none.

**A dump alone is not a recovery plan.** Restoring needs both the dump and the
`TOKEN_ENC_KEYS` value that was live when it was taken. Store them separately, and test
the restore once on a scratch host — not on this one:

```bash
gunzip -c callanalytics-<stamp>.sql.gz | docker compose \
  -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.tunnel.yml \
  exec -T postgres psql -U postgres -d callanalytics
```

What survives a restore and what does not:

- `portals`, `portal_sync` — irreplaceable. Tokens, cursors, placement state.
- `calls`, `employees`, `crm_contexts` — rebuildable. The worker re-fetches them from
  Bitrix24; the cost is a backfill, not data.
- `rest_log` — retention is seven days anyway.

---

## Operating it

The commands below only need to resolve running containers, so the file list matters less
here than it does for `up` and `build` — but keep passing all three anyway, so you are
never one habit away from a bare `docker compose` that means something else. They are
written with the `dc` helper from
[step 0](#0-the-command-prefix-and-the-two-ways-to-get-it-wrong) for readability.

### Where to look when a portal complains

Everything starts from the portal's row:

```bash
dc exec -T postgres psql -U ca_owner -d callanalytics -c \
  "SELECT id, domain, status, token_status, purge_pending FROM portals WHERE domain = 'acme.bitrix24.kz';"
```

`token_status` is the one column that explains why sync stopped:

| Value | Meaning | What fixes it |
| --- | --- | --- |
| `ok` | Syncing | — |
| `reauth_required` | The refresh chain is dead or was revoked | An administrator opens the app; it re-seeds automatically |
| `no_stats_permission` | The stored credential lost "Call statistics — view" | Restore the right in Bitrix24, then an administrator opens the app |
| `method_missing` | This build has no `voximplant.statistic.get` | On-premise upgrade; nothing we can do |
| `filter_unsupported` | The build ignores the `ID` filter operator | Reported so it parks instead of looping; needs investigation |

Then the lifecycle audit, which outlives the REST log's retention:

```bash
dc exec -T postgres psql -U ca_owner -d callanalytics -c \
  "SELECT created_at, kind, details FROM portal_events WHERE portal_id = 42 ORDER BY id DESC LIMIT 20;"
```

And the REST exchanges themselves, kept seven days with every credential redacted:

```bash
dc exec -T postgres psql -U ca_owner -d callanalytics -c \
  "SELECT ts, direction, kind, method, http_status, error_code FROM rest_log WHERE portal_id = 42 ORDER BY id DESC LIMIT 30;"
```

### Reading the container logs

One JSON object per line, correlated by `request_id`, with every token redacted before
serialization:

```bash
dc logs -f --tail=200                             # everything
dc logs -f worker | grep -v '"level": "DEBUG"'
dc logs api | grep '"request_id": "<id>"'
```

Two that exist only on this topology:

```bash
dc logs --tail=50 cloudflared    # four "Registered tunnel connection" lines is healthy
dc logs --tail=50 caddy          # a 502 here is api/web; a 502 at the edge is the tunnel
```

`cloudflared` reconnecting every few minutes, or logging `Unauthorized`, means the token
in `.env` was rotated or revoked in the dashboard — not a fault in our stack.

### Things that are working as designed

- **A portal shows "authorisation expired" right after you seed it by hand.** The worker
  found a portal with no credential and parked it correctly.
- **The first sync takes an hour or two on a busy portal.** The backfill is deliberately
  paced: every tenant shares one source IP against a 2 requests-per-second limit. The
  dashboard shows recent days within seconds and fills in history behind a progress
  banner.
- **`rejected_rows` is non-zero.** Rows Bitrix24 returned that could not be parsed were
  quarantined so one bad row cannot stall the cursor. A non-zero count is worth
  reporting to us, not worth an incident.
- **Our Caddy only ever sees cloudflared's address.** It is the sole client on the
  compose network; the caller's real address arrives in the forwarded headers.

### Things that are not

- **`purge_pending` set for more than a few hours.** Look for `purge_incomplete` in
  `portal_events`.
- **`consecutive_failures` climbing past 10.** The portal is paused for six hours;
  `last_error_code` says why.
- **The worker container restarting.** It should run for weeks. Check its logs for an
  unhandled exception rather than restarting it. On this host also check
  `dmesg -T | tail`: an exit code of 137 is the kernel's OOM killer, which means the
  whole box was short of memory and a neighbour may have been hit in the same event.

The worker deliberately has no healthcheck: it is a scheduler with nothing to serve, and
a probe that only proved the process exists would be worse than none. To see whether it
is actually working, look at what it writes — `next_run_at` moves on every visit:

```bash
dc exec -T postgres psql -U ca_owner -d callanalytics -c   "SELECT portal_id, next_run_at, lease_owner, last_error_code FROM portal_sync ORDER BY next_run_at LIMIT 10;"
```

Every `next_run_at` in the past on an active portal with no lease owner, for more than a
minute, means the tick is not running.

### Keeping an eye on the box, not just the app

Cheap, read-only, and worth doing whenever you are logged in anyway:

```bash
df -h /                                           # the shared cliff edge
free -h                                           # swap in use is normal; swap growing is not
docker stats --no-stream --format '{{.Name}}\t{{.MemUsage}}\t{{.CPUPerc}}'
docker ps --filter status=exited                  # a neighbour that fell over
```

Our four containers should sit far below their caps — 384M postgres, 384M api, 320M
worker, 256M web, 1344M in total against roughly 2.2 GiB available. A container running
at its ceiling is a leak to investigate, not a limit to raise: the caps are sized so that
if we leak, the kernel kills *us* instead of choosing a victim elsewhere on the machine.

---

## Configuration reference

Every setting is an environment variable, documented inline in
[`.env.example`](../.env.example). The ones worth knowing on day one:

| Variable | Default | Notes |
| --- | --- | --- |
| `RECORDING_MODE` | `off` | `off` until the [playback spike](spike-recording-playback.md) is answered. `redirect` is refused at startup. |
| `REST_LOG_RETENTION_DAYS` | `7` | Bitrix24 moderation requires at least 3; the app refuses to start below that. |
| `GLOBAL_PORTAL_CONCURRENCY` | `4` | Portals synced in parallel. Raising it does not raise the per-portal rate limit. |
| `SYNC_INTERVAL_SEC` | `300` | How often a healthy portal is revisited. |
| `UNINSTALL_GRACE_DAYS` | `30` | A portal whose token chain died and which no administrator has opened is treated as uninstalled and purged. |
| `API_WORKERS` | `2` | uvicorn processes. The worker container is always exactly one. On this two-core host, raising it takes CPU from a neighbour — and PostgreSQL's `max_connections` was derived from this value. |
| `LOG_LEVEL` | `INFO` | `DEBUG` is noisy but never prints a token. |
| `TUNNEL_TOKEN` | — | The Cloudflare Tunnel credential. Rotating it in the dashboard means editing `.env` and recreating `cloudflared`. |

Changing any of them means editing `.env` and running

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.tunnel.yml \
  up -d --wait
```

which recreates the containers whose environment changed.

---

## Appendix: a dedicated host

Everything above assumes the shared box. On a host we own outright — 80 and 443 free,
`make` installed, no neighbours — the deployment is the older and simpler one:

- Use `docker/Caddyfile.snippet` instead of the tunnel. It is a full site block for
  `b24.texnobus.uz` with automatic HTTPS, and it expects Caddy on the host, outside
  compose. Paste it into `/etc/caddy/Caddyfile`, then
  `caddy validate --config /etc/caddy/Caddyfile` and `systemctl reload caddy`. Read its
  comments first: the trailing slash on `/settings/` is load-bearing, and
  `X-Frame-Options` must stay stripped or the app cannot render inside a Bitrix24 iframe
  at all. If Caddy runs on the host rather than on the compose network, change the two
  `reverse_proxy` lines from `api:8000` and `web:3000` to `127.0.0.1:8000` and
  `127.0.0.1:3000`, which is what the base compose file publishes.
- `b24.texnobus.uz` must resolve to that host and ports 80/443 must be reachable from
  the internet, or Let's Encrypt cannot validate.
- Do not layer `docker-compose.tunnel.yml`, and leave `TUNNEL_TOKEN` unset.
- The `Makefile` targets are written for exactly this case, and each honours `IMAGE_TAG`
  (defaulting to the short git sha):

  | Target | What it runs |
  | --- | --- |
  | `make prod-build` | `docker compose -f docker-compose.yml -f docker-compose.prod.yml build` |
  | `make prod-up` | `… up -d --wait` |
  | `make prod-migrate` | `… run --rm api alembic upgrade head` |
  | `make prod-logs` | `… logs -f --tail=200` |
  | `make prod-down` | `… down` |

**Do not run the `prod-*` targets on the shared host**, even after installing `make`.
They omit `-f docker-compose.tunnel.yml`, so Compose no longer knows that `caddy` and
`cloudflared` belong to the project: it reports them as orphan containers, and one
`--remove-orphans` — which plenty of tools and habits add by reflex — takes our ingress
down while the app keeps running and looking healthy.
