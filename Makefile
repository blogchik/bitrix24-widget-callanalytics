# Call Analytics - developer shortcuts (GNU make).
# Every target is a single docker compose command; nothing runs on the host.
# Dev overrides (bind mounts, --reload) are layered by DEV_FILES.

COMPOSE ?= docker compose
FILES   := -f docker-compose.yml -f docker-compose.dev.yml
DC      := $(COMPOSE) $(FILES)

.PHONY: up down build logs migrate revision test lint fmt psql shell

## Start postgres, api, worker and web in the background (dev overrides on).
up:
	$(DC) up -d

## Stop and remove the containers; the postgres volume survives.
down:
	$(DC) down

## Rebuild both images (api/worker share one, web has its own).
build:
	$(DC) build

## Follow the logs of every service.
logs:
	$(DC) logs -f --tail=200

## Apply all Alembic migrations as ca_owner (DATABASE_URL_MIGRATIONS).
migrate:
	$(DC) run --rm api alembic upgrade head

## Autogenerate a revision: make revision m="add widget table"
revision:
	$(DC) run --rm api alembic revision --autogenerate -m "$(m)"

## Run pytest in the dev-only `test` service against the same postgres.
test:
	@# The worker leases due portals every 15 s from the same database the tests use,
	@# so a live worker steals the lease a fencing test is about to take and the suite
	@# fails for a reason that has nothing to do with the code under test.
	-$(DC) stop worker
	$(DC) --profile test run --rm test
	-$(DC) start worker

## Static checks: ruff lint + mypy.
lint:
	$(DC) run --rm api sh -c "ruff check . && mypy app"

## Format the Python sources in place (ruff format + import fixes).
fmt:
	$(DC) run --rm api sh -c "ruff format . && ruff check --fix ."

## Interactive psql as the object owner.
psql:
	$(DC) exec postgres psql -U ca_owner -d callanalytics

## Shell inside a fresh api container (non-root).
shell:
	$(DC) run --rm api bash
