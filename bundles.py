"""The individuation bundle — the documents one agent needs for one task
(issue_01M2VCWQXTF5EGHD1X1J3P4RTJ, the first slice of issue_01M2MATTV4PRXC1RP4367W9BJN).

`platform/decisions/0085` (the operator, 2026-09-16): "The Library hands the Bosun a
token, the individuator redeems it for the document bundle, and the bundle is
scoped to the combination of agent and task." This module is the Library's side
of that seam. The Bosun asks `POST /bundles` for a token; the individuator
presents it to `POST /bundles/redeem` and receives the documents. Nothing on
the redeeming side reads the shared knowledge checkout.

## What a bundle contains, and what this slice does not do

A bundle is the agent's own definition plus the skills its profile selects for
it: the two kinds `knowledge-ctx` writes into `.claude/` as files (every other
kind stays reachable through the `kb` tools whatever a profile selects). The
profile is `<project>/<variant>`, where `project` is the item's product for an
item-scoped bundle and the named repository for a repository-scoped one.

**The item contributes its product and its existence, and nothing else.** No
selection narrows by an item's topics or body; the spec for this slice names
agent and task as the scope and defines no task-side selector, and inventing
one would be guessing. The token is still scoped to the item, so a later
task-side rule changes what `resolve_bundle` selects and not what the wire
carries.

**Each kind narrows to the agent through the documents' own `audience`.** A
skill or agent document whose `audience` lists the requested agent is in; one
that does not is out. There is no mapping between role names: `platform-coder`
is not resolved as `coder`. That is deliberate. The vocabulary the skills
declare is generic (`coder` on 23 skills, `reviewer` on 22, `platform-coder`
on none, measured 2026-09-18), correcting it is a Reference Room change made
through the `kb` tools, and until it lands the honest answer for a
`platform-coder` is the refusal below.

## Refusal is the design; nothing is dropped

An omitted rule and a served bundle look identical to the caller, so every
condition below refuses the WHOLE bundle and names what it found. None
degrades to a smaller bundle.

* **A document the profile selects whose `status` is not `current`** (a rule
  that names no `status` selects `current` and `draft`). The refusal names each
  such document and its status.
* **An audience filter that matches nothing — unless the agent said to expect
  it.** For each kind the profile has a rule for, at least one document must
  survive the agent's audience filter, UNLESS the agent's own `agent`-kind
  definition names that kind in its `takes_none_of` list. The live case this
  guards is a skill filter for `platform-coder`, which matches 0 of 31 skills
  by vocabulary error rather than by design: an empty bundle there would ship
  a silently under-equipped agent, so it still refuses. A read-only agent like
  `scout` that is never given skills declares `takes_none_of: [skill]` on its
  own definition, and for it the same empty match is the design working, so a
  bundle issues with zero documents of that kind. The declaration is read only
  from the matched, `current` `agent`-kind document the bundle itself serves;
  it cannot exempt the `agent` kind itself, which every bundle always carries;
  and a declaration that is not a list of known kind names refuses instead of
  being read as "takes nothing." The refusal, when it still fires, says how
  many documents the filter saw and which audience values they do carry, which
  is what someone correcting the vocabulary needs.
* **No definition.** The agent's own definition is the `agent` filter above and
  is always owed: a profile with no `agent` rule, or none naming the agent, is
  refused, and a definition the index holds but the profile does not select
  (a retired one, most often) is named in the refusal.
* **At redemption, a named document that is missing, no longer `current`, or no
  longer has the hash the token named.** The token names `(id, body_sha256)` at
  issue, and a document can change between issue and redeem (`reference
  .doc_dict` records why the hash exists). The token is not consumed by that
  refusal: nothing was served.

## Naming each document with its hash

Every document in an issue or redeem response carries `body_sha256`, the
Reference Room index's own `body_hash` (`sha256(body.encode("utf-8"))`,
verified against the live index by `reference.doc_dict`). An individuator that
compares it to the hash of the copy already on disk detects a stale one.

## Why this is its own table and not `call_slips`

`call_slips.py` is this repository's existing single-use, short-lived
credential. Its shape is reused: expiry is a STORED ABSOLUTE INSTANT set once
at issue and never extended, and single use is a conditional `UPDATE` whose
`rowcount` decides the winner rather than a read followed by a write. What is
not reused is the table, for three reasons.

1. **A slip is bound to an item and a bundle token is not always.** `call_slips
   .item_id` is `NOT NULL` with a foreign key to `issue_items`, because a slip
   is minted inside a checkout. A repository-scoped bundle has no item, so
   sharing the table means loosening a column whose guarantee `library_cards
   .issue` reads.
2. **The two credentials exchange for different things.** Redeeming a slip
   yields an IDENTITY, a library card bound to an actor, and that is why
   `POST /tokens/redeem` runs ADR-0062's transport actor cross-check.
   Redeeming a bundle token yields CONTENT and grants nothing: the Reference
   Room's documents are readable by every `reference.read` holder already. So
   this route carries no identity claim to cross-check, and it does not run
   `credential_binding.redemption_binding_holds`, which gates every
   `POST /tokens/redeem`.
3. **Separate ids make cross-use structurally impossible.** A `slip_` id looked
   up in `bundle_tokens` finds nothing, and a `bundle_` id looked up in
   `call_slips` likewise. One table with a `kind` column would make "which route
   may redeem this row" a check somebody must remember.

## Why `TTL_SECONDS` is its own literal

The window a bundle token must survive is issue at dispatch to redeem by the
individuator, the same shape of gap `call_slips.py` measured with dispatch
latency (109 matched dispatches, worst 19s, p99 6s, on 2026-09-03), and 300
seconds is that measurement's headroom. **It is a literal here and not
`call_slips.TTL_SECONDS`** because the two constants answer different
questions. A slip grants an identity, so shortening its lifetime is a plausible
security change, and a bundle token grants only content every `reference.read`
holder already has. Sharing the value would let a change made for one reason
move the other without either owner seeing it. This number moves on evidence
about the individuator's redemption latency, which nothing measures yet.

## Why retirement is refused at redemption and not at issue

A profile's rules are PREDICATES, and a rule naming a document id **cannot be
stored**: `crew_registry._INCLUDE_KEYS` is `{kind, project, status,
audience_contains}` and the write path rejects any other key, so the constraint
is enforced rather than observed. (The live `profiles` table held 169 rules on
2026-09-18, all four-key predicates — corroboration, not the reason. A count
holds for a day; the validator holds.)

So no profile can name a document that is later retired, and a rule with
`status: current` that stops selecting a retired document is the selector doing
its job rather than a silent drop. The refusal that matters is at redemption,
for a document retired between issue and redeem, which `read_named` implements.
Retirement is also refused AT ISSUE where it empties an audience filter, which
is the `agent whose only definition is retired` case.

The incident behind the epic (`start-overnight` individuated in 8 workspaces
after it was retired) is a stale COPY on disk, which the `body_sha256` each
document carries lets the individuator detect.

**THIS REASONING DEPENDS ON `_INCLUDE_KEYS` AND FAILS WITH IT.** If a rule could
ever name an id — which would take changing both `_INCLUDE_KEYS` and
`crew_profiles._rule_clause` — a retired named document would drop silently at
issue, because the default status filter excludes it and the token would never
have named it, so `read_named` could not catch it either. That is exactly the
omitted-rule case this module's opening refuses, so whoever widens those keys
owes a refusal here. Nothing is built for it now: it cannot occur today.
"""
from __future__ import annotations

import datetime as _dt
import json
import sqlite3

import call_slips
import crew_profiles
import reference
from store import new_ulid

# The kinds a bundle carries, in the order they are reported. `agent` first, so
# a refusal about the definition is read before one about the skills.
BUNDLE_KINDS = ("agent", "skill")

# The `agent`-kind document's own frontmatter key naming the kinds it takes
# none of by design (`platform/agents/scout`: `takes_none_of: [skill]`). A
# list of kinds rather than a skills-only flag, because the same "this agent
# is never given documents of kind X" shape applies to any future kind in
# `BUNDLE_KINDS`, and a second flag would have to be invented for each one.
FIELD_TAKES_NONE_OF = "takes_none_of"

DEFAULT_VARIANT = "daytime"

# See the module docstring: issue at dispatch to redeem by the individuator.
TTL_SECONDS = 300

TOKEN_PREFIX = "bundle_"

SCOPE_ITEM = "item"
SCOPE_REPO = "repo"


class BundleRefused(Exception):
    """A bundle that cannot be assembled or served, and why.

    Carries the HTTP status and the fields the route puts in the refusal body.
    `documents` is always the list of documents the refusal is about, each
    `{"id", "reason", ...}`, so a caller branches on data rather than parsing
    `error`.
    """

    def __init__(self, status: int, error: str, *, hint: str | None = None,
                 **fields):
        super().__init__(error)
        self.status = status
        self.error = error
        self.hint = hint
        self.fields = fields


class MintFailed(RuntimeError):
    """The insert itself failed; nothing was committed, so retrying is safe.
    Matches `call_slips.MintFailed`'s reasoning."""


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)


def _audience(row) -> list:
    """A row's `audience` as a list of strings, or `[]` when it is anything else.

    The Python-side twin of `reference.audience_clause`'s guard: that one is SQL
    and this one runs over rows already fetched, but both must agree that only
    a JSON array of values can match. The index is written by the `knowledge`
    repository, so a row can hold text that is not JSON at all.
    """
    try:
        parsed = json.loads(row["audience"])
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [a for a in parsed if isinstance(a, str)]


def _rules_of_kind(profile: dict, kind: str) -> list[dict]:
    return [r for r in profile["include"] if r.get("kind") == kind]


def _raw_takes_none_of(row) -> object:
    """The `agent` row's `takes_none_of` value, exactly as `frontmatter_extra`
    holds it, or `None` when there is nothing usable to read.

    `None` on the same causes `reference._frontmatter_extra_dict` collapses to
    one value for: the column is absent (an index predating
    agent-platform#517), the value is NULL or empty, the text is not valid
    JSON, or it parses to something other than an object. This module does
    not reuse that function across the module boundary (nothing in this
    repository does), so it is reimplemented here the way `_audience` above
    reimplements `reference.audience_clause`'s guard for one already-fetched
    row.

    Returned RAW and unvalidated — a caller checks the shape and refuses on a
    bad one rather than this function guessing or silently ignoring it.
    """
    try:
        keys = row.keys()
    except AttributeError:
        return None
    if "frontmatter_extra" not in keys:
        return None
    raw = row["frontmatter_extra"]
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed.get(FIELD_TAKES_NONE_OF)


def _declared_kinds(agent_row) -> frozenset[str]:
    """The kinds `agent_row` declares it takes none of, validated.

    A malformed declaration — anything other than a list of strings, or a
    string that names no kind in `BUNDLE_KINDS` — refuses naming the
    definition and the bad value, rather than being read as "takes nothing"
    (which would silently exempt every kind) or being ignored (which would
    silently exempt none while looking like a no-op to whoever wrote it).
    """
    raw = _raw_takes_none_of(agent_row)
    if raw is None:
        return frozenset()
    if not isinstance(raw, list) or not all(isinstance(k, str) for k in raw):
        raise BundleRefused(
            409,
            f"{agent_row['id']!r}'s {FIELD_TAKES_NONE_OF!r} must be a list "
            f"of document kinds, got {raw!r}",
            hint=f"fix the {FIELD_TAKES_NONE_OF!r} frontmatter field on "
                 f"{agent_row['id']}",
            id=agent_row["id"], field=FIELD_TAKES_NONE_OF, value=raw)
    unknown = [k for k in raw if k not in BUNDLE_KINDS]
    if unknown:
        raise BundleRefused(
            409,
            f"{agent_row['id']!r}'s {FIELD_TAKES_NONE_OF!r} names unknown "
            f"kind(s) {unknown!r}: a bundle only ever carries "
            f"{list(BUNDLE_KINDS)!r}",
            hint=f"fix the {FIELD_TAKES_NONE_OF!r} frontmatter field on "
                 f"{agent_row['id']}",
            id=agent_row["id"], field=FIELD_TAKES_NONE_OF, value=raw)
    return frozenset(raw)


# How many documents a refusal will name outside the profile, and how many
# distinct audience values it will list. The bound is the refusal's own size:
# the live corpus holds 31 skills, so neither is reached today, and a wrong
# audience on a large corpus should not turn one 409 into a corpus dump.
MAX_NAMED = 20
MAX_AUDIENCES = 100


def _listing(ids: list[str]) -> str:
    shown = ", ".join(ids[:10])
    return shown + (f" and {len(ids) - 10} more" if len(ids) > 10 else "")


def _empty_filter(kind: str, agent: str, profile: dict, candidates: list,
                  index_conn: sqlite3.Connection) -> BundleRefused:
    """The refusal for an audience filter that matched nothing.

    **It says how many documents the filter saw and which audience values they
    do carry**, because those two facts tell a vocabulary error (31 skills, none
    naming `platform-coder`, all naming `coder`) from an empty profile (0 of 0),
    and the first is corrected in the Reference Room while the second is a
    profile edit. It also names, from the whole index and ignoring the profile,
    any document of this kind that DOES list the agent: a retired definition
    lands there, and "the agent's only definition is deprecated" is the
    sentence that gets it un-retired or the roster corrected.
    """
    present = sorted({a for row in candidates for a in _audience(row)})
    outside = [
        {"id": row["id"], "reason": "not-selected", "status": row["status"],
         "project": row["project"]}
        for row in index_conn.execute(
            f"SELECT id, status, project FROM docs WHERE kind = ? AND "
            f"{reference.audience_clause(1)} ORDER BY id LIMIT ?",
            (kind, agent, MAX_NAMED))]
    where = f"{profile['project']}/{profile['variant']}"
    error = (f"the {kind} filter for {agent!r} matches nothing: 0 of "
             f"{len(candidates)} {kind} documents {where} selects list "
             f"{agent!r} in their audience")
    if outside:
        error += (", and the profile does not select "
                  + _listing([f"{d['id']} ({d['status']})" for d in outside])
                  + ", which " + ("lists" if len(outside) == 1 else "list")
                  + " it")
    return BundleRefused(
        409, error,
        hint="an empty bundle would ship an under-equipped agent, so none is "
             "issued. The audience field of the documents, or the profile's "
             "rules, is what to correct",
        kind=kind, audience=agent, matched=0, candidates=len(candidates),
        audiences_present=present[:MAX_AUDIENCES], documents=outside)


def resolve_bundle(profile: dict, agent: str,
                   index_conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """The documents `agent` is individuated with under `profile`, or a
    `BundleRefused` naming why it cannot be.

    `profile` is `crew_profiles.profile_row`'s shape. Sorted by id, so two
    issues against the same index answer in the same order.

    **The profile's own rules select, and this only narrows.** Each rule is
    evaluated by `crew_profiles.resolve`, the evaluator `GET /profiles` serves
    and `knowledge-ctx` runs, so a bundle cannot select something the profile
    would not. A rule's own `status` is respected: `status: current` is a
    profile author excluding non-current documents on purpose, and turning
    that into a refusal would make every retirement an outage. Only a document
    the profile DOES select and the bundle cannot serve, which happens when a
    rule names no `status` and so also selects `draft`, is a conflict between
    the profile and the bundle's `current`-only rule, and that is refused.

    **An empty match for a kind the agent's own `current` definition declares
    `takes_none_of` is not a refusal.** See the module docstring. The
    declaration is read only from a matched, `current` row of kind `agent`
    for this agent, never from a document the profile did not select or that
    is not `current`, and it can never exempt the `agent` kind itself.
    """
    per_kind: dict[str, tuple[list, list]] = {}
    not_current: list[sqlite3.Row] = []
    for kind in BUNDLE_KINDS:
        rules = _rules_of_kind(profile, kind)
        if not rules:
            continue
        candidates = crew_profiles.resolve({"include": rules}, index_conn)
        matched = [r for r in candidates if agent in _audience(r)]
        per_kind[kind] = (candidates, matched)
        not_current.extend(r for r in matched if r["status"] != "current")

    declared: frozenset[str] = frozenset()
    for row in per_kind.get("agent", ([], []))[1]:
        if row["status"] == "current":
            declared |= _declared_kinds(row)

    if not_current:
        listed = [f"{r['id']} ({r['status']})" for r in not_current]
        raise BundleRefused(
            409,
            f"{profile['project']}/{profile['variant']} selects "
            f"{len(not_current)} document(s) for {agent!r} that are not "
            f"`current`: {_listing(listed)}",
            hint="a bundle serves only `current` documents and never drops "
                 "one silently. Publish the document, or narrow the "
                 "profile's rule with `status: current`",
            agent=agent,
            documents=[{"id": r["id"], "reason": "not-current",
                        "status": r["status"], "project": r["project"]}
                       for r in not_current])

    selected: list[sqlite3.Row] = []
    for kind, (candidates, matched) in per_kind.items():
        if not matched:
            if kind != "agent" and kind in declared:
                # Exempted: this kind contributes zero documents rather than
                # refusing the whole bundle.
                continue
            raise _empty_filter(kind, agent, profile, candidates, index_conn)
        selected.extend(matched)
    if "agent" not in per_kind:
        # A profile with no `agent` rule names no definition at all, and a
        # bundle without one is not the agent's.
        raise _empty_filter("agent", agent, profile, [], index_conn)
    return sorted(selected, key=lambda r: r["id"])


def named_documents(rows) -> list[dict]:
    """The `(id, body_sha256)` pairs a token names, in row order."""
    return [{"id": r["id"], "body_sha256": r["body_hash"]} for r in rows]


def mint(conn, *, agent: str, project: str, variant: str, scope_kind: str,
         item_id: str | None, repo: str | None, product: str | None,
         documents: list[dict], issued_by: str, now=None) -> tuple[str, str]:
    """Insert a bundle token and return `(token_id, expires_at)`.

    `expires_at` is computed and written here, once, and no function in this
    module extends it (`call_slips.mint`'s reasoning).
    """
    now = now or _now()
    token_id = f"{TOKEN_PREFIX}{new_ulid()}"
    expires_at = (now + _dt.timedelta(seconds=TTL_SECONDS)).isoformat(" ")
    try:
        conn.execute(
            "INSERT INTO bundle_tokens (id, agent, project, variant, "
            "scope_kind, item_id, repo, product, documents, issued_by, "
            "issued_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (token_id, agent, project, variant, scope_kind, item_id, repo,
             product, json.dumps(documents), issued_by, now.isoformat(" "),
             expires_at))
    except sqlite3.Error as e:
        raise MintFailed(str(e)) from e
    return token_id, expires_at


def get(conn, token_id: str):
    """The raw row for `token_id`, or `None`."""
    return conn.execute("SELECT * FROM bundle_tokens WHERE id = ?",
                        (token_id,)).fetchone()


def consume(conn, token_id: str, *, redeemed_by: str, now=None) -> bool:
    """Atomically mark `token_id` redeemed. True when THIS call did it.

    **A CONDITIONAL UPDATE, NOT READ-THEN-WRITE**, for `call_slips.redeem`'s
    reason: two concurrent redemptions must serve exactly one bundle, and the
    `WHERE redeemed_at IS NULL` is what lets only one of them change a row.
    """
    now = now or _now()
    cur = conn.execute(
        "UPDATE bundle_tokens SET redeemed_at = ?, redeemed_by = ? "
        "WHERE id = ? AND redeemed_at IS NULL",
        (now.isoformat(" "), redeemed_by, token_id))
    return cur.rowcount == 1


def is_expired(row, *, now=None) -> bool:
    return not call_slips.is_redeemable(row, now=now)


def read_named(index_conn: sqlite3.Connection, named: list[dict]
               ) -> list[sqlite3.Row]:
    """The index rows for a token's named documents, in the order named, or a
    `BundleRefused` listing every one that no longer matches what was named.

    A document is refused when it is missing, when its `status` is no longer
    `current`, or when its `body_hash` is not the hash the token named. The
    reasons are `missing`, `not-current` and `changed`, and a document is
    reported under the first that applies. **All of them are collected before
    raising**, so a caller learns everything to fix in one round trip.
    """
    rows: list[sqlite3.Row] = []
    problems: list[dict] = []
    for entry in named:
        row = index_conn.execute("SELECT * FROM docs WHERE id = ?",
                                 (entry["id"],)).fetchone()
        base = {"id": entry["id"], "named_body_sha256": entry["body_sha256"]}
        if row is None:
            problems.append({**base, "reason": "missing"})
        elif row["status"] != "current":
            problems.append({**base, "reason": "not-current",
                             "status": row["status"]})
        elif row["body_hash"] != entry["body_sha256"]:
            problems.append({**base, "reason": "changed",
                             "body_sha256": row["body_hash"]})
        else:
            rows.append(row)
    if problems:
        listed = [f"{p['id']} ({p['reason']})" for p in problems]
        raise BundleRefused(
            409,
            f"{len(problems)} document(s) this token names no longer match "
            f"the Reference Room: {_listing(listed)}",
            hint="nothing was served and the token was not consumed. It will "
                 "keep refusing until the Reference Room agrees with it "
                 "again or it expires; ask POST /bundles for a new one",
            documents=problems)
    return rows
