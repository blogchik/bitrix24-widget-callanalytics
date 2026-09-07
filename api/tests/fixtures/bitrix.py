"""A fake Bitrix24 REST + OAuth server, and the payload/database builders around it.

WHY this exists as one shared module: milestone 2 has four behavioural tests
(`test_client_refresh`, `test_refresh_singleflight`, `test_install_guard`,
`test_secret_logging`) that all need the *same* thing - a Bitrix24 that answers
realistically, counts exactly how many HTTP round trips were made, and can be told to
fail one command in one specific way. §5.8 and §4.3 are both "exactly one refresh"
rules, so the count is the assertion; a per-test ad-hoc stub would make those counts
mean four slightly different things.

How the transport is injected
-----------------------------
Neither `BitrixClient` nor `oauth.exchange_refresh_token` takes a transport in the
milestone-2 contract, so `patch_httpx()` wraps `httpx.AsyncClient.__init__` and fills in
`transport=` **only when the caller did not pass one**. That leaves an explicit
`httpx.ASGITransport(app=...)` (how `test_install_guard` drives FastAPI) untouched while
every client the application code builds for itself lands on the fake.

Nothing here asserts. The fake records; the tests decide what the recording must say.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from urllib.parse import parse_qsl

import httpx
from sqlalchemy import text

from app.db.session import control_txn
from app.security.crypto import encrypt

__all__ = [
    "APP_KEY",
    "CLIENT_ENDPOINT",
    "DOMAIN",
    "FRESH_ACCESS",
    "FRESH_REFRESH",
    "OAUTH_HOST",
    "SEED_ACCESS",
    "SEED_REFRESH",
    "SERVER_ENDPOINT",
    "USER_AUTH",
    "USER_REFRESH",
    "Err",
    "FakeBitrix",
    "RecordedRequest",
    "SeededPortal",
    "clear_rest_logs",
    "delete_portal",
    "fetch_rest_logs",
    "install_form",
    "install_query",
    "new_member_id",
    "patch_httpx",
    "portal_snapshot",
    "portal_sync_snapshot",
    "seed_portal",
    "token_response",
]

# --- constants the tests assert on --------------------------------------------------
#
# The sentinel strings are long, unique and pronounceable-in-a-grep on purpose:
# `test_secret_logging` proves they appear in NO log line and NO rest_log column, and a
# short value like "abc" would collide with real text and make that test lie.

OAUTH_HOST: Final[str] = "oauth.bitrix.info"
DOMAIN: Final[str] = "portal.bitrix24.test"
CLIENT_ENDPOINT: Final[str] = f"https://{DOMAIN}/rest/"
SERVER_ENDPOINT: Final[str] = f"https://{OAUTH_HOST}/rest/"

#: The credential a seeded portal already holds (what `store_portal_credential` wrote).
SEED_ACCESS: Final[str] = "seedaccess.5f1c9a2e4b7d8c6a0e3f1b2d4a6c8e0f"
SEED_REFRESH: Final[str] = "seedrefresh.9c2e4a6b8d0f1a3c5e7b9d1f3a5c7e9b"
#: What the fake OAuth server hands back on a refresh (§5.8 step 4 rotates both).
FRESH_ACCESS: Final[str] = "freshaccess.11223344556677889900aabbccddeeff"
FRESH_REFRESH: Final[str] = "freshrefresh.ffeeddccbbaa00998877665544332211"
#: The pair a browser/iframe POST carries (AUTH_ID / REFRESH_ID) - a *user's* token.
USER_AUTH: Final[str] = "userauth.0a1b2c3d4e5f60718293a4b5c6d7e8f9"
USER_REFRESH: Final[str] = "userrefresh.f9e8d7c6b5a4938271605f4e3d2c1b0a"
#: APPLICATION_TOKEN: `[A-Za-z0-9]{8,128}` per §4.2, so no punctuation here.
APP_KEY: Final[str] = "applicationkey0011223344556677889900aabb"

_DEFAULT_TOKEN_USER_ID: Final[int] = 42

# HTTP status Bitrix24 documents for each error string (research note (e)). Only used
# to give a scripted `Err` a realistic status when the test does not pin one.
_STATUS_BY_CODE: Final[Mapping[str, int]] = {
    "expired_token": 401,
    "no_auth_found": 401,
    "access_denied": 403,
    "invalid_credentials": 403,
    "insufficient_scope": 403,
    "user_access_error": 403,
    "payment_required": 402,
    "operation_time_limit": 429,
    "query_limit_exceeded": 503,
    "overload_limit": 503,
    "portal_deleted": 500,
    "invalid_grant": 400,
    "error_method_not_found": 400,
}

_CMD_KEY_RE: Final[re.Pattern[str]] = re.compile(r"^cmd\[([^\[\]]+)\]$")
_HEX32_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{32}$")


def new_member_id() -> str:
    """A fresh tenant key in the exact shape §4.2 and `portals_member_id_fmt` demand."""
    member_id = uuid.uuid4().hex
    assert _HEX32_RE.match(member_id)  # guards the fixture, not the app
    return member_id


@dataclass(frozen=True)
class Err:
    """One scripted Bitrix24 failure.

    Deliberately NOT an `app.bitrix.errors` class: the fake produces the wire shape
    (`{"error": ..., "error_description": ...}`) and lets `classify()` remain the only
    place that turns a string into a type (§4.1 / errors.py docstring).
    """

    code: str
    description: str = ""
    status: int | None = None

    @property
    def http_status(self) -> int:
        if self.status is not None:
            return self.status
        return _STATUS_BY_CODE.get(self.code.strip().lower(), 400)

    def body(self) -> dict[str, str]:
        return {"error": self.code, "error_description": self.description}


#: A scripted answer: a raw result value, an `Err`, or a callable taking the recorded
#: request and returning either of those (used when the answer depends on the params).
Scripted = Any


def token_response(
    *,
    access_token: str = FRESH_ACCESS,
    refresh_token: str = FRESH_REFRESH,
    member_id: str,
    client_endpoint: str = CLIENT_ENDPOINT,
    server_endpoint: str = SERVER_ENDPOINT,
    expires_in: int = 3600,
    scope: str = "crm,telephony,user_brief,placement",
    status: str = "F",
    user_id: int = _DEFAULT_TOKEN_USER_ID,
    domain: str = OAUTH_HOST,
) -> dict[str, Any]:
    """The documented refresh-grant response body, verbatim in shape.

    Note `domain` is the AUTHORIZATION server, not the portal (research note (b)): a
    handler that copied it into `portals.domain` would rename every tenant to
    `oauth.bitrix.info`, so the default here is deliberately the wrong-looking value.
    """
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires": int(datetime.now(tz=UTC).timestamp()) + expires_in,
        "expires_in": expires_in,
        "scope": scope,
        "domain": domain,
        "server_endpoint": server_endpoint,
        "client_endpoint": client_endpoint,
        "member_id": member_id,
        "user_id": user_id,
        "status": status,
    }


def _time_block(operating: float = 0.35) -> dict[str, Any]:
    """The `time{}` block §5.6 reads for the operating-limit budget."""
    now = datetime.now(tz=UTC)
    return {
        "start": now.timestamp(),
        "finish": now.timestamp() + operating,
        "duration": operating,
        "processing": operating,
        "date_start": now.isoformat(),
        "date_finish": (now + timedelta(seconds=operating)).isoformat(),
        "operating_reset_at": int((now + timedelta(minutes=10)).timestamp()),
        "operating": operating,
    }


def user_current(
    user_id: int = _DEFAULT_TOKEN_USER_ID, timezone: str = "Asia/Tashkent"
) -> dict[str, Any]:
    """`user.current` under the `user_brief` scope: ids arrive as STRINGS from Bitrix24."""
    return {
        "ID": str(user_id),
        "ACTIVE": True,
        "NAME": "Aziza",
        "LAST_NAME": "Karimova",
        "SECOND_NAME": "",
        "WORK_POSITION": "Head of Sales",
        "PERSONAL_PHOTO": "https://portal.bitrix24.test/upload/avatar.png",
        "TIME_ZONE": timezone,
        "UF_DEPARTMENT": [1],
    }


def app_info(installed: bool = True, version: int = 3) -> dict[str, Any]:
    return {
        "ID": 12,
        "CODE": "texnobus.callanalytics",
        "VERSION": version,
        "STATUS": "F",
        "INSTALLED": installed,
        "PAYMENT_EXPIRED": "N",
        "DAYS": None,
        "LANGUAGE_ID": "ru",
        "LICENSE": "kz_ent250",
        "LICENSE_TYPE": "ent250",
        "LICENSE_FAMILY": "ent",
    }


def method_get(exists: bool = True) -> dict[str, Any]:
    """`method.get {name: voximplant.statistic.get}` - the §4.3 step 3 capability probe."""
    return {"isExisting": exists, "isAvailable": exists}


#: Answers for every method the install/open flows call, so a test only has to script
#: the ONE command whose failure it is about.
_DEFAULT_RESULTS: Final[Mapping[str, Any]] = {
    "user.current": user_current(),
    "user.admin": True,
    "app.info": app_info(),
    "method.get": method_get(True),
    "placement.get": [],
    "placement.bind": True,
    "placement.unbind": 1,
    "event.bind": True,
    "voximplant.statistic.get": [],
    "user.get": [],
}


@dataclass(frozen=True)
class RecordedRequest:
    """One HTTP round trip the application actually made, as the fake saw it."""

    kind: str  # "oauth" | "rest" | "batch" | "other"
    http_method: str
    url: httpx.URL
    rest_method: str | None  # Bitrix24 method name for kind in {"rest", "batch"}
    params: dict[str, str]  # query + form body, flattened
    json_body: Any | None
    headers: dict[str, str]
    commands: dict[str, str]  # batch only: key -> "method?query"

    @property
    def access_token(self) -> str | None:
        """The token this call was authenticated with (`auth=` param or bearer header)."""
        bearer = self.headers.get("authorization", "")
        if bearer.lower().startswith("bearer "):
            return bearer[7:]
        return self.params.get("auth")


class FakeBitrix:
    """A scriptable Bitrix24 cloud: one OAuth host, one REST base, full request log.

    Scripting is per *method name* (`"user.admin"`, `"batch"`, `"__oauth__"`) and each
    method holds a queue: entries are consumed one per call and the LAST entry repeats
    forever. That is what makes "fails once, then succeeds" - the shape both §5.8 tests
    need - a single line.
    """

    def __init__(self, *, client_endpoint: str = CLIENT_ENDPOINT) -> None:
        self.client_endpoint = client_endpoint
        self.requests: list[RecordedRequest] = []
        self.oauth_latency: float = 0.0
        self.rest_latency: float = 0.0
        #: Set by the tests that need the OAuth answer to depend on the refresh_token
        #: presented (e.g. "the second exchange must return a different member_id").
        self._script: dict[str, deque[Scripted]] = {}

    # -- scripting ------------------------------------------------------------------

    def on(self, method: str, *entries: Scripted) -> FakeBitrix:
        """Queue answers for one REST method. The last entry repeats."""
        self._script[method.strip().lower()] = deque(entries)
        return self

    def on_oauth(self, *entries: Scripted) -> FakeBitrix:
        """Queue answers for the refresh grant. Entry = a token dict, `Err`, or callable."""
        self._script["__oauth__"] = deque(entries)
        return self

    # -- inspection -----------------------------------------------------------------

    def of_kind(self, kind: str) -> list[RecordedRequest]:
        return [r for r in self.requests if r.kind == kind]

    @property
    def oauth_count(self) -> int:
        """How many refresh exchanges happened. The assertion of §4.1, §4.4/6 and §5.8."""
        return len(self.of_kind("oauth"))

    @property
    def rest_count(self) -> int:
        """Every REST round trip, batch included (a batch is ONE HTTP request)."""
        return len(self.of_kind("rest")) + len(self.of_kind("batch"))

    def calls_to(self, method: str) -> list[RecordedRequest]:
        """Round trips whose top-level method was `method` (does not look inside batch)."""
        want = method.strip().lower()
        return [r for r in self.requests if (r.rest_method or "").lower() == want]

    def reset(self) -> None:
        self.requests.clear()

    # -- transport ------------------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        record = self._record(request)
        self.requests.append(record)
        if record.kind == "oauth":
            if self.oauth_latency:
                await asyncio.sleep(self.oauth_latency)
            return self._answer_oauth(record)
        if self.rest_latency:
            await asyncio.sleep(self.rest_latency)
        if record.kind == "batch":
            return self._answer_batch(record)
        if record.kind == "rest":
            return self._answer_rest(record)
        # Anything else is a bug in the code under test (§4.1: no REST base is ever
        # built from DOMAIN), so make it loud rather than silently plausible.
        return httpx.Response(
            404, json={"error": "ERROR_MANIFEST_IS_NOT_AVAILABLE", "error_description": str(request.url)}
        )

    # -- request parsing ------------------------------------------------------------

    def _record(self, request: httpx.Request) -> RecordedRequest:
        params: dict[str, str] = dict(parse_qsl(request.url.query.decode(), keep_blank_values=True))
        json_body: Any | None = None
        raw = request.content.decode("utf-8", "replace") if request.content else ""
        if raw:
            if "json" in request.headers.get("content-type", "").lower():
                try:
                    json_body = json.loads(raw)
                except ValueError:
                    json_body = None
            else:
                params.update(dict(parse_qsl(raw, keep_blank_values=True)))
        if json_body is None and raw and not params:
            # Some clients post JSON without declaring the content type.
            try:
                json_body = json.loads(raw)
            except ValueError:
                json_body = None
        if isinstance(json_body, Mapping):
            # Flatten the scalar top level into `params` so a test can ask for
            # `record.params["refresh_token"]` without caring which encoding the code
            # under test chose - the contract does not pin one.
            for key, value in json_body.items():
                if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                    params.setdefault(str(key), str(value))

        path = request.url.path
        host = request.url.host.lower()
        kind = "other"
        rest_method: str | None = None
        if "/oauth/token" in path or (
            host == OAUTH_HOST and path.rstrip("/").endswith("/oauth")
        ):
            kind = "oauth"
        else:
            _, sep, tail = path.partition("/rest/")
            if sep:
                rest_method = tail.strip("/").removesuffix(".json") or None
                kind = "batch" if (rest_method or "").lower() == "batch" else "rest"

        commands: dict[str, str] = {}
        if kind == "batch":
            commands = _extract_commands(params, json_body)

        return RecordedRequest(
            kind=kind,
            http_method=request.method,
            url=request.url,
            rest_method=rest_method,
            params=params,
            json_body=json_body,
            headers={k.lower(): v for k, v in request.headers.items()},
            commands=commands,
        )

    # -- answers --------------------------------------------------------------------

    def _next(self, key: str, default: Scripted) -> Scripted:
        queue = self._script.get(key)
        if not queue:
            return default
        if len(queue) == 1:
            return queue[0]  # the last entry repeats forever
        return queue.popleft()

    @staticmethod
    def _resolve(entry: Scripted, record: RecordedRequest) -> Scripted:
        if callable(entry) and not isinstance(entry, type):
            return entry(record)
        return entry

    def _answer_oauth(self, record: RecordedRequest) -> httpx.Response:
        member_id = record.params.get("member_id") or new_member_id()
        entry = self._resolve(self._next("__oauth__", None), record)
        if isinstance(entry, Err):
            return httpx.Response(entry.http_status, json=entry.body())
        if entry is None:
            entry = token_response(member_id=member_id)
        return httpx.Response(200, json=entry)

    def _answer_rest(self, record: RecordedRequest) -> httpx.Response:
        method = (record.rest_method or "").lower()
        entry = self._resolve(self._next(method, _DEFAULT_RESULTS.get(method, True)), record)
        if isinstance(entry, Err):
            return httpx.Response(entry.http_status, json=entry.body())
        return httpx.Response(200, json={"result": entry, "time": _time_block()})

    def _answer_batch(self, record: RecordedRequest) -> httpx.Response:
        # A whole-batch failure (HTTP 401 expired_token on the batch envelope itself)
        # is scripted as `on("batch", Err(...))`; per-command failures are scripted on
        # the individual method and land in `result_error` (§5.6, §5.8 step 2).
        envelope = self._resolve(self._next("batch", None), record)
        if isinstance(envelope, Err):
            return httpx.Response(envelope.http_status, json=envelope.body())

        results: dict[str, Any] = {}
        errors: dict[str, Any] = {}
        times: dict[str, Any] = {}
        for key, command in record.commands.items():
            method = command.split("?", 1)[0].strip().lower()
            entry = self._resolve(self._next(method, _DEFAULT_RESULTS.get(method, True)), record)
            if isinstance(entry, Err):
                errors[key] = entry.body()
            else:
                results[key] = entry
            times[key] = _time_block()
        return httpx.Response(
            200,
            json={
                "result": {
                    "result": results,
                    "result_error": errors,
                    "result_total": {},
                    "result_next": {},
                    "result_time": times,
                },
                "time": _time_block(sum(t["operating"] for t in times.values()) or 0.1),
            },
        )


def _extract_commands(params: Mapping[str, str], json_body: Any) -> dict[str, str]:
    """Read the `cmd` map out of either encoding a client might have used."""
    if isinstance(json_body, Mapping):
        cmd = json_body.get("cmd")
        if isinstance(cmd, Mapping):
            return {str(k): str(v) for k, v in cmd.items()}
        if isinstance(cmd, Sequence) and not isinstance(cmd, str):
            return {str(i): str(v) for i, v in enumerate(cmd)}
    out: dict[str, str] = {}
    for key, value in params.items():
        match = _CMD_KEY_RE.match(key)
        if match:
            out[match.group(1)] = value
    return out


@contextmanager
def patch_httpx(fake: FakeBitrix) -> Iterator[FakeBitrix]:
    """Route every `httpx.AsyncClient` built WITHOUT an explicit transport to `fake`.

    WHY a monkeypatch rather than a constructor argument: the milestone-2 contract for
    `BitrixClient` and `oauth.exchange_refresh_token` has no transport seam, and adding
    one would be a design change. Clients that pass their own transport (the FastAPI
    `ASGITransport` in `test_install_guard`) are left alone, so both can be live at once.
    """
    real_init: Callable[..., None] = httpx.AsyncClient.__init__

    def patched_init(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> None:
        if kwargs.get("transport") is None and not kwargs.get("mounts"):
            kwargs["transport"] = fake.transport()
        real_init(self, *args, **kwargs)

    httpx.AsyncClient.__init__ = patched_init  # type: ignore[method-assign]
    try:
        yield fake
    finally:
        httpx.AsyncClient.__init__ = real_init  # type: ignore[method-assign]


# --- Bitrix24-facing POST bodies ----------------------------------------------------


def install_form(
    *,
    member_id: str,
    refresh_id: str | None = USER_REFRESH,
    auth_id: str = USER_AUTH,
    domain: str = DOMAIN,
    application_token: str | None = APP_KEY,
    placement: str = "DEFAULT",
    protocol: str = "1",
    lang: str = "ru",
    app_sid: str = "b8b3a9e1c7d24f0a",
    status: str = "F",
    application_scope: str = "crm,telephony,user_brief,placement",
    server_endpoint: str = SERVER_ENDPOINT,
    auth_expires: str = "3600",
) -> dict[str, str]:
    """The `application/x-www-form-urlencoded` body Bitrix24 POSTs to `/install/` (§4.2).

    `refresh_id=None` produces the empty `REFRESH_ID` of §4.3 step 2 - the field is sent
    but blank, which is what an isolated on-premise box actually does; omitting the key
    entirely would test a different (and less interesting) branch.
    """
    body: dict[str, str] = {
        "DOMAIN": domain,
        "PROTOCOL": protocol,
        "LANG": lang,
        "APP_SID": app_sid,
        "AUTH_ID": auth_id,
        "AUTH_EXPIRES": auth_expires,
        "REFRESH_ID": "" if refresh_id is None else refresh_id,
        "member_id": member_id,
        "status": status,
        "PLACEMENT": placement,
        "PLACEMENT_OPTIONS": "{}",
        "APPLICATION_SCOPE": application_scope,
        "SERVER_ENDPOINT": server_endpoint,
    }
    if application_token is not None:
        body["APPLICATION_TOKEN"] = application_token
    return body


def install_query(
    *, domain: str = DOMAIN, protocol: str = "1", lang: str = "ru", app_sid: str = "b8b3a9e1c7d24f0a"
) -> str:
    """The query string Bitrix24 puts on the handler URL; forwarded verbatim (§4.4 step 8)."""
    return f"DOMAIN={domain}&PROTOCOL={protocol}&LANG={lang}&APP_SID={app_sid}"


# --- database seeding ---------------------------------------------------------------
#
# These write `access_token_enc` / `refresh_token_enc` directly, which production code
# may not do (§4.1, enforced by test_registry_lint) - the lint scans `api/app` only,
# precisely so a test can construct the "before" state it wants to prove nothing touched.


@dataclass
class SeededPortal:
    portal_id: int
    member_id: str
    access: str
    refresh: str
    application_key: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


async def seed_portal(
    *,
    member_id: str | None = None,
    status: str = "active",
    access: str = SEED_ACCESS,
    refresh: str = SEED_REFRESH,
    application_key: str | None = APP_KEY,
    client_endpoint: str = CLIENT_ENDPOINT,
    domain: str = DOMAIN,
    token_version: int = 0,
    token_status: str = "ok",  # noqa: S107 - a portals column name, not a credential
    token_user_id: int = _DEFAULT_TOKEN_USER_ID,
    expires_in: int = 3600,
    install_completed: bool = True,
    high_id: int = 0,
    low_id: int | None = None,
    backfill_status: str = "pending",
    sync_generation: int = 0,
) -> SeededPortal:
    """Create one `portals` + `portal_sync` pair in the state a test needs.

    Tokens are encrypted with the real envelope (AAD `member_id:column`, decision 19) so
    the code under test decrypts them exactly as it would in production; a plaintext
    shortcut here would make `test_refresh_singleflight` pass against a broken crypto path.
    """
    mid = member_id or new_member_id()
    now = datetime.now(tz=UTC)
    async with control_txn() as session:
        portal_id = int(
            (
                await session.execute(
                    text(
                        """
                        INSERT INTO portals (
                            member_id, domain, protocol_https, client_endpoint, server_endpoint,
                            status, scope, lang, timezone, capabilities,
                            token_user_id, access_token_enc, refresh_token_enc,
                            token_expires_at, token_refreshed_at, token_admin_verified_at,
                            token_version, token_status, application_token_enc,
                            install_completed_at, uninstalled_at
                        ) VALUES (
                            :member_id, :domain, true, :client_endpoint, :server_endpoint,
                            :status, 'crm,telephony,user_brief,placement', 'ru', 'Asia/Tashkent',
                            CAST(:capabilities AS jsonb),
                            :token_user_id, :access_enc, :refresh_enc,
                            :expires_at, :refreshed_at, :verified_at,
                            :token_version, :token_status, :app_enc,
                            :install_completed_at, :uninstalled_at
                        ) RETURNING id
                        """
                    ),
                    {
                        "member_id": mid,
                        "domain": domain,
                        "client_endpoint": client_endpoint,
                        "server_endpoint": SERVER_ENDPOINT,
                        "status": status,
                        "capabilities": json.dumps(
                            {"statistic_get": True, "operating_limit_s": 480}
                        ),
                        "token_user_id": token_user_id,
                        "access_enc": encrypt(access, member_id=mid, column="access_token"),
                        "refresh_enc": encrypt(refresh, member_id=mid, column="refresh_token"),
                        "expires_at": now + timedelta(seconds=expires_in),
                        "refreshed_at": now - timedelta(days=1),
                        "verified_at": now - timedelta(days=1),
                        "token_version": token_version,
                        "token_status": token_status,
                        "app_enc": (
                            encrypt(application_key, member_id=mid, column="application_token")
                            if application_key
                            else None
                        ),
                        "install_completed_at": now - timedelta(days=7) if install_completed else None,
                        "uninstalled_at": now - timedelta(hours=1) if status == "uninstalled" else None,
                    },
                )
            ).scalar_one()
        )
        await session.execute(
            text(
                """
                INSERT INTO portal_sync (portal_id, sync_generation, high_id, low_id, backfill_status)
                VALUES (:pid, :gen, :high_id, :low_id, :backfill_status)
                """
            ),
            {
                "pid": portal_id,
                "gen": sync_generation,
                "high_id": high_id,
                "low_id": low_id,
                "backfill_status": backfill_status,
            },
        )
    return SeededPortal(
        portal_id=portal_id,
        member_id=mid,
        access=access,
        refresh=refresh,
        application_key=application_key,
    )


async def delete_portal(member_id: str) -> None:
    """Remove a seeded tenant and everything attributable to it.

    `rest_log.portal_id` is `ON DELETE SET NULL`, so the rows would otherwise survive as
    orphans and leak into the next test's `fetch_rest_logs(member_id)`.
    """
    async with control_txn() as session:
        await session.execute(text("DELETE FROM rest_log WHERE member_id = :mid"), {"mid": member_id})
        await session.execute(
            text(
                "DELETE FROM portal_events WHERE portal_id IN "
                "(SELECT id FROM portals WHERE member_id = :mid)"
            ),
            {"mid": member_id},
        )
        await session.execute(
            text(
                "DELETE FROM portal_sync WHERE portal_id IN "
                "(SELECT id FROM portals WHERE member_id = :mid)"
            ),
            {"mid": member_id},
        )
        await session.execute(text("DELETE FROM portals WHERE member_id = :mid"), {"mid": member_id})


def _normalise(row: Mapping[str, Any]) -> dict[str, Any]:
    """Make a row comparable and printable: bytea -> hex, everything else as-is."""
    out: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, (bytes, bytearray, memoryview)):
            out[key] = bytes(value).hex()
        else:
            out[key] = value
    return out


async def portal_snapshot(member_id: str) -> dict[str, Any] | None:
    """Every column of the `portals` row, for a before/after equality assertion (§4.3)."""
    async with control_txn() as session:
        row = (
            await session.execute(
                text("SELECT * FROM portals WHERE member_id = :mid"), {"mid": member_id}
            )
        ).mappings().one_or_none()
    return _normalise(row) if row is not None else None


async def portal_sync_snapshot(portal_id: int) -> dict[str, Any] | None:
    async with control_txn() as session:
        row = (
            await session.execute(
                text("SELECT * FROM portal_sync WHERE portal_id = :pid"), {"pid": portal_id}
            )
        ).mappings().one_or_none()
    return _normalise(row) if row is not None else None


async def fetch_rest_logs(member_id: str | None = None) -> list[dict[str, Any]]:
    """Every `rest_log` row for one tenant (§6). `None` returns the whole table."""
    sql = "SELECT * FROM rest_log"
    binds: dict[str, Any] = {}
    if member_id is not None:
        sql += " WHERE member_id = :mid"
        binds["mid"] = member_id
    sql += " ORDER BY id"
    async with control_txn() as session:
        rows = (await session.execute(text(sql), binds)).mappings().all()
    return [_normalise(row) for row in rows]


async def clear_rest_logs() -> None:
    """Empty the moderation log so a secret-scan test can assert over the WHOLE table."""
    async with control_txn() as session:
        await session.execute(text("DELETE FROM rest_log"))
