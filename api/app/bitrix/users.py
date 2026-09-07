"""The `user.*` methods: the two identity probes and the employee-cache bulk read.

WHY a module of its own: §4.3 step 3, §4.4 step 4, §5.8 (daily admin re-verification)
and §7 all issue the same three Bitrix24 calls, and the *parsing* of their results needs
exactly one definition. Bitrix24 is loose about shapes in ways that would otherwise be
re-guessed at every call site:

* `ID` arrives as a string on some builds and as an int on others.
* `ACTIVE` arrives as a JSON bool on the cloud and as `"Y"`/`"N"` on older on-premise
  builds.
* `user.admin` is documented as returning a bare boolean, but the sibling `profile`
  method returns an object whose `ADMIN` field "matches the result of the user.admin
  method" (docs/bitrix24-api-research.md), so an object-shaped result is accepted too.

`identity.py` builds the §4.1 credential proof on top of the parsers here; the
`employees` table writer (another module) consumes `fetch_users` output. Nothing in this
file touches the database - it is pure protocol.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Final

from app.bitrix.errors import (
    AccessDenied,
    BitrixError,
    InvalidCredentials,
    UnknownBitrixError,
    UserAccessError,
    classify,
)

if TYPE_CHECKING:  # pragma: no cover - typing only; avoids an import cycle at runtime
    from app.bitrix.client import BitrixClient

__all__ = [
    "ID_CHUNK",
    "MAX_COMMANDS_PER_BATCH",
    "USER_ADMIN",
    "USER_CURRENT",
    "USER_GET",
    "fetch_users",
    "parse_admin_flag",
    "parse_user",
    "user_admin",
    "user_current",
]

USER_CURRENT: Final[str] = "user.current"
USER_ADMIN: Final[str] = "user.admin"
USER_GET: Final[str] = "user.get"

#: `user.get` pages are fixed at 50 rows (verified), so a `@ID` filter of at most 50 ids
#: is answered by a single page and the command never needs `start=` paging (§7).
ID_CHUNK: Final[int] = 50

#: Documented `batch` ceiling; exceeding it returns ERROR_BATCH_LENGTH_EXCEEDED (§7).
MAX_COMMANDS_PER_BATCH: Final[int] = 50

#: Errors that plausibly mean "this token may not use ADMIN_MODE" and are therefore
#: worth one retry without it (§7). Everything else - expired_token, the throttle codes,
#: PORTAL_DELETED - must reach `with_portal_token` / the sync runner untouched, because
#: those layers own the refresh and back-off decisions (§5.6, §5.8).
_ADMIN_MODE_RETRYABLE: Final[tuple[type[BitrixError], ...]] = (
    InvalidCredentials,
    AccessDenied,
    UserAccessError,
    UnknownBitrixError,
)

_TRUE_STRINGS: Final[frozenset[str]] = frozenset({"y", "yes", "true", "1"})
_FALSE_STRINGS: Final[frozenset[str]] = frozenset({"n", "no", "false", "0", ""})


def _as_bool(value: Any, *, default: bool) -> bool:
    """Bitrix24 spells booleans three ways; an unknown spelling keeps the default.

    `default=True` for `ACTIVE` is deliberate: a build that omits the field must not make
    the whole employee cache look dismissed (§7 greys dismissed users in the UI).
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        token = value.strip().lower()
        if token in _TRUE_STRINGS:
            return True
        if token in _FALSE_STRINGS:
            return False
    return default


def _as_int(value: Any) -> int | None:
    """Numeric id from `"17"`, `17` or `17.0`; anything else is not an id."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        text = value.strip()
        if text.isascii() and text.isdigit():
            return int(text)
    return None


def _as_str(value: Any) -> str | None:
    """Non-empty string, or None. Never coerces a dict/list into its repr."""
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return None


def _as_int_list(value: Any) -> list[int]:
    """`UF_DEPARTMENT` is a list of ids, sometimes as strings, sometimes absent."""
    if not isinstance(value, (list, tuple)):
        return []
    out: list[int] = []
    for item in value:
        number = _as_int(item)
        if number is not None:
            out.append(number)
    return out


def _field(raw: Mapping[str, Any], *names: str) -> Any:
    """First present key out of several spellings (cabinets differ in case)."""
    for name in names:
        if name in raw:
            return raw[name]
    return None


def parse_user(raw: Any) -> dict[str, Any] | None:
    """One `user_brief` record -> the shape the `employees` cache stores (§7).

    Returns None when the record carries no usable numeric `ID`; the caller then leaves
    that id unresolved (`found=false`) rather than inventing a row. Contact fields are
    never read: the app holds `user_brief` only, and §3 has no column for them.
    """
    if not isinstance(raw, Mapping):
        return None
    user_id = _as_int(_field(raw, "ID", "id"))
    if user_id is None:
        return None
    return {
        "id": user_id,
        "name": _as_str(_field(raw, "NAME", "name")),
        "last_name": _as_str(_field(raw, "LAST_NAME", "last_name")),
        "second_name": _as_str(_field(raw, "SECOND_NAME", "second_name")),
        "work_position": _as_str(_field(raw, "WORK_POSITION", "work_position")),
        "photo_url": _as_str(_field(raw, "PERSONAL_PHOTO", "personal_photo")),
        # Never filtered on (§7): dismissed employees own historical calls and must
        # still resolve to a name, so the flag is stored and rendered, not used to skip.
        "active": _as_bool(_field(raw, "ACTIVE", "active"), default=True),
        "departments": _as_int_list(_field(raw, "UF_DEPARTMENT", "uf_department")),
        "timezone": _as_str(_field(raw, "TIME_ZONE", "time_zone")),
    }


def parse_admin_flag(result: Any) -> bool:
    """`user.admin` -> bool, fail-closed.

    Anything we cannot read as an affirmative is false: this value decides whether a
    token may become a stored credential (§4.1 credential invariant), so an unparseable
    answer must never be mistaken for "yes".
    """
    if isinstance(result, Mapping):
        result = _field(result, "admin", "ADMIN", "isAdmin", "IS_ADMIN")
    return _as_bool(result, default=False)


def _require_user(result: Any) -> dict[str, Any]:
    """Turn an unusable `user.current` body into a typed Bitrix error, not a KeyError."""
    parsed = parse_user(result)
    if parsed is None:
        # Built through `classify` so the error-string -> type mapping stays in one file.
        raise classify(
            "ERROR_UNEXPECTED_ANSWER",
            http_status=200,
            description="user.current returned no numeric ID",
        )
    return parsed


async def user_current(client: BitrixClient) -> dict[str, Any]:
    """`user.current` as a parsed record (§4.4 step 4 upserts the viewer from it).

    Scope: `user`, `user_brief` or `user_basic` - any of the three (verified). The app
    holds `user_brief`, so EMAIL and phones are absent by construction.
    """
    return _require_user(await client.call(USER_CURRENT, {}))


async def user_admin(client: BitrixClient) -> bool:
    """`user.admin` -> "may manage application settings", which §4.1 treats as admin.

    No scope is required (it is a `basic`-scope method, executable by any user), so this
    probe works even on a portal that granted us nothing but the defaults.
    """
    return parse_admin_flag(await client.call(USER_ADMIN, {}))


def _collect(into: dict[int, dict[str, Any]], result: Any) -> None:
    """Fold one `user.get` command result into the id -> record map."""
    rows: Sequence[Any]
    if isinstance(result, Mapping):
        # Some builds wrap the list; a bare mapping is treated as a single record.
        inner = _field(result, "users", "USERS", "result")
        rows = inner if isinstance(inner, (list, tuple)) else [result]
    elif isinstance(result, (list, tuple)):
        rows = result
    else:
        return
    for row in rows:
        parsed = parse_user(row)
        if parsed is not None:
            into[int(parsed["id"])] = parsed


def _chunks(values: Sequence[int], size: int) -> list[list[int]]:
    return [list(values[index : index + size]) for index in range(0, len(values), size)]


async def fetch_users(client: BitrixClient, ids: Iterable[int]) -> dict[int, dict[str, Any]]:
    """Bulk-resolve user ids for the employee cache (§7).

    Shape of the round trip, straight from §7 and the verified research notes: ids are
    de-duplicated and chunked by `ID_CHUNK` (50 - one full `user.get` page, so no command
    ever needs paging), up to `MAX_COMMANDS_PER_BATCH` commands per `batch` (2 500 ids per
    HTTP request), `halt=0` so one bad chunk cannot cancel the rest.

    `ADMIN_MODE: true` is requested because the portal token must see users outside its
    own department; the docs state no executor restriction for it, so a command that
    errors is retried ONCE without it rather than assumed fatal.

    **`ACTIVE` is never filtered.** Dismissed employees own historical calls and must
    still resolve to a name (§7, and a correction to the brief in
    docs/bitrix24-api-research.md); the flag is returned in each record instead.

    Ids with no result are simply absent from the returned mapping - the caller marks
    them `found=false` (§7). Writing the `employees` table is not this module's job.
    """
    wanted = sorted({number for number in (_as_int(i) for i in ids) if number is not None})
    resolved: dict[int, dict[str, Any]] = {}
    if not wanted:
        return resolved

    id_chunks = _chunks(wanted, ID_CHUNK)
    for group_start in range(0, len(id_chunks), MAX_COMMANDS_PER_BATCH):
        group = id_chunks[group_start : group_start + MAX_COMMANDS_PER_BATCH]
        keyed = {f"u{index}": chunk for index, chunk in enumerate(group)}

        first = await client.batch(
            [
                (key, USER_GET, {"FILTER": {"@ID": chunk}, "ADMIN_MODE": True})
                for key, chunk in keyed.items()
            ],
            halt=0,
        )

        retry: list[tuple[str, str, dict[str, Any]]] = []
        for key, chunk in keyed.items():
            error = first.error(key)
            if error is None:
                _collect(resolved, first.get(key))
                continue
            if not isinstance(error, _ADMIN_MODE_RETRYABLE):
                # expired_token / throttle / PORTAL_DELETED: the caller owns these.
                raise error
            retry.append((key, USER_GET, {"FILTER": {"@ID": chunk}}))

        if not retry:
            continue
        second = await client.batch(retry, halt=0)
        for key, _method, _params in retry:
            error = second.error(key)
            if error is not None:
                # The retry proved ADMIN_MODE was not the problem; report the real error
                # instead of silently returning a half-populated cache.
                raise error
            _collect(resolved, second.get(key))

    return resolved
