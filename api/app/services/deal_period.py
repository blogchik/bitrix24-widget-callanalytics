"""Which deals a period counts beyond the ones created in it (owner decision 3, §4.12).

A deal belongs to the period it was CREATED in. For the stages an administrator names on the
Settings page it ALSO belongs to the period in which it moved into that stage (`movedTime`) or
was modified (`updatedTime`) - the owner's rule of 2026-10-02, "not only by creation time, but by
the time it changed as well", asked for «Успешные» and «Заклад» and left to the administrator for
any other stage. Every other stage keeps creation time alone.

The rule is stored as `portals.deal_period_rule = {"stage_keys": [...]}`, each key the report's
own column key `"<category_id>:<status_id>"` (`bitrix/deals.py::stage_key`). Keys rather than
names, because a name is whatever an administrator last typed (§4.12 constraint 1), and keys
rather than semantics, because the owner's two stages are one won stage and one working stage -
and because a portal can hold a won stage nobody wants counted this way: on 2026-09-14 portal 1
moved 112 old won deals into the «База» funnel in four minutes, and a semantic rule would have put
all of them into September's «Успешные».

Three callers, which is why this is a module of its own:

* the live read (`deal_stats._scan`) turns the resolved stages into two more selections;
* the mirror (`crm_repo.deal_stage_counts`) turns them into one more SQL leg;
* the Settings page (`api/portal.py`) reads and writes the rule.

The first two must resolve the SAME stages, or the two paths draw two different reports. So
`resolve` takes the dictionary each path already holds and nothing else, and the parity tests in
`tests/test_crm_mirror_reports.py` fold the same rows through both.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

from app.bitrix.deals import Stage

__all__ = [
    "MAX_STAGE_KEYS",
    "RULE_KEY",
    "parse_body",
    "resolve",
    "rule_of",
    "stage_keys",
    "validate",
]

#: The one member of `portals.deal_period_rule` this version reads and writes.
RULE_KEY: Final[str] = "stage_keys"

#: More than any real portal names; the bound keeps one request and one `IN` list small.
MAX_STAGE_KEYS: Final[int] = 100

#: `"<category_id>:<status_id>"`. A status id is Bitrix24's (`C16:UC_0U9IW2`, `WON`) and carries
#: colons of its own, so only the first one separates; 128 is `crm_stages.status_id`.
_STAGE_KEY_RE: Final = re.compile(r"^(0|[1-9][0-9]{0,8}):(\S{1,128})$")


def _keys(raw: Any) -> tuple[str, ...] | None:
    """Well-formed, de-duplicated keys in their given order, or None for any malformed one."""
    if not isinstance(raw, list) or len(raw) > MAX_STAGE_KEYS:
        return None
    out: list[str] = []
    for value in raw:
        if not isinstance(value, str) or not _STAGE_KEY_RE.match(value):
            return None
        if value not in out:
            out.append(value)
    return tuple(out)


def stage_keys(rule: Mapping[str, Any] | None) -> tuple[str, ...]:
    """The stored keys, or none.

    Tolerant on purpose: the column is written by one validated function, but a row edited by
    hand must degrade to "creation time alone" - today's report - rather than to a 500.
    """
    if not isinstance(rule, Mapping):
        return ()
    return _keys(rule.get(RULE_KEY)) or ()


def rule_of(keys: Sequence[str]) -> dict[str, Any]:
    """The column value for these keys - the inverse of `stage_keys`."""
    return {RULE_KEY: list(keys)}


def resolve(rule: Mapping[str, Any] | None, stages: Mapping[int, Sequence[Stage]]) -> tuple[Stage, ...]:
    """The rule's stages that this dictionary has, in the dictionary's own order.

    A key whose stage is gone - deleted, or in a funnel this viewer may not see - simply drops
    out: its deals cannot be on screen, so there is nothing to count them into.
    """
    wanted = set(stage_keys(rule))
    if not wanted:
        return ()
    return tuple(stage for funnel in stages.values() for stage in funnel if stage.key in wanted)


def parse_body(payload: Any) -> tuple[str, ...] | None:
    """`{"stage_keys": [...]}` from the Settings page, shape only; None refuses the request."""
    if not isinstance(payload, Mapping) or set(payload) != {RULE_KEY}:
        return None
    return _keys(payload[RULE_KEY])


def validate(keys: Sequence[str], stages: Mapping[int, Sequence[Stage]]) -> tuple[str, ...] | None:
    """The keys in dictionary order when every one names a current stage, else None.

    A key the dictionary does not know is refused rather than stored: it would be a rule that
    silently does nothing, and the administrator who saved it would believe it works.
    """
    known = [stage.key for funnel in stages.values() for stage in funnel]
    if not set(keys) <= set(known):
        return None
    wanted = set(keys)
    return tuple(key for key in known if key in wanted)
