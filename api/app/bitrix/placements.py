"""`placement.get` / `placement.bind` / `event.bind` wrappers (§4.3 step 5).

Three rules of that step live here rather than in the install handler, because they are
protocol facts and the handler should read as a flow:

* **The four CRM detail tabs only.** `LEFT_MENU` is deliberately never bound: the
  vendor-cabinet version-card option "add your page and item to the main menu" already
  opens our handler with `PLACEMENT=DEFAULT`, so a `placement.bind('LEFT_MENU')` would
  put a *second*, duplicate item in the menu (docs/bitrix24-api-research.md, block (g)).
* **Binding is never fatal.** Placements are a convenience; a portal whose bind failed
  still has a working dashboard and a "Re-bind" button on the settings page. Every
  function here therefore returns a per-item verdict instead of raising, and an HTTP 200
  carrying an `error` body counts as a failure exactly like a transport error does.
* **`event.bind` for the lifecycle events is best effort.** The docs neither confirm nor
  deny that `ONAPPUNINSTALL` / `ONAPPUPDATE` can be subscribed this way, so its result is
  recorded in `capabilities.event_bind` for support and never gates the install; the
  vendor-cabinet event handler URL remains the primary path (§4.9).

There is deliberately **no `unbind_placements`**: §4.9 rule 3 and the research show all
API access is revoked at uninstall (the event carries no token), so `placement.unbind`
could only ever fail there - placements die with the app. Do not add one.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Collection
from typing import Any, Final

from app.bitrix.client import BitrixClient
from app.bitrix.errors import BitrixError
from app.i18n import LOCALES, has_message, resolve_locale, t
from app.logging import get_logger
from app.security.redact import redact

__all__ = [
    "CRM_TAB_PLACEMENTS",
    "LIFECYCLE_EVENTS",
    "bind_lifecycle_events",
    "bind_placements",
    "get_bound_placements",
]

log = get_logger(__name__)

#: The placements §4.3 step 5 binds. One handler URL serves all of them plus DEFAULT;
#: Bitrix24 tells them apart by the POSTed `PLACEMENT` value (§4.4).
CRM_TAB_PLACEMENTS: Final[tuple[str, ...]] = (
    "CRM_DEAL_DETAIL_TAB",
    "CRM_LEAD_DETAIL_TAB",
    "CRM_CONTACT_DETAIL_TAB",
    "CRM_COMPANY_DETAIL_TAB",
)

#: Subscribed best effort; the vendor cabinet's handler URL is the documented path.
LIFECYCLE_EVENTS: Final[tuple[str, ...]] = ("ONAPPUNINSTALL", "ONAPPUPDATE")

#: §8: one message source. The key lives in `web/messages/<locale>.json` next to every
#: other string, so adding a language and re-binding renames the CRM tab automatically.
_TITLE_KEY: Final = "placement.crm_tab_title"

#: Used only if the message source cannot answer. A missing translation must not stop an
#: install, and an untitled placement renders as a blank tab in the CRM card.
_FALLBACK_TITLES: Final[dict[str, str]] = {"ru": "Звонки", "en": "Calls"}

#: `portals.placements` is read by support and rendered on the settings page; a Bitrix24
#: description is free text, so it is capped and redacted before it is stored.
_MAX_ERROR_CHARS: Final = 200


def _now_iso() -> str:
    """Timestamp for the `placements` JSON; UTC and second precision are enough here."""
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


def _title(locale: str) -> str:
    """Tab title for one locale, from the shared message source with a hard fallback.

    §4.3 step 5 must not be able to fail on a missing i18n key: `t()` answers with the
    key itself when the catalogue has no entry, and a CRM tab literally titled
    "placement.crm_tab_title" is worse than an untranslated one. The fallback goes
    through `resolve_locale` so a locale added to `locales.json` before its messages
    exist (uz -> ru) still gets a sensible title.
    """
    if has_message(_TITLE_KEY):
        title = t(locale, _TITLE_KEY)
        if title and title != _TITLE_KEY:
            return title
    return _FALLBACK_TITLES.get(resolve_locale(locale), _FALLBACK_TITLES["en"])


def _bind_params(placement: str, handler_url: str) -> dict[str, Any]:
    """Parameters of one `placement.bind` call.

    Only `PLACEMENT`, `HANDLER`, `TITLE` and `LANG_ALL` are sent. `OPTIONS` is ignored
    for CRM detail tabs, and the optional grouping parameter - which is `GROUP_NAME`,
    **not** the `GROUP_LABEL` the original brief named - applies only to widget types
    that group several handlers, which these do not (research block (g)).

    `TITLE` is the fallback Bitrix24 shows for a language absent from `LANG_ALL`, so it
    takes the default locale of the shared fallback map (§8).
    """
    return {
        "PLACEMENT": placement,
        "HANDLER": handler_url,
        "TITLE": _title(resolve_locale(None)),
        "LANG_ALL": {locale: {"TITLE": _title(locale)} for locale in LOCALES},
    }


def _failure(error: BitrixError) -> dict[str, Any]:
    """One failed bind, in the shape `portals.placements` stores (§3)."""
    code = error.code or "unknown_error"
    detail = f"{code}: {error.description}" if error.description else code
    return {"ok": False, "error": str(redact(detail))[:_MAX_ERROR_CHARS]}


async def get_bound_placements(client: BitrixClient) -> dict[str, str]:
    """`placement.get` -> {PLACEMENT: handler URL} for this application.

    Used to make the install idempotent (§4.3 step 5): a placement already registered is
    not bound again, because a second `placement.bind` with a different handler adds a
    duplicate tab rather than replacing the first.

    Raises `BitrixError` like any other REST call - in the install flow the same data
    already arrives inside the step-3 proof batch, so this is for the settings-page
    re-bind, where an error is shown rather than swallowed. The response is untrusted
    (§4.1): anything that is not a list of objects with a string `placement` is ignored.
    """
    raw = await client.call("placement.get")
    bound: dict[str, str] = {}
    if not isinstance(raw, list):
        return bound
    for item in raw:
        if not isinstance(item, dict):
            continue
        # Documented lowercase keys; some builds echo the uppercase request names.
        code = item.get("placement") or item.get("PLACEMENT")
        if not isinstance(code, str) or not code:
            continue
        handler = item.get("handler") or item.get("HANDLER")
        bound.setdefault(code.upper(), handler if isinstance(handler, str) else "")
    return bound


async def bind_placements(
    client: BitrixClient,
    *,
    handler_url: str,
    existing: Collection[str] = (),
) -> dict[str, dict[str, Any]]:
    """Bind the CRM detail tabs that are not bound yet, in ONE batch with halt=0.

    §4.3 step 5. `existing` is what `get_bound_placements()` (or the step-3 batch's
    `placement.get` result) reported; halt=0 so one rejected placement cannot cancel the
    other three. Returns the map stored verbatim in `portals.placements`:
    `{"CRM_DEAL_DETAIL_TAB": {"ok": true, "at": "..."} | {"ok": false, "error": "..."}}`.

    Never raises: a whole-batch failure (transport, expired token) is recorded as a
    failure for every attempted placement, because §4.3 step 5 must still reach
    `install.html` - without `BX24.installFinish()` the app stays "not installed" and
    the widgets would not appear even if every bind had succeeded.
    """
    at = _now_iso()
    already = {code.upper() for code in existing}
    results: dict[str, dict[str, Any]] = {
        code: {"ok": True, "at": at, "already": True}
        for code in CRM_TAB_PLACEMENTS
        if code in already
    }
    pending = [code for code in CRM_TAB_PLACEMENTS if code not in already]
    if not pending:
        return results

    commands = [(code, "placement.bind", _bind_params(code, handler_url)) for code in pending]
    try:
        batch = await client.batch(commands, halt=0)
    except BitrixError as exc:
        for code in pending:
            results[code] = _failure(exc)
        log.warning("placement.bind batch failed", extra={"error_code": exc.code})
        return results

    for code in pending:
        error = batch.error(code)
        if error is not None:
            results[code] = _failure(error)
        elif not batch.get(code):
            # placement.bind answers result:true/false; a false is a refusal with a 200.
            results[code] = {"ok": False, "error": "placement.bind returned false"}
        else:
            results[code] = {"ok": True, "at": at}

    failed = [code for code, outcome in results.items() if not outcome["ok"]]
    if failed:
        log.warning("some placements were not bound", extra={"placements": failed})
    return results


async def bind_lifecycle_events(
    client: BitrixClient,
    *,
    handler_url: str,
) -> dict[str, dict[str, Any]]:
    """Best-effort `event.bind` for ONAPPUNINSTALL and ONAPPUPDATE (§4.3 step 5).

    Same batch style and same result shape as `bind_placements`, and equally non-fatal:
    subscribing lifecycle events this way is undocumented, so a failure here is expected
    on some portals and only means §4.9 relies on the vendor-cabinet handler URL (plus
    the §5.8 inferred-uninstall rule) for that portal. The caller stores the result under
    `capabilities.event_bind` so support can see which portals have the extra path.
    """
    at = _now_iso()
    results: dict[str, dict[str, Any]] = {}
    commands = [
        (event, "event.bind", {"event": event, "handler": handler_url})
        for event in LIFECYCLE_EVENTS
    ]
    try:
        batch = await client.batch(commands, halt=0)
    except BitrixError as exc:
        log.info("event.bind batch failed (non-fatal)", extra={"error_code": exc.code})
        return {event: _failure(exc) for event in LIFECYCLE_EVENTS}

    for event in LIFECYCLE_EVENTS:
        error = batch.error(event)
        if error is not None:
            results[event] = _failure(error)
        elif not batch.get(event):
            results[event] = {"ok": False, "error": "event.bind returned false"}
        else:
            results[event] = {"ok": True, "at": at}
    return results


async def bind_all(
    client: BitrixClient,
    *,
    handler_url: str,
    events_url: str,
    existing: Collection[str] = (),
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Bind the CRM tabs and subscribe the lifecycle events in ONE batch (§4.3 step 5).

    The design is explicit that `event.bind` goes "in the same batch" as the placement
    binds, and §4.3 step 6 budgets the whole install at three HTTP round trips: the OAuth
    exchange, the step-3 proof batch, and this one. Issuing two batches here would spend a
    fourth for nothing and burn shared operating time (§5.6).

    Returns `(placements, event_bind)`: the first is stored verbatim in
    `portals.placements`, the second under `capabilities.event_bind`. Neither ever raises -
    reaching `install.html` and running `BX24.installFinish()` matters more than a bind,
    which the settings page can retry.
    """
    at = _now_iso()
    already = {code.upper() for code in existing}
    placements: dict[str, dict[str, Any]] = {
        code: {"ok": True, "at": at, "already": True}
        for code in CRM_TAB_PLACEMENTS
        if code in already
    }
    pending = [code for code in CRM_TAB_PLACEMENTS if code not in already]

    # Event keys are prefixed so they cannot collide with a placement code.
    commands = [(code, "placement.bind", _bind_params(code, handler_url)) for code in pending]
    commands += [
        (f"event_{event}", "event.bind", {"event": event, "handler": events_url})
        for event in LIFECYCLE_EVENTS
    ]
    if not commands:
        return placements, {}

    try:
        batch = await client.batch(commands, halt=0)
    except BitrixError as exc:
        log.info("bind batch failed (non-fatal)", extra={"error_code": exc.code})
        placements.update({code: _failure(exc) for code in pending})
        return placements, {event: _failure(exc) for event in LIFECYCLE_EVENTS}

    for code in pending:
        error = batch.error(code)
        if error is not None:
            placements[code] = _failure(error)
        elif not batch.get(code):
            placements[code] = {"ok": False, "error": "placement.bind returned false"}
        else:
            placements[code] = {"ok": True, "at": at}

    events: dict[str, dict[str, Any]] = {}
    for event in LIFECYCLE_EVENTS:
        key = f"event_{event}"
        error = batch.error(key)
        if error is not None:
            events[event] = _failure(error)
        elif not batch.get(key):
            events[event] = {"ok": False, "error": "event.bind returned false"}
        else:
            events[event] = {"ok": True, "at": at}
    return placements, events
