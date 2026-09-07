# Call Analytics — Bitrix24 Marketplace app

`texnobus.callanalytics` — a single deployment that serves many Bitrix24 portals and shows
each of them their own telephony activity: a dashboard on the left-menu page and a call list
on the Deal / Lead / Contact / Company detail tabs.

The app is **not** a telephony provider. It registers no calls, uploads no recordings and
connects no PBX. It reads `voximplant.statistic.get` and presents it.

## Status

Design phase. No application code yet — the database schema is awaiting the owner's
confirmation before the sync layer is implemented.

## Documents

| Document | Contents |
|---|---|
| [docs/architecture.md](docs/architecture.md) | The design: key decisions, file structure, full PostgreSQL DDL, auth/request flows, sync design, logging, i18n, spike plan, milestone-1 build order |
| [docs/architecture-appendix.md](docs/architecture-appendix.md) | Assumptions, open questions for the owner, and the disposition of every review finding |
| [docs/bitrix24-api-research.md](docs/bitrix24-api-research.md) | Verified facts from the official Bitrix24 documentation, including where they contradict the original brief |
| [docs/design-review-findings.md](docs/design-review-findings.md) | The adversarial review that produced the final design: 38 issues with failure scenarios |

## Stack

Python 3.12 / FastAPI / SQLAlchemy 2 / Alembic / PostgreSQL 16 / httpx / APScheduler,
Next.js App Router + TypeScript + Tailwind, Docker Compose behind Caddy.
