"""The single outbound HTTP path to a portal's REST API (§4.1, §5.1, §5.6, §6).

Everything this layer does is deliberately narrow:

* **One request in, one typed outcome out.** A JSON `error`, a non-2xx status and a
  connect/DNS/TLS/timeout failure all become an `errors.BitrixError` subclass through
  `classify()` - the only place Bitrix24 error strings are ever compared (§4.4 step 5).
* **No retries, no backoff, no refresh.** By design. The refresh-once-then-fail rule lives
  in `bitrix/oauth.py::with_portal_token` (§5.8), the pacing / `Retry-After` / operating-time
  guard lives in `sync/throttle.py` (§5.6) and the failure counters live in the worker. A
  retry hidden down here would double-spend the shared 2 req/s bucket and the per-account
  operating-time budget without any of those layers seeing it.
* **One `rest_log` row per HTTP request** (§6), written through `services/rest_log.py` in
  its own transaction, on the success path and on the exception path alike. A batch is ONE
  row: the sub-commands in `request`, the `result_error` / `result_time` maps in `response`.

Two hard rules from §4.1 and §4.10 are enforced here rather than trusted:

1. The endpoint is supplied by the caller from an OAuth-derived `client_endpoint`. It is
   never built from a portal-sent `DOMAIN`; this module only checks that what it was given
   is an absolute http(s) URL and refuses anything else.
2. `auth=<access_token>` goes in the request BODY, never in the query string, because a
   query string reaches Caddy's and Bitrix24's access logs. For the same reason the logged
   URL carries no query at all.

Timeout is 120 s (§5.1): a hung request must never outlive the worker's 5-minute lease.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from email.utils import parsedate_to_datetime
from time import perf_counter
from typing import Any, Final
from urllib.parse import quote, urlsplit

import httpx

from app.bitrix.errors import BitrixError, TransportError, classify
from app.logging import get_logger
from app.services.rest_log import write_rest_log

__all__ = [
    "MAX_BATCH_COMMANDS",
    "BatchResult",
    "BitrixClient",
    "CommandResult",
    "php_query_pairs",
    "php_query_string",
]

logger = get_logger(__name__)

#: Documented ceiling; more gives HTTP 400 ERROR_BATCH_LENGTH_EXCEEDED (research note (e)).
MAX_BATCH_COMMANDS: Final = 50

#: §5.1: shorter than the 5-minute lease, longer than any legitimate 50-command batch.
_TIMEOUT_SECONDS: Final = 120.0

# A REST method name is always a constant in this codebase. Validating it keeps a `?`, `&`
# or `../` out of the URL and out of the `cmd[...]` string, where either would silently
# rewrite the request rather than fail it.
_METHOD_RE: Final = re.compile(r"^[A-Za-z0-9_.]+$")

# Batch keys become `cmd[<key>]`; a `[`, `]` or `&` there would corrupt the envelope.
_CMD_KEY_RE: Final = re.compile(r"^[A-Za-z0-9_.\-]{1,32}$")

# Deeper than any documented Bitrix24 parameter shape; a runaway structure is a bug.
_MAX_PARAM_DEPTH: Final = 8

# Retry-After comes from the portal side; clamp it so a stray value cannot park a portal
# for a year (§5.6 decides the real delay from it).
_MAX_RETRY_AFTER_SECONDS: Final = 86_400.0


# --------------------------------------------------------------------------- encoding


def _scalar(value: Any) -> str:
    """One leaf parameter as Bitrix24 (PHP) will read it.

    Booleans follow `http_build_query` semantics (`1` / `0`); a method that wants the
    literal `Y` / `N` / `true` spelling must pass that string - the caller knows which of
    the three conventions its method uses, this encoder cannot.
    """
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, str):
        return value
    if isinstance(value, (datetime, date)):
        # ISO 8601 with the `T` separator; `str(datetime)` uses a space, which Bitrix24's
        # date parser rejects for filter values like `>=CALL_START_DATE`.
        return value.isoformat()
    if isinstance(value, (int, float, Decimal, uuid.UUID)):
        return str(value)
    return str(value)


def _encode_into(out: list[tuple[str, str]], prefix: str, value: Any, *, depth: int) -> None:
    if value is None:
        # `http_build_query` drops NULLs; sending `KEY=` would mean "empty string", which
        # is a different filter.
        return
    if depth <= 0:
        raise ValueError(f"parameter nesting too deep at {prefix!r}")
    if isinstance(value, Mapping):
        for key, item in value.items():
            _encode_into(out, f"{prefix}[{key}]", item, depth=depth - 1)
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        # Explicit numeric indices rather than `KEY[]`: PHP reconstructs the same array and
        # the result is stable, which matters because these strings land in `rest_log`.
        items = sorted(value, key=str) if isinstance(value, (set, frozenset)) else list(value)
        for index, item in enumerate(items):
            _encode_into(out, f"{prefix}[{index}]", item, depth=depth - 1)
        return
    out.append((prefix, _scalar(value)))


def php_query_pairs(params: Mapping[str, Any] | None) -> list[tuple[str, str]]:
    """Flatten nested params into PHP array-style `name` / `value` pairs, NOT url-encoded.

    WHY this exists and why it is tested: `voximplant.statistic.get` is driven entirely by
    `FILTER[>ID]` / `FILTER[<ID]` (§5.2-§5.4). An encoder that loses the bracket path sends
    an unfiltered request that returns HTTP 200 with the wrong rows - the cursor then walks
    over data it never saw, which is the worst failure mode in this system. So the shape is
    produced in exactly one place::

        {"FILTER": {">ID": 5}, "SORT": "ID"}  ->  [("FILTER[>ID]", "5"), ("SORT", "ID")]
    """
    if not params:
        return []
    out: list[tuple[str, str]] = []
    for key, value in params.items():
        _encode_into(out, str(key), value, depth=_MAX_PARAM_DEPTH)
    return out


def php_query_string(params: Mapping[str, Any] | None) -> str:
    """The pairs above as one url-encoded query string, for the `cmd[key]=method?...` form.

    `safe=""` encodes the brackets and the comparison operators (`FILTER%5B%3EID%5D=5`),
    which is what PHP's own `http_build_query` emits and what `parse_str` reverses. The
    whole string is then form-encoded once more as the value of `cmd[key]`, so Bitrix24
    decodes exactly twice - once for the batch envelope, once for the sub-request.
    """
    return "&".join(
        f"{quote(key, safe='')}={quote(value, safe='')}" for key, value in php_query_pairs(params)
    )


def _parse_retry_after(raw: str | None) -> float | None:
    """`Retry-After` as seconds, from either documented form (§5.6 honours it on 429/503)."""
    if not raw:
        return None
    text = raw.strip()
    try:
        seconds = float(text)
    except ValueError:
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:  # an HTTP-date without a zone is GMT by definition
            when = when.replace(tzinfo=UTC)
        seconds = (when - datetime.now(UTC)).total_seconds()
    if not math.isfinite(seconds):  # "nan" / "inf" parse as floats but are not delays
        return None
    return max(0.0, min(seconds, _MAX_RETRY_AFTER_SECONDS))


# ---------------------------------------------------------------------------- results


@dataclass(frozen=True)
class CommandResult:
    """One sub-command of a `batch`, in the position it was requested in.

    `error` is a value, never a raised exception: with `halt=0` Bitrix24 answers HTTP 200
    and reports per-command failures in `result_error`, and §5.2's contiguous-prefix rule
    needs to see the whole ordered list to decide how far the cursor may advance.

    `next` and `total` carry `result_next` / `result_total`, which the envelope keeps
    OUTSIDE `result`; §5.4 sizes the next batch from them.
    """

    key: str
    result: Any | None
    error: BitrixError | None
    time: dict[str, Any] | None
    next: int | None = None
    total: int | None = None


@dataclass(frozen=True)
class BatchResult:
    """The ordered outcome of one `batch` HTTP request plus the batch's own `time{}` block."""

    commands: tuple[CommandResult, ...]
    time: dict[str, Any] | None

    def _find(self, key: str) -> CommandResult | None:
        for command in self.commands:
            if command.key == key:
                return command
        return None

    def get(self, key: str) -> Any | None:
        """The result of one command; None when it errored or was never requested."""
        command = self._find(key)
        return None if command is None or command.error is not None else command.result

    def error(self, key: str) -> BitrixError | None:
        """The typed error of one command, or None."""
        command = self._find(key)
        return None if command is None else command.error

    def ok(self, key: str) -> bool:
        """True when the command was requested and returned without an error."""
        command = self._find(key)
        return command is not None and command.error is None

    def next_of(self, key: str) -> int | None:
        """`result_next` for one command - the offset of its following page (§5.4)."""
        command = self._find(key)
        return None if command is None else command.next

    def total_of(self, key: str) -> int | None:
        """`result_total` for one command - the size of its selection (§5.2, §5.4)."""
        command = self._find(key)
        return None if command is None else command.total

    @property
    def first_error_index(self) -> int | None:
        """Index of the first failed command; None when every command is clean.

        This is the whole input to §5.2's contiguous-prefix rule: the cursor may advance
        only across `commands[:first_error_index]`.
        """
        for index, command in enumerate(self.commands):
            if command.error is not None:
                return index
        return None


def _pick(container: Any, key: str, index: int) -> Any:
    """Read one command's entry from a `result_*` map.

    Bitrix24 returns these keyed by command key, but PHP renders an array whose keys are
    sequential integers as a JSON LIST - which is what an empty map (`[]`) always looks
    like, and what numeric command keys would produce. Both shapes are read here so the
    difference never reaches the sync layer.
    """
    if isinstance(container, Mapping):
        return container.get(key)
    if isinstance(container, (list, tuple)):
        return container[index] if 0 <= index < len(container) else None
    return None


def _as_int(value: Any) -> int | None:
    """`result_next` / `result_total` arrive as strings on some builds."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _command_error(raw: Any, http_status: int | None) -> BitrixError | None:
    """One `result_error` entry -> a typed error, or None when the command was clean.

    An entry may be `{"error": ..., "error_description": ...}`, a bare string, or an empty
    container that PHP rendered for "no error"; only a real code becomes an error.
    """
    if raw is None:
        return None
    if isinstance(raw, Mapping):
        if not raw:
            return None
        code = raw.get("error") or raw.get("error_code") or ""
        description = raw.get("error_description") or raw.get("error_information") or ""
        return classify(
            str(code) if code else None,
            http_status=http_status,
            description=str(description),
            payload=dict(raw),
        )
    if isinstance(raw, str):
        return classify(raw, http_status=http_status) if raw.strip() else None
    if isinstance(raw, (list, tuple)) and not raw:
        return None
    return classify(None, http_status=http_status, description=str(raw))


# ----------------------------------------------------------------------------- client


class BitrixClient:
    """One httpx client bound to one portal endpoint and (optionally) one access token.

    Not shared between portals on purpose: the endpoint is part of the trust decision of
    §4.1 (a token is only ever presented at the endpoint it was proven at), and one
    connection pool per portal keeps a slow portal out of another portal's way.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        access_token: str | None = None,
        portal_id: int | None = None,
        member_id: str | None = None,
        token_user_id: int | None = None,
        correlation_id: uuid.UUID | None = None,
    ) -> None:
        self._endpoint = self._normalize_endpoint(endpoint)
        self._access_token = access_token
        self._portal_id = portal_id
        self._member_id = member_id
        self._token_user_id = token_user_id
        self._correlation_id = correlation_id
        self._last_time: dict[str, Any] | None = None
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(_TIMEOUT_SECONDS),
            # §4.1: a redirect would move the request off the endpoint the token was proven
            # at, so it is surfaced as an error instead of being followed.
            follow_redirects=False,
            headers={"Accept": "application/json", "User-Agent": "CallAnalytics/1"},
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        )

    # ---------------------------------------------------------------- lifecycle

    async def aclose(self) -> None:
        """Release the connection pool. Safe to call twice."""
        await self._client.aclose()

    async def __aenter__(self) -> BitrixClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------- public

    @property
    def endpoint(self) -> str:
        """The normalized REST base this client talks to (always ends in `/`)."""
        return self._endpoint

    @property
    def last_time(self) -> dict[str, Any] | None:
        """The `time{}` block of the most recent response, error responses included.

        §5.6 reads `operating` / `operating_reset_at` from it to drive the operating-time
        guard, which must keep working across a 429 - hence "including errors".
        """
        return self._last_time

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Call one REST method and return its `result` value.

        Raises the typed error for a JSON `error`, a non-2xx status or a transport failure;
        never retries (§5.8 owns that decision).
        """
        name = self._check_method(method)
        body = self._form_body(php_query_pairs(params))

        def analyze(payload: Any) -> tuple[Any, str | None]:
            # §6 keeps the whole body for a single call; the cap is applied by the writer.
            return payload, None

        response_body, _ = await self._send(
            log_method=name,
            url=f"{self._endpoint}{name}",
            body=body,
            log_request=params or {},
            analyze=analyze,
        )
        return response_body.get("result") if isinstance(response_body, Mapping) else None

    async def batch(
        self,
        commands: Sequence[tuple[str, str, dict[str, Any]]],
        *,
        halt: int = 0,
    ) -> BatchResult:
        """Run up to 50 `(key, method, params)` commands in one HTTP request.

        The returned `commands` tuple is in the ORDER REQUESTED, not the order Bitrix24
        chose to key its maps in: §5.2's contiguous-prefix rule is defined on that order,
        and a reordering here would let a cursor jump over a failed page.

        `halt=0` (the default) means per-command errors come back as values in
        `result_error`; they are classified into `CommandResult.error` and never raised.
        A failure of the batch REQUEST itself (transport, non-2xx, top-level `error`) is
        still an exception - nothing about the batch succeeded in that case.
        """
        if not commands:
            raise ValueError("batch requires at least one command")
        if len(commands) > MAX_BATCH_COMMANDS:
            # Bitrix24 answers ERROR_BATCH_LENGTH_EXCEEDED; failing here costs no request
            # against the shared 2 req/s bucket.
            raise ValueError(f"batch accepts at most {MAX_BATCH_COMMANDS} commands")

        keys: list[str] = []
        cmd_map: dict[str, str] = {}
        for key, method, params in commands:
            if not _CMD_KEY_RE.match(key):
                raise ValueError(f"invalid batch command key: {key!r}")
            if key in cmd_map:
                raise ValueError(f"duplicate batch command key: {key!r}")
            name = self._check_method(method)
            query = php_query_string(params)
            cmd_map[key] = f"{name}?{query}" if query else name
            keys.append(key)

        halt_flag = 1 if halt else 0
        body = self._form_body(php_query_pairs({"halt": halt_flag, "cmd": cmd_map}))
        log_request: dict[str, Any] = {"halt": halt_flag, "cmd": cmd_map}

        def analyze(payload: Any) -> tuple[Any, str | None]:
            """Log the envelope, not the rows: §6 wants result_error / result_time here."""
            if not isinstance(payload, Mapping):
                return payload, None
            envelope = payload.get("result")
            if not isinstance(envelope, Mapping):
                return payload, None
            logged: dict[str, Any] = {
                "result_error": envelope.get("result_error"),
                "result_next": envelope.get("result_next"),
                "result_total": envelope.get("result_total"),
                "result_time": envelope.get("result_time"),
            }
            first: str | None = None
            errors = envelope.get("result_error")
            for index, key in enumerate(keys):
                error = _command_error(_pick(errors, key, index), None)
                if error is not None:
                    first = error.code or "unknown"
                    break
            return logged, first

        response_body, http_status = await self._send(
            log_method="batch",
            url=f"{self._endpoint}batch",
            body=body,
            log_request=log_request,
            analyze=analyze,
        )

        envelope = response_body.get("result") if isinstance(response_body, Mapping) else None
        if not isinstance(envelope, Mapping):
            # HTTP 200 with no batch envelope: a failed request, not 50 silently empty
            # commands - the latter would let a cursor walk past pages it never read.
            raise classify(
                None,
                http_status=http_status,
                description="batch response carried no result envelope",
                payload=dict(response_body) if isinstance(response_body, Mapping) else {},
            )

        results = envelope.get("result")
        errors = envelope.get("result_error")
        times = envelope.get("result_time")
        nexts = envelope.get("result_next")
        totals = envelope.get("result_total")

        built: list[CommandResult] = []
        for index, key in enumerate(keys):
            error = _command_error(_pick(errors, key, index), http_status)
            time_block = _pick(times, key, index)
            built.append(
                CommandResult(
                    key=key,
                    result=None if error is not None else _pick(results, key, index),
                    error=error,
                    time=dict(time_block) if isinstance(time_block, Mapping) else None,
                    next=_as_int(_pick(nexts, key, index)),
                    total=_as_int(_pick(totals, key, index)),
                )
            )

        batch_time = response_body.get("time") if isinstance(response_body, Mapping) else None
        return BatchResult(
            commands=tuple(built),
            time=dict(batch_time) if isinstance(batch_time, Mapping) else None,
        )

    # ---------------------------------------------------------------- internals

    @staticmethod
    def _normalize_endpoint(endpoint: str) -> str:
        """Validate and canonicalize the REST base (§4.1).

        This is the last line of the "never build a REST base from DOMAIN" rule: what
        arrives here must already be an OAuth-derived `client_endpoint`, and anything that
        is not an absolute http(s) URL is refused loudly instead of being requested.
        """
        raw = (endpoint or "").strip()
        parts = urlsplit(raw)
        if parts.scheme not in ("https", "http") or not parts.netloc:
            raise ValueError(f"endpoint must be an absolute http(s) URL, got {raw!r}")
        if parts.query or parts.fragment:
            raise ValueError("endpoint must carry no query string or fragment")
        return raw if raw.endswith("/") else f"{raw}/"

    @staticmethod
    def _check_method(method: str) -> str:
        """Reject anything that is not a plain `scope.method` name (see `_METHOD_RE`)."""
        name = (method or "").strip()
        if not _METHOD_RE.match(name):
            raise ValueError(f"invalid REST method name: {method!r}")
        return name

    def _form_body(self, pairs: Sequence[tuple[str, str]]) -> dict[str, str]:
        """Flattened params plus `auth` - in the BODY, never in the query string (§4.10)."""
        body: dict[str, str] = {}
        for key, value in pairs:
            if key in body:
                raise ValueError(f"duplicate parameter after flattening: {key!r}")
            body[key] = value
        if self._access_token:
            # Set last on purpose: the token this client was constructed with always wins
            # over anything a caller happened to put in `params`.
            body["auth"] = self._access_token
        return body

    async def _send(
        self,
        *,
        log_method: str,
        url: str,
        body: dict[str, str],
        log_request: Any,
        analyze: Callable[[Any], tuple[Any, str | None]],
    ) -> tuple[Any, int | None]:
        """POST once, write exactly one `rest_log` row, raise the typed error (§6).

        The row is written from a `finally` block so the success path, the Bitrix24-error
        path and the transport path each produce exactly one row, and the row exists even
        though the caller's work transaction is about to be rolled back (§6, decision 20).
        """
        started = perf_counter()
        http_status: int | None = None
        error_code: str | None = None
        time_block: dict[str, Any] | None = None
        log_response: Any = None
        try:
            try:
                response = await self._client.post(url, data=body)
            except httpx.HTTPError as exc:
                # DNS / TLS / connect / read timeout: no response at all. §4.4 step 4 and
                # §5.8 branch on this exact type to re-learn `client_endpoint`.
                raise TransportError(description=f"{type(exc).__name__}: {exc}") from exc

            http_status = response.status_code
            retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            payload = self._decode(response)

            if isinstance(payload, Mapping):
                raw_time = payload.get("time")
                if isinstance(raw_time, Mapping):
                    time_block = dict(raw_time)
                    self._last_time = time_block

            log_response, error_code = analyze(payload)

            failure = self._envelope_error(payload, http_status, retry_after)
            if failure is not None:
                raise failure
            return payload, http_status
        except BitrixError as exc:
            error_code = exc.code or error_code
            raise
        finally:
            await write_rest_log(
                direction="out",
                kind="rest",
                method=log_method,
                url=url,
                portal_id=self._portal_id,
                member_id=self._member_id,
                correlation_id=self._correlation_id,
                token_user_id=self._token_user_id,
                request=log_request,
                http_status=http_status,
                error_code=error_code,
                response=log_response,
                time_block=time_block,
                duration_ms=int((perf_counter() - started) * 1000),
            )

    @staticmethod
    def _decode(response: httpx.Response) -> Any:
        """The JSON body, or a wrapper carrying the first bytes of a non-JSON answer.

        A proxy error page or a Bitrix24 maintenance splash arrives as HTML with any status
        code; it must not raise a JSONDecodeError out of this module, because every caller
        branches on `BitrixError` and nothing else.
        """
        try:
            return response.json()
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            return {"_non_json_body": response.text[:1000]}

    @staticmethod
    def _envelope_error(
        payload: Any, http_status: int | None, retry_after: float | None
    ) -> BitrixError | None:
        """Classify a failed exchange, or return None when the response is usable.

        The JSON `error` is checked BEFORE the status because Bitrix24 documents one status
        for several distinct decisions (§4.4 step 5 renders a different page for each), and
        a `halt=0` batch reports HTTP 200 with per-command errors, so the status alone
        proves nothing. `Retry-After` is carried on `payload["retry_after"]` so §5.6's
        throttle can honour it without re-reading the response headers.
        """
        status_ok = http_status is not None and 200 <= http_status < 300
        body: dict[str, Any] = dict(payload) if isinstance(payload, Mapping) else {}
        code = body.get("error")
        has_code = isinstance(code, str) and code.strip() != ""

        if status_ok and not has_code and "_non_json_body" not in body:
            return None

        if retry_after is not None:
            body["retry_after"] = retry_after
        description = body.get("error_description") or body.get("error_information") or ""
        if not description and "_non_json_body" in body:
            description = "non-JSON response body"
        return classify(
            code if has_code else None,
            http_status=http_status,
            description=str(description),
            payload=body,
        )
