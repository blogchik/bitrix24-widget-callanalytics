"""Recursive redaction of secret-bearing values.

WHY: §6 requires that no token, secret or auth parameter can ever reach stdout or the
`rest_log` table. Both writers - the logging filter (app/logging.py) and the REST log
writer (bitrix/client.py) - run every payload through `redact()` first, so the rule is
enforced in one place and covers payload shapes we have not seen yet (`auth[*]` bracket
bodies, `data{}` of ONAPPUSERREADY, refresh responses, future fields).

Two properties matter as much as the redaction itself:

* It NEVER raises. A redaction bug must not take down a request or lose a log line;
  anything unexpected degrades to a placeholder string.
* It never mutates its input. Callers keep passing the original object on to Bitrix24 or
  to the database; the redacted copy exists only for the log.

Everything here is pure: no module state, no configuration, no I/O - so it is directly
unit-testable and cheap enough to run on every request.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence, Set
from typing import Any, Final

REDACTED: Final = "[redacted]"
TRUNCATED: Final = "[truncated]"

# §6: any key whose NAME suggests a credential, at any depth. `client_id` is included
# because it identifies our vendor application in support-visible logs.
_SENSITIVE_KEY_RE: Final = re.compile(r"(token|secret|auth|password|client_id)", re.IGNORECASE)

# Query/form parameters carrying a credential inside a URL string. The OAuth refresh URL
# is the reason this exists: httpx-style "GET https://.../oauth/token/?client_secret=..."
# lines are the classic way a secret escapes into a log.
_SENSITIVE_PARAM_RE: Final = re.compile(
    r"(?i)\b(auth|token|access_token|refresh_token|client_secret|client_id|password|secret)"
    r"=([^&\s\"'<>#]*)"
)

# Recursing into a container of containers is bounded; deeper structures are almost
# certainly a cycle or a mistake, and truncating is safer than walking forever.
_DEFAULT_MAX_DEPTH: Final = 12


def is_sensitive_key(key: Any) -> bool:
    """True when a mapping key names a credential (§6 key regex)."""
    try:
        return _SENSITIVE_KEY_RE.search(key if isinstance(key, str) else str(key)) is not None
    except Exception:  # pragma: no cover - str() of a hostile object
        return True  # unknown key shape: redact rather than leak


def redact_text(value: str) -> str:
    """Rewrite credential-bearing `name=value` parameters inside a string.

    WHY: URLs reach logs as plain strings (request lines, exception messages, Location
    headers), so key-based redaction alone would miss them.
    """
    if "=" not in value:
        return value
    if _SENSITIVE_PARAM_RE.search(value) is None:
        return value
    return _SENSITIVE_PARAM_RE.sub(lambda m: f"{m.group(1)}={REDACTED}", value)


def redact(obj: Any, *, max_depth: int = _DEFAULT_MAX_DEPTH) -> Any:
    """Return a redacted copy of `obj`; never mutates it and never raises."""
    try:
        return _walk(obj, depth=max_depth, seen=frozenset())
    except Exception:  # pragma: no cover - defence in depth (§6: a failure must not leak)
        return REDACTED


def _walk(obj: Any, *, depth: int, seen: frozenset[int]) -> Any:
    if depth <= 0:
        return TRUNCATED

    if obj is None or isinstance(obj, (bool, int, float)):
        return obj

    if isinstance(obj, str):
        return redact_text(obj)

    if isinstance(obj, (bytes, bytearray, memoryview)):
        # Raw bytes in a log payload are either binary noise or a key; neither belongs there.
        return REDACTED

    if isinstance(obj, Mapping):
        marker = id(obj)
        if marker in seen:  # a cycle: stop instead of raising RecursionError
            return TRUNCATED
        inner = seen | {marker}
        out: dict[Any, Any] = {}
        for key, value in obj.items():
            safe_key = key if isinstance(key, (str, int, bool)) else str(key)
            if is_sensitive_key(key):
                out[safe_key] = REDACTED
            else:
                out[safe_key] = _walk(value, depth=depth - 1, seen=inner)
        return out

    if isinstance(obj, (Sequence, Set)):  # str/bytes already handled above
        marker = id(obj)
        if marker in seen:
            return TRUNCATED
        inner = seen | {marker}
        items = [_walk(item, depth=depth - 1, seen=inner) for item in obj]
        if isinstance(obj, tuple):
            # Preserved as a tuple because logging uses record.args for %-formatting.
            return tuple(items)
        return items

    # Anything else (datetime, Decimal, UUID, an ORM object): left alone. The serializer
    # stringifies it, and its own repr goes through no redaction we can do generically -
    # so treat only its string form defensively.
    return _walk_repr(obj)


def _walk_repr(obj: Any) -> Any:
    """Last resort for objects the serializer will stringify anyway."""
    try:
        text = str(obj)
    except Exception:  # pragma: no cover - a broken __str__
        return REDACTED
    redacted = redact_text(text)
    # Only pay the substitution cost when something was actually found; otherwise hand
    # the original object back so the caller's serializer can format it properly.
    return obj if redacted == text else redacted
