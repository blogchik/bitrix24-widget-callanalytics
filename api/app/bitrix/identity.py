"""Who is behind a token, and is that person an administrator (§4.1).

This module is the **credential invariant** of §4.1 in code form:

    `access_token_enc` / `refresh_token_enc` may be written only after `user.current`
    + `user.admin = true` succeeded with that token at an OAuth-derived
    `client_endpoint`.

Every path that stores a credential - install (§4.3 step 3), self-heal (§4.4 step 2),
opportunistic re-seed (§4.4 step 6), `/portal/reauthorize` (§4.5), the `ONAPPUPDATE`
ladder and the unknown-portal event rule (§4.9 rules 4 and 6) - calls
`verify_admin_token` first and hands the resulting `Identity` to
`services.portals.store_portal_credential`. There is no other way to prove a token, and
`services/portals.py` takes an `Identity` argument precisely so the proof cannot be
skipped by accident.

Two facts from docs/bitrix24-api-research.md shape the implementation:

* **`user.admin` needs no scope.** It is a `basic`-scope method executable by any user,
  and it returns "true if the current user has permissions to manage application
  settings". Bitrix24 equates that with being a portal administrator (the `profile`
  method's `ADMIN` field is documented as matching this method's result), and §11
  assumption 4 leans on it: installation is restricted to administrators, and an
  administrator always has full telephony access, so an admin-proven credential returns
  the complete call history.
* **The endpoint is never derived from `DOMAIN`.** The caller passes an endpoint that
  came either from an OAuth refresh response (`client_endpoint`) or from the stored
  portal row - §4.1's endpoint invariant. This module only uses what it is given, which
  is why `endpoint` is a required keyword argument with no default.

Why exactly one `batch` and not two calls: §4.3 step 3 and §4.4 step 4 budget three HTTP
round trips for a whole app open, and the two probes must describe the *same* token at
the *same* instant - a token that expires between them would produce an `Identity` whose
`is_admin` belongs to a different moment than its `user_id`.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.bitrix.client import BatchResult, BitrixClient
from app.bitrix.errors import classify
from app.bitrix.users import USER_ADMIN, USER_CURRENT, parse_admin_flag, parse_user
from app.logging import get_logger

__all__ = ["Identity", "NotAnAdministrator", "resolve_identity", "verify_admin_token"]

logger = get_logger(__name__)

_ME = "me"
_ADMIN = "admin"


@dataclass(frozen=True)
class Identity:
    """The proven owner of one access token.

    Frozen because it travels into `store_portal_credential` as the admin proof (§4.1);
    a mutable proof object could be edited between the check and the write.

    `timezone` is the portal user's `TIME_ZONE`, which §4.6 copies into the JWT `tz`
    claim so every number the SPA renders is in the viewer's own time.
    """

    user_id: int
    is_admin: bool
    timezone: str | None
    name: str | None
    last_name: str | None
    second_name: str | None
    work_position: str | None
    photo_url: str | None


class NotAnAdministrator(Exception):
    """The token is valid but its owner may not manage application settings.

    Carries the resolved `identity` because every caller needs it: §4.3 step 3 renders
    the translated "administrators only" state and **writes nothing**, §4.4 step 2 renders
    "ask your administrator to open the app once", and §4.9 rules 4/6 reject the event -
    all of which want to log *who* it was without repeating the round trip.
    """

    identity: Identity

    def __init__(self, identity: Identity) -> None:
        self.identity = identity
        super().__init__(f"user {identity.user_id} is not a portal administrator")


def _identity_from(current: dict[str, Any], is_admin: bool) -> Identity:
    return Identity(
        user_id=int(current["id"]),
        is_admin=is_admin,
        timezone=current["timezone"],
        name=current["name"],
        last_name=current["last_name"],
        second_name=current["second_name"],
        work_position=current["work_position"],
        photo_url=current["photo_url"],
    )


async def resolve_identity_with(
    *,
    endpoint: str,
    access_token: str,
    extra_commands: Sequence[tuple[str, str, dict[str, Any]]] = (),
    portal_id: int | None = None,
    member_id: str | None = None,
    correlation_id: uuid.UUID | None = None,
) -> tuple[Identity, BatchResult]:
    """One `batch` (`user.current`, `user.admin`) at `endpoint` with `access_token`.

    `halt=0` so both answers arrive together, but any per-command error is raised: a
    half-resolved identity is worse than none. The first error wins, and because both
    commands carry the same token the first error is also the interesting one -
    `expired_token` (§4.4 step 4 renders `/state/retry`; §5.8 refreshes),
    `user_access_error` (this user has no access to the app at all), `NO_AUTH_FOUND`
    (the token does not belong to this portal, which is exactly how §11 assumption 3
    defeats a spoofed `member_id`).

    `portal_id` / `member_id` / `correlation_id` are attribution for the `rest_log` row
    the client writes (§6); none of them influences the call.
    """
    async with BitrixClient(
        endpoint=endpoint,
        access_token=access_token,
        portal_id=portal_id,
        member_id=member_id,
        correlation_id=correlation_id,
    ) as client:
        result = await client.batch(
            [(_ME, USER_CURRENT, {}), (_ADMIN, USER_ADMIN, {}), *extra_commands],
            halt=0,
        )

    for key in (_ME, _ADMIN):
        error = result.error(key)
        if error is not None:
            raise error

    current = parse_user(result.get(_ME))
    if current is None:
        # Built through `classify` so error strings are still mapped in exactly one file.
        raise classify(
            "ERROR_UNEXPECTED_ANSWER",
            http_status=200,
            description="user.current returned no numeric ID",
        )

    # Fail-closed: an unreadable `user.admin` answer is "not an administrator", never a
    # silent yes (§4.1 credential invariant).
    return _identity_from(current, parse_admin_flag(result.get(_ADMIN))), result


async def resolve_identity(
    *,
    endpoint: str,
    access_token: str,
    portal_id: int | None = None,
    member_id: str | None = None,
    correlation_id: uuid.UUID | None = None,
) -> Identity:
    """`resolve_identity_with` for callers that need nothing but the identity."""
    identity, _ = await resolve_identity_with(
        endpoint=endpoint,
        access_token=access_token,
        portal_id=portal_id,
        member_id=member_id,
        correlation_id=correlation_id,
    )
    return identity


async def verify_admin_token(
    *,
    endpoint: str,
    access_token: str,
    portal_id: int | None = None,
    member_id: str | None = None,
    correlation_id: uuid.UUID | None = None,
) -> Identity:
    """`resolve_identity` plus the §4.1 gate: no admin, no credential.

    Returns the `Identity` that `services.portals.store_portal_credential` requires as
    its admin proof; raises `NotAnAdministrator` otherwise. Nothing is written here -
    this module never touches the database, so a caller that forgets to handle the
    exception fails closed by definition.
    """
    identity, _ = await verify_admin_token_with(
        endpoint=endpoint,
        access_token=access_token,
        portal_id=portal_id,
        member_id=member_id,
        correlation_id=correlation_id,
    )
    return identity


async def verify_admin_token_with(
    *,
    endpoint: str,
    access_token: str,
    extra_commands: Sequence[tuple[str, str, dict[str, Any]]] = (),
    portal_id: int | None = None,
    member_id: str | None = None,
    correlation_id: uuid.UUID | None = None,
) -> tuple[Identity, BatchResult]:
    """`verify_admin_token` that folds the caller's own commands into the SAME batch.

    §4.3 step 3 specifies ONE batch carrying `user.current`, `user.admin`, `app.info`,
    `method.get` and `placement.get`. Splitting the admin proof from the capability
    probe would double the round trips of an install and burn shared operating time
    (§5.6) for no benefit, so the install handler passes its three commands here.

    Errors in `extra_commands` are NOT raised: the caller inspects them on the returned
    `BatchResult`. Only the two identity commands are fatal, because without them there
    is no admin proof and §4.1 forbids writing a credential.
    """
    identity, result = await resolve_identity_with(
        endpoint=endpoint,
        access_token=access_token,
        extra_commands=extra_commands,
        portal_id=portal_id,
        member_id=member_id,
        correlation_id=correlation_id,
    )
    if not identity.is_admin:
        # Logged at INFO, not WARNING: a non-admin opening the app is an ordinary,
        # expected event (§4.11), it just may not seed a credential.
        logger.info(
            "admin proof failed",
            extra={"portal_id": portal_id, "member_id": member_id, "user_id": identity.user_id},
        )
        raise NotAnAdministrator(identity)
    return identity, result
