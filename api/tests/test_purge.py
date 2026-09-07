"""§10 step 1 — lock in the purge trap the reviewers found, before `sync/purge.py` exists.

The trap: `calls` / `employees` / `crm_contexts` carry FORCED RLS and the runtime role
cannot bypass it, so a `DELETE` issued from a transaction with no `app.portal_id` matches
zero rows **and reports success**. A purge loop written as "delete a chunk until a chunk
comes back empty" therefore finishes instantly, clears `purge_pending` and logs
`purge_done` while every row of the uninstalled customer is still in the table — brief
rule 7 violated with the audit trail claiming otherwise (§5.9, design review MAJOR).

`purge_portal_data` below is a local stand-in for the milestone-4 implementation,
written to the §5.9 contract: pre-count, chunked delete, and a final emptiness check —
all in the same context. The tests pin the two properties that make it safe:
  * run it from `control_txn` and it deletes nothing, so nothing may be reported as done;
  * a positive pre-count followed by a zero-row first DELETE is `purge_incomplete`,
    never "already empty".
When the real `sync/purge.py` lands it must satisfy the same assertions.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import control_txn, tenant_txn
from tests.conftest import TENANT_TABLES, TwoPortals

pytestmark = pytest.mark.asyncio

#: A factory that opens ONE transaction. §5.9 requires a fresh one per chunk, because
#: `SET LOCAL app.portal_id` dies with the transaction that issued it.
TxnFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

_CHUNK = 1000


def tenant_ctx(portal_id: int) -> TxnFactory:
    return lambda: tenant_txn(portal_id)


def control_ctx(_portal_id: int) -> TxnFactory:
    return control_txn


@dataclass(frozen=True)
class PurgeOutcome:
    """What the milestone-4 job has to report. `incomplete` is the only safe failure."""

    pre_count: int
    deleted: int
    final_count: int
    incomplete: bool

    @property
    def verified_empty(self) -> bool:
        return self.final_count == 0


async def _count(ctx: TxnFactory, portal_id: int) -> int:
    total = 0
    async with ctx() as session:
        for table in TENANT_TABLES:
            total += int(
                (
                    await session.execute(
                        text(f"SELECT count(*) FROM {table} WHERE portal_id = :pid"),  # noqa: S608
                        {"pid": portal_id},
                    )
                ).scalar_one()
            )
    return total


async def purge_portal_data(
    portal_id: int,
    *,
    count_ctx: TxnFactory,
    delete_ctx: TxnFactory | None = None,
) -> PurgeOutcome:
    """§5.9 purge, chunk by chunk, each chunk in its own transaction.

    `count_ctx` / `delete_ctx` are separate only so a test can build the pathological
    mix (count where the rows are visible, delete where they are not). Production has
    exactly one context: `tenant_txn(portal_id)`.
    """
    deletes = delete_ctx or count_ctx
    pre_count = await _count(count_ctx, portal_id)

    deleted = 0
    first_delete_rows: int | None = None
    for table in TENANT_TABLES:
        while True:
            async with deletes() as session:
                # ctid keeps the chunking identical for the surrogate-key table and the
                # two composite-key ones; RLS applies to the sub-select as well.
                result = await session.execute(
                    text(  # noqa: S608 - table names come from a module constant
                        f"""
                        DELETE FROM {table}
                        WHERE ctid IN (
                            SELECT ctid FROM {table} WHERE portal_id = :pid LIMIT {_CHUNK}
                        )
                        """
                    ),
                    {"pid": portal_id},
                )
            rows = int(result.rowcount)
            if first_delete_rows is None:
                first_delete_rows = rows
            deleted += rows
            if rows == 0:
                break

    final_count = await _count(count_ctx, portal_id)
    incomplete = final_count != 0 or (pre_count > 0 and not first_delete_rows)
    return PurgeOutcome(
        pre_count=pre_count,
        deleted=deleted,
        final_count=final_count,
        incomplete=incomplete,
    )


async def _visible_under_tenant(portal_id: int) -> int:
    """The only honest count of a tenant's rows: taken under that tenant's context."""
    return await _count(tenant_ctx(portal_id), portal_id)


async def test_purge_from_control_txn_deletes_nothing(two_portals: TwoPortals) -> None:
    """The trap itself: from a control transaction the purge is a silent no-op."""
    a = two_portals.a
    before = await _visible_under_tenant(a.portal_id)
    assert before > 0

    outcome = await purge_portal_data(a.portal_id, count_ctx=control_ctx(a.portal_id))

    assert outcome.pre_count == 0, "control txn must not be able to see tenant rows"
    assert outcome.deleted == 0, "control txn must not be able to delete tenant rows"
    # And this is why the pre-count and the emptiness check are worthless from here:
    # every signal the job has says "done" while the data is untouched.
    assert outcome.verified_empty is True
    assert outcome.incomplete is False
    assert await _visible_under_tenant(a.portal_id) == before


async def test_purge_under_tenant_txn_empties_only_that_portal(two_portals: TwoPortals) -> None:
    """The correct shape: tenant context deletes the rows and can prove it."""
    a, b = two_portals.a, two_portals.b
    before_a = await _visible_under_tenant(a.portal_id)
    before_b = await _visible_under_tenant(b.portal_id)

    outcome = await purge_portal_data(a.portal_id, count_ctx=tenant_ctx(a.portal_id))

    assert outcome.pre_count == before_a
    assert outcome.deleted == before_a
    assert outcome.verified_empty is True
    assert outcome.incomplete is False
    assert await _visible_under_tenant(a.portal_id) == 0
    # One tenant's uninstall is not another tenant's data loss.
    assert await _visible_under_tenant(b.portal_id) == before_b


async def test_purge_reports_incomplete_when_rows_exist_but_deletes_match_nothing(
    two_portals: TwoPortals,
) -> None:
    """The assertion §5.9 demands: pre-count > 0 and a zero-row first DELETE is a failure.

    This is the shape of every real regression here — a job that keeps its counting
    query under tenant context but loses the context for the writes (a commit inside
    the loop, a helper that opens its own session).
    """
    a = two_portals.a
    before = await _visible_under_tenant(a.portal_id)
    assert before > 0

    outcome = await purge_portal_data(
        a.portal_id,
        count_ctx=tenant_ctx(a.portal_id),
        delete_ctx=control_ctx(a.portal_id),
    )

    assert outcome.pre_count == before
    assert outcome.deleted == 0
    assert outcome.verified_empty is False
    assert outcome.incomplete is True, "a purge that deleted nothing must never report success"
    assert await _visible_under_tenant(a.portal_id) == before


async def test_purge_is_idempotent_on_an_already_empty_portal(two_portals: TwoPortals) -> None:
    """A second visit after a real purge is a clean no-op, not a false `purge_incomplete`."""
    a = two_portals.a
    await purge_portal_data(a.portal_id, count_ctx=tenant_ctx(a.portal_id))

    outcome = await purge_portal_data(a.portal_id, count_ctx=tenant_ctx(a.portal_id))

    assert outcome.pre_count == 0
    assert outcome.deleted == 0
    assert outcome.verified_empty is True
    assert outcome.incomplete is False
