"""The write path for `roster_agents` and `profiles` (issue_01M19QP8S0QPKM0MN7XHYWNGKY).

Where the roster and the per-project profiles used to live: `ALLOWED_AUDIENCE`,
a Python set literal in agent-platform's `lib/knowledge/knowledge.py`, read
once at import by a long-lived watcher; and `projects/<product>/profiles/
*.yaml` in the knowledge repo, read off local disk by the Bosun. Neither
takes effect without a restart or a sync. These two tables replace both,
read fresh per request the same way `identity._tokens()` and
`machine_identity._registry_status` already are — the shape the operator's 2026-09-04
ruling requires: "the roster for a crew should be malleable and not need a
pull request."

**Agent DEFINITIONS stay canon and pull-request-governed.** This module
never accepts a `model`, `tools`, `allowed_tools`, `disallowed_tools` or
`gateway` field on an include block — `_validate_include_block` refuses one
outright — because those live in the Reference Room definition ARCH-134's
model-pin enforcement already protects. What lives here is CREW COMPOSITION
(the roster) and PROFILE SELECTION RULES (which documents and which roster
names a profile pulls in), never a definition.

**The capability check lives in this module, not in a route — a deliberate
departure from `machine_registry.py`'s shape**, which is otherwise the model
this follows (bounds enforced once, so a second caller cannot skip them by
not knowing they exist). `machine_registry.py` leaves capability-checking to
its callers because both of its callers — the HTTP route and
`enroll_machine.py` — already exist and already check `machine.write` or run
as a trusted local operator. Neither existed yet for `roster.write` when this module was written; the read HTTP
surface exists as `crew_profiles.py` and its three `GET` routes in
`app.py` (issue_01M19QPV0J72EK1NDB0H8KT5EM), gated on its own `roster.read`
capability rather than this module's `roster.write`. The roster half of the
write surface — `PUT`/`DELETE /roster/{name}` — now exists too
(issue_01M34STNFA2CSD78PW5Y9XHX1M); the profile half is still later work.
Shipping this write path with no enforcement point at all would have left
"an agent must not be able to change which agents exist while it runs" as
a capability declared in `identity.py` and checked nowhere — worse than
not building it, since it would read as a live guarantee. So every
mutating function here takes the caller's `identity.Identity` and refuses
one that does not hold `roster.write` before touching the database; the
route sits in front of this and adds nothing to that check, it only
supplies the `Identity`.
"""
from __future__ import annotations

import datetime as _dt
import json

import identity
import store


class InvalidRosterRow(ValueError):
    """A caller-supplied field is malformed, or a profile's `include` block
    carries a field that belongs to an agent's definition rather than to a
    selection rule (ARCH-134)."""


class RosterWriteForbidden(PermissionError):
    """`ident` does not hold `roster.write`. Distinct from
    `InvalidRosterRow` (a 400/422-shaped problem) and from
    `store.WritesDisabled` (the shadow gate) so a caller can tell "you may
    never do this" from "this request was malformed" from "nobody may write
    right now"."""


# Matches `store.MAX_IDENT` — every string this module stores is
# identifier-shaped (an agent name, a project slug, a profile variant, a
# `kind`/`status`/`audience_contains` value), never free text.
MAX_IDENT = store.MAX_IDENT

# **Bounded independently of `MAX_IDENT`, the same reasoning
# `machine_registry.MAX_NOTE` gives**: `store.connect_rw()` serialises every
# write in the Library through one SQLite writer on a 5-second
# `busy_timeout`, so one oversized write stalls every other writer. Sized
# generously against what exists — measured 2026-09-06 across the 24
# current profile YAML files, the largest `include` serialises to 927 bytes
# and the largest `claude_md_extras` to 3463, so both bounds below carry
# roughly 15-20x headroom rather than being sized to exactly what is there
# today.
MAX_INCLUDE_JSON = 20_000
MAX_EXTRAS_JSON = 50_000
MAX_EXTRA_BLOCK = 20_000

_INCLUDE_KEYS = frozenset({"kind", "project", "status", "audience_contains"})

# **NAMED EXPLICITLY RATHER THAN LEFT TO THE ALLOW-LIST ALONE**, so a reader
# of a rejection sees exactly which definition-shaped fields this write path
# exists to keep off a profile, not just that some unrecognised key was
# present.
_DEFINITION_ONLY_FIELDS = frozenset(
    {"model", "tools", "allowed_tools", "disallowed_tools", "gateway"})


def _require_write(ident: identity.Identity) -> None:
    """The capability check, plus the one bound on `ident.actor` — placed
    here rather than in each mutating function, so that a future mutating
    function cannot skip it by not knowing it exists (the same reasoning
    `machine_registry._validate`'s docstring gives for putting every bound
    in one place). Every mutating function in this module calls
    `_require_write` first; nothing here reaches the database without
    going through it.

    `identity._safe_actor` proves `ident.actor` is a string but never
    bounds its length, and `roster_add`/`profile_put` write it straight
    into `created_by`/`updated_by` (`store.connect_rw()` serialises every
    write in the Library through one SQLite writer on a 5-second
    `busy_timeout`, so one oversized repeatable write stalls every other
    writer — the same reasoning this module's own docstring already gives
    for `MAX_INCLUDE_JSON`, `MAX_EXTRAS_JSON` and `MAX_EXTRA_BLOCK`).
    `store.MAX_ACTOR` is the bound, matching the `created_by`/`updated_by`
    column width the migration declares.
    """
    if not identity.authorized(ident, "roster.write"):
        raise RosterWriteForbidden(
            f"{ident.kind!r} identity lacks 'roster.write' — only an "
            f"orchestrator credential may change crew composition")
    if not isinstance(ident.actor, str) or len(ident.actor) > store.MAX_ACTOR:
        raise InvalidRosterRow(
            f"actor must be a string of at most {store.MAX_ACTOR} "
            f"characters")


def _validate_ident_str(value, field: str, max_len: int = MAX_IDENT) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > max_len:
        raise InvalidRosterRow(
            f"{field} must be a non-empty string of at most {max_len} "
            f"characters")


def _validate_str_list(value, field: str, max_len: int = MAX_IDENT) -> None:
    if not isinstance(value, list) or not value:
        raise InvalidRosterRow(f"{field} must be a non-empty list of strings")
    for i, item in enumerate(value):
        _validate_ident_str(item, f"{field}[{i}]", max_len)


def _validate_include_block(block, index: int) -> None:
    if not isinstance(block, dict):
        raise InvalidRosterRow(f"include[{index}] must be an object")
    # **CHECKED BEFORE THE UNKNOWN-KEY TEST**, so a caller sending `model` or
    # `gateway` sees the ARCH-134 reason rather than the generic "unrecognised
    # field" one — the same rejection, but naming why it exists.
    keys = set(block.keys())
    forbidden = keys & _DEFINITION_ONLY_FIELDS
    if forbidden:
        raise InvalidRosterRow(
            f"include[{index}] carries {sorted(forbidden)} — a profile "
            f"selects agents, it does not define them (ARCH-134)")
    unknown = keys - _INCLUDE_KEYS
    if unknown:
        raise InvalidRosterRow(
            f"include[{index}] has unrecognised field(s) {sorted(unknown)}; "
            f"only {sorted(_INCLUDE_KEYS)} are selection rules")
    _validate_ident_str(block.get("kind"), f"include[{index}].kind")
    if "project" in block:
        _validate_str_list(block["project"], f"include[{index}].project")
    if "status" in block:
        _validate_ident_str(block["status"], f"include[{index}].status")
    if "audience_contains" in block:
        _validate_str_list(block["audience_contains"],
                            f"include[{index}].audience_contains")


def validate_profile(project, variant, include, claude_md_extras) -> None:
    """Public since PR #131: `load_crew_registry.py --dry-run` skips
    `profile_put` and so skipped every shape check with it, making a clean dry
    run no evidence that a real run would get as far as writing. One name
    rather than a wrapper, so the dry run and the write cannot validate
    differently."""
    _validate_ident_str(project, "project")
    _validate_ident_str(variant, "variant")
    if not isinstance(include, list):
        raise InvalidRosterRow("include must be a list")
    for i, block in enumerate(include):
        _validate_include_block(block, i)
    if len(json.dumps(include)) > MAX_INCLUDE_JSON:
        raise InvalidRosterRow(
            f"include is larger than {MAX_INCLUDE_JSON} bytes serialised")
    if not isinstance(claude_md_extras, list):
        raise InvalidRosterRow("claude_md_extras must be a list")
    for i, block in enumerate(claude_md_extras):
        if not isinstance(block, str) or not block.strip():
            raise InvalidRosterRow(
                f"claude_md_extras[{i}] must be a non-empty string")
        if len(block) > MAX_EXTRA_BLOCK:
            raise InvalidRosterRow(
                f"claude_md_extras[{i}] is longer than {MAX_EXTRA_BLOCK} "
                f"characters")
    if len(json.dumps(claude_md_extras)) > MAX_EXTRAS_JSON:
        raise InvalidRosterRow(
            f"claude_md_extras is larger than {MAX_EXTRAS_JSON} bytes "
            f"serialised")


def _now_iso(now=None) -> str:
    now = now or _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    if isinstance(now, str):
        now = _dt.datetime.fromisoformat(now)
    if now.tzinfo is not None:
        now = now.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    return now.isoformat(" ")


def _profile_dict(row) -> dict:
    """A stored `profiles` row, with `include` and `claude_md_extras`
    decoded back from the JSON text the column holds — the same
    `_json_or`-style leniency `store.py` uses elsewhere, since a row this
    module itself wrote is trusted to decode."""
    d = dict(row)
    d["include"] = json.loads(d["include"])
    d["claude_md_extras"] = json.loads(d["claude_md_extras"])
    return d


# --- roster ---------------------------------------------------------------


def roster_add(ident: identity.Identity, name: str, *, now=None) -> dict:
    """Add `name` to the roster, or refresh its `updated_at`/`updated_by` if
    it is already on it. Returns the stored row.

    **An upsert, not an insert** — adding a name already present is not an
    error. A crew's roster is state to read and revise, the same shape
    `tool_policy`'s own docstring claims for itself, not an append-only log
    of events.
    """
    _require_write(ident)
    _validate_ident_str(name, "name")
    now_iso = _now_iso(now)
    conn = store.connect_rw()
    try:
        conn.execute(
            "INSERT INTO roster_agents "
            "(name, created_at, created_by, updated_at, updated_by) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (name) DO UPDATE SET "
            "  updated_at = excluded.updated_at, "
            "  updated_by = excluded.updated_by",
            (name, now_iso, ident.actor, now_iso, ident.actor))
        conn.commit()
        stored = conn.execute(
            "SELECT * FROM roster_agents WHERE name = ?", (name,)).fetchone()
        return dict(stored)
    finally:
        conn.close()


def roster_remove(ident: identity.Identity, name: str, *, now=None
                   ) -> dict | None:
    """Remove `name` from the roster. Returns the row as it stood, or
    `None` if the name was not on the roster.

    Unused `now`, kept for symmetry with `roster_add` and so a future
    soft-delete does not need to change the call signature every caller
    already uses.
    """
    del now
    _require_write(ident)
    _validate_ident_str(name, "name")
    conn = store.connect_rw()
    try:
        row = conn.execute(
            "SELECT * FROM roster_agents WHERE name = ?", (name,)).fetchone()
        if row is None:
            return None
        conn.execute("DELETE FROM roster_agents WHERE name = ?", (name,))
        conn.commit()
        return dict(row)
    finally:
        conn.close()


# --- profiles ---------------------------------------------------------------


def profile_put(ident: identity.Identity, project: str, variant: str,
                 include: list, claude_md_extras: list, *, now=None) -> dict:
    """Create or replace the profile named `<project>/<variant>`. Returns
    the stored row, with `include` and `claude_md_extras` decoded.

    **An upsert on `(project, variant)`, not an insert** — a profile that
    changes its selection rules overwrites the existing row rather than
    colliding against it, the same shape `credential_binding.bind` uses for
    a rebind.
    """
    _require_write(ident)
    validate_profile(project, variant, include, claude_md_extras)
    now_iso = _now_iso(now)
    include_json = json.dumps(include)
    extras_json = json.dumps(claude_md_extras)
    conn = store.connect_rw()
    try:
        conn.execute(
            "INSERT INTO profiles "
            "(project, variant, include, claude_md_extras, "
            " created_at, created_by, updated_at, updated_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (project, variant) DO UPDATE SET "
            "  include = excluded.include, "
            "  claude_md_extras = excluded.claude_md_extras, "
            "  updated_at = excluded.updated_at, "
            "  updated_by = excluded.updated_by",
            (project, variant, include_json, extras_json, now_iso,
             ident.actor, now_iso, ident.actor))
        conn.commit()
        stored = conn.execute(
            "SELECT * FROM profiles WHERE project = ? AND variant = ?",
            (project, variant)).fetchone()
        return _profile_dict(stored)
    finally:
        conn.close()


def profile_delete(ident: identity.Identity, project: str, variant: str, *,
                    now=None) -> dict | None:
    """Delete the profile named `<project>/<variant>`. Returns the row as
    it stood, with `include` and `claude_md_extras` decoded, or `None` if no
    such profile exists."""
    del now
    _require_write(ident)
    _validate_ident_str(project, "project")
    _validate_ident_str(variant, "variant")
    conn = store.connect_rw()
    try:
        row = conn.execute(
            "SELECT * FROM profiles WHERE project = ? AND variant = ?",
            (project, variant)).fetchone()
        if row is None:
            return None
        conn.execute(
            "DELETE FROM profiles WHERE project = ? AND variant = ?",
            (project, variant))
        conn.commit()
        return _profile_dict(row)
    finally:
        conn.close()
