"""`POST /events/` - the lifecycle event handler (§4.9).

This endpoint is the one place where a *server-to-server* POST can change a tenant's
row, and the only one where a request that proves nothing at all can still be a
legitimate, expected message (a retried `ONAPPUNINSTALL` carries no access token by
design - Bitrix24 revokes API access before it sends the event). Everything below is
therefore written as an ordered ladder rather than as a switch on the event name, and
the order is load-bearing:

1. **Idempotency first** (§4.9 rule 1). An event whose `ts` is not greater than
   `portals.last_event_ts`, or - for `ONAPPUNINSTALL` - earlier than
   `install_completed_at`, is a logged 200 no-op. Bitrix24 retries events, so without
   this a duplicated uninstall delivered *after* the admin reinstalled would wipe the
   fresh installation and its cache. Idempotency is checked before authentication
   precisely because the retry is authentic: it is not a request to reject, it is a
   request that has already been answered.
2. **`application_token` compare** (§4.9 rule 2, brief rule 5). If the portal has a
   stored token, an event must present a constant-time-equal `auth[application_token]`
   or the answer is 403 plus `portal_events(event_rejected)` - `ONAPPINSTALL`,
   `ONAPPUSERREADY` and `ONAPPUNINSTALL` alike, not just the uninstall the docs single
   out. `ONAPPUPDATE` is the one event that carries a *new* value by design (it is the
   rotation), so for it the compare is replaced - never dropped - by the strictly
   stronger proof of rung 3, which is what refuses the forged rotation either way.
3. **Elevated proof for anything that writes a credential or the application token**
   (§4.9 rules 4-6): `auth[access_token]` must pass `user.current` + `user.admin=true`
   at the **stored** `client_endpoint`, and `auth[refresh_token]` must exchange to the
   same `member_id`. Both halves are required. Without them any employee could read
   their own pair from `BX24.getAuth()`, POST a forged `ONAPPUPDATE` that installs an
   `application_token` of their choosing, and then forge an `ONAPPUNINSTALL` that wipes
   the portal's cache - the attack two independent reviewers found in the draft.

What this handler deliberately does NOT do:

* **No REST call on `ONAPPUNINSTALL`.** Access is already revoked, so `placement.unbind`
  could only fail, and placements die with the app anyway
  (docs/bitrix24-api-research.md, corrections to brief). The row transition is
  everything; the worker deletes the data (§5.9).
* **No sync with the application system user.** `ONAPPUSERREADY` carries a long-lived
  credential in `data{}`; v1 logs the arrival and discards it, because that account
  holds regular-employee rights and would silently cache a truncated slice of the
  portal's calls (§4.9 rule 5).
* **No credential write of its own.** `store_portal_credential()` is the only door
  (§4.1), and it takes the `Identity` produced by the admin proof as an argument so the
  proof cannot be skipped by accident.
"""

from __future__ import annotations

import hmac
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import or_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.bitrix.errors import BitrixError
from app.bitrix.forms import EventPost, FormValidationError, parse_event_post
from app.bitrix.identity import Identity, NotAnAdministrator, verify_admin_token
from app.bitrix.oauth import OAuthRateLimited, TokenResponse, exchange_refresh_token
from app.db.models import Portal
from app.db.session import control_txn

# §4.4/§4.9 share a trust boundary, so they share its helpers rather than growing a
# second copy that could drift: the inbound-log shape, the request-id correlation and
# the `member_id`-for-a-rejected-body rule must have exactly ONE definition.
from app.handlers.install import (
    _client_ip,
    _correlation_id,
    _raw_member_id,
    _read_form,
    _reason,
    _request_url,
)
from app.logging import get_logger
from app.security.crypto import encrypt
from app.security.redact import REDACTED, is_sensitive_key
from app.services.portals import (
    decrypt_portal_token,
    get_portal_by_member_id,
    mark_uninstalled,
    record_event,
    store_portal_credential,
)
from app.services.rest_log import write_rest_log

__all__ = ["handle_event_form", "router"]

router = APIRouter()
_log = get_logger(__name__)

# --- the event vocabulary (§4.9, research note (d)) ----------------------------------

_INSTALL: Final[str] = "ONAPPINSTALL"
_USER_READY: Final[str] = "ONAPPUSERREADY"
_UPDATE: Final[str] = "ONAPPUPDATE"
_UNINSTALL: Final[str] = "ONAPPUNINSTALL"

#: The events that carry an `auth[access_token]`/`auth[refresh_token]` pair. They are
#: the only ones that can ever prove elevated identity, and therefore the only ones
#: that may create a portal row (rule 6) or store an application token (rules 4-5).
_TOKEN_EVENTS: Final[frozenset[str]] = frozenset({_INSTALL, _USER_READY, _UPDATE})
_LIFECYCLE_EVENTS: Final[frozenset[str]] = _TOKEN_EVENTS | {_UNINSTALL}

#: §4.5: the settings placement events. v1 has no user-editable settings, so they are
#: logged and answered with an empty form rather than routed through the ladder.
_SETTINGS_EVENTS: Final[frozenset[str]] = frozenset(
    {"ONAPPSETTINGSINSTALL", "ONAPPSETTINGSDISPLAY", "ONAPPSETTINGSCHANGE"}
)

#: `portals.app_version` is a plain `integer` (§3); a version string outside this range
#: would abort the transaction, so it is dropped instead.
_MAX_APP_VERSION: Final[int] = 2**31 - 1


# --- the one log row per inbound event (§6) ------------------------------------------


@dataclass
class _EventLog:
    """What the single `rest_log` row for this request will say.

    Filled in as the ladder walks so that exactly one row is written, with the outcome
    the caller actually received.

    There is deliberately no `portal_id`: an inbound event row is attributed by
    `member_id` and `correlation_id` only, exactly as `/install/` and `/app/` attribute
    theirs (§6 - at the moment a body arrives nothing has proven which portal sent it,
    and §4.9 requires a REJECTED event to store `portal_id` NULL with the `member_id`
    kept). Keeping the whole family unattributed also keeps the trail honest across a
    `data[CLEAN]=1` uninstall: §5.9 NULLs `rest_log` bodies BY `portal_id`, so an
    attributed row would erase the record of the very event that asked for the wipe.
    """

    member_id: str | None = None
    event: str | None = None
    status: int = 200
    error_code: str | None = None


def _is_data_key(key: str) -> bool:
    """True for the `data` bracket family of a PHP-style event body."""
    lowered = key.lower()
    return lowered == "data" or lowered.startswith("data[")


def _loggable(form: Mapping[str, str], *, redact_data: bool) -> dict[str, Any]:
    """The event body as it may be written to `rest_log` (§6, recursive redaction).

    `write_rest_log` runs `redact()` over whatever it is given, which already blanks
    every `auth[...]` key (they all contain `auth` or `token`). The extra rule here is
    `ONAPPUSERREADY`: its `data{}` carries the application system user's long-lived
    credential, and the field names inside it are not pinned by any documentation we
    verified - so the whole subtree is blanked by POSITION rather than trusted to match
    the key regex. An unparsable body is treated the same way, because at that point we
    do not know which event it was.
    """
    return {
        key: (REDACTED if is_sensitive_key(key) or (redact_data and _is_data_key(key)) else value)
        for key, value in form.items()
    }


async def _write_log(
    request: Request, form: Mapping[str, str], entry: _EventLog, correlation_id: uuid.UUID
) -> None:
    """Write the single inbound `rest_log` row (`kind="event"`, §6)."""
    await write_rest_log(
        direction="in",
        kind="event",
        method="POST",
        url=_request_url(request),
        # Never the portal id - see `_EventLog`.
        portal_id=None,
        member_id=entry.member_id,
        correlation_id=correlation_id,
        token_user_id=None,
        request=_loggable(form, redact_data=entry.event in (None, _USER_READY)),
        http_status=entry.status,
        error_code=entry.error_code,
    )


# --- small readers over the untrusted body -------------------------------------------


def _event_name(event: EventPost) -> str:
    """Upper-cased event name; `bitrix/forms.py` keeps the original case (§4.2)."""
    return event.event.strip().upper()


def _data_field(event: EventPost, name: str) -> str | None:
    """One `data[...]` field, read case-insensitively.

    The captured payloads spell these upper-case (`data[CLEAN]`, `data[VERSION]`) but
    the case is Bitrix24's to change and no CHECK in this codebase depends on it.
    """
    for key, value in event.data.items():
        if isinstance(key, str) and key.lower() == name.lower() and isinstance(value, str):
            return value.strip() or None
    return None


def _clean_requested(event: EventPost) -> bool:
    """`data[CLEAN] == '1'` - the uninstalling admin ticked "clear application data".

    §4.9 rule 3 maps it to `portals.purge_bodies`, which makes the purge job additionally
    NULL this portal's `rest_log` bodies (§5.9). Strictly `'1'`: anything else, including
    an absent field, is the ordinary uninstall that still deletes the cached calls.
    """
    return _data_field(event, "CLEAN") == "1"


def _app_version(event: EventPost) -> int | None:
    """`data[VERSION]` as an int, or None when it is not one we can store."""
    raw = _data_field(event, "VERSION")
    if raw is None or not raw.isascii() or not raw.isdigit():
        return None
    value = int(raw)
    return value if 0 <= value <= _MAX_APP_VERSION else None


def _event_ts(event: EventPost) -> datetime | None:
    """`ts` as an aware datetime; `bitrix/forms.py` already bounded it to 0..2100."""
    if event.ts is None:
        return None
    try:
        return datetime.fromtimestamp(event.ts, UTC)
    except (OSError, OverflowError, ValueError):  # pragma: no cover - bounded upstream
        return None


# --- responses ------------------------------------------------------------------------


def _ok(entry: _EventLog) -> Response:
    """The 200 that every accepted, ignored or unknown event gets.

    Empty on purpose: Bitrix24 reads the status code only, and a body would be one more
    thing that could carry something it should not.
    """
    entry.status = 200
    return Response(status_code=200)


def _settings_form(entry: _EventLog) -> Response:
    """§4.5: a minimal valid JSON form with **zero steps**.

    v1 exposes no user-editable settings - everything the admin can act on lives on the
    `/settings` page behind the JWT - so the settings placement is answered with an
    empty step list rather than left to time out.
    """
    entry.status = 200
    return JSONResponse({"result": {"steps": []}})


async def _reject(
    entry: _EventLog, portal: Portal | None, reason: str, *, user_id: int | None = None
) -> Response:
    """403 + `portal_events(event_rejected)` (§4.9 rule 2 and rules 4-6).

    The `rest_log` row stays unattributed (§6: a refused event keeps `member_id` as its
    only attribution, so a forged body cannot attach itself to a tenant's REST log). The
    `portal_events` row does name the portal - that table is the audit trail support
    greps, and "somebody presented the wrong application token for this portal" is
    exactly what it exists to record.
    """
    entry.status = 403
    entry.error_code = reason
    if portal is not None:
        async with control_txn() as session:
            await record_event(
                session,
                portal.id,
                "event_rejected",
                user_id=user_id,
                details={"event": entry.event, "reason": reason},
            )
    _log.warning(
        "events: rejected",
        extra={
            "member_id": entry.member_id,
            "portal_id": portal.id if portal else None,
            "event": entry.event,
            "reason": reason,
        },
    )
    return Response(status_code=403)


def _retry_later(entry: _EventLog, reason: str) -> Response:
    """429 for the rate-limited refresh exchange of §4.1.

    NOT a 403: nothing was disproven, we simply refused to spend an exchange. Bitrix24
    retries the event, and the idempotency rule makes that retry safe.
    """
    entry.status = 429
    entry.error_code = reason
    return Response(status_code=429)


# --- the two halves of the elevated proof (§4.9 rule 4) -------------------------------


async def _prove_admin(
    portal: Portal, event: EventPost, correlation_id: uuid.UUID
) -> Identity | None:
    """`user.current` + `user.admin=true` with `auth[access_token]` at the STORED endpoint.

    The endpoint is the stored one and never anything the event named (§4.1 endpoint
    invariant): a token minted by another portal fails there, which is what makes a
    forged `auth[member_id]` worthless. Returns None on every failure - a non-admin, an
    expired token, a transport error - because §4.9 draws no distinction between them:
    anything short of a proven administrator is a rejected event.
    """
    if not event.access_token:
        return None
    try:
        return await verify_admin_token(
            endpoint=portal.client_endpoint,
            access_token=event.access_token,
            portal_id=portal.id,
            member_id=portal.member_id,
            correlation_id=correlation_id,
        )
    except NotAnAdministrator as exc:
        _log.info(
            "events: bearer is not an administrator",
            extra={"portal_id": portal.id, "user_id": exc.identity.user_id, "event": event.event},
        )
        return None
    except BitrixError as exc:
        _log.warning(
            "events: admin proof failed",
            extra={"portal_id": portal.id, "event": event.event, "error_code": exc.code},
        )
        return None


async def _prove_refresh(
    event: EventPost,
    *,
    expected_member_id: str,
    portal_id: int | None,
    source_ip: str | None,
    correlation_id: uuid.UUID,
) -> TokenResponse | None:
    """`auth[refresh_token]` must exchange to `expected_member_id` (§4.9 rules 4 and 6).

    This is the half of the proof that cannot be produced from a user's own
    `BX24.getAuth()` pair *for another portal*: the OAuth server answers with the
    `member_id` the credential actually belongs to, and `exchange_refresh_token` raises
    on a mismatch. `server_endpoint` is not passed because `bitrix/forms.py` does not
    expose `auth[server_endpoint]`; the exchange then uses the documented default host,
    which is the same value §4.1 would have forced for anything outside the allowlist.

    `OAuthRateLimited` propagates: the caller answers 429 so Bitrix24 retries, rather
    than pretending the proof failed.
    """
    if not event.refresh_token:
        return None
    try:
        return await exchange_refresh_token(
            event.refresh_token,
            expected_member_id=expected_member_id,
            portal_id=portal_id,
            source_ip=source_ip,
            correlation_id=correlation_id,
        )
    except OAuthRateLimited:
        raise
    except Exception:
        # Includes `MemberIdMismatch` - the event claimed a portal this refresh token
        # does not belong to. The exchange payload itself is never logged (§6).
        _log.warning(
            "events: refresh proof failed",
            extra={"member_id": expected_member_id, "event": event.event},
            exc_info=True,
        )
        return None


# --- portal writes --------------------------------------------------------------------


async def _touch_last_event_ts(session: AsyncSession, portal_id: int, ts: datetime | None) -> None:
    """Advance `portals.last_event_ts`, never backwards (§4.9 rule 1).

    The guard is in the WHERE clause rather than in Python so two events delivered
    concurrently cannot interleave into a cursor that moves back. Written here rather
    than in `services/portals.py` because it is neither a credential column nor an
    endpoint (§4.1 guards exactly those three); it is the idempotency cursor of this
    handler, and this handler is its only writer.
    """
    if ts is None:
        return
    await session.execute(
        update(Portal)
        .where(
            Portal.id == portal_id,
            or_(Portal.last_event_ts.is_(None), Portal.last_event_ts < ts),
        )
        .values(last_event_ts=ts)
    )


def _is_replay(portal: Portal, event: EventPost, name: str) -> bool:
    """§4.9 rule 1, the first rung: has this event already been answered?

    Two clocks, both Bitrix24's:

    * `ts <= last_event_ts` - a duplicate or an out-of-order delivery of something we
      have already applied.
    * for `ONAPPUNINSTALL` only, `ts < install_completed_at` - the uninstall predates
      the installation currently on record, i.e. the admin reinstalled in the meantime
      and this is the *old* uninstall arriving late. Applying it would wipe a live
      portal, which is the failure this rung exists to prevent.

    An event without a `ts` cannot be judged and is allowed through to the token
    compare, which is the rung that actually protects the row.
    """
    ts = _event_ts(event)
    if ts is None:
        return False
    if portal.last_event_ts is not None and ts <= portal.last_event_ts:
        return True
    return bool(
        name == _UNINSTALL
        and portal.install_completed_at is not None
        and ts < portal.install_completed_at
    )


async def _application_token_matches(portal: Portal, event: EventPost) -> bool:
    """§4.9 rule 2: constant-time compare against the stored `application_token`.

    `hmac.compare_digest` rather than `==`: the value is a secret being compared against
    attacker-supplied input, and a short-circuiting comparison leaks its prefix.

    An undecryptable stored token fails CLOSED. It means the key ring lost the key that
    sealed it (§3 envelope), and "we cannot check" must never read as "check passed" on
    the one control that authenticates an uninstall.
    """
    stored = await decrypt_portal_token(portal, "application_token")
    if stored is None:
        _log.warning(
            "events: stored application token unreadable, refusing the event",
            extra={"portal_id": portal.id, "event": event.event},
        )
        return False
    return hmac.compare_digest(stored, event.application_token or "")


# --- rule 3: ONAPPUNINSTALL -----------------------------------------------------------


async def _handle_uninstall(portal: Portal | None, event: EventPost, entry: _EventLog) -> Response:
    """§4.9 rule 3. One transaction, no REST call, 200 immediately.

    Unknown or already-uninstalled portal is a 200 no-op: uninstall is the one event we
    must never argue with, and answering 4xx would only make Bitrix24 retry a message
    whose effect is already in place.

    `mark_uninstalled` re-reads the row `FOR UPDATE` and returns False when the
    transition had already been applied, so two deliveries racing each other still
    produce exactly one `portal_events(uninstall)`.
    """
    if portal is None or portal.status == "uninstalled":
        _log.info(
            "events: uninstall for an unknown or already-uninstalled portal",
            extra={"member_id": entry.member_id},
        )
        return _ok(entry)

    clean = _clean_requested(event)
    async with control_txn() as session:
        applied = await mark_uninstalled(
            session,
            portal.id,
            purge_bodies=clean,
            details={"event": entry.event, "clean": clean, "source": "events_handler"},
        )
        await _touch_last_event_ts(session, portal.id, _event_ts(event))

    _log.info(
        "events: portal uninstalled",
        extra={"portal_id": portal.id, "member_id": portal.member_id, "clean": clean,
               "applied": applied},
    )
    return _ok(entry)


# --- rule 4: ONAPPUPDATE, and rule 5's "store the token when none is stored" ----------


async def _elevated_proof(
    request: Request,
    portal: Portal,
    event: EventPost,
    entry: _EventLog,
    correlation_id: uuid.UUID,
) -> tuple[Identity, TokenResponse] | Response:
    """Both halves of §4.9 rule 4, or the Response that refuses the event.

    Order matters for cost, not for security: the admin proof is one batch against the
    stored endpoint and costs nothing but a round trip, while the refresh exchange is
    rate-limited AND spends the presented refresh token (Bitrix24 rotates it). Proving
    the cheap half first means a forged event never burns either budget.
    """
    identity = await _prove_admin(portal, event, correlation_id)
    if identity is None:
        return await _reject(entry, portal, "admin_proof_failed")

    try:
        tokens = await _prove_refresh(
            event,
            expected_member_id=portal.member_id,
            portal_id=portal.id,
            source_ip=_client_ip(request),
            correlation_id=correlation_id,
        )
    except OAuthRateLimited:
        async with control_txn() as session:
            await record_event(
                session,
                portal.id,
                "refresh_failed",
                details={"event": entry.event, "reason": "rate_limited"},
            )
        return _retry_later(entry, "rate_limited")

    if tokens is None:
        return await _reject(entry, portal, "refresh_proof_failed", user_id=identity.user_id)
    return identity, tokens


async def _handle_update(
    request: Request,
    portal: Portal,
    event: EventPost,
    entry: _EventLog,
    correlation_id: uuid.UUID,
) -> Response:
    """§4.9 rule 4 - the ONLY event that may replace `application_token_enc`.

    Rule 2's equality compare cannot gate this event: Bitrix24 delivers the ROTATED
    application token inside the update itself, so the value presented here is expected
    to differ from the stored one. The elevated proof below is what stands in its place,
    and it is a higher bar than knowing the old secret - an administrator's token, at
    the stored endpoint, plus a refresh exchange that lands on this same `member_id`.

    An uninstalled portal is a logged no-op: resurrecting a tenant is §4.3's decision
    (it owns the cursor-reset rule that distinguishes a reinstall from a version bump),
    and doing it here would race the purge job that is deleting the rows.

    The credential is re-seeded only when `token_status != 'ok'`, and then only through
    `store_portal_credential()` with the `Identity` this ladder just proved - §4.1
    allows no other writer. A healthy portal keeps its working credential: the event's
    token pair belongs to whoever clicked "update", which is not necessarily the
    installer whose rights the sync depends on.
    """
    if portal.status == "uninstalled":
        _log.info(
            "events: update for an uninstalled portal, ignored",
            extra={"portal_id": portal.id, "member_id": portal.member_id},
        )
        return _ok(entry)

    proof = await _elevated_proof(request, portal, event, entry, correlation_id)
    if isinstance(proof, Response):
        return proof
    identity, tokens = proof

    version = _app_version(event)
    reseeded = portal.token_status != "ok"  # noqa: S105 - a state enum (§3), not a credential
    async with control_txn() as session:
        if reseeded:
            # §4.9 rule 4: a portal whose credential is already broken is exactly the one
            # an update can repair, and the admin proof for it is in hand.
            await store_portal_credential(
                session,
                member_id=portal.member_id,
                tokens=tokens,
                admin=identity,
                application_token=event.application_token,
                domain=portal.domain,
                protocol_https=portal.protocol_https,
                lang=portal.lang,
                app_status=portal.app_status,
                capabilities={
                    **portal.capabilities,
                    "scope": tokens.scope,
                    "checked_at": datetime.now(UTC).isoformat(),
                },
                token_status="ok",  # noqa: S106 - a state enum (§3), not a credential
            )

        # The columns an update owns that no service writes for us. None of them is a
        # credential or an endpoint, so §4.1's single-writer rule does not reach them.
        values: dict[str, Any] = {}
        if event.application_token:
            values["application_token_enc"] = encrypt(
                event.application_token, member_id=portal.member_id, column="application_token"
            )
        if version is not None:
            values["app_version"] = version
        if event.scope and not reseeded:
            # `store_portal_credential` already wrote the authoritative scope from the
            # OAuth response; the event's copy is only used when it did not run.
            values["scope"] = event.scope
        if values:
            await session.execute(update(Portal).where(Portal.id == portal.id).values(**values))

        await record_event(
            session,
            portal.id,
            "app_update",
            user_id=identity.user_id,
            details={
                "event": entry.event,
                "app_version": version,
                "application_token_rotated": bool(event.application_token),
                "credential_reseeded": reseeded,
            },
        )
        await _touch_last_event_ts(session, portal.id, _event_ts(event))

    _log.info(
        "events: application updated",
        extra={"portal_id": portal.id, "app_version": version, "credential_reseeded": reseeded},
    )
    return _ok(entry)


# --- rule 5: ONAPPINSTALL / ONAPPUSERREADY on a known portal --------------------------


async def _handle_known_install(
    request: Request,
    portal: Portal,
    event: EventPost,
    entry: _EventLog,
    correlation_id: uuid.UUID,
) -> Response:
    """§4.9 rule 5: a no-op beyond seeding an application token we do not have yet.

    The interesting case is a portal installed through `/install/` by a cabinet that
    sends no `APPLICATION_TOKEN` in the iframe POST: the first `ONAPPINSTALL` is then
    the only chance to learn the secret that authenticates every later event. Storing it
    is a privileged write, so it takes the SAME proof as rule 4 - otherwise an employee
    could seed a token of their own choosing on a portal that has none and then forge
    uninstalls at will.

    `ONAPPUSERREADY` additionally carries the application system user's credential in
    `data{}`. It is logged (redacted) and discarded: that account has regular-employee
    rights, and syncing with it would silently cache only the calls it can see (§4.9
    rule 5, research note (d) on the application system user).
    """
    if entry.event == _USER_READY:
        _log.info(
            "events: system-user credential received and discarded",
            extra={"portal_id": portal.id, "member_id": portal.member_id},
        )

    if portal.application_token_enc or not event.application_token:
        # Nothing to learn: either the token is already stored (and rule 2 just proved
        # the sender knows it) or the event brought none.
        async with control_txn() as session:
            await _touch_last_event_ts(session, portal.id, _event_ts(event))
        return _ok(entry)

    proof = await _elevated_proof(request, portal, event, entry, correlation_id)
    if isinstance(proof, Response):
        return proof
    identity, _tokens = proof

    async with control_txn() as session:
        await session.execute(
            update(Portal)
            .where(Portal.id == portal.id)
            .values(
                application_token_enc=encrypt(
                    event.application_token, member_id=portal.member_id, column="application_token"
                )
            )
        )
        await record_event(
            session,
            portal.id,
            "app_update",
            user_id=identity.user_id,
            details={"event": entry.event, "application_token": "stored"},
        )
        await _touch_last_event_ts(session, portal.id, _event_ts(event))

    _log.info(
        "events: application token seeded",
        extra={"portal_id": portal.id, "member_id": portal.member_id, "event": entry.event},
    )
    return _ok(entry)


# --- rule 6: a token-bearing event for a portal we have never seen --------------------


async def _handle_unknown_portal(
    request: Request, event: EventPost, entry: _EventLog, correlation_id: uuid.UUID
) -> Response:
    """§4.9 rule 6: refresh exchange (authoritative `member_id`) AND `user.admin` first.

    Nothing about an unknown portal can be checked against stored state - there is no
    `application_token` to compare and no `client_endpoint` to call - so the exchange
    runs first: its response *tells us* which portal the credential belongs to and where
    that portal's REST base is. Only then is the bearer proven an administrator, at that
    endpoint, and only then may a row exist.

    The row is created through `store_portal_credential()` like every other install
    path. No placements are bound and no capabilities are probed: this is a recovery
    path (our database lost a row, or a cabinet delivered `ONAPPINSTALL` before the
    iframe POST), and the next admin open runs §4.4 in full.
    """
    if not (event.access_token and event.refresh_token):
        # §4.9 rule 7: nothing to prove with, nothing to create. Logged, ignored, 200.
        _log.info(
            "events: token-less event for an unknown portal, ignored",
            extra={"member_id": entry.member_id, "event": entry.event},
        )
        return _ok(entry)
    if event.member_id is None or not event.domain:
        # `portals.domain` is NOT NULL and is display/CSP data (§4.10) with no second
        # source on this path; without it a row cannot be created honestly.
        _log.info(
            "events: unknown portal without a usable identity, ignored",
            extra={"member_id": entry.member_id, "event": entry.event},
        )
        return _ok(entry)

    try:
        tokens = await _prove_refresh(
            event,
            expected_member_id=event.member_id,
            portal_id=None,
            source_ip=_client_ip(request),
            correlation_id=correlation_id,
        )
    except OAuthRateLimited:
        _log.warning(
            "events: unknown-portal exchange rate limited", extra={"member_id": entry.member_id}
        )
        return _retry_later(entry, "rate_limited")
    if tokens is None:
        return await _reject(entry, None, "refresh_proof_failed")

    try:
        identity = await verify_admin_token(
            endpoint=tokens.client_endpoint,
            access_token=event.access_token,
            member_id=tokens.member_id,
            correlation_id=correlation_id,
        )
    except NotAnAdministrator:
        # §4.9 rule 6: "A non-admin token creates nothing."
        _log.info(
            "events: unknown-portal event from a non-administrator",
            extra={"member_id": entry.member_id, "event": entry.event},
        )
        return await _reject(entry, None, "admin_proof_failed")
    except BitrixError as exc:
        _log.warning(
            "events: unknown-portal admin proof failed",
            extra={"member_id": entry.member_id, "error_code": exc.code},
        )
        return await _reject(entry, None, "admin_proof_failed")

    async with control_txn() as session:
        outcome = await store_portal_credential(
            session,
            member_id=tokens.member_id,
            tokens=tokens,
            admin=identity,
            application_token=event.application_token,
            domain=event.domain,
            # Not sent by an event body; the cloud default. A portal that is really
            # http:// corrects both columns on its next `/app/` open (§4.4 step 6).
            protocol_https=True,
            lang=None,
            app_status=tokens.status,
            capabilities={
                # Deliberately not `False`: this path never ran `method.get`, and
                # claiming the method is missing would render the wrong state page.
                "statistic_get": None,
                "probed": False,
                "source": "events_handler",
                "event": entry.event,
                "scope": tokens.scope,
                "checked_at": datetime.now(UTC).isoformat(),
            },
            token_status="ok",  # noqa: S106 - a state enum (§3), not a credential
        )
        await _touch_last_event_ts(session, outcome.portal_id, _event_ts(event))

    _log.info(
        "events: portal created from a lifecycle event",
        extra={
            "portal_id": outcome.portal_id,
            "member_id": outcome.member_id,
            "event": entry.event,
            "portal_created": outcome.created,
        },
    )
    return _ok(entry)


# --- the ladder -----------------------------------------------------------------------


async def _ladder(
    request: Request, form: Mapping[str, str], entry: _EventLog, correlation_id: uuid.UUID
) -> Response:
    """§4.9 rules 1-7, in the order the design fixes them."""
    try:
        event = parse_event_post(form)
    except FormValidationError as exc:
        entry.status = 400
        entry.error_code = _reason(exc)
        _log.warning("events: rejected body", extra={"field": exc.field, "reason": exc.reason})
        return Response(status_code=400)

    name = _event_name(event)
    entry.event = name
    entry.member_id = event.member_id or entry.member_id

    if name in _SETTINGS_EVENTS:
        _log.info("events: settings placement event", extra={"event": name})
        return _settings_form(entry)

    if name not in _LIFECYCLE_EVENTS:
        # An event we never subscribed to (or a future one, e.g. ONVOXIMPLANTCALLEND).
        # 200, not 4xx: Bitrix24 retries anything else, and there is nothing to retry.
        _log.info("events: unhandled event", extra={"event": name})
        return _ok(entry)

    if event.member_id is None:
        _log.info("events: lifecycle event without a member_id, ignored", extra={"event": name})
        return _ok(entry)

    async with control_txn() as session:
        portal = await get_portal_by_member_id(session, event.member_id)

    # ---- rule 1: idempotency, before anything can act on the row --------------------
    if portal is not None and _is_replay(portal, event, name):
        _log.info(
            "events: replayed or stale event, ignored",
            extra={"portal_id": portal.id, "event": name, "ts": event.ts},
        )
        return _ok(entry)

    # ---- rule 2: the application_token compare, for EVERY event ---------------------
    # ONAPPUPDATE is the documented exception, and it is an exception of ORDER, not of
    # strictness. Bitrix24 rotates `application_token` when a new version is installed
    # and delivers the NEW value in that very event (research note (d) on
    # application_token verification), so a literal equality gate there could never be
    # satisfied by a genuine update: the stored value would freeze, every later event
    # would fail rule 2, and the uninstall webhook - brief rule 7 - would stop working.
    # What replaces the compare is strictly stronger, never weaker: rule 4 requires a
    # proven administrator AND a refresh exchange that lands on this same `member_id`
    # before a single column is written, which is exactly what refuses the forged
    # ONAPPUPDATE that the review found.
    if (
        portal is not None
        and portal.application_token_enc
        and name != _UPDATE
        and not await _application_token_matches(portal, event)
    ):
        return await _reject(entry, portal, "application_token_mismatch")

    # ---- rules 3-7 -------------------------------------------------------------------
    if name == _UNINSTALL:
        return await _handle_uninstall(portal, event, entry)
    if portal is None:
        return await _handle_unknown_portal(request, event, entry, correlation_id)
    if name == _UPDATE:
        return await _handle_update(request, portal, event, entry, correlation_id)
    return await _handle_known_install(request, portal, event, entry, correlation_id)


async def handle_event_form(
    request: Request,
    form: Mapping[str, str],
    *,
    correlation_id: uuid.UUID | None = None,
) -> Response:
    """Run §4.9 over one already-read form body, writing exactly one `rest_log` row.

    Called both by this module's own route and by the `event=` dispatch of §4.2 in
    `/install/`, `/app/` and `/settings/` - some cabinets deliver lifecycle events to
    the installation URL, and an `ONAPPUNINSTALL` posted there must not be answered with
    "bad request".

    The blanket except is deliberate: an unhandled failure here would otherwise reach
    the iframe handlers' own safety net and render an HTML error page at a
    server-to-server endpoint. 500 is the honest answer - it is also the one status that
    makes Bitrix24 redeliver, which is what a transient database failure during an
    uninstall needs.
    """
    cid = correlation_id or _correlation_id()
    entry = _EventLog(member_id=_raw_member_id(form))
    try:
        return await _ladder(request, form, entry, cid)
    except Exception:
        entry.status = 500
        entry.error_code = "internal_error"
        # No body and no headers in the line (§6): this path sees credentials.
        _log.exception("events: unhandled failure", extra={"event": entry.event})
        return Response(status_code=500)
    finally:
        await _write_log(request, form, entry, cid)


@router.post("/events/", include_in_schema=False)
async def events(request: Request) -> Response:
    """`POST /events/` (§4.9) - the vendor cabinet's event handler URL.

    A body we cannot even read is logged and answered 400: it carries no `member_id` to
    attribute, no `ts` to deduplicate and no token to compare, so there is nothing this
    handler could safely do with it.
    """
    correlation_id = _correlation_id()
    try:
        form = await _read_form(request)
    except FormValidationError as exc:
        await _write_log(
            request,
            {},
            _EventLog(status=400, error_code=_reason(exc)),
            correlation_id,
        )
        _log.warning("events: unreadable body", extra={"reason": exc.reason})
        return Response(status_code=400)
    return await handle_event_form(request, form, correlation_id=correlation_id)
