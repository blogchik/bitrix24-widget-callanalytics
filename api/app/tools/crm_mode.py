"""`python -m app.tools.crm_mode` - see and move a portal's CRM mirror mode (§4.14).

    python -m app.tools.crm_mode status [PORTAL_ID]
    python -m app.tools.crm_mode set PORTAL_ID {off,sync,shadow,mirror}

An operator's tool, not an administrator's switch. `off` here stops the worker's CRM requests
for one portal and keeps its rows; the administrator's "turn CRM analytics off"
(`POST /api/v1/portal/crm-analytics`) is the privacy switch, and it deletes them. For the same
reason this refuses to move a portal whose administrator turned CRM analytics off anywhere but
`off`: that decision is theirs.

The fleet-wide stop is not here either - it is one UPDATE of `app_flags.crm_mirror`.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import func, select

from app.db.engine import dispose_engine
from app.db.models import AppFlag, CrmLane, Portal
from app.db.session import control_txn
from app.services.portals import CRM_MODES, set_crm_mode


async def _status(portal_id: int | None) -> int:
    async with control_txn() as session:
        enabled = (
            await session.execute(select(AppFlag.enabled).where(AppFlag.name == "crm_mirror"))
        ).scalar_one_or_none()
        query = (
            select(
                Portal.id,
                Portal.domain,
                Portal.status,
                Portal.crm_mode,
                Portal.crm_opt_out_at,
                Portal.crm_purge_pending,
                func.count(CrmLane.lane).filter(CrmLane.status == "done").label("done"),
                func.count(CrmLane.lane).label("lanes"),
            )
            .outerjoin(CrmLane, CrmLane.portal_id == Portal.id)
            .group_by(Portal.id)
            .order_by(Portal.id)
        )
        if portal_id is not None:
            query = query.where(Portal.id == portal_id)
        rows = (await session.execute(query)).all()

    print(f"crm_mirror flag: {'on' if enabled else 'OFF'}")
    for row in rows:
        opted_out = "opted out" if row.crm_opt_out_at is not None else ""
        purging = "purge pending" if row.crm_purge_pending else ""
        notes = ", ".join(note for note in (opted_out, purging) if note)
        print(
            f"{row.id:>6}  {row.domain:<40} {row.status:<12} {row.crm_mode:<7} "
            f"lanes done {row.done}/{row.lanes}  {notes}"
        )
    return 0


async def _set(portal_id: int, mode: str) -> int:
    try:
        async with control_txn() as session:
            previous = await set_crm_mode(session, portal_id, mode)
    except ValueError as exc:
        print(f"crm_mode: {exc}", file=sys.stderr)
        return 1
    if previous is None:
        print(f"crm_mode: no portal {portal_id}", file=sys.stderr)
        return 1
    print(f"portal {portal_id}: crm_mode {previous} -> {mode}")
    return 0


async def _run(args: argparse.Namespace) -> int:
    try:
        if args.command == "status":
            return await _status(args.portal_id)
        return await _set(args.portal_id, args.mode)
    finally:
        await dispose_engine()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.tools.crm_mode")
    commands = parser.add_subparsers(dest="command", required=True)
    status = commands.add_parser("status", help="modes, opt-outs and lane progress")
    status.add_argument("portal_id", nargs="?", type=int)
    move = commands.add_parser("set", help="move one portal's crm_mode")
    move.add_argument("portal_id", type=int)
    move.add_argument("mode", choices=CRM_MODES)
    sys.exit(asyncio.run(_run(parser.parse_args())))


if __name__ == "__main__":
    main()
