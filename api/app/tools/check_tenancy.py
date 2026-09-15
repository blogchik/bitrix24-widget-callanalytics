"""`python -m app.tools.check_tenancy` - CI's schema gate for the tenancy model (§3).

Connects as the runtime role, the one the application actually uses, and asserts that the
migrated catalog matches `app/db/tenancy.py`. Exits 1 and prints every problem on stderr,
so a migration that drops a policy is reported as that, not as a data leak later.
"""

from __future__ import annotations

import asyncio
import sys

from app.db.engine import dispose_engine, engine
from app.db.tenancy import TENANT_TABLES, check_tenancy


async def _run() -> int:
    try:
        async with engine.connect() as conn:
            problems = await check_tenancy(conn)
    finally:
        await dispose_engine()
    for problem in problems:
        print(f"tenancy: {problem}", file=sys.stderr)
    if problems:
        return 1
    print(f"tenancy ok: {len(TENANT_TABLES)} tenant tables, registered, forced and bound to app.portal_id")
    return 0


def main() -> None:
    sys.exit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
