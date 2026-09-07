"""Every write to a `portals` row, in one module (§4.1, §4.3 step 4, §4.9).

WHY this module exists at all: §4.1 makes two invariants structural rather than
conventional, and both are only enforceable if there is exactly one writer.

* **Credential invariant** — `access_token_enc`, `refresh_token_enc` and
  `client_endpoint` may be written only after `user.current` + `user.admin=true`
  succeeded with that token at an OAuth-derived endpoint. `store_portal_credential()`
  is that single door; `tests/test_registry_lint.py` fails the build if any other
  module writes those three columns. The `admin.is_admin` check below is deliberate
  defence in depth: every caller already proved it, and the one that some day forgets
  must fail loudly here rather than silently seed a non-admin credential whose sync
  would quietly return a truncated slice of the portal's calls.
* **Endpoint invariant** — `client_endpoint` is taken from the `TokenResponse` and
  from nowhere else. There is no parameter here that could carry a `DOMAIN`-derived
  URL, which is what makes "never build a REST base from DOMAIN" checkable by reading
  one signature.

Everything here takes an `AsyncSession` from `control_txn()`: `portals`, `portal_sync`
and `portal_events` are control-plane tables with no RLS (§3), because the worker tick
and support must scan across portals and none of them holds call data.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import bindparam, select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func, text

from app.db.models import Portal, PortalEvent, PortalSync
from app.logging import get_logger
from app.security.crypto import DecryptionError, decrypt, encrypt

if TYPE_CHECKING:
    # Import-time cycle guard: `bitrix/oauth.py` reads and re-seeds credentials through
    # this module (§5.8), so importing it here for a type would be circular. These names
    # are used for annotations only - nothing at runtime depends on either module.
    from app.bitrix.identity import Identity
    from app.bitrix.oauth import TokenResponse

__all__ = [
    "CredentialWriteRefused",
    "InstallOutcome",
    "clear_credentials",
    "decrypt_portal_token",
    "get_portal_by_member_id",
    "mark_uninstalled",
    "record_event",
    "record_placements",
    "set_token_status",
    "store_portal_credential",
]

log = get_logger(__name__)

#: Same shape as `portals_member_id_fmt` (§3). Checked before the AAD is built: the
#: encryption binds each blob to `member_id:column`, so a malformed tenant key would
#: produce a row nobody can ever decrypt, and the DB CHECK would only catch it after
#: the ciphertext was already computed.
_MEMBER_ID_RE: Final = re.compile(r"^[0-9a-f]{32}$")

#: `portals_token_status_chk` (§3). Validated in Python so a bad value fails before the
#: statement runs, instead of aborting the whole install transaction on a constraint.
_TOKEN_STATUSES: Final[frozenset[str]] = frozenset(
    {"ok", "reauth_required", "no_stats_permission", "method_missing", "filter_unsupported"}
)

#: Columns that carry a ciphertext, keyed by the `column` name used as GCM AAD.
_ENCRYPTED_COLUMNS: Final[frozenset[str]] = frozenset(
    {"access_token", "refresh_token", "application_token"}
)

#: `next_run_at = 'infinity'` parks a portal forever (§5.8 terminal states). Python has
#: no infinite datetime, so the value is a SQL literal.
_INFINITY: Final = text("'infinity'::timestamptz")

#: A token is treated as already expired if the response gives us less than this, so a
#: clock skew of a few seconds can never hand the worker a dead access token (§5.8).
_EXPIRY_FLOOR_SECONDS: Final = 30


class CredentialWriteRefused(Exception):
    """A credential write that would break a §4.1 invariant was refused.

    Never rendered to a portal user: reaching it means a caller skipped the admin proof
    or passed a `TokenResponse` for a different tenant, i.e. a bug in our code, not a
    state a Bitrix24 portal can talk us into.
    """


@dataclass(frozen=True)
class InstallOutcome:
    """What §4.3 step 4 actually did, for the handler's logging and rendering."""

    portal_id: int
    member_id: str
    #: The `portals` row did not exist before this call.
    created: bool
    #: The row existed with `status='uninstalled'` - a genuine reinstall, not a re-run
    #: of the install URL after a version update (which is neither `created` nor this).
    reinstalled: bool
    token_status: str
    capabilities: dict[str, Any]


def _require_token_status(value: str) -> str:
    if value not in _TOKEN_STATUSES:
        raise CredentialWriteRefused(f"unknown token_status {value!r}")
    return value


def _expires_at(tokens: TokenResponse) -> dt.datetime:
    """Absolute expiry of the access token.

    Bitrix24 sends both `expires` (absolute, epoch seconds) and `expires_in` (relative).
    The absolute value is preferred but only when it is plausibly in the future: a
    portal whose clock is wrong, or a field we mis-parsed, must degrade to `expires_in`
    rather than persist an expiry in 1970 that makes every worker run refresh first.
    """
    now = dt.datetime.now(dt.UTC)
    if tokens.expires:
        try:
            absolute = dt.datetime.fromtimestamp(int(tokens.expires), tz=dt.UTC)
        except (OverflowError, OSError, ValueError):
            absolute = None
        if absolute is not None and absolute > now + dt.timedelta(seconds=_EXPIRY_FLOOR_SECONDS):
            return absolute
    seconds = max(int(tokens.expires_in or 0), _EXPIRY_FLOOR_SECONDS)
    return now + dt.timedelta(seconds=seconds)


def _cursor_reset_values() -> dict[str, Any]:
    """The `portal_sync` columns that describe *what has been imported* (§5.2).

    Reset only when there is provably nothing to keep: a new portal, or one that was
    uninstalled (its rows are purged, so the cursors describe a table that no longer
    has the data). §4.3 step 4 is explicit that a re-run of the install URL after a
    version update must NOT land here - re-importing 500k rows on every update would
    burn the portal's operating-time budget for nothing.
    """
    return {
        "high_id": 0,
        "low_id": None,
        "rescan_from_id": None,
        "backfill_status": "pending",
        "backfill_total": None,
        "backfill_done": 0,
        "backfill_started_at": None,
        "backfill_finished_at": None,
        "batch_pages": 20,
        "clean_visits": 0,
        "last_incremental_at": None,
        "last_rescan_at": None,
        "last_recheck_at": None,
        "last_employees_at": None,
        "last_appinfo_at": None,
        "operating_seconds": None,
        "operating_reset_at": None,
        "throttle_hits": 0,
        "consecutive_failures": 0,
        "rejected_rows": 0,
        "last_error_code": None,
        "last_error_text": None,
        "last_error_at": None,
    }


def _lease_release_values() -> dict[str, Any]:
    """Clear the lease and make the portal due now (§4.3 step 4, §5.9).

    `sync_generation` is bumped by the caller in the same statement: together these
    fence any run that is still in flight - its next cursor UPDATE carries the old
    generation, matches zero rows and aborts (§3 `sync_generation` comment).
    """
    return {
        "lease_owner": None,
        "lease_expires_at": None,
        "run_started_at": None,
        "next_run_at": func.now(),
    }


async def store_portal_credential(
    session: AsyncSession,
    *,
    member_id: str,
    tokens: TokenResponse,
    admin: Identity,
    application_token: str | None,
    domain: str,
    protocol_https: bool,
    lang: str | None,
    app_status: str | None,
    capabilities: dict[str, Any],
    token_status: str = "ok",  # noqa: S107 - a state enum (§3), not a credential
) -> InstallOutcome:
    """Upsert the tenant row and its credential. THE only writer of the token columns.

    §4.1 credential invariant + §4.3 step 4. Preconditions, all refused rather than
    logged-and-continued because each one silently corrupts a tenant:

    1. `admin.is_admin` must be true - the stored token is used by the worker for the
       whole portal, so a regular employee's token would cache a filtered subset of the
       calls and no one would ever see that it happened.
    2. `tokens.member_id` must equal `member_id` - it is the GCM AAD *and* the tenant
       key; a mismatch would write one portal's credential onto another's row.
    3. `tokens.client_endpoint` must be present - the endpoint invariant has no second
       source, and a row without a REST base is unusable by the worker.

    Cursors are reset only for a new or previously uninstalled portal (§4.3 step 4).
    """
    if not admin.is_admin:
        # Defence in depth: every caller checks this first (§4.3 step 3, §4.4 step 6,
        # §4.9 rule 4). This is the check that survives a caller being rewritten.
        raise CredentialWriteRefused("refusing to store a credential proven for a non-administrator")
    if not _MEMBER_ID_RE.fullmatch(member_id):
        raise CredentialWriteRefused("member_id is not 32 hex characters")
    if tokens.member_id != member_id:
        raise CredentialWriteRefused("token response belongs to a different member_id")
    client_endpoint = (tokens.client_endpoint or "").strip()
    if not client_endpoint:
        raise CredentialWriteRefused("token response carries no client_endpoint")
    _require_token_status(token_status)

    access_enc = encrypt(tokens.access_token, member_id=member_id, column="access_token")
    refresh_enc = encrypt(tokens.refresh_token, member_id=member_id, column="refresh_token")

    # Lock the row (if any) for the whole transaction and learn what we are about to
    # become: `created` / `reinstalled` decide both the cursor reset and the event kind.
    # Column-only select on purpose - loading the entity would leave a stale object in
    # the identity map that the caller could read back after this UPDATE.
    previous = (
        await session.execute(
            select(Portal.id, Portal.status).where(Portal.member_id == member_id).with_for_update()
        )
    ).one_or_none()
    created = previous is None
    reinstalled = previous is not None and previous.status == "uninstalled"

    values: dict[str, Any] = {
        "member_id": member_id,
        "domain": domain,
        "protocol_https": protocol_https,
        # §4.1 endpoint invariant: OAuth response only, never DOMAIN.
        "client_endpoint": client_endpoint,
        "status": "active",
        "app_status": app_status,
        "scope": tokens.scope or "",
        "capabilities": capabilities,
        "token_user_id": admin.user_id,
        "access_token_enc": access_enc,
        "refresh_token_enc": refresh_enc,
        "token_expires_at": _expires_at(tokens),
        "token_refreshed_at": func.now(),
        "token_admin_verified_at": func.now(),
        "token_status": token_status,
        "uninstalled_at": None,
        "install_completed_at": func.now(),
    }
    if tokens.server_endpoint:
        # Absent on some responses; keeping the stored value beats overwriting a known
        # good OAuth host with an empty string (§4.1 allowlist is applied on use).
        values["server_endpoint"] = tokens.server_endpoint
    if admin.timezone:
        values["timezone"] = admin.timezone
    if lang:
        values["lang"] = lang
    if application_token:
        values["application_token_enc"] = encrypt(
            application_token, member_id=member_id, column="application_token"
        )

    insert_values = {**values, "token_version": 1}
    # Never update the conflict key; `token_version` counts writes of the credential.
    update_values = {k: v for k, v in values.items() if k != "member_id"}
    update_values["token_version"] = Portal.token_version + 1

    portal_id = int(
        (
            await session.execute(
                pg_insert(Portal)
                .values(**insert_values)
                .on_conflict_do_update(index_elements=["member_id"], set_=update_values)
                .returning(Portal.id)
            )
        ).scalar_one()
    )

    sync_values: dict[str, Any] = {"portal_id": portal_id, **_lease_release_values()}
    sync_update: dict[str, Any] = {
        **_lease_release_values(),
        "sync_generation": PortalSync.sync_generation + 1,
    }
    if created or reinstalled:
        sync_values.update(_cursor_reset_values())
        sync_update.update(_cursor_reset_values())
    await session.execute(
        pg_insert(PortalSync)
        .values(sync_generation=1, **sync_values)
        .on_conflict_do_update(index_elements=["portal_id"], set_=sync_update)
    )

    kind = "install" if created else ("reinstall" if reinstalled else "app_update")
    await record_event(
        session,
        portal_id,
        kind,
        user_id=admin.user_id,
        details={
            "token_status": token_status,
            "cursors_reset": created or reinstalled,
            "app_status": app_status,
            "scope_count": len([s for s in (tokens.scope or "").split(",") if s]),
        },
    )
    return InstallOutcome(
        portal_id=portal_id,
        member_id=member_id,
        created=created,
        reinstalled=reinstalled,
        token_status=token_status,
        capabilities=capabilities,
    )


async def get_portal_by_member_id(session: AsyncSession, member_id: str) -> Portal | None:
    """The one lookup by tenant key (§4.3 step 1, §4.4 step 1, §4.9).

    `member_id` proves nothing on its own (§4.1) - it selects a row, it does not
    authorise anything - so every caller must still prove the payload before writing.
    """
    return (
        await session.execute(select(Portal).where(Portal.member_id == member_id))
    ).scalar_one_or_none()


async def record_event(
    session: AsyncSession,
    portal_id: int,
    kind: str,
    *,
    user_id: int | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Append one `portal_events` row - the lifecycle audit support greps first (§3).

    `details` NEVER contains a token, a secret or any credential material, and it is
    stored verbatim (no redaction pass): this table outlives `rest_log` retention, and
    a redactor here would also blank legitimate fields such as `token_status`. Callers
    put decisions and counts in `details`, never payloads.
    """
    session.add(
        PortalEvent(portal_id=portal_id, kind=kind, user_id=user_id, details=details or {})
    )


async def decrypt_portal_token(portal: Portal, column: str) -> str | None:
    """Decrypt one credential column, or return None after logging a warning.

    WHY it never raises: §5.9's tick walks every due portal, and one row whose
    ciphertext no longer opens (a retired key id, a restored backup, a truncated blob)
    must degrade to "this portal cannot sync now" rather than take the whole tick down
    with it. The caller sees None and records the portal's own failure state.
    """
    if column not in _ENCRYPTED_COLUMNS:
        raise ValueError(f"{column!r} is not an encrypted portals column")
    blob: bytes | None = getattr(portal, f"{column}_enc")
    if not blob:
        return None
    try:
        return decrypt(blob, member_id=portal.member_id, column=column)
    except DecryptionError:
        # No detail beyond the column: crypto.DecryptionError is deliberately opaque and
        # the plaintext must never appear anywhere near a log line (§6).
        log.warning(
            "portal credential could not be decrypted",
            extra={"portal_id": portal.id, "column": column},
        )
        return None


async def record_placements(
    session: AsyncSession,
    portal_id: int,
    *,
    placements: dict[str, Any] | None = None,
    capabilities_patch: dict[str, Any] | None = None,
) -> None:
    """Store the §4.3 step 5 bind results (`placements`, `capabilities.event_bind`).

    Separate from `store_portal_credential` because the binds happen *after* the
    credential is stored (the batch runs with the freshly proven token), and neither
    column is credential material. `capabilities_patch` is merged with `||` so the
    install-time probe results (`statistic_get`, `operating_limit_s`) survive a later
    re-bind from the settings page.
    """
    values: dict[str, Any] = {}
    if placements is not None:
        values["placements"] = placements
    if capabilities_patch:
        patch = bindparam("capabilities_patch", value=capabilities_patch, type_=postgresql.JSONB)
        values["capabilities"] = Portal.capabilities.op("||")(patch)
    if not values:
        return
    await session.execute(update(Portal).where(Portal.id == portal_id).values(**values))


async def set_token_status(
    session: AsyncSession,
    portal_id: int,
    token_status: str,
    *,
    block_sync: bool = False,
    last_error_code: str | None = None,
    last_error_text: str | None = None,
) -> None:
    """Move a portal into one of the §5.8 token states.

    `block_sync=True` is the terminal form: `next_run_at='infinity'` parks the portal
    until an admin open re-seeds the credential, instead of letting the tick retry a
    call that is guaranteed to fail (and burn operating time) every 15 seconds.
    """
    _require_token_status(token_status)
    await session.execute(
        update(Portal).where(Portal.id == portal_id).values(token_status=token_status)
    )
    sync_values: dict[str, Any] = {}
    if block_sync:
        sync_values["next_run_at"] = _INFINITY
    if last_error_code is not None:
        sync_values["last_error_code"] = last_error_code
        sync_values["last_error_text"] = last_error_text
        sync_values["last_error_at"] = func.now()
    if sync_values:
        await session.execute(
            update(PortalSync).where(PortalSync.portal_id == portal_id).values(**sync_values)
        )


async def clear_credentials(
    session: AsyncSession,
    portal_id: int,
    *,
    token_status: str = "reauth_required",  # noqa: S107 - a state enum, not a credential
) -> None:
    """Wipe the API tokens, keeping the tenant row (§4.9 rule 3, §5.8).

    `token_version` is bumped even though nothing was written: a single-flight refresher
    that is mid-`fn()` re-reads under `FOR UPDATE` and skips its refresh when the version
    moved (§5.8 step 3), so bumping here is what stops it from resurrecting the
    credential it captured a moment ago. `application_token_enc` is deliberately kept -
    §4.9 needs it to verify late or duplicated uninstall events.
    """
    _require_token_status(token_status)
    await session.execute(
        update(Portal)
        .where(Portal.id == portal_id)
        .values(
            access_token_enc=None,
            refresh_token_enc=None,
            token_status=token_status,
            token_version=Portal.token_version + 1,
        )
    )


async def mark_uninstalled(
    session: AsyncSession,
    portal_id: int,
    *,
    purge_bodies: bool = False,
    kind: str = "uninstall",
    user_id: int | None = None,
    details: dict[str, Any] | None = None,
) -> bool:
    """Apply the §4.9 rule 3 transition; returns False when it was already applied.

    Column effects, exactly as §4.9 lists them: `status='uninstalled'`,
    `uninstalled_at=now()`, both API tokens NULL, `token_status='reauth_required'`,
    `placements='{}'`, `purge_pending=true`, `purge_bodies` from `data[CLEAN]`, plus a
    `portal_sync` cursor reset with the lease cleared and `sync_generation+1` so any
    in-flight run is fenced. `application_token_enc` is kept.

    No REST call is made and none may be added: access is already revoked at uninstall,
    which is also why there is no `placement.unbind` anywhere in this codebase.

    `kind` names the audit event - `uninstall` for the real one, `uninstall_inferred`
    for the §5.8 grace-period rule, so support can tell them apart.
    """
    status = (
        await session.execute(
            select(Portal.status).where(Portal.id == portal_id).with_for_update()
        )
    ).scalar_one_or_none()
    if status is None or status == "uninstalled":
        # §4.9 rule 1: a retried uninstall is a logged no-op, never a second wipe that
        # could undo a reinstall performed in between.
        return False

    await session.execute(
        update(Portal)
        .where(Portal.id == portal_id)
        .values(
            status="uninstalled",
            uninstalled_at=func.now(),
            access_token_enc=None,
            refresh_token_enc=None,
            token_status="reauth_required",  # noqa: S106 - a state enum, not a credential
            token_version=Portal.token_version + 1,
            placements={},
            purge_pending=True,
            purge_bodies=purge_bodies,
        )
    )
    await session.execute(
        update(PortalSync)
        .where(PortalSync.portal_id == portal_id)
        .values(
            sync_generation=PortalSync.sync_generation + 1,
            **_lease_release_values(),
            **_cursor_reset_values(),
        )
    )
    await record_event(session, portal_id, kind, user_id=user_id, details=details)
    return True
