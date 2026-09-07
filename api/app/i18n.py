"""Backend half of the single message source (§8).

WHY this module holds no strings of its own: §8 fixes ONE catalogue,
`web/messages/<locale>.json`, shared by the Next.js SPA and by the four
server-rendered pages (`install.html`, `handoff.html`, `state.html`,
`error.html`), and ONE locale definition, `web/src/i18n/locales.json` (locale
list + the `kz->ru`, `uz->ru` fallbacks). Adding Uzbek must be "drop `uz.json`,
add one line to `locales.json`" - which is only true while no second copy of the
list or of a string exists in Python.

WHY loading never raises: this module is imported by the request path that
renders the moderator-facing pages. §4.11 makes a blank frame or a raw error a
moderation rejection, so a missing bundle degrades (loudly, at CRITICAL, naming
every path searched) instead of taking the process down. The *structural*
defaults below are the shape of `locales.json`, not a second copy of the UI
text: if the catalogue is absent the pages render their keys, which is
immediately visible in a smoke test, whereas an import-time exception would take
the whole API with it.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Final

__all__ = ["DEFAULT_LOCALE", "FALLBACK", "LOCALES", "has_message", "resolve_locale", "t"]

_log = logging.getLogger(__name__)

# --- where the shared bundle lives ---------------------------------------------------

_APP_DIR: Final[Path] = Path(__file__).resolve().parent  # .../api/app
_IMAGE_ROOT: Final[Path] = _APP_DIR.parent  # /app in the container, .../api in a checkout
_REPO_ROOT: Final[Path] = _IMAGE_ROOT.parent  # repo root when running from a checkout

#: (locales.json, messages dir) pairs, most specific first. The first pair whose
#: locales.json exists wins. Order matters: the image copy (§8: "the api image copies
#: messages/ and src/i18n/locales.json at build time") must beat a stray checkout.
_BUNDLES: Final[tuple[tuple[Path, Path], ...]] = (
    (_IMAGE_ROOT / "i18n" / "locales.json", _IMAGE_ROOT / "i18n" / "messages"),
    (_REPO_ROOT / "web" / "src" / "i18n" / "locales.json", _REPO_ROOT / "web" / "messages"),
)

# Shape of locales.json, used only when the file itself cannot be found. Not UI text:
# no string a moderator can read is duplicated here (see the module docstring).
_DEFAULT_LOCALES: Final[tuple[str, ...]] = ("ru", "en")
_DEFAULT_DEFAULT: Final[str] = "ru"
_DEFAULT_FALLBACK: Final[dict[str, str]] = {"kz": "ru", "uz": "ru"}

#: §8: anything the fallback map does not name falls back to English.
_UNKNOWN_LOCALE: Final[str] = "en"

# --- loading -------------------------------------------------------------------------


def _read_json(path: Path) -> dict[str, Any] | None:
    """One JSON object from disk, or None with a log line. Never raises."""
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        _log.exception("i18n: unreadable message file", extra={"path": str(path)})
        return None
    if not isinstance(data, dict):
        _log.error("i18n: message file is not a JSON object", extra={"path": str(path)})
        return None
    return data


def _flatten(node: Any, prefix: str, out: dict[str, str]) -> None:
    """`{"state": {"denied": {"title": "..."}}}` -> `state.denied.title`.

    The SPA (next-intl) addresses the same catalogue with dotted keys, so the two
    sides quote identical key strings in code review.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            _flatten(value, f"{prefix}.{key}" if prefix else str(key), out)
    elif isinstance(node, str):
        out[prefix] = node
    elif isinstance(node, (int, float, bool)):
        out[prefix] = str(node)
    # Anything else (list, null) is a catalogue mistake: skipped, never rendered.


def _load() -> tuple[tuple[str, ...], str, dict[str, str], dict[str, dict[str, str]]]:
    """Read `locales.json` plus one `<locale>.json` per declared locale, at import."""
    searched: list[str] = []
    for locales_path, messages_dir in _BUNDLES:
        searched.append(str(locales_path))
        config = _read_json(locales_path)
        if config is None:
            continue

        raw_locales = config.get("locales")
        locales = tuple(
            code.strip().lower()
            for code in (raw_locales if isinstance(raw_locales, list) else [])
            if isinstance(code, str) and code.strip()
        )
        if not locales:
            _log.error("i18n: locales.json declares no locales", extra={"path": str(locales_path)})
            continue

        raw_default = config.get("default")
        default = (
            raw_default.strip().lower()
            if isinstance(raw_default, str) and raw_default.strip().lower() in locales
            else locales[0]
        )
        raw_fallback = config.get("fallback")
        fallback = {
            str(key).strip().lower(): str(value).strip().lower()
            for key, value in (raw_fallback if isinstance(raw_fallback, dict) else {}).items()
        }

        catalogues: dict[str, dict[str, str]] = {}
        for locale in locales:
            data = _read_json(messages_dir / f"{locale}.json")
            flat: dict[str, str] = {}
            if data is None:
                _log.error(
                    "i18n: message file missing for a declared locale",
                    extra={"locale": locale, "path": str(messages_dir / f"{locale}.json")},
                )
            else:
                _flatten(data, "", flat)
            catalogues[locale] = flat
        return locales, default, fallback, catalogues

    # §8's bundle is copied into the api image; if that copy is missing the pages
    # would render their keys, so this has to be impossible to miss in the logs.
    _log.critical(
        "i18n: shared message bundle not found - pages will render message keys",
        extra={"searched": searched},
    )
    return (
        _DEFAULT_LOCALES,
        _DEFAULT_DEFAULT,
        dict(_DEFAULT_FALLBACK),
        {code: {} for code in _DEFAULT_LOCALES},
    )


_BUNDLE = _load()

#: The locale list of §8, in the order `locales.json` declares them. The placement
#: binder derives `LANG_ALL` from exactly this tuple.
LOCALES: Final[tuple[str, ...]] = _BUNDLE[0]
DEFAULT_LOCALE: Final[str] = _BUNDLE[1]
FALLBACK: Final[dict[str, str]] = _BUNDLE[2]
_MESSAGES: Final[dict[str, dict[str, str]]] = _BUNDLE[3]


# --- public API ----------------------------------------------------------------------


def resolve_locale(lang: str | None) -> str:
    """Bitrix24's `LANG` (or any tag) -> one of `LOCALES` (§8).

    Rules, in order: an exact supported locale wins; `kz`/`uz` map through the shared
    fallback map (a Kazakh or Uzbek portal is Russian-speaking in practice); anything
    else unknown becomes English; an ABSENT value becomes `locales.json`'s `default`,
    which is what that key exists for - the app's primary market is ru.
    """
    if not lang:
        return DEFAULT_LOCALE
    # "ru-RU", "RU", "ru_RU" all arrive from one cabinet or another; §4.2 already
    # constrains LANG to two letters, but this function is also called with values
    # that never passed the form allowlist (the error page renders before parsing).
    code = lang.strip().lower().replace("_", "-").split("-", 1)[0]
    if code in LOCALES:
        return code
    mapped = FALLBACK.get(code)
    if mapped is not None and mapped in LOCALES:
        return mapped
    if _UNKNOWN_LOCALE in LOCALES:
        return _UNKNOWN_LOCALE
    return DEFAULT_LOCALE


def _chain(locale: str) -> tuple[str, ...]:
    """Lookup order for one locale: itself, its fallback, the default, English."""
    order: list[str] = []
    for candidate in (locale, FALLBACK.get(locale), DEFAULT_LOCALE, _UNKNOWN_LOCALE):
        if candidate and candidate in _MESSAGES and candidate not in order:
            order.append(candidate)
    return tuple(order)


def has_message(key: str) -> bool:
    """True when ANY loaded catalogue defines `key`.

    Callers use it to decide whether a string exists at all before building something
    around it - `bitrix/placements.py` skips a locale that has no tab title rather than
    binding a placement whose `LANG_ALL` entry would be a dotted key. Note what it is
    NOT for: choosing a state kind. A page whose copy is missing still renders under its
    own kind, because the kind is the thing a moderator and a test identify (§4.11).
    """
    return any(key in catalogue for catalogue in _MESSAGES.values())


def t(locale: str, key: str, **kw: object) -> str:
    """One translated string; the key itself if no catalogue in the chain has it.

    Returning the key is deliberate: a missing string must be visible and greppable,
    never an exception on the moderator-facing render path (§4.11).
    """
    for candidate in _chain(locale):
        value = _MESSAGES[candidate].get(key)
        if value is None:
            continue
        if not kw:
            return value
        try:
            return value.format(**kw)
        except (KeyError, IndexError, ValueError):
            # A placeholder mismatch is a catalogue bug; the unformatted sentence is
            # still readable, and the page must render regardless.
            _log.warning("i18n: placeholder mismatch", extra={"key": key, "locale": candidate})
            return value
    return key
