"""Bitrix24 error strings -> Python types, in exactly one place.

Every other module (client, oauth, handlers, sync) branches on the *type* returned
here and never on the error string, so the mapping can be corrected in one edit when a
cabinet turns out to emit an undocumented code (§4.4 step 5, §5.6, §5.8).

Matching order is the JSON `error` string first, HTTP status only as a fallback,
because Bitrix24 documents one status for several distinct decisions: 403 covers
ACCESS_DENIED (a user-rights state), INVALID_CREDENTIALS, insufficient_scope (a config
bug that must fail loudly) and user_access_error, and §4.4 step 5 renders a different
page for each. `docs/bitrix24-api-research.md` also records that the docs mix cases -
hence the case-insensitive compare - and that the published list is *not* closed
(`invalid_token` and `WRONG_AUTH_TYPE` survive only in stale snippets), hence
`UnknownBitrixError` instead of an assertion.
"""

__all__ = [
    "BitrixError",
    "TransportError",
    "ExpiredToken",
    "NoAuthFound",
    "InvalidCredentials",
    "AccessDenied",
    "UserAccessError",
    "InsufficientScope",
    "QueryLimitExceeded",
    "OperationTimeLimit",
    "MethodNotFound",
    "PortalDeleted",
    "PaymentRequired",
    "InvalidGrant",
    "UnknownBitrixError",
    "classify",
]


class BitrixError(Exception):
    """Base of the hierarchy.

    `payload` holds the response body verbatim, and for OAuth and event bodies that can
    contain live tokens: anything that logs it MUST pass it through
    `app.security.redact.redact` first (§6). `str(self)` therefore renders only the
    code, the status and the description, never the payload.
    """

    #: Canonical Bitrix24 `error` string for this class. Used when a caller constructs
    #: the exception directly - a transport failure has no response body to quote.
    default_code: str = ""

    code: str
    description: str
    http_status: int | None
    payload: dict

    def __init__(
        self,
        code: str = "",
        *,
        description: str = "",
        http_status: int | None = None,
        payload: dict | None = None,
    ) -> None:
        self.code = code or self.default_code
        self.description = description
        self.http_status = http_status
        self.payload = payload if payload is not None else {}
        detail = self.code or "unknown"
        if self.http_status is not None:
            detail = f"{detail} (HTTP {self.http_status})"
        if self.description:
            detail = f"{detail}: {self.description}"
        super().__init__(detail)


class TransportError(BitrixError):
    """DNS / TLS / connect / read timeout - no HTTP response at all.

    §4.4 step 4 and §5.8 treat this differently from every REST error: it is the
    trigger for re-learning `client_endpoint` from a refresh exchange (renamed portal
    or a newly connected custom domain), not for touching the credential.
    """

    default_code = "transport_error"


class ExpiredToken(BitrixError):
    """The only error that may trigger a refresh (§5.8 step 2; brief rule 4)."""

    default_code = "expired_token"


class NoAuthFound(BitrixError):
    """401 wrong authorization data; terminal `reauth_required` for the portal token."""

    default_code = "NO_AUTH_FOUND"


class InvalidCredentials(BitrixError):
    """403 - the caller lacks the right; §4.4 step 5 maps a probe hit to `acc='denied'`."""

    default_code = "INVALID_CREDENTIALS"


class AccessDenied(BitrixError):
    """No "Call statistics - view" right on `voximplant.statistic.get` (§5.8 terminal)."""

    default_code = "ACCESS_DENIED"


class UserAccessError(BitrixError):
    """403 - this user has no access to the application itself (§4.4 step 4)."""

    default_code = "user_access_error"


class InsufficientScope(BitrixError):
    """A configuration bug, never retried: §5.8 makes it terminal and loud."""

    default_code = "insufficient_scope"


class QueryLimitExceeded(BitrixError):
    """503 leaky-bucket rejection; §5.6 counts it as a throttle hit, not a failure."""

    default_code = "QUERY_LIMIT_EXCEEDED"


class OperationTimeLimit(BitrixError):
    """429 operating-time block; §5.6 parks the portal until `operating_reset_at`."""

    default_code = "OPERATION_TIME_LIMIT"


class MethodNotFound(BitrixError):
    """The build lacks the method - §4.3 step 3 renders `/state/method_missing`."""

    default_code = "ERROR_METHOD_NOT_FOUND"


class PortalDeleted(BitrixError):
    """§5.8 terminal: `status='uninstalled'`, `purge_pending=true`."""

    default_code = "PORTAL_DELETED"


class PaymentRequired(BitrixError):
    """The plan no longer includes REST; §5.8 terminal `reauth_required`."""

    default_code = "PAYMENT_REQUIRED"


class InvalidGrant(BitrixError):
    """The OAuth refresh chain is dead (180 days idle, or the token was reused)."""

    default_code = "invalid_grant"


class UnknownBitrixError(BitrixError):
    """Unmapped code. The published error list is not closed (research note (e))."""


# Keys are lower-cased; `classify` lower-cases the incoming code before the lookup.
_BY_ERROR_CODE: dict[str, type[BitrixError]] = {
    "expired_token": ExpiredToken,
    "no_auth_found": NoAuthFound,
    "invalid_credentials": InvalidCredentials,
    "access_denied": AccessDenied,
    "user_access_error": UserAccessError,
    "insufficient_scope": InsufficientScope,
    # The OAuth server spells the same condition `invalid_scope`; both mean "this app
    # was never granted that scope", which is a deploy-time bug, not a runtime state.
    "invalid_scope": InsufficientScope,
    "query_limit_exceeded": QueryLimitExceeded,
    "overload_limit": QueryLimitExceeded,
    "operation_time_limit": OperationTimeLimit,
    "error_method_not_found": MethodNotFound,
    "portal_deleted": PortalDeleted,
    "payment_required": PaymentRequired,
    "invalid_grant": InvalidGrant,
}

# Fallback only. 403 resolves to InvalidCredentials because that is the fail-closed
# reading (`acc='denied'`, §4.4 step 5): a status arriving without a code must never
# widen access. 500 is deliberately absent - INTERNAL_SERVER_ERROR / ERROR_UNEXPECTED_
# ANSWER are transient and belong in the generic retry path of §5.6, not in a typed
# branch that some caller might treat as terminal.
_BY_HTTP_STATUS: dict[int, type[BitrixError]] = {
    401: NoAuthFound,
    402: PaymentRequired,
    403: InvalidCredentials,
    429: OperationTimeLimit,
    503: QueryLimitExceeded,
}


def classify(
    error_code: str | None,
    *,
    http_status: int | None = None,
    description: str = "",
    payload: dict | None = None,
) -> BitrixError:
    """Build - never raise - the typed error for one Bitrix24 failure.

    Called by the HTTP layer for both the response envelope and each `result_error`
    entry of a halt=0 `batch`, which returns HTTP 200 with per-command errors (§5.6),
    so the status on its own can never be trusted. Defensive coercion everywhere: this
    function sits on the path that classifies attacker-influenced bodies, and a
    TypeError escaping here would turn a handled Bitrix error into a 500.
    """
    raw: str = error_code if isinstance(error_code, str) else ""
    key: str = raw.strip().lower()

    cls: type[BitrixError] | None = _BY_ERROR_CODE.get(key)
    status: int | None = http_status if isinstance(http_status, int) else None
    if cls is None and status is not None:
        cls = _BY_HTTP_STATUS.get(status)
    if cls is None:
        cls = UnknownBitrixError

    safe_description: str = description if isinstance(description, str) else str(description)
    safe_payload: dict = payload if isinstance(payload, dict) else {}
    return cls(
        raw.strip(),
        description=safe_description,
        http_status=status,
        payload=safe_payload,
    )
