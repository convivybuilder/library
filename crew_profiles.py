"""The read path for `roster_agents` and `profiles`
(issue_01M19QPV0J72EK1NDB0H8KT5EM), and the profile RESOLUTION itself.

`crew_registry.py` is the write path for the same two tables; this is its
sibling rather than a growth of it, so a caller can read the schema `crew_
registry`'s own module docstring once claimed as "later work" without also
picking up a write module's capability-checked mutating functions.

**THE ONE PROPERTY THIS EXISTS FOR: a profile is a SELECTION RULE, and
`resolve()` returns its DOCUMENT SET, never the rule.** The epic's step 4
names why — three clients in three languages (Orient's `library_client.py`,
agent-platform's `vault_plane.py`, Deck's Swift) would each have to
re-implement the evaluation if the route handed back `include` instead, and
any disagreement between them would be silent. `GET /profiles/{project}/
{variant}` calls `resolve()` and returns its output; nothing in `app.py`
reaches into a profile row's `include` column and serialises it.

**`resolve()` MUST MATCH `agent-platform/bin/knowledge-ctx`'s `query_rule` +
`collect_matches` EXACTLY** (around lines 273-331 there), because that pair
is the evaluator already running in production, reading the same YAML shape
this module's rows were loaded from (`load_crew_registry.py`). Two
evaluators over the same rule that disagree is the exact failure this
module exists to prevent — so a change to the semantics below is a change
to both places, made deliberately and together, not a drift one of them
suffers alone.

**THREE DELIBERATE NARROWINGS FROM WHAT `knowledge-ctx` ACCEPTS, recorded
here rather than fixed, and this list is meant to be the complete one.** A
list of divergences that omits a divergence is worse than no list, because
the next reader compares the two evaluators against it and stops when it
runs out.

1. `query_rule` accepts `project` as a bare string as well as a list.
2. It accepts `status` as a list as well as a scalar.
3. It treats `kind` as OPTIONAL — `if kind:` guards the clause — while
   `_rule_clause` below opens with an unconditional `kind = ?`.

The stored schema is narrower than all three:
`crew_registry._validate_include_block` requires `project` to be a list,
`status` to be a single string, and `kind` to be present and non-empty on
every rule. So a YAML profile using any of these forms fails LOUDLY at
`load_crew_registry.py` load time rather than resolving differently here —
the failure a caller sees is "this profile could not be loaded", never a
route quietly answering a different document set than `knowledge-ctx` would
have for the same YAML. Measured 2026-09-06 across all 24 current profile
YAML files and 184 rules: zero use any of the three forms, so nothing in
the corpus is affected today.

**Each narrowing holds only while `crew_registry` keeps validating that
field.** `_rule_clause` reads `rule["kind"]` directly and would raise
`KeyError` on a rule without one; that is the intended shape while the
write path guarantees it, and this paragraph is the note to read if that
validation is ever relaxed.
"""
from __future__ import annotations

import json
import sqlite3

import identity
import reference


def roster(conn: sqlite3.Connection) -> list[dict]:
    """Every `roster_agents` row, ordered by name."""
    return [dict(row) for row in
            conn.execute("SELECT * FROM roster_agents ORDER BY name")]


def profiles(conn: sqlite3.Connection) -> list[dict]:
    """Every `profiles` row — which profiles exist, and who last touched
    each — ordered by `(project, variant)`.

    **`include` IS NOT SELECTED.** This is the "which profiles exist" read,
    not "what does each one select"; the column this module exists to keep
    off the wire is left out of the query rather than trusted to be dropped
    downstream.
    """
    return [dict(row) for row in conn.execute(
        "SELECT project, variant, created_at, created_by, updated_at, "
        "updated_by FROM profiles ORDER BY project, variant")]


def profile_row(conn: sqlite3.Connection, project: str, variant: str
                 ) -> dict | None:
    """The one `profiles` row named `<project>/<variant>`, or None.

    **`include` and `claude_md_extras` come back decoded**, the same
    `json.loads` `crew_registry._profile_dict` already does on a row this
    service itself wrote — trusted for the same reason that one is: this
    table has exactly one writer, `crew_registry.profile_put`, and it always
    serialises through `json.dumps` first. No new guard is added around the
    decode; adding one here would imply a distrust of this column that the
    write path's own reader does not carry.
    """
    row = conn.execute(
        "SELECT * FROM profiles WHERE project = ? AND variant = ?",
        (project, variant)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["include"] = json.loads(d["include"])
    d["claude_md_extras"] = json.loads(d["claude_md_extras"])
    return d


def _rule_clause(rule: dict, *, alias: str = "") -> tuple[str, list]:
    """One `include` rule as a SQL `WHERE` fragment and its bound params,
    matching `knowledge-ctx.query_rule`'s per-field semantics exactly (see
    this module's docstring).

    **`alias`, ADDED FOR `bound_scope` BELOW** (issue_01M35QG0VANA5P1XE3PH6A7PS4):
    this function's own caller, `resolve_counted`, queries bare `docs` with
    no join and no alias, which is the default (`alias=""`) and leaves every
    column name exactly as it always read. `bound_scope` ANDs a profile's
    scope into a caller's own query over `docs d` — every Reference Room
    route in `app.py` joins the FTS table or aliases the listing this way —
    so it passes `alias="d"` to get `d.kind`, `d.project`, `d.status` and
    `d.audience` instead. One function either way: there is no second
    translation of a rule into SQL for the aliased case."""
    prefix = f"{alias}." if alias else ""
    where = [f"{prefix}kind = ?"]
    params: list = [rule["kind"]]

    rule_projects = rule.get("project")
    if rule_projects:
        where.append(
            f"{prefix}project IN ({','.join('?' * len(rule_projects))})")
        params.extend(rule_projects)

    status = rule.get("status")
    if status:
        where.append(f"{prefix}status = ?")
        params.append(status)
    else:
        # THE DEFAULT MATCHES `reference.DEFAULT_STATUSES`, the same
        # "current + draft unless asked for by name" rule the Reference
        # Room's own routes apply — a profile with no `status` should not
        # silently pull in an archived document `knowledge-ctx` would also
        # have skipped.
        placeholders = ",".join("?" * len(reference.DEFAULT_STATUSES))
        where.append(f"{prefix}status IN ({placeholders})")
        params.extend(reference.DEFAULT_STATUSES)

    audience_contains = rule.get("audience_contains")
    if audience_contains:
        # UNALIASED BY DEFAULT: this query has no join and gives `docs` no
        # alias, unlike `app.py`'s `d`-aliased Reference Room routes, so the
        # default `expr="audience"` is right here without being passed
        # explicitly — and `expr=f"{prefix}audience"` is exactly `"audience"`
        # again when `prefix` is empty.
        where.append(reference.audience_clause(len(audience_contains),
                                                expr=f"{prefix}audience"))
        params.extend(audience_contains)

    return " AND ".join(where), params


def bound_scope(conn: sqlite3.Connection, ident: identity.Identity, *,
                alias: str = "") -> tuple[str, list] | None:
    """The SQL fragment and its bound params testing whether a `docs` row
    (optionally `alias`-prefixed) falls inside `ident`'s bound dispatch
    profile — ready to AND straight into a caller's own `WHERE`. `None` when
    `ident` carries no binding at all (`ident.profile is None`), the signal
    to the caller that it must not narrow its query — every credential
    behaves exactly as it did before this existed (issue_01M35QG0VANA5P1XE3PH6A7PS4).

    **THE SAME `_rule_clause` `resolve_counted` EVALUATES — NEVER A SECOND
    TRANSLATION OF A RULE INTO SQL.** `alias` only prefixes the column names
    so a caller joining `docs` under its own alias can AND this straight
    into an existing query rather than re-running it.

    **FAIL CLOSED.** A binding naming a `<project>/<variant>` the `profiles`
    table does not hold, or one that resolves to a profile with no `include`
    rules at all, answers `("(1 = 0)", [])` — every row refused, never the
    unscoped corpus. A profile WITH rules is the OR of each rule's own
    clause, matching `resolve()`'s own union: a row in scope for any one
    rule is in scope.
    """
    if ident.profile is None:
        return None
    project, variant = ident.profile.split("/", 1)
    row = profile_row(conn, project, variant)
    rules = row["include"] if row is not None else []
    if not rules:
        return "(1 = 0)", []
    parts: list[str] = []
    params: list = []
    for rule in rules:
        clause, rule_params = _rule_clause(rule, alias=alias)
        parts.append(f"({clause})")
        params.extend(rule_params)
    return "(" + " OR ".join(parts) + ")", params


def resolve(profile_row: dict, index_conn: sqlite3.Connection) -> list[dict]:
    """The document set a profile's `include` rules select — a profile as
    read by `profile_row()` above, evaluated against the knowledge index.

    **Union across rules, deduped by document id, FIRST WINS, then sorted by
    id** — the same fold `knowledge-ctx.collect_matches` performs, so that a
    document two rules both match is not somehow present twice, and the
    order a caller sees is stable across requests rather than an artifact of
    which rule happened to run last.
    """
    return resolve_counted(profile_row, index_conn)[0]


def resolve_counted(profile_row: dict, index_conn: sqlite3.Connection
                    ) -> tuple[list[dict], list[int]]:
    """`resolve()`'s document set, plus how many documents each `include`
    rule matched on its own, in rule order and before the union dedupes them
    (issue_01M34SV3BDRHBTXZPZ8C4AH9HD). These are the counts
    `knowledge-ctx.collect_matches` records as individuation telemetry: a
    rule that matches nothing is dead, and one that dominates the total is
    over-broad. Counting before the dedupe is what makes a rule whose every
    match another rule already reached still read as alive."""
    seen: dict[str, sqlite3.Row] = {}
    counts: list[int] = []
    for rule in profile_row["include"]:
        clause, params = _rule_clause(rule)
        rows = index_conn.execute(
            f"SELECT * FROM docs WHERE {clause}", params).fetchall()
        counts.append(len(rows))
        for row in rows:
            seen.setdefault(row["id"], row)
    return sorted(seen.values(), key=lambda r: r["id"]), counts
