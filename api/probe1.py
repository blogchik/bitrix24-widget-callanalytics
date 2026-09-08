"""Ad-hoc probe: seed a portal with known calls, compare load_dashboard against SQL."""
import asyncio, datetime as dt, uuid, json
from sqlalchemy import text
from app.db.session import control_txn, tenant_txn
from app.security.principal import Principal
from app.services.stats import load_dashboard, parse_filters, load_filter_facets
from starlette.datastructures import QueryParams

TZ = "Asia/Tashkent"  # UTC+5, no DST

async def seed():
    member_id = uuid.uuid4().hex
    domain = f"probe{member_id[:8]}.bitrix24.test"
    async with control_txn() as s:
        pid = int((await s.execute(text("""
            INSERT INTO portals (member_id, domain, client_endpoint, lang, timezone)
            VALUES (:m,:d,:c,'en',:tz) RETURNING id"""),
            {"m": member_id, "d": domain, "c": f"https://{domain}/rest/", "tz": TZ})).scalar_one())
        await s.execute(text("INSERT INTO portal_sync (portal_id) VALUES (:p)"), {"p": pid})
    return pid, member_id

async def main():
    pid, mid = await seed()
    print("portal", pid)
    # Calls: craft UTC instants that land on specific local days in Asia/Tashkent (UTC+5)
    rows = [
        # (bx_id, utc_iso, duration, failed_code, user)
        (1, "2026-03-09T19:30:00Z", 100, "200", 11),   # local 2026-03-10 00:30
        (2, "2026-03-10T18:59:00Z", 200, "200", 11),   # local 2026-03-10 23:59
        (3, "2026-03-10T19:00:00Z", 300, "304", 12),   # local 2026-03-11 00:00
        (4, "2026-03-11T05:00:00Z",   0, "603", None), # local 2026-03-11 10:00, unassigned
        (5, "2026-03-08T19:30:00Z",  50, "200", 12),   # local 2026-03-09 00:30 (previous window)
    ]
    async with tenant_txn(pid) as s:
        for bx, ts, dur, code, uid in rows:
            await s.execute(text("""
                INSERT INTO calls (portal_id,bx_id,call_id,call_type,call_start_date,
                                   call_duration,call_failed_code,portal_user_id,phone_number)
                VALUES (:p,:b,:cid,1,:ts,:d,:c,:u,'+998900000000')"""),
                {"p": pid, "b": bx, "cid": f"c{bx}", "ts": dt.datetime.fromisoformat(ts.replace("Z","+00:00")), "d": dur, "c": code, "u": uid})
        await s.execute(text("""INSERT INTO employees (portal_id,bx_user_id,name,last_name,active,found,fetched_at)
            VALUES (:p,11,'Ann','Lee',true,true,now()),(:p,12,'Bob','Kim',true,true,now())"""), {"p": pid})

    p = Principal(portal_id=pid, member_id=mid, user_id=11, is_admin=True, access="all",
                  timezone=TZ, lang="en", placement="DEFAULT", entity=None, issued_at=0)
    f = parse_filters(QueryParams("period=custom&from=2026-03-10&to=2026-03-11"), p)
    print("filters:", f.date_from, f.date_to, f.start_utc, f.end_utc, f.previous_start_utc)
    d = await load_dashboard(p, f)
    print(json.dumps({k: d[k] for k in ("range","summary","per_day","per_employee")}, indent=1, default=str))
    print("heat non-zero:", [(c["weekday"], c["hour"], c["count"]) for c in d["hour_weekday"] if c["count"]])
    # ground truth
    async with tenant_txn(pid) as s:
        gt = (await s.execute(text("""
            SELECT (timezone(:tz, call_start_date))::date AS d, count(*), 
                   sum(call_duration) FILTER (WHERE result_group='answered') AS talk
            FROM calls WHERE portal_id=:p GROUP BY 1 ORDER BY 1"""), {"p": pid, "tz": TZ})).all()
        print("ground truth per local day:", gt)
    return pid

asyncio.run(main())
