"""`crew_registry.py`: the write path for `roster_agents` and `profiles`
(issue_01M19QP8S0QPKM0MN7XHYWNGKY).

**Do not mock the DB layer.** A real pair of tables, built from the
migration's own DDL, the same shape `test_machine_registry.py` already
uses for `machine_certificates`.

**The capability check is asserted here, at the module boundary, because
this module — unlike `machine_registry.py` — is where it actually lives**
(see `crew_registry.py`'s own docstring for why). A worker's write must be
refused and an orchestrator's must land, tested by presenting each kind of
`identity.Identity` rather than by reading `identity.ROLE_CAPS` — the same
instruction `test_orchestrator_role.py` follows for the capability delta
itself.
"""
from __future__ import annotations

import sqlite3

import pytest

import crew_registry
import identity
import store

WORKER = identity.Identity(actor="a-worker", kind="agent")
SYSTEM = identity.Identity(actor="capture", kind="system")
ORCHESTRATOR = identity.Identity(actor="co-library", kind="orchestrator")


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "control.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE roster_agents (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            created_by TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            updated_by TEXT NOT NULL
        );
        CREATE TABLE profiles (
            id INTEGER PRIMARY KEY,
            project TEXT NOT NULL,
            variant TEXT NOT NULL,
            include TEXT NOT NULL,
            claude_md_extras TEXT NOT NULL,
            created_at TEXT NOT NULL,
            created_by TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            updated_by TEXT NOT NULL,
            UNIQUE (project, variant)
        );
    """)
    conn.commit()
    conn.close()
    monkeypatch.setattr(store, "DB_PATH", path)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    return path


def _roster_rows(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in
                conn.execute("SELECT * FROM roster_agents ORDER BY id")]
    finally:
        conn.close()


def _profile_rows(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in
                conn.execute("SELECT * FROM profiles ORDER BY id")]
    finally:
        conn.close()


# --- the capability boundary ---------------------------------------------


def test_a_worker_is_refused_roster_add(db):
    with pytest.raises(crew_registry.RosterWriteForbidden):
        crew_registry.roster_add(WORKER, "coder")
    assert _roster_rows(db) == []


def test_a_system_credential_is_also_refused(db):
    """`roster.write` is on neither literal role, `agent` nor `system` —
    the whole point of the new kind rather than widening `system`, since
    the github-capture poller resolves to `kind: system` today."""
    with pytest.raises(crew_registry.RosterWriteForbidden):
        crew_registry.roster_add(SYSTEM, "coder")
    assert _roster_rows(db) == []


def test_an_orchestrator_credential_lands(db):
    row = crew_registry.roster_add(ORCHESTRATOR, "coder")
    assert row["name"] == "coder"
    assert _roster_rows(db) != []


def test_a_worker_is_refused_profile_put(db):
    with pytest.raises(crew_registry.RosterWriteForbidden):
        crew_registry.profile_put(WORKER, "hollis", "daytime", [], [])
    assert _profile_rows(db) == []


def test_an_orchestrator_can_write_a_profile(db):
    row = crew_registry.profile_put(
        ORCHESTRATOR, "hollis", "daytime",
        [{"kind": "agent", "audience_contains": ["hollis-coder"]}], [])
    assert row["project"] == "hollis" and row["variant"] == "daytime"


def test_a_worker_is_refused_roster_remove(db):
    crew_registry.roster_add(ORCHESTRATOR, "coder")
    with pytest.raises(crew_registry.RosterWriteForbidden):
        crew_registry.roster_remove(WORKER, "coder")
    assert _roster_rows(db)[0]["name"] == "coder"


def test_a_worker_is_refused_profile_delete(db):
    crew_registry.profile_put(ORCHESTRATOR, "hollis", "daytime", [], [])
    with pytest.raises(crew_registry.RosterWriteForbidden):
        crew_registry.profile_delete(WORKER, "hollis", "daytime")
    assert _profile_rows(db) != []


# --- roster: upsert semantics ----------------------------------------------


def test_adding_the_same_name_twice_does_not_duplicate_the_row(db):
    crew_registry.roster_add(ORCHESTRATOR, "coder")
    crew_registry.roster_add(ORCHESTRATOR, "coder")
    assert len(_roster_rows(db)) == 1


def test_re_adding_refreshes_updated_at_and_updated_by(db):
    crew_registry.roster_add(
        identity.Identity(actor="first", kind="orchestrator"), "coder",
        now="2026-01-01T00:00:00")
    row = crew_registry.roster_add(
        identity.Identity(actor="second", kind="orchestrator"), "coder",
        now="2026-02-01T00:00:00")
    assert row["updated_by"] == "second"
    assert row["created_by"] == "first"
    assert row["updated_at"] == "2026-02-01 00:00:00"
    assert row["created_at"] == "2026-01-01 00:00:00"


def test_removing_a_name_not_on_the_roster_returns_none(db):
    assert crew_registry.roster_remove(ORCHESTRATOR, "nobody") is None


def test_removing_deletes_the_row(db):
    crew_registry.roster_add(ORCHESTRATOR, "coder")
    row = crew_registry.roster_remove(ORCHESTRATOR, "coder")
    assert row["name"] == "coder"
    assert _roster_rows(db) == []


def test_removing_only_touches_the_named_agent(db):
    crew_registry.roster_add(ORCHESTRATOR, "coder")
    crew_registry.roster_add(ORCHESTRATOR, "reviewer")
    crew_registry.roster_remove(ORCHESTRATOR, "coder")
    remaining = {r["name"] for r in _roster_rows(db)}
    assert remaining == {"reviewer"}


def test_a_removed_name_can_be_re_added(db):
    crew_registry.roster_add(ORCHESTRATOR, "coder")
    crew_registry.roster_remove(ORCHESTRATOR, "coder")
    crew_registry.roster_add(ORCHESTRATOR, "coder")
    assert len(_roster_rows(db)) == 1


# --- roster: validation ------------------------------------------------------


@pytest.mark.parametrize("bad_name", [
    "", "   ", None, 5, ["coder"], {"name": "coder"},
    "x" * (store.MAX_IDENT + 1),
])
def test_an_invalid_name_is_refused(db, bad_name):
    with pytest.raises(crew_registry.InvalidRosterRow, match="name"):
        crew_registry.roster_add(ORCHESTRATOR, bad_name)
    assert _roster_rows(db) == []


def test_validation_runs_before_the_capability_bypass_would_matter(db):
    """A worker sees `RosterWriteForbidden` even for a malformed name — the
    capability check runs first, so a caller without `roster.write` learns
    nothing about whether the data it sent would otherwise have been
    accepted."""
    with pytest.raises(crew_registry.RosterWriteForbidden):
        crew_registry.roster_add(WORKER, "")


# --- actor bound ------------------------------------------------------------
#
# `ident.actor` reaches `created_by`/`updated_by` on every mutating function
# here, and `identity._safe_actor` proves it is a string without ever
# bounding its length. Every mutating function is covered, matching the
# oversized-name / oversized-project-variant / oversized-extras-block tests
# above rather than exercising only one caller.


def _oversized_actor(kind="orchestrator"):
    return identity.Identity(actor="a" * (store.MAX_ACTOR + 1), kind=kind)


def test_roster_add_refuses_an_oversized_actor(db):
    with pytest.raises(crew_registry.InvalidRosterRow, match="actor"):
        crew_registry.roster_add(_oversized_actor(), "coder")
    assert _roster_rows(db) == []


def test_roster_remove_refuses_an_oversized_actor(db):
    crew_registry.roster_add(ORCHESTRATOR, "coder")
    with pytest.raises(crew_registry.InvalidRosterRow, match="actor"):
        crew_registry.roster_remove(_oversized_actor(), "coder")
    assert _roster_rows(db)[0]["name"] == "coder"


def test_profile_put_refuses_an_oversized_actor(db):
    with pytest.raises(crew_registry.InvalidRosterRow, match="actor"):
        crew_registry.profile_put(
            _oversized_actor(), "hollis", "daytime", [], [])
    assert _profile_rows(db) == []


def test_profile_delete_refuses_an_oversized_actor(db):
    crew_registry.profile_put(ORCHESTRATOR, "hollis", "daytime", [], [])
    with pytest.raises(crew_registry.InvalidRosterRow, match="actor"):
        crew_registry.profile_delete(_oversized_actor(), "hollis", "daytime")
    assert _profile_rows(db) != []


def test_the_actor_bound_is_checked_after_the_capability_check(db):
    """A worker with both an oversized actor and no `roster.write` sees
    `RosterWriteForbidden`, not `InvalidRosterRow` — a caller who may never
    write learns that, not whether its malformed actor would otherwise have
    been accepted, matching the ordering the name-validation test above
    asserts for the capability check against a malformed `name`."""
    with pytest.raises(crew_registry.RosterWriteForbidden):
        crew_registry.roster_add(_oversized_actor(kind="agent"), "coder")


# --- profiles: upsert semantics ---------------------------------------------


def test_putting_the_same_profile_twice_does_not_duplicate_the_row(db):
    crew_registry.profile_put(ORCHESTRATOR, "hollis", "daytime", [], [])
    crew_registry.profile_put(ORCHESTRATOR, "hollis", "daytime", [], [])
    assert len(_profile_rows(db)) == 1


def test_putting_again_replaces_the_selection_rules(db):
    crew_registry.profile_put(
        ORCHESTRATOR, "hollis", "daytime",
        [{"kind": "agent", "audience_contains": ["hollis-coder"]}], ["a"])
    row = crew_registry.profile_put(
        ORCHESTRATOR, "hollis", "daytime",
        [{"kind": "agent", "audience_contains": ["hollis-reviewer"]}], ["b"])
    assert row["include"] == [
        {"kind": "agent", "audience_contains": ["hollis-reviewer"]}]
    assert row["claude_md_extras"] == ["b"]


def test_different_variants_of_the_same_project_are_distinct_rows(db):
    crew_registry.profile_put(ORCHESTRATOR, "hollis", "daytime", [], [])
    crew_registry.profile_put(ORCHESTRATOR, "hollis", "overnight", [], [])
    assert len(_profile_rows(db)) == 2


def test_include_and_claude_md_extras_round_trip_through_json(db):
    include = [{"kind": "skill", "project": ["hollis", "platform"],
               "status": "current"}]
    extras = ["one block", "another block"]
    row = crew_registry.profile_put(
        ORCHESTRATOR, "hollis", "daytime", include, extras)
    assert row["include"] == include
    assert row["claude_md_extras"] == extras


def test_deleting_a_profile_that_does_not_exist_returns_none(db):
    assert crew_registry.profile_delete(ORCHESTRATOR, "hollis", "daytime") \
        is None


def test_deleting_removes_the_row_and_returns_it_decoded(db):
    crew_registry.profile_put(
        ORCHESTRATOR, "hollis", "daytime",
        [{"kind": "agent", "audience_contains": ["hollis-coder"]}], ["x"])
    row = crew_registry.profile_delete(ORCHESTRATOR, "hollis", "daytime")
    assert row["include"] == [
        {"kind": "agent", "audience_contains": ["hollis-coder"]}]
    assert row["claude_md_extras"] == ["x"]
    assert _profile_rows(db) == []


# --- profiles: validation ----------------------------------------------------


def test_include_must_be_a_list(db):
    with pytest.raises(crew_registry.InvalidRosterRow, match="include"):
        crew_registry.profile_put(
            ORCHESTRATOR, "hollis", "daytime", "not-a-list", [])
    assert _profile_rows(db) == []


def test_an_include_block_must_be_an_object(db):
    with pytest.raises(crew_registry.InvalidRosterRow, match=r"include\[0\]"):
        crew_registry.profile_put(
            ORCHESTRATOR, "hollis", "daytime", ["not-a-dict"], [])


def test_an_include_block_needs_a_kind(db):
    with pytest.raises(crew_registry.InvalidRosterRow, match="kind"):
        crew_registry.profile_put(
            ORCHESTRATOR, "hollis", "daytime", [{"project": ["hollis"]}], [])


@pytest.mark.parametrize("field", ["model", "tools", "allowed_tools",
                                    "disallowed_tools", "gateway"])
def test_an_include_block_may_not_carry_a_definition_field(db, field):
    """ARCH-134: a model pin (or a tool grant) lives in the Reference Room
    definition, never in a profile's selection rule. This is the guard
    that keeps this write path from becoming a side door around it."""
    with pytest.raises(crew_registry.InvalidRosterRow, match="ARCH-134"):
        crew_registry.profile_put(
            ORCHESTRATOR, "hollis", "daytime",
            [{"kind": "agent", field: "whatever"}], [])
    assert _profile_rows(db) == []


def test_an_include_block_rejects_an_unrecognised_field(db):
    with pytest.raises(crew_registry.InvalidRosterRow,
                        match="unrecognised"):
        crew_registry.profile_put(
            ORCHESTRATOR, "hollis", "daytime",
            [{"kind": "agent", "made_up_field": "x"}], [])


def test_claude_md_extras_must_be_a_list(db):
    with pytest.raises(crew_registry.InvalidRosterRow,
                        match="claude_md_extras"):
        crew_registry.profile_put(
            ORCHESTRATOR, "hollis", "daytime", [], "not-a-list")


def test_a_claude_md_extras_block_must_be_a_non_empty_string(db):
    with pytest.raises(crew_registry.InvalidRosterRow,
                        match=r"claude_md_extras\[0\]"):
        crew_registry.profile_put(ORCHESTRATOR, "hollis", "daytime", [], [""])


def test_an_oversized_claude_md_extras_block_is_refused(db):
    with pytest.raises(crew_registry.InvalidRosterRow,
                        match=r"claude_md_extras\[0\]"):
        crew_registry.profile_put(
            ORCHESTRATOR, "hollis", "daytime", [],
            ["x" * (crew_registry.MAX_EXTRA_BLOCK + 1)])
    assert _profile_rows(db) == []


def test_an_oversized_project_or_variant_is_refused(db):
    with pytest.raises(crew_registry.InvalidRosterRow, match="project"):
        crew_registry.profile_put(
            ORCHESTRATOR, "p" * (store.MAX_IDENT + 1), "daytime", [], [])
    with pytest.raises(crew_registry.InvalidRosterRow, match="variant"):
        crew_registry.profile_put(
            ORCHESTRATOR, "hollis", "v" * (store.MAX_IDENT + 1), [], [])


def test_invalid_input_does_not_touch_an_existing_profile(db):
    crew_registry.profile_put(
        ORCHESTRATOR, "hollis", "daytime",
        [{"kind": "agent", "audience_contains": ["hollis-coder"]}], [])
    with pytest.raises(crew_registry.InvalidRosterRow):
        crew_registry.profile_put(
            ORCHESTRATOR, "hollis", "daytime", "not-a-list", [])
    row = _profile_rows(db)[0]
    assert "hollis-coder" in row["include"]


# --- the shadow gate ---------------------------------------------------------


def test_roster_add_refuses_while_writes_are_disabled(db, monkeypatch):
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    with pytest.raises(store.WritesDisabled):
        crew_registry.roster_add(ORCHESTRATOR, "coder")


def test_profile_put_refuses_while_writes_are_disabled(db, monkeypatch):
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    with pytest.raises(store.WritesDisabled):
        crew_registry.profile_put(ORCHESTRATOR, "hollis", "daytime", [], [])
