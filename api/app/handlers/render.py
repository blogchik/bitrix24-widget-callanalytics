"""Server-rendered pages for the Bitrix24 iframe (§4.10, §4.11).

Three rules shape this module, all of them moderation rules rather than taste:

* **Every page carries `Content-Security-Policy: frame-ancestors 'self'
  <scheme>://<domain>` and never `X-Frame-Options`** (§4.10). `X-Frame-Options` has
  no origin list, so a single stray header anywhere would blank the app inside
  Bitrix24; the CSP directive is the only framing control this app uses. The domain
  is re-validated HERE, not trusted from the caller: `render_state` is called on the
  path where `bitrix/forms.py` has just REJECTED the body, so the value reaching it
  can be arbitrary attacker text, and it is interpolated into a header.
* **A blank frame or a raw error is a rejection** (§4.11), so rendering never raises:
  an unknown state kind degrades to the generic error copy and a missing message
  degrades to its key (`app.i18n.t`).
* **`Cache-Control: no-store`** (§4.10): these pages are rendered per portal, per user
  and per request id.

The templates live next to this file and are entirely self-contained - no script, no
webfont, no stylesheet - because the only network the frame is guaranteed to reach is
the response itself.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Final
from urllib.parse import parse_qsl

from fastapi import Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from app.i18n import has_message, resolve_locale, t
from app.logging import get_logger, get_request_id

__all__ = ["render_error", "render_handoff", "render_install", "render_state"]

_log = get_logger(__name__)

_TEMPLATES_DIR: Final[Path] = Path(__file__).resolve().parent / "templates"

# autoescape is non-negotiable: `extra` values and the request id are interpolated
# into HTML. auto_reload off - the templates ship inside the image and never change
# under a running process.
_env: Final[Environment] = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR), encoding="utf-8"),
    autoescape=select_autoescape(default_for_string=True, default=True),
    auto_reload=False,
    trim_blocks=True,
    lstrip_blocks=True,
)

#: RFC hostname with an optional `:port`, anchored. Deliberately stricter than a URL
#: parse: anything that is not a bare hostname (a scheme, a path, a space, a second
#: colon, a comma) must fail closed to `'none'` rather than be escaped into the header.
_HOST_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?=.{1,253}$)"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
)

#: The state kinds `state.html` knows (§4.11). A kind outside this set is a bug in a
#: caller, and it renders the generic error copy instead of a dotted key.
STATE_KINDS: Final[frozenset[str]] = frozenset(
    {
        "bad_request",
        "not_installed",
        "admin_only",
        "unsupported_portal",
        "method_missing",
        "retry",
        "scope",
        "denied",
        "crm_no_access",
        "reauth",
        "error",
    }
)

_FALLBACK_KIND: Final[str] = "error"


def _frame_ancestors(domain: str | None, protocol_https: bool) -> str:
    """The `frame-ancestors` value for one portal (§4.10).

    `'none'` when there is no valid domain: without a portal origin we cannot know who
    may frame this page, and the page then says "open this app from Bitrix24" in a top
    level window rather than silently allowing an attacker's frame.
    """
    if not domain:
        return "'none'"
    host = domain.strip().lower()
    port = ""
    if ":" in host:
        head, _, tail = host.rpartition(":")
        if not tail.isdigit() or not 1 <= int(tail) <= 65535 or len(tail) > 5:
            return "'none'"
        host, port = head, f":{tail}"
    if not _HOST_RE.match(host):
        return "'none'"
    scheme = "https" if protocol_https else "http"
    return f"{scheme}://{host}{port}"


def _headers(domain: str | None, protocol_https: bool) -> dict[str, str]:
    """§4.10. Note what is NOT here: `X-Frame-Options`, at any value, ever."""
    ancestors = _frame_ancestors(domain, protocol_https)
    directive = (
        "frame-ancestors 'none'"
        if ancestors == "'none'"
        else f"frame-ancestors 'self' {ancestors}"
    )
    return {"Content-Security-Policy": directive, "Cache-Control": "no-store"}


def _render(template: str, context: dict[str, Any]) -> str:
    """Render a template, degrading to plain text rather than raising (§4.11)."""
    try:
        return _env.get_template(template).render(**context)
    except Exception:
        _log.exception("render: template failed", extra={"template": template})
        title = str(context.get("title", ""))
        body = str(context.get("body", ""))
        # Deliberately minimal and unstyled: the only job left is "not a blank frame".
        return (
            "<!doctype html><html><head><meta charset='utf-8'><title>"
            f"{title}</title></head><body><h1>{title}</h1><p>{body}</p></body></html>"
        )


def _base_context(locale: str) -> dict[str, Any]:
    """Everything every page shows: language, product name and the quotable id."""
    return {
        "locale": locale,
        "app_name": t(locale, "common.appName"),
        "request_id": get_request_id() or "",
        "request_id_label": t(locale, "common.requestId"),
    }


def render_state(
    request: Request,
    kind: str,
    *,
    lang: str | None,
    domain: str | None,
    protocol_https: bool = True,
    status_code: int = 200,
    extra: dict[str, Any] | None = None,
) -> HTMLResponse:
    """One of the §4.11 states as a translated, self-contained page.

    `kind` selects the copy (`state.<kind>.title` / `.body` in the shared catalogue of
    §8) and nothing else - no state page ever shows a raw Bitrix24 error string, an
    exception message or a field value, because those are attacker-influenced text and
    a moderator reads this frame.

    `extra` is merged last so a caller can add template variables (`hint`, a countdown)
    without this module growing a parameter per state.
    """
    resolved = kind if kind in STATE_KINDS else _FALLBACK_KIND
    if resolved != kind:
        _log.warning("render_state: unknown state kind", extra={"kind": kind})
    locale = resolve_locale(lang)

    context = _base_context(locale)
    context.update(
        {
            "kind": resolved,
            "title": t(locale, f"state.{resolved}.title"),
            "body": t(locale, f"state.{resolved}.body"),
            "hint": None,
        }
    )
    if extra:
        context.update(extra)

    # `request` is part of the shared signature so that every handler passes its
    # request object uniformly; the page itself needs nothing from it (the request id
    # comes from the contextvar the middleware bound, so it matches the log lines).
    return HTMLResponse(
        content=_render("state.html", context),
        status_code=status_code,
        headers=_headers(domain, protocol_https),
    )


def render_install(
    request: Request,
    *,
    lang: str | None,
    domain: str | None,
    protocol_https: bool = True,
) -> HTMLResponse:
    """The final page of `POST /install/` (§4.3 step 6).

    It is the browser's turn: the page loads the BX24 SDK and calls
    `BX24.init(() => BX24.installFinish())`. Everything Bitrix24 needs us to have done
    first - tokens stored, admin proven, placements and events bound - is already done
    server-side, because widgets stay invisible until `installFinish` runs and it must
    be the last step.
    """
    locale = resolve_locale(lang)
    context = _base_context(locale)
    context.update(
        {
            "title": t(locale, "install.title"),
            "progress": t(locale, "install.progress"),
            "hint": t(locale, "install.hint"),
            "fallback": t(locale, "install.fallback"),
        }
    )
    return HTMLResponse(
        content=_render("install.html", context),
        status_code=200,
        headers=_headers(domain, protocol_https),
    )


def render_error(
    request: Request,
    *,
    lang: str | None,
    domain: str | None,
    protocol_https: bool = True,
    status_code: int = 500,
) -> HTMLResponse:
    """Last resort for an unexpected exception (§4.11).

    Same copy as the `error` state but its own template, because this is the page a
    handler renders when it does not know what failed: it must not depend on anything
    the failing code path computed, only on the request id that ties the frame to the
    log line and to `rest_log.correlation_id` (§6).
    """
    locale = resolve_locale(lang)
    context = _base_context(locale)
    context.update(
        {
            "title": t(locale, "state.error.title"),
            "body": t(locale, "state.error.body"),
        }
    )
    return HTMLResponse(
        content=_render("error.html", context),
        status_code=status_code,
        headers=_headers(domain, protocol_https),
    )


#: An SPA route this handler may hand the browser to. Root-relative, no scheme, no
#: host, no dot segments: `render_handoff` writes it into `location.replace()`, and a
#: value that could start with `//` or `http:` would be an open redirect out of the
#: Bitrix24 frame. Every caller passes a constant, and this is the check that keeps it
#: that way.
_TARGET_PATH_RE: Final[re.Pattern[str]] = re.compile(r"^/[A-Za-z0-9._~/-]{0,255}$")

#: RFC 3986 query characters. `bitrix/forms.py` already applied the same allowlist to
#: `raw_query` (§4.2); it is repeated because this value is interpolated into a page.
_RAW_QUERY_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9._~:/?#\[\]@!$&()*+,;=%-]*$")

#: A compact JWS: three base64url segments. Anything else never reaches the page.
_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")


def _lang_of(query: str) -> str | None:
    """Bitrix24's `LANG` out of the forwarded query string (§4.4 step 8).

    `parse_qsl` never raises on a malformed pair, and the value is handed to
    `resolve_locale`, which maps anything unknown onto a supported locale - so a
    crafted `LANG` can only change which translation a spinner is rendered in.
    """
    for key, value in parse_qsl(query, keep_blank_values=False):
        if key.lower() == "lang" and value:
            return value
    return None


def _js_string(value: str) -> Markup:
    """One JS string literal, safe in a `<script>` block inside an HTML document.

    Two escaping layers are involved and both have to be right:

    * JSON gives a valid JS literal, with `ensure_ascii=True` so the output is pure
      ASCII whatever the page's charset ends up being;
    * `<`, `>` and `&` are then escaped to `\\uXXXX`, because an HTML parser looks for
      `</script` inside a script element no matter what JavaScript thinks the quoting
      is - that sequence, not a quote, is how a string in an inline script breaks out.

    The result is returned as `Markup` so Jinja's autoescape (which would turn the
    literal's own quotes into `&quot;` and break the script) leaves it alone. That is
    safe precisely because nothing outside this ASCII, HTML-inert set survives above.
    """
    encoded = json.dumps(value, ensure_ascii=True)
    for raw, escaped in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026")):
        encoded = encoded.replace(raw, escaped)
    # S704 is suppressed below because this is the one place the escaping is DONE
    # rather than assumed:
    # `encoded` is pure ASCII JSON with `<`, `>` and `&` already replaced above, so
    # nothing in it can close the script element or break the literal.
    return Markup(encoded)  # noqa: S704


def render_handoff(
    request: Request,
    *,
    target_path: str,
    raw_query: str,
    token: str,
    domain: str | None,
    protocol_https: bool = True,
) -> HTMLResponse:
    """Hand the browser to the SPA, with the session token in the URL fragment (§4.4 step 8).

    This is the decision of §4.6 / decision 6 made concrete. The alternative - a 303
    whose `Location` carries the token - would write a live session credential into the
    reverse proxy's access log on every open of every tenant, and into the browser's
    history and any intermediary that logs URLs. So the response is an ordinary HTML
    page whose inline script runs::

        location.replace("<target_path>?<Bitrix24's original query string>#s=<jwt>")

    Three properties of that string are load-bearing:

    * **the query string is forwarded verbatim**, not rebuilt from parsed fields:
      `APP_SID` (plus `DOMAIN`, `PROTOCOL`, `LANG`) must reach the SPA or `BX24.init`
      never fires and `fitWindow` / `openPath` / `getAuth` are inert (§11 assumption 11);
    * **the token is in the fragment**, which browsers never send to a server - so it
      cannot appear in an access log, a `Referer` or a `rest_log` row (§4.6);
    * **`target_path` is root-relative and allowlisted**, so this page can never be
      turned into an open redirect that carries a fresh session token off-origin.

    `token=""` is a first-class case: `/state/denied` and `/state/crm_no_access` are
    reached through exactly this page **with no fragment at all** (§4.4 step 8 table),
    because they must be shown inside the same SPA shell but must not carry a session.

    Every input is re-validated here rather than trusted from the caller, in the same
    spirit as the `frame-ancestors` domain check above: this function is the last code
    that touches these values before they become a page.
    """
    if not _TARGET_PATH_RE.match(target_path):
        # A programming error, never a portal-supplied value: fail loudly into the
        # generic error page rather than emit a redirect we cannot vouch for.
        _log.error("render_handoff: refusing an unsafe target path")
        return render_error(
            request, lang=None, domain=domain, protocol_https=protocol_https
        )

    query = raw_query[1:] if raw_query.startswith("?") else raw_query
    if not _RAW_QUERY_RE.match(query):
        # The SPA loses BX24 (§11 assumption 11) but the page still renders; a
        # malformed query string is not worth a dead frame.
        _log.warning("render_handoff: dropping a malformed query string")
        query = ""

    if token and not _TOKEN_RE.match(token):
        # Cannot happen with `security/session_token.issue_session`; if it ever did,
        # sending the SPA on without a session is far better than emitting garbage.
        _log.error("render_handoff: refusing a token that is not a compact JWS")
        token = ""

    target = target_path
    if query:
        target = f"{target}?{query}"
    if token:
        # §4.6: `#s=` is the only channel the token ever travels in.
        target = f"{target}#s={token}"

    # The signature carries no `lang`: Bitrix24 repeats `LANG` in the very query
    # string this page forwards, so the page's own locale is read back out of it
    # rather than passed twice and risk disagreeing with what the SPA will use.
    locale = resolve_locale(_lang_of(query))
    context = _base_context(locale)
    context.update(
        {
            "title": context["app_name"],
            # §8 keeps every string in the shared catalogue. These two are optional
            # there: the page is visible for a few milliseconds, so a missing key
            # renders nothing rather than a dotted key on a moderator's screen.
            "progress": t(locale, "handoff.progress") if has_message("handoff.progress") else None,
            "noscript": (
                t(locale, "handoff.noscript")
                if has_message("handoff.noscript")
                else t(locale, "common.openFromBitrix24")
            ),
            "target": _js_string(target),
        }
    )
    return HTMLResponse(
        content=_render("handoff.html", context),
        status_code=200,
        # `Cache-Control: no-store` (§4.10) is what keeps a page carrying a live
        # session token out of every shared cache and out of the browser's own.
        headers=_headers(domain, protocol_https),
    )
