"""sync_method_budgets - operating-time state per (portal, Bitrix24 method) (§5.6).

WHY this table exists: Bitrix24 accounts operating time per METHOD per application, and
`time.operating` is that method's own running accumulator (docs/spike-crm-mirror.md,
S-A.6). `portal_sync.operating_*` is one pair of columns per portal, so the worker folded
every method it called into it: a slow `user.get` could park the portal's call sync, and a
statistics soft limit stopped the employee refresh and the daily admin check, which spend a
different budget entirely. The CRM mirror adds `crm.item.list` and friends, whose budgets are
shared with the live deal and UTM reports, which makes the conflation a real outage.

One row per method the worker has observed, except `voximplant.statistic.get`, whose state
stays in `portal_sync` because that row also schedules the portal.

WHY it is control plane (no RLS): the rows hold numbers and timestamps about a portal's API
budget, never a Bitrix24 record, and the worker reads them before any tenant context exists.
`app/db/tenancy.py` lists it as such, and `python -m app.tools.check_tenancy` fails CI if it
ever grows RLS or loses its registration.

Revision ID: 0003_sync_method_budgets
Revises: 0002_employees_phone_inner
"""

from __future__ import annotations

from alembic import op

revision: str = "0003_sync_method_budgets"
down_revision: str | None = "0002_employees_phone_inner"
branch_labels: str | None = None
depends_on: str | None = None


CREATE_TABLE = """
CREATE TABLE sync_method_budgets (
    portal_id            bigint      NOT NULL REFERENCES portals(id) ON DELETE CASCADE,
    method               varchar(64) NOT NULL,
    operating_seconds    numeric(8,2),
    operating_reset_at   timestamptz,
    blocked_until        timestamptz,
    limit_s              numeric(8,2),
    throttle_hits        integer     NOT NULL DEFAULT 0,
    clean_visits         smallint    NOT NULL DEFAULT 0,
    updated_at           timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (portal_id, method)
);
CREATE TRIGGER sync_method_budgets_updated_at BEFORE UPDATE ON sync_method_budgets
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
"""

COMMENTS = """
COMMENT ON TABLE sync_method_budgets IS
    'Operating-time state per (portal, Bitrix24 method), for every method except '
    'voximplant.statistic.get (which stays in portal_sync). Numbers and timestamps only; no RLS.';
COMMENT ON COLUMN sync_method_budgets.method IS
    'Bitrix24 REST method name, as the worker called it.';
COMMENT ON COLUMN sync_method_budgets.operating_seconds IS
    'The method''s own time.operating accumulator as last observed - the budget used so far in '
    'its 10-minute window, not the cost of one call.';
COMMENT ON COLUMN sync_method_budgets.operating_reset_at IS
    'time.operating_reset_at as last observed.';
COMMENT ON COLUMN sync_method_budgets.blocked_until IS
    'The worker leaves this method alone until then: set by a soft-limit stop or an '
    'OPERATION_TIME_LIMIT on this method, never by another method''s limit.';
COMMENT ON COLUMN sync_method_budgets.limit_s IS
    'Learned operating limit in seconds; NULL = the default (480). Lowered only when a 429 is '
    'attributable to our own consumption, restored after CLEAN_VISITS_TO_RECOVER clean visits.';
COMMENT ON COLUMN sync_method_budgets.throttle_hits IS
    'Soft-limit stops and 429s on this method. Throttling is not failure: it never touches '
    'portal_sync.consecutive_failures.';
COMMENT ON COLUMN sync_method_budgets.clean_visits IS
    'Consecutive visits that used this method without a stop, towards restoring limit_s.';
"""

# Default privileges already cover a table ca_owner creates, but the baseline grants
# explicitly and a developer database migrated as another role would otherwise have none.
GRANTS = """
DO $do$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ca_app') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON sync_method_budgets TO ca_app';
    END IF;
END
$do$;
"""

DROP_TABLE = """
DROP TABLE IF EXISTS sync_method_budgets;
"""


def _run(sql: str) -> None:
    """Raw DDL through the driver, never `op.execute(str)` (see 0001/0002 for why)."""
    op.get_bind().exec_driver_sql(sql)


def upgrade() -> None:
    _run(CREATE_TABLE)
    _run(COMMENTS)
    _run(GRANTS)


def downgrade() -> None:
    """Drop the table. The trigger dies with it; nothing else references it."""
    _run(DROP_TABLE)
