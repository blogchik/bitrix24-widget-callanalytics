"""The one writer of `rest_log` (§6).

WHY a service of its own rather than a helper inside `bitrix/client.py`:

* **It must survive the failure it is describing.** §6 / decision 20 require the exchange
  to be on record even when the work transaction that triggered it rolls back, so this
  function opens its OWN `control_txn()` - never the caller's session - and is called on
  the exception path before the error is re-raised.
* **It must never break the work.** A moderation log that can raise turns a recoverable
  Bitrix24 error into a 500 and, worse, hides the exchange it was meant to record. Every
  path here is wrapped: on any failure it emits one warning line and returns.
* **It is the redaction choke point.** Both directions (outbound REST/OAuth from
  `bitrix/*`, inbound POSTs from `handlers/*`) funnel through here, so `redact()` and the
  `REST_LOG_BODY_LIMIT` cap are applied in exactly one place and cannot be forgotten by a
  new call site (§6: no token may ever reach the table).

Bodies are round-tripped through `json.dumps(default=str)` before they are handed to the
JSONB column: the redacted copy may still contain a `datetime`, `Decimal` or `UUID` that
SQLAlchemy's serializer would refuse, and losing the row to a TypeError is exactly the
outcome this module exists to prevent.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Final

from app.config import settings
from app.db.models import RestLog
from app.db.session import control_txn
from app.logging import get_logger, get_request_id
from app.security.redact import redact

__all__ = ["write_rest_log"]

logger = get_logger(__name__)

# Column widths of §3; a value longer than the column would abort the INSERT and lose the
# row, so they are trimmed here rather than trusted from the caller.
_METHOD_MAX: Final = 128
_MEMBER_ID_MAX: Final = 32
_ERROR_CODE_MAX: Final = 64


def _trim(value: str | None, limit: int) -> str | None:
    """Empty string -> NULL, over-long -> cut. Keeps a bad value from aborting the INSERT."""
    if value is None:
        return None
    text = str(value)
    return text[:limit] if text else None


def _correlation(correlation_id: uuid.UUID | None) -> uuid.UUID | None:
    """Fall back to the ambient request id so a log row is still correlatable (§6).

    The middleware binds a request id per request and per worker run; when a caller did
    not thread an explicit correlation id through, that value is the same identifier.
    """
    if correlation_id is not None:
        return correlation_id
    raw = get_request_id()
    if not raw:
        return None
    try:
        return uuid.UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        return None


def _prepare_body(value: Any, limit: int) -> tuple[dict[str, Any] | None, int | None, bool]:
    """Redact, JSON-normalize and cap one body. Returns (stored, byte length, truncated).

    §6: the cap is on the SERIALIZED size, because that is what the JSONB column pays for.
    A body over the limit is replaced by a marker object carrying a UTF-8-safe prefix, so a
    moderator still sees the shape of what was exchanged without the table growing without
    bound.
    """
    if value is None:
        return None, None, False

    text = json.dumps(redact(value), ensure_ascii=False, default=str)
    raw = text.encode("utf-8")
    size = len(raw)
    if size > limit:
        preview = raw[:limit].decode("utf-8", "ignore")
        return {"truncated": True, "bytes": size, "preview": preview}, size, True

    parsed: Any = json.loads(text)
    if not isinstance(parsed, dict):
        # The column is JSONB, but the ORM type is a mapping; wrapping keeps every reader
        # (and `redact_on_clean`, which NULLs these columns) on one shape.
        parsed = {"value": parsed}
    return parsed, size, False


async def write_rest_log(
    *,
    direction: str,
    kind: str,
    method: str,
    url: str,
    portal_id: int | None,
    member_id: str | None,
    correlation_id: uuid.UUID | None,
    token_user_id: int | None,
    request: Any = None,
    http_status: int | None = None,
    error_code: str | None = None,
    response: Any = None,
    time_block: dict | None = None,
    duration_ms: int | None = None,
) -> None:
    """Record exactly one exchange (§6). Opens its own transaction; never raises.

    `direction` is `out` (we called Bitrix24) or `in` (Bitrix24 posted to us); `kind` is
    one of `rest`, `oauth`, `event`, `open`, `install` - both are CHECK-constrained in §3,
    and a value outside them is caught by the blanket except below rather than by an
    assertion, because a bad log call must still not break the caller's work.
    """
    try:
        limit = settings.rest_log_body_limit
        stored_request, _, _ = _prepare_body(request, limit)
        stored_response, response_bytes, truncated = _prepare_body(response, limit)
        stored_time, _, _ = _prepare_body(time_block, limit)

        row = RestLog(
            portal_id=portal_id,
            member_id=_trim(member_id, _MEMBER_ID_MAX),
            correlation_id=_correlation(correlation_id),
            direction=direction,
            kind=kind,
            method=str(method)[:_METHOD_MAX],
            # §6: the URL is stored without its query string. Nothing in this codebase
            # puts a parameter there (§4.10), but an inbound handler may pass one through.
            url=str(url).split("?", 1)[0],
            token_user_id=token_user_id,
            request=stored_request,
            http_status=http_status,
            error_code=_trim(error_code, _ERROR_CODE_MAX),
            response=stored_response,
            response_bytes=response_bytes,
            truncated=truncated,
            time_block=stored_time,
            duration_ms=duration_ms,
        )
        async with control_txn() as session:
            session.add(row)
    except Exception:  # a logging failure must never fail the work (§6)
        # No body, no ids beyond the coarse ones: this line lands on stdout, which is the
        # second moderation trail, and it must not become the leak the table prevents.
        logger.warning(
            "rest_log write failed",
            exc_info=True,
            extra={"log_direction": direction, "log_kind": kind, "log_method": str(method)[:64]},
        )
