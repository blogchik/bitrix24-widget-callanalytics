"""Seed one portal with real call rows and print a session token for a live smoke test."""

import asyncio
import datetime as dt
import random
import sys

sys.path.insert(0, "/app")

from sqlalchemy import text  # noqa: E402

from app.db.session import control_txn, tenant_txn  # noqa: E402
from app.security.session_token import issue_session  # noqa: E402

MEMBER_ID = "5c0de5m0ke5c0de5m0ke5c0de5m0ke11"
USERS = [(101, "Азиз", "Каримов"), (102, "Дилноза", "Юсупова"), (103, "Тимур", "Сафаров")]
CODES = ["200", "200", "200", "304", "486", "603"]


async def main() -> None:
    async with control_txn() as session:
        await session.execute(
            text("DELETE FROM portals WHERE member_id = :m"), {"m": MEMBER_ID}
        )
        portal_id = int(
            (
                await session.execute(
                    text(
                        "INSERT INTO portals (member_id, domain, client_endpoint, status, "
                        "timezone, lang, token_status, install_completed_at) "
                        "VALUES (:m, 'smoke.bitrix24.kz', "
                        "'https://smoke.bitrix24.kz/rest/', 'active', 'Asia/Tashkent', 'ru', "
                        "'ok', now()) RETURNING id"
                    ),
                    {"m": MEMBER_ID},
                )
            ).scalar_one()
        )
        await session.execute(
            text(
                "INSERT INTO portal_sync (portal_id, high_id, low_id, backfill_status, "
                "backfill_total, backfill_done) VALUES (:p, 900, 1, 'done', 900, 900)"
            ),
            {"p": portal_id},
        )

    rng = random.Random(7)
    now = dt.datetime.now(dt.UTC)
    rows = []
    for i in range(1, 901):
        started = now - dt.timedelta(days=rng.randint(0, 29), hours=rng.randint(8, 19),
                                     minutes=rng.randint(0, 59))
        code = CODES[rng.randrange(len(CODES))]
        rows.append(
            {
                "p": portal_id,
                "bx": i,
                "t": rng.choice([1, 2]),
                "d": started,
                "dur": rng.randint(15, 480) if code == "200" else 0,
                "c": code,
                "u": USERS[rng.randrange(len(USERS))][0],
                "ph": f"+9989{rng.randint(10_000_000, 99_999_999)}",
                "app": rng.choice([None, None, 41]),
            }
        )

    async with tenant_txn(portal_id) as session:
        for user_id, name, last in USERS:
            await session.execute(
                text(
                    "INSERT INTO employees (portal_id, bx_user_id, name, last_name, active, "
                    "found, fetched_at) VALUES (:p, :u, :n, :l, true, true, now())"
                ),
                {"p": portal_id, "u": user_id, "n": name, "l": last},
            )
        await session.execute(
            text(
                "INSERT INTO calls (portal_id, bx_id, call_type, call_start_date, "
                "call_duration, call_failed_code, portal_user_id, phone_number, rest_app_id, "
                "rest_app_name) VALUES (:p, :bx, :t, :d, :dur, :c, :u, :ph, :app, "
                "CASE WHEN :app IS NULL THEN NULL ELSE 'SIP-линия' END)"
            ),
            rows,
        )

    token = issue_session(
        pid=portal_id,
        mid=MEMBER_ID,
        sub=101,
        adm=True,
        acc="all",
        tz="Asia/Tashkent",
        lang="ru",
        plc="DEFAULT",
        ent=None,
        ttl_seconds=3600,
    )
    print(f"PORTAL_ID={portal_id}")
    print(f"TOKEN={token}")


asyncio.run(main())
