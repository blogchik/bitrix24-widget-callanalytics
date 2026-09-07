"""The open-time access decision, in one pure function (§4.4 step 5, §4.7).

WHY a module of its own, and why it touches neither the network nor the database:
`handlers/open.py` gathers the evidence (is the opener an administrator, did the
`voximplant.statistic.get` probe run, what did it answer, does the portal have the
method at all) and this module turns that evidence into the two values the rest of
the request needs - the `acc` claim of the JWT and, when the app must not be shown
at all, the §4.11 state page to render instead.

Keeping the mapping here is what makes it testable: `tests/test_access.py` drives
every branch directly, without a portal, a token or an HTTP round trip. A second
`isinstance(error, AccessDenied)` anywhere else would be a second, silently
divergent policy - the same reason `bitrix/errors.py` owns the error strings.

The vocabulary is §4.7's: Bitrix24's four statistics levels (own / department /
any / none) collapse to three - `all` for administrators, `own` for a user whose
probe succeeded (a "department" or "any" user is deliberately shown their own
calls in v1, a documented simplification), `denied` for everyone else.

**Every unclear outcome fails closed.** `level` is `denied` whenever a state page is
returned, so a caller that forgot to check `state` still cannot widen access.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from app.bitrix.errors import (
    AccessDenied,
    BitrixError,
    InsufficientScope,
    InvalidCredentials,
    MethodNotFound,
    OperationTimeLimit,
    QueryLimitExceeded,
)

__all__ = ["ACCESS_LEVELS", "AccessDecision", "decide_access"]

#: §4.6 `acc` claim / §4.7. `security/session_token.py` validates the same three.
LEVEL_ALL: Final[str] = "all"
LEVEL_OWN: Final[str] = "own"
LEVEL_DENIED: Final[str] = "denied"
ACCESS_LEVELS: Final[frozenset[str]] = frozenset({LEVEL_ALL, LEVEL_OWN, LEVEL_DENIED})

#: State kinds this module may ask for. All three exist in `render.STATE_KINDS` and in
#: the shared message catalogue (§8); `denied` is NOT among them, because a denied user
#: is sent to the SPA's `/state/denied` with no JWT (§4.4 step 8 table), not to a
#: server-rendered page.
STATE_SCOPE: Final[str] = "scope"
STATE_RETRY: Final[str] = "retry"
STATE_METHOD_MISSING: Final[str] = "method_missing"


@dataclass(frozen=True)
class AccessDecision:
    """What one open may see, and whether it may see the app at all.

    `state` is not an error channel: it is a §4.11 state page kind to render INSTEAD
    of handing the browser to the SPA. When it is None the request continues and
    `level` becomes the JWT's `acc` claim.
    """

    #: `all` | `own` | `denied` - the `acc` claim of §4.6.
    level: str
    #: A `render_state` kind, or None to continue into the app.
    state: str | None


def decide_access(
    *,
    is_admin: bool,
    probe_error: BitrixError | None,
    probe_ran: bool,
    statistic_get_available: bool,
) -> AccessDecision:
    """Map one open's evidence onto (`acc`, state) exactly as §4.4 step 5 lists it.

    Arguments, all four of which the handler establishes before calling:

    * `is_admin` - `user.admin` for the *opener* (not for the stored credential).
      §11 assumption 5: an administrator always has full "Call statistics" visibility,
      which is also why §4.4 step 4 does not spend a probe on them.
    * `probe_ran` - whether `voximplant.statistic.get` was actually called with the
      opener's own token. False for administrators and for a portal whose build lacks
      the method; `probe_error` is then meaningless and is ignored.
    * `probe_error` - the typed error that probe returned, or None when it was clean.
      A clean probe means "this user may read *some* statistics"; it does NOT mean
      "all", because a user with the own-calls level gets HTTP 200 with a filtered
      result rather than an error (docs/bitrix24-api-research.md), which is precisely
      why a successful probe maps to `own` and never to `all`.
    * `statistic_get_available` - `portals.capabilities.statistic_get`. False means the
      build has no telephony statistics method at all (§4.3 step 3 records it at
      install), so nobody - administrator included - can be shown a number.

    The order below is the order of the rules, and it matters: a missing method beats
    everything (there is no data for anyone), an administrator beats the probe (which
    is why it was never spent), and every remaining branch is keyed on the *type* the
    error classifier returned, never on an error string (`bitrix/errors.py` owns that).
    """
    if not statistic_get_available:
        # §4.4 step 5 / §4.3 step 3: an explicit, translated state beats an empty
        # dashboard that looks broken. Level is fail-closed; the state renders anyway.
        return AccessDecision(level=LEVEL_DENIED, state=STATE_METHOD_MISSING)

    if is_admin:
        # §4.4 step 4: no probe was spent, and none is needed (§11 assumption 4/5).
        return AccessDecision(level=LEVEL_ALL, state=None)

    if not probe_ran:
        # No evidence at all for a non-administrator. This is unreachable from
        # `handlers/open.py` (it probes every non-admin whose portal has the method),
        # and it stays fail-closed so that it can never become a way in.
        return AccessDecision(level=LEVEL_DENIED, state=None)

    if probe_error is None:
        # Some access, of an unknown level -> the viewer's own calls (§4.7).
        return AccessDecision(level=LEVEL_OWN, state=None)

    if isinstance(probe_error, (AccessDenied, InvalidCredentials)):
        # The mandated "ask your administrator" path (§4.11): the SPA's /state/denied,
        # reached with no JWT, not a server-rendered page.
        return AccessDecision(level=LEVEL_DENIED, state=None)

    if isinstance(probe_error, InsufficientScope):
        # A deploy-time fault (the app was never granted `telephony`), not a user
        # state: reinstalling from the Market with the permissions confirmed fixes it.
        return AccessDecision(level=LEVEL_DENIED, state=STATE_SCOPE)

    if isinstance(probe_error, MethodNotFound):
        # The capability flag said the method exists but this build disagrees; trust
        # the live answer and say so explicitly (§4.3 step 3's state).
        return AccessDecision(level=LEVEL_DENIED, state=STATE_METHOD_MISSING)

    if isinstance(probe_error, (QueryLimitExceeded, OperationTimeLimit)):
        # §4.4 step 5: the shared 2 req/s bucket or the operating-time block. Nothing
        # is wrong with this user's rights, so the page invites a reload.
        return AccessDecision(level=LEVEL_DENIED, state=STATE_RETRY)

    # Everything else - a transport failure, an expired/foreign user token, an
    # unmapped code from a cabinet we have not seen. We know nothing about this
    # user's rights, so we claim nothing and ask them to reopen the app (§4.11);
    # rendering "no access" instead would accuse them of missing a permission they
    # may well have.
    return AccessDecision(level=LEVEL_DENIED, state=STATE_RETRY)
