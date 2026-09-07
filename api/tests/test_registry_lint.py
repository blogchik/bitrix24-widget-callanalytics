"""§4.1 - the invariants that only a static check can hold.

Three of this design's guarantees are "there is exactly one place where X happens".
They are true today because someone wrote them that way, and they stay true only if a
second place is a build failure rather than a code review someone might skim:

* **Credential invariant** (§4.1) - `access_token_enc`, `refresh_token_enc` and
  `client_endpoint` may be written only through `services/portals.py`, which is where
  the `user.admin` proof is enforced. A second writer is how a non-admin token becomes
  the worker's credential, or how `client_endpoint` gets learned from `DOMAIN`. §4.1
  names this file explicitly: "tests/test_registry_lint.py fails the build if any other
  module writes those columns."
* **One error mapping** (errors.py) - every module branches on the *type* `classify()`
  returned, never on the string. Bitrix24's published error list is not closed
  (research note (e)), and a stray `== "expired_token"` somewhere is a mapping that
  nobody will remember to update when a cabinet turns out to emit another spelling.
* **RLS-bound tables** (§1 decision 8, §5.9) - `calls`, `employees` and `crm_contexts`
  are reachable only under `tenant_txn`, and RLS fails *silently* closed. A stray
  `DELETE FROM calls` in a control transaction reports success having deleted nothing,
  which is precisely the purge bug the design review found.

The scan is AST-based rather than a plain grep so that reading a value
(`portal.client_endpoint`) and naming a local (`client_endpoint = tokens.client_endpoint`)
stay legal, while assigning an attribute, a dict key or a SQL column does not.

To extend any rule **deliberately**, add the module to the one allowlist below and say
why in the comment beside it. That comment is the audit trail.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Final, NamedTuple

import pytest

APP_ROOT: Final[Path] = Path(__file__).resolve().parents[1] / "app"


# =====================================================================================
# THE ALLOWLIST. Every exemption in the codebase is in this block and nowhere else.
# =====================================================================================

#: §4.1 credential + endpoint invariants. `services/portals.py` owns
#: `store_portal_credential()`, the only function permitted to write a portal credential.
#: `bitrix/oauth.py` is the narrow exception the milestone-2 contract grants: §5.8 step 4
#: writes the rotated pair, `token_expires_at`, `client_endpoint` and `token_version`
#: under the single-flight row lock, and routing that through the service layer would
#: mean releasing the lock. `db/models.py` merely *declares* the columns.
CREDENTIAL_WRITERS: Final[frozenset[str]] = frozenset(
    {
        "services/portals.py",
        "bitrix/oauth.py",
        "db/models.py",
    }
)

#: The only module allowed to compare a Bitrix24 error string. `classify()` lives here.
ERROR_STRING_MATCHERS: Final[frozenset[str]] = frozenset({"bitrix/errors.py"})

#: §2 assigns each customer-data table exactly one writer. Everything else reads through
#: `services/calls_repo.py`, which is also where `scope_filter` is applied (§4.7).
TENANT_TABLE_WRITERS: Final[frozenset[str]] = frozenset(
    {
        "services/calls_repo.py",   # every read of `calls`; the single scope_filter site
        "services/employees.py",    # employee cache upserts (§7)
        "services/crm_context.py",  # crm_contexts resolve + cache (§4.4 step 7)
        "sync/upsert.py",           # the statistic.get upsert (§5.5)
        "jobs/definitions.py",      # purge_portal, under tenant_txn per chunk (§5.9)
        "db/models.py",             # declarations only
    }
)

# =====================================================================================


#: The columns whose write is a security decision, not a data update (§4.1).
GUARDED_COLUMNS: Final[frozenset[str]] = frozenset(
    {"access_token_enc", "refresh_token_enc", "client_endpoint"}
)

#: The FORCED-RLS tables of §3.
TENANT_TABLES: Final[tuple[str, ...]] = ("calls", "employees", "crm_contexts")

_DML_RE: Final[re.Pattern[str]] = re.compile(
    r"(?is)\b(?:insert\s+into|update|delete\s+from)\s+(?:only\s+)?\"?"
    rf"({'|'.join(TENANT_TABLES)})\b"
)
_GUARDED_SQL_RE: Final[re.Pattern[str]] = re.compile(
    r"(?is)\b(?:insert\s+into|update|set)\b[^;]*?\b(" + "|".join(sorted(GUARDED_COLUMNS)) + r")\b"
)


class Violation(NamedTuple):
    module: str
    line: int
    detail: str

    def __str__(self) -> str:  # pragma: no cover - only rendered on failure
        return f"{self.module}:{self.line}: {self.detail}"


def _modules() -> list[tuple[str, Path, ast.Module]]:
    """Every `app/**.py`, keyed by its path relative to `app/` (what the allowlist uses)."""
    found: list[tuple[str, Path, ast.Module]] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        relative = path.relative_to(APP_ROOT).as_posix()
        source = path.read_text(encoding="utf-8")
        found.append((relative, path, ast.parse(source, filename=str(path))))
    return found


def _string_constants(tree: ast.Module) -> list[tuple[int, str]]:
    """Every string literal in the module, with its line - where raw SQL hides."""
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def test_the_app_package_is_present_and_scannable() -> None:
    """Guards the lint itself: an empty scan would make every rule below pass silently."""
    modules = _modules()
    assert modules, f"no modules found under {APP_ROOT}"
    names = {relative for relative, _, _ in modules}
    assert "config.py" in names and "db/session.py" in names


# --- §4.1 credential and endpoint invariants ----------------------------------------


#: Call shapes that persist a row. The keyword rule below is restricted to these on
#: purpose: `client_endpoint` is also a perfectly ordinary *parsed* field name
#: (`EventPost(client_endpoint=...)` in forms.py, `TokenResponse(...)` in oauth.py), and
#: a lint that cannot tell reading a payload from writing a row would be turned off.
_DB_WRITE_CALLEES: Final[frozenset[str]] = frozenset(
    {"values", "update", "insert", "Portal", "returning", "on_conflict_do_update"}
)


def _is_db_write_call(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr in _DB_WRITE_CALLEES
    if isinstance(func, ast.Name):
        return func.id in _DB_WRITE_CALLEES
    return False


def _credential_write_violations(relative: str, tree: ast.Module) -> list[Violation]:
    violations: list[Violation] = []

    def flag(node: ast.AST, detail: str) -> None:
        violations.append(Violation(relative, getattr(node, "lineno", 0), detail))

    def check_target(target: ast.AST) -> None:
        # `portal.access_token_enc = ...` - the ORM write.
        if isinstance(target, ast.Attribute) and target.attr in GUARDED_COLUMNS:
            flag(target, f"assigns attribute {target.attr!r}")
        # `values["client_endpoint"] = ...` - building a Core update mapping.
        if isinstance(target, ast.Subscript):
            key = target.slice
            if isinstance(key, ast.Constant) and key.value in GUARDED_COLUMNS:
                flag(target, f"assigns dict key {key.value!r}")
        for element in getattr(target, "elts", []):
            check_target(element)

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                check_target(target)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            check_target(node.target)
        elif isinstance(node, ast.Call) and _is_db_write_call(node):
            # `update(Portal).values(access_token_enc=...)`, `Portal(client_endpoint=...)`.
            for keyword in node.keywords:
                if keyword.arg in GUARDED_COLUMNS:
                    flag(node, f"persists {keyword.arg!r} via a write call")
            for argument in node.args:
                if isinstance(argument, ast.Dict):
                    for key in argument.keys:
                        if isinstance(key, ast.Constant) and key.value in GUARDED_COLUMNS:
                            flag(node, f"persists {key.value!r} via a write mapping")
    return violations


def test_only_the_credential_writer_may_write_the_guarded_columns() -> None:
    """§4.1: "Every path ... goes through `services/portals.py::store_portal_credential()`."

    The columns below decide *whose* token the worker syncs with and *where* it points.
    A write from anywhere else is either a credential stored without the `user.admin`
    proof (decision 4) or a REST base learned from an unproven payload (decision 2) -
    the two takeovers this design exists to close.
    """
    violations = [
        violation
        for relative, _, tree in _modules()
        if relative not in CREDENTIAL_WRITERS
        for violation in _credential_write_violations(relative, tree)
    ]
    assert not violations, (
        "these modules write a guarded credential column (§4.1):\n  "
        + "\n  ".join(str(v) for v in violations)
        + "\n\nRoute the write through services/portals.py::store_portal_credential(), "
        "or - if the exemption is genuinely intended - add the module to "
        "CREDENTIAL_WRITERS at the top of this file with a comment saying why."
    )


def test_no_module_writes_a_guarded_column_through_raw_sql() -> None:
    """The AST rule above cannot see inside a `text(\"UPDATE portals SET ...\")` string."""
    violations: list[Violation] = []
    for relative, _, tree in _modules():
        if relative in CREDENTIAL_WRITERS:
            continue
        for line, value in _string_constants(tree):
            match = _GUARDED_SQL_RE.search(value)
            if match:
                violations.append(
                    Violation(relative, line, f"raw SQL writes {match.group(1)!r}")
                )
    assert not violations, "\n  ".join(["guarded column written in raw SQL (§4.1):", *map(str, violations)])


def test_no_module_builds_a_rest_base_from_domain() -> None:
    """Decision 2 / §4.1: "`DOMAIN` is only ever display/CSP data and is never used to
    build a REST base."

    The forged-`DOMAIN` takeover is a one-line mistake - an f-string joining a POSTed
    hostname to `/rest/` - so it is worth catching by shape as well as by review.
    """
    suspicious = re.compile(r"(?i)(https?://)?\{?\s*domain\s*\}?\s*(/rest/|\+\s*[\"']/rest)")
    violations: list[Violation] = []
    for relative, _, tree in _modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr):
                rendered = "".join(
                    part.value if isinstance(part, ast.Constant) and isinstance(part.value, str)
                    else "{domain}" if _mentions_domain(part) else "{x}"
                    for part in node.values
                )
                if "/rest" in rendered and "{domain}" in rendered:
                    violations.append(
                        Violation(relative, node.lineno, "f-string builds a REST base from DOMAIN")
                    )
        for line, value in _string_constants(tree):
            if suspicious.search(value) and relative != "db/models.py":
                violations.append(Violation(relative, line, "string builds a REST base from DOMAIN"))
    assert not violations, "\n  ".join(["a REST base derived from DOMAIN (§4.1):", *map(str, violations)])


def _mentions_domain(node: ast.AST) -> bool:
    """True when an f-string placeholder is (or reads) something called `domain`."""
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id.lower() in {"domain", "dom"}:
            return True
        if isinstance(child, ast.Attribute) and child.attr.lower() == "domain":
            return True
    return False


# --- one error mapping --------------------------------------------------------------


def _known_error_codes() -> frozenset[str]:
    """Read the codes from `errors.py` itself, so the lint follows the mapping."""
    from app.bitrix import errors as errors_module

    codes: set[str] = set()
    mapping = getattr(errors_module, "_BY_ERROR_CODE", {})
    codes.update(str(code).lower() for code in mapping)
    for name in errors_module.__all__:
        klass = getattr(errors_module, name, None)
        default = getattr(klass, "default_code", "") if isinstance(klass, type) else ""
        if default:
            codes.add(str(default).lower())
    codes.discard("")
    return frozenset(codes)


def _literal_strings(node: ast.AST) -> list[str]:
    """String literals directly inside a comparison operand (including set/list/tuples)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        out: list[str] = []
        for element in node.elts:
            out.extend(_literal_strings(element))
        return out
    return []


def test_only_errors_py_compares_a_bitrix_error_string() -> None:
    """errors.py: "Every other module branches on the *type* returned here and never on
    the error string."

    Matching order, case handling and the undocumented spellings (`invalid_token`,
    `WRONG_AUTH_TYPE` - research note (e)) all live in one function. A comparison
    elsewhere is a second, silently divergent mapping.
    """
    codes = _known_error_codes()
    assert "expired_token" in codes, "the lint lost sight of errors.py's mapping"

    violations: list[Violation] = []
    for relative, _, tree in _modules():
        if relative in ERROR_STRING_MATCHERS:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            operands = [node.left, *node.comparators]
            for operand in operands:
                for literal in _literal_strings(operand):
                    if literal.strip().lower() in codes:
                        violations.append(
                            Violation(relative, node.lineno, f"compares the error string {literal!r}")
                        )
    assert not violations, (
        "these modules compare a Bitrix24 error string (errors.py owns that):\n  "
        + "\n  ".join(str(v) for v in violations)
        + "\n\nBranch on the type `classify()` returned instead - `except ExpiredToken:` "
        "or `isinstance(err, AccessDenied)`."
    )


def test_classify_is_reachable_and_still_the_single_mapping() -> None:
    """A behavioural companion to the lint: the mapping must actually be case-insensitive
    (the docs mix cases) and must not raise on an unmapped code."""
    from app.bitrix.errors import ExpiredToken, UnknownBitrixError, classify

    assert isinstance(classify("EXPIRED_TOKEN"), ExpiredToken)
    assert isinstance(classify("expired_token"), ExpiredToken)
    assert isinstance(classify("SOMETHING_NEW_IN_2027"), UnknownBitrixError)
    assert isinstance(classify(None), UnknownBitrixError)


# --- RLS-bound tables ---------------------------------------------------------------


def test_raw_dml_against_a_tenant_table_stays_in_its_owning_module() -> None:
    """§1 decision 8 / §5.9: RLS fails **silently** closed.

    A `DELETE FROM calls` issued from a control transaction reports success having
    deleted nothing - which is exactly how `purge_pending` cleared with a customer's
    rows still on disk in the review's finding. Keeping the DML in the modules that are
    known to open `tenant_txn` is what makes that reviewable at all.
    """
    violations: list[Violation] = []
    for relative, _, tree in _modules():
        if relative in TENANT_TABLE_WRITERS:
            continue
        for line, value in _string_constants(tree):
            match = _DML_RE.search(value)
            if match:
                violations.append(
                    Violation(relative, line, f"raw DML against {match.group(1)!r}")
                )
    assert not violations, (
        "raw DML against an RLS-bound table outside its owning module (§3, §5.9):\n  "
        + "\n  ".join(str(v) for v in violations)
        + "\n\nMove it into the owning service, or add the module to "
        "TENANT_TABLE_WRITERS with a comment saying why it is safe."
    )


def test_the_dml_pattern_actually_matches_the_statements_it_is_meant_to_catch() -> None:
    """Negative control for the regex above: a lint that matches nothing passes forever."""
    for statement in (
        "DELETE FROM calls WHERE portal_id = :pid",
        "delete from crm_contexts",
        "INSERT INTO employees (portal_id, bx_user_id) VALUES (1, 2)",
        "UPDATE calls SET refresh_requested = false",
        "update  only  calls set x = 1",
    ):
        assert _DML_RE.search(statement), f"the DML lint would miss: {statement!r}"
    for benign in (
        "SELECT count(*) FROM calls",
        "DELETE FROM rest_log WHERE ts < now()",
        "UPDATE portals SET token_version = token_version + 1",
        "employees are refreshed every EMPLOYEE_TTL_HOURS",
    ):
        assert not _DML_RE.search(benign), f"the DML lint would false-positive on: {benign!r}"


def test_the_guarded_column_sql_pattern_matches_what_it_is_meant_to_catch() -> None:
    for statement in (
        "UPDATE portals SET access_token_enc = :a, refresh_token_enc = :r",
        "insert into portals (member_id, client_endpoint) values (:m, :c)",
    ):
        assert _GUARDED_SQL_RE.search(statement), f"the credential lint would miss: {statement!r}"
    assert not _GUARDED_SQL_RE.search("SELECT access_token_enc FROM portals WHERE id = :pid")


@pytest.mark.parametrize(
    "snippet",
    [
        "portal.access_token_enc = blob",
        "values['client_endpoint'] = tokens.client_endpoint",
        "stmt = update(Portal).values(refresh_token_enc=blob)",
        "row = Portal(member_id=m, client_endpoint=endpoint)",
        "stmt.values({'client_endpoint': endpoint})",
    ],
)
def test_the_credential_ast_rule_matches_every_write_shape(snippet: str) -> None:
    """Negative control: each of these is a real way to write the column, and the rule
    must see all of them - otherwise the invariant is enforced only against the one
    style whoever wrote the lint happened to picture."""
    tree = ast.parse(snippet)
    assert _credential_write_violations("probe.py", tree), f"the lint would miss: {snippet!r}"


@pytest.mark.parametrize(
    "snippet",
    [
        "endpoint = portal.client_endpoint",
        "if portal.access_token_enc is None: pass",
        "client_endpoint = tokens.client_endpoint",
        "return portal.refresh_token_enc",
        "post = EventPost(client_endpoint=endpoint)",
        "payload = {'client_endpoint': endpoint}",
    ],
)
def test_the_credential_ast_rule_leaves_reads_and_locals_alone(snippet: str) -> None:
    """The other half: reading the value, and naming a local after it, must stay legal
    or every consumer of the credential would have to be allowlisted too."""
    tree = ast.parse(snippet)
    assert not _credential_write_violations("probe.py", tree), f"false positive on: {snippet!r}"
