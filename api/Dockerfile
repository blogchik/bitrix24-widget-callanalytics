# One image for both the `api` and the `worker` service (docs/architecture.md §2).
# CMD is uvicorn; docker-compose.yml overrides it with `python -m app.worker`.
FROM python:3.12-slim AS runtime

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
COPY pyproject.toml ./
RUN pip install --no-cache-dir ".[dev]"

# ---- source layer -----------------------------------------------------------
COPY --chown=app:app . .

USER app

EXPOSE 8000

# uvicorn inserts --app-dir (default ".") at the front of sys.path, so `app.main`
# resolves from /app rather than from site-packages - which is what makes the dev
# bind mount in docker-compose.dev.yml hot-reload the real sources.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
