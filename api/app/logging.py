"""JSON logging to stdout with mandatory redaction (§6).

WHY this module exists rather than a `logging.basicConfig` call:

* Docker's `json-file` driver is the second moderation trail (§6), so every line must be
  one JSON object on stdout - never a multi-line traceback that breaks log shipping.
* Redaction is not optional. The filter is attached to the HANDLER, so every record from
  every logger - ours, uvicorn's, a third-party library's - passes through it before it
  is serialized. §6: no token may ever reach a log.
* `httpx` and `httpcore` log full request URLs at INFO, which for the OAuth refresh means
  `client_secret` and `refresh_token` in plain text. They are pinned to WARNING; the
  redaction filter is the second layer, not the first.
* A `request_id` contextvar correlates every line of one request (and of one worker run)
  with the `rest_log.correlation_id` written for the same unit of work.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, Final

from app.config import settings
from app.security.redact import REDACTED, redact

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)

# Attributes the stdlib puts on every LogRecord; anything else is caller-supplied `extra`
# and therefore both redactable and worth emitting as a structured field.
_RESERVED_ATTRS: Final[frozenset[str]] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)

# Never let an `extra` key overwrite the envelope of the line.
_ENVELOPE_KEYS: Final[frozenset[str]] = frozenset(
    {"ts", "level", "logger", "msg", "request_id", "exc", "stack"}
)

# §6: their INFO lines contain the full OAuth URL.
_MUTED_LOGGERS: Final[tuple[str, ...]] = ("httpx", "httpcore", "hpack", "asyncio")


def set_request_id(request_id: str | None) -> None:
    """Bind the correlation id for the current context (set by the request middleware).

    No reset token is returned on purpose: each request/task runs in its own context copy,
    so the value cannot leak into the next one.
    """
    _request_id.set(request_id)


def get_request_id() -> str | None:
    """The correlation id of the current context, if one was bound."""
    return _request_id.get()


class RedactingFilter(logging.Filter):
    """Runs `redact()` over the record before anything can serialize it (§6).

    Attached to the root handler, so it covers records produced by code that never heard
    of this project. It always returns True: a redaction failure must drop the SECRET,
    never the log line.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            # Redacted as ONE mapping so a sensitive `extra` KEY is caught too:
            # extra={"access_token": "..."} must not survive just because its value
            # happens to be an innocent-looking string.
            extras = {
                key: value
                for key, value in record.__dict__.items()
                if key not in _RESERVED_ATTRS and not key.startswith("_")
            }
            if extras:
                safe = redact(extras)
                if isinstance(safe, dict):
                    record.__dict__.update(safe)
                else:  # redact() degraded the whole payload; drop the fields entirely
                    for key in extras:
                        record.__dict__[key] = REDACTED
            if isinstance(record.msg, str):
                record.msg = redact(record.msg)
            if record.args:
                # redact() preserves tuple/dict shape, so %-formatting still works.
                record.args = redact(record.args)
        except Exception:  # pragma: no cover - the filter itself must never break logging
            record.msg = "[redaction failed]"
            record.args = None
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line; tracebacks folded into a single `exc` string."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }

        request_id = record.__dict__.get("request_id") or get_request_id()
        if request_id:
            payload["request_id"] = request_id

        for key, value in record.__dict__.items():
            if key in _RESERVED_ATTRS or key in _ENVELOPE_KEYS or key.startswith("_"):
                continue
            payload[key] = value

        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        if record.stack_info:
            payload["stack"] = redact(self.formatStack(record.stack_info))

        # default=str keeps a stray datetime/UUID from turning a log line into an
        # exception inside the logging machinery.
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging() -> None:
    """Install the stdout JSON handler and the redaction filter. Idempotent."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactingFilter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(settings.log_level)

    for name in _MUTED_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    # uvicorn installs its own coloured handlers; strip them so its lines are emitted
    # once, as JSON, through the filter above rather than twice in two formats.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True


def get_logger(name: str) -> logging.Logger:
    """Module logger; `setup_logging()` configures the root these inherit from."""
    return logging.getLogger(name)
