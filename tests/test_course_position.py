"""A Course's `position` — which of its own entries are reached, in flight,
and remaining (issue_01M12NE0TT1HA3WXGW4EATE8YT clauses 4, 5, 7).

Built on a database from the real migration, `test_ledger_routes.py`'s
reason: `issue_leases.root_id` carries a real foreign key against
`issue_items`, and a hand-written fixture schema is a second copy that
drifts.

**REACHED looks only at the entry's own state**, never at children — an
epic with zero children can be complete, and nothing here walks the tree to
check.

**IN FLIGHT has two distinct sources, and the second is the one worth
guarding.** An entry whose own state has left `open` (claimed, pr-open) is
the obvious case. The other is the epic that is itself still `open` while a
lease sits on it anyway — the ordinary shape of a checked-out CHILD, whose
lease roots on its parent epic rather than on itself (`lease.root_id_for`).
`lease.claim_statuses` cannot see this: it only ever answers for a row
already in state `claimed`, and an open epic under its child's lease never
is one. `lease.live_lease_rows` is what closes that gap, and the tests
below are built around proving it does.

**Nothing in the response ranks what can start next.** One test pins the
exact key set `position` carries, so a `next`/`current`/`up_next` field
reintroduced later fails loudly rather than shipping unnoticed.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as libapp
import identity
import store

REPO = Path(__file__).resolve().parent.parent
AGENT = "agent-token-wide"
H = {"Library-Protocol": "1"}


def _h(token=AGENT):
    return {**H, "Authorization": f"Bearer {token}"}


def _ts(**delta):
    return (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
            - dt.timedelta(**delta)).isoformat(" ")


def _insert_item(db_path, **fields):
    defaults = {
        "body": "b", "refs": "{}", "severity": "medium",
        "created_by": "seed", "created_at": _ts(days=1), "needs": "agent",
        "created_by_kind": "agent",
    }
    defaults.update(fields)
    conn = sqlite3.connect(db_path)
    try:
        cols = ", ".join(defaults)
        marks = ", ".join("?" * len(defaults))
        conn.execute(f"INSERT INTO issue_items ({cols}) VALUES ({marks})",
                    list(defaults.values()))
        conn.commit()
    finally:
        conn.close()


def _insert_lease(db_path, *, id, root_id, taken_for, holder="someone",
                  taken_at=None, heartbeat_at=None):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO issue_leases (id, root_id, taken_for, holder, "
            "taken_at, heartbeat_at) VALUES (?,?,?,?,?,?)",
            (id, root_id, taken_for, holder, taken_at or _ts(minutes=5),
             heartbeat_at or _ts(minutes=1)))
        conn.commit()
    finally:
        conn.close()


def _insert_course(db_path, *, id, product, entries, status=store.COURSE_LIVE,
                   title="the plan"):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO courses (id, product, title, entries, status, "
            "version, created_by, created_at) VALUES (?,?,?,?,?,1,?,?)",
            (id, product, title, json.dumps(entries), status, "seed",
             _ts(days=1)))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def db_path(tmp_path):
    db = tmp_path / "control.db"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO, capture_output=True, text=True, check=False,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LIBRARY_DB": str(db)})
    assert result.returncode == 0, result.stderr
    return db


@pytest.fixture
def client(db_path, tmp_path, monkeypatch):
    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        AGENT: {"name": "wide-agent", "products": ["*"]}}))
    monkeypatch.setattr(store, "DB_PATH", db_path)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    return TestClient(libapp.app)


# --- the five-state course -----------------------------------------------------


def test_a_course_with_every_state_reports_exact_counts_and_ids(client, db_path):
    _insert_item(db_path, id="e_reached", product="vivy", title="reached",
                type="epic", state="fixed")
    _insert_item(db_path, id="e_claimed", product="vivy", title="claimed",
                type="epic", state="claimed", claimed_by="someone",
                claimed_at=_ts(minutes=1))
    _insert_item(db_path, id="e_pr_open", product="vivy", title="pr-open",
                type="epic", state="pr-open")
    _insert_item(db_path, id="e_open", product="vivy", title="open",
                type="epic", state="open")
    entries = ["e_reached", "e_claimed", "e_pr_open", "e_open", "e_gone"]
    _insert_course(db_path, id="course1", product="vivy", entries=entries)

    r = client.get("/courses/course1", headers=_h())
    assert r.status_code == 200, r.text
    position = r.json()["position"]
    assert position["entries"] == 5
    assert position["reached"] == 1
    assert position["reached_ids"] == ["e_reached"]
    assert position["in_flight_ids"] == ["e_claimed", "e_pr_open"]
    assert position["remaining_ids"] == ["e_open"]
    assert position["missing"] == 1


# --- the lease-rooted-on-an-open-epic case --------------------------------------


def test_an_open_epic_under_a_live_lease_is_in_flight(client, db_path):
    _insert_item(db_path, id="e_covered", product="vivy", title="covered",
                type="epic", state="open")
    _insert_item(db_path, id="child", product="vivy", title="a child task",
                type="task", state="claimed", parent_id="e_covered",
                claimed_by="someone", claimed_at=_ts(minutes=1))
    # THE LEASE ROOTS ON THE EPIC, not the child — `lease.root_id_for`'s own
    # rule for a leaf with a parent. The epic's own row never turns
    # `claimed`; the lease is the only signal that it is spoken for.
    _insert_lease(db_path, id="lease1", root_id="e_covered", taken_for="child")
    _insert_item(db_path, id="e_bare", product="vivy", title="bare",
                type="epic", state="open")
    entries = ["e_covered", "e_bare"]
    _insert_course(db_path, id="course1", product="vivy", entries=entries)

    r = client.get("/courses/course1", headers=_h())
    assert r.status_code == 200, r.text
    position = r.json()["position"]
    assert position["in_flight_ids"] == ["e_covered"]
    assert position["remaining_ids"] == ["e_bare"]


# --- a childless terminal epic ---------------------------------------------------


def test_a_terminal_epic_with_zero_children_is_reached(client, db_path):
    _insert_item(db_path, id="e_solo", product="vivy", title="solo",
                type="epic", state="wontfix")
    _insert_course(db_path, id="course1", product="vivy", entries=["e_solo"])

    r = client.get("/courses/course1", headers=_h())
    assert r.status_code == 200, r.text
    position = r.json()["position"]
    assert position["reached_ids"] == ["e_solo"]
    assert position["entries"] == 1
    assert position["missing"] == 0


# --- more than one entry in flight at once --------------------------------------


def test_two_entries_in_flight_at_once_are_both_listed(client, db_path):
    _insert_item(db_path, id="e_claimed", product="vivy", title="claimed",
                type="epic", state="claimed", claimed_by="someone",
                claimed_at=_ts(minutes=1))
    _insert_item(db_path, id="e_leased", product="vivy", title="leased",
                type="epic", state="open")
    _insert_item(db_path, id="child", product="vivy", title="a child task",
                type="task", state="claimed", parent_id="e_leased",
                claimed_by="someone", claimed_at=_ts(minutes=1))
    _insert_lease(db_path, id="lease1", root_id="e_leased", taken_for="child")
    entries = ["e_claimed", "e_leased"]
    _insert_course(db_path, id="course1", product="vivy", entries=entries)

    r = client.get("/courses/course1", headers=_h())
    assert r.status_code == 200, r.text
    assert r.json()["position"]["in_flight_ids"] == ["e_claimed", "e_leased"]


# --- a dead lease does not cover its entry ---------------------------------------


def test_a_dead_lease_lands_its_open_entry_in_remaining_not_in_flight(client, db_path):
    _insert_item(db_path, id="e_dead_lease", product="vivy", title="dead lease",
                type="epic", state="open")
    # heartbeat 5 hours ago outlives the 4-hour grace default, so this lease
    # is unreleased but `is_dead` — the row `course_position` has to route
    # around rather than treat as coverage.
    _insert_lease(db_path, id="lease_dead", root_id="e_dead_lease",
                 taken_for="e_dead_lease", taken_at=_ts(hours=5),
                 heartbeat_at=_ts(hours=5))
    _insert_item(db_path, id="e_fresh_lease", product="vivy", title="fresh lease",
                type="epic", state="open")
    _insert_lease(db_path, id="lease_fresh", root_id="e_fresh_lease",
                 taken_for="e_fresh_lease")
    entries = ["e_dead_lease", "e_fresh_lease"]
    _insert_course(db_path, id="course1", product="vivy", entries=entries)

    r = client.get("/courses/course1", headers=_h())
    assert r.status_code == 200, r.text
    position = r.json()["position"]
    assert position["remaining_ids"] == ["e_dead_lease"]
    assert position["in_flight_ids"] == ["e_fresh_lease"]


# --- no next-shaped field --------------------------------------------------------


def test_position_carries_no_next_current_or_up_next_key(client, db_path):
    _insert_item(db_path, id="e_open", product="vivy", title="open",
                type="epic", state="open")
    _insert_course(db_path, id="course1", product="vivy", entries=["e_open"])

    r = client.get("/courses/course1", headers=_h())
    assert r.status_code == 200, r.text
    position = r.json()["position"]
    assert set(position.keys()) == {
        "entries", "reached", "reached_ids", "in_flight", "in_flight_ids",
        "remaining", "remaining_ids", "missing"}


# --- the measurement script -----------------------------------------------------


def test_measurement_script_runs_against_the_test_database(db_path):
    _insert_item(db_path, id="e_reached", product="vivy", title="reached",
                type="epic", state="fixed")
    _insert_item(db_path, id="e_open", product="vivy", title="open",
                type="epic", state="open")
    _insert_course(db_path, id="course1", product="vivy",
                   entries=["e_reached", "e_open"])

    result = subprocess.run(
        [sys.executable, "bin/measure_course_position.py", str(db_path),
         "--repeat", "3"],
        cwd=REPO, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "entries=2" in result.stdout
    assert "repetitions=3" in result.stdout
    assert "per_call_ms=" in result.stdout
