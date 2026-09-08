# One image for both the `api` and the `worker` service (docs/architecture.md §2).
# CMD is uvicorn; docker-compose.yml overrides it with `python -m app.worker`.
FROM python:3.14-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Unprivileged runtime user; /app is its home so pip/pytest caches never land in /root.
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app \
 && install -d -o app -g app /app

WORKDIR /app

# ---- dependency layer -------------------------------------------------------
# Only pyproject.toml, so editing a source file does not re-resolve the wheels.
# Every dependency ships a manylinux wheel (asyncpg, psycopg[binary], cryptography,
# uvloop/httptools), so no compiler is installed in the image.
# The [dev] extra is included because `make test` and `make lint` run pytest, ruff
# and mypy inside this same image.
COPY api/pyproject.toml ./
RUN pip install --no-cache-dir ".[dev]"

# ---- shared i18n bundle -----------------------------------------------------
# docs/architecture.md §8: web/messages/*.json and web/src/i18n/locales.json are the
# SINGLE source of locales and strings, shared by the Next.js app and the API's
# server-rendered install/state/error pages. The build context is the repo root so
# this one bundle can be copied into both images; app/i18n.py reads /app/i18n/.
COPY --chown=app:app web/messages/ ./i18n/messages/
COPY --chown=app:app web/src/i18n/locales.json ./i18n/locales.json

# The ingress snippet is copied in only so tests/test_routing.py can assert which
# upstream each path resolves to. Caddy's `path` matcher is exact, and a single
# stray token in it once sent the SPA's /settings page to the API, which answered
# a raw JSON error inside the Bitrix24 iframe (a moderation rejection under §4.11).
# The file is inert at runtime; the host Caddy is configured from the repo copy.
COPY --chown=app:app docker/Caddyfile.snippet /docker/Caddyfile.snippet

# ---- source layer -----------------------------------------------------------
COPY --chown=app:app api/ .

USER app

EXPOSE 8000

# uvicorn inserts --app-dir (default ".") at the front of sys.path, so `app.main`
# resolves from /app rather than from site-packages - which is what makes the dev
# bind mount in docker-compose.dev.yml hot-reload the real sources.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
