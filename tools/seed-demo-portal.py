"""Seed one portal with real call rows and print a session token for a live smoke test."""

import asyncio
import datetime as dt
import random
import sys

sys.path.insert(0, "/app")

from sqlalchemy import text  # noqa: E402

from app.db.session import control_txn, tenant_txn  # noqa: E402
from app.security.session_token import issue_session  # noqa: E402

# 32 lowercase hex: portals_member_id_fmt rejects anything else (§3).
MEMBER_ID = "5c0de5000ce5c0de5000ce5c0de50011"
USERS = [(101, "Азиз", "Каримов"), (102, "Дилноза", "Юсупова"), (103, "Тимур", "Сафаров")]
CODES = ["200", "200", "200", "304", "486", "603"]

# The CRM tab is reached from a deal, a lead, a contact or a company card, so a portal with
# no CRM-linked calls can only ever render that tab's empty state. Roughly a third of the
# seeded calls are attached to one of four fixed cards, which is also what makes the call
# table's CRM column - hidden when every loaded row is empty for it - testable in both of
# its states rather than only the hidden one.
CRM_CARDS = [("DEAL", 4021), ("LEAD", 1187), ("CONTACT", 903), ("COMPANY", 55)]


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
        app_id = rng.choice([None, None, 41])
        card = rng.choice([None, None, *CRM_CARDS])
        # Only answered calls carry a recording, which is what a real provider does and what
        # keeps the "no recording" cell meaningful rather than uniform. The URL is a local
        # placeholder: `RECORDING_MODE` is `off` in every environment this script runs in, so
        # nothing ever fetches it, and it must not resemble a real provider link.
        recorded = code == "200" and rng.random() < 0.45
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
                "app": app_id,
                "appname": None if app_id is None else "SIP-линия",
                "ctype": None if card is None else card[0],
                "cid": None if card is None else card[1],
                "cact": None if card is None else 900_000 + i,
                "rurl": f"https://example.invalid/demo-recording/{i}.mp3" if recorded else None,
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
                # asyncpg cannot infer a type for a parameter used only inside CASE,
                # so the nullable integer is cast explicitly and the name passed in.
                "rest_app_name, crm_entity_type, crm_entity_id, crm_activity_id, "
                "call_record_url) VALUES (:p, :bx, :t, :d, :dur, :c, :u, :ph, "
                "cast(:app as integer), :appname, :ctype, cast(:cid as integer), "
                "cast(:cact as bigint), :rurl)"
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
    # The CRM tab reads its entity from the JWT, never from the query string (§4.4 step 5),
    # so a token minted for the left-menu placement cannot open it. This second one can.
    crm_token = issue_session(
        pid=portal_id,
        mid=MEMBER_ID,
        sub=101,
        adm=True,
        acc="all",
        tz="Asia/Tashkent",
        lang="ru",
        plc="CRM_DEAL_DETAIL_TAB",
        ent={"t": CRM_CARDS[0][0], "id": CRM_CARDS[0][1]},
        ttl_seconds=3600,
    )

    # Written AFTER the token is minted, and that order is the whole point: §4.8 serves a
    # CRM tab only from a context resolved no earlier than the JWT was, so a row inserted
    # first is one the API correctly refuses with 409 `context_missing`. Seeding it in the
    # other order produced a tab that could never render its table.
    #
    # `entity_keys` and `activity_ids` stay empty on purpose. The match clause also tests
    # the raw `(ent.t, ent.id)` pair, which is what the seeded calls carry, so an empty
    # cache row exercises the path a portal that emits DEAL in its statistics rows takes.
    async with tenant_txn(portal_id) as session:
        for entity_type, entity_id in CRM_CARDS:
            await session.execute(
                text(
                    "INSERT INTO crm_contexts (portal_id, entity_type, entity_id, "
                    "resolved_by_user_id, resolved_at) VALUES (:p, :t, :i, 101, now())"
                ),
                {"p": portal_id, "t": entity_type, "i": entity_id},
            )

    print(f"PORTAL_ID={portal_id}")
    print(f"TOKEN={token}")
    print(f"CRM_TOKEN={crm_token}")


asyncio.run(main())
