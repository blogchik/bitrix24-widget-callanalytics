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

import re
from pathlib import Path
from typing import Any, Final

from fastapi import Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.i18n import resolve_locale, t
from app.logging import get_logger, get_request_id

__all__ = ["render_error", "render_install", "render_state"]

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
