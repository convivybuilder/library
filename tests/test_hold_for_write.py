"""Every change to a work item happens under a checkout, and never under
someone else's (the operator, 2026-09-11).

`_hold_for_write` is the one gate `/resolve`, `/correct`, `/link-pr` and
`/prerequisites/verdict` share. Each case below runs against all four, so a
route that stopped calling it fails here by name rather than passing on the
strength of the others.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

import app as libapp
import identity
import lease
import protocol
import store

AGENT = "agent-token-vivy"
ACTOR = "vivy-agent"
CONDITION = "the vendor replies"


def _ts(**delta):
    return (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
            - dt.timedelta(**delta)).isoformat(" ")


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = tmp_path / "control.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE issue_items (
            id TEXT PRIMARY KEY, product TEXT, title TEXT, body TEXT, refs TEXT,
            severity TEXT, state TEXT, claimed_by TEXT, claimed_at TEXT,
            resolution TEXT, resolved_by TEXT, resolved_at TEXT, outcome_note TEXT,
            once_key TEXT UNIQUE, created_by TEXT, created_at TEXT, run_id TEXT,
            type TEXT, created_by_kind TEXT, needs TEXT, park_reason TEXT,
            unpark_condition TEXT, parked_at TEXT, embedded_at TEXT,
            parent_id TEXT, topics TEXT, maturity TEXT, specification TEXT,
            definition_of_done TEXT, prerequisites TEXT,
            model_choice TEXT, model_reason TEXT, model_set_by_client TEXT,
            model_acting_human TEXT, model_set_at TEXT, found_during TEXT, item_key TEXT);
        CREATE TABLE issue_item_key_counters (
            scope_kind TEXT, scope_id TEXT, next_value INTEGER,
            PRIMARY KEY (scope_kind, scope_id));
        CREATE TABLE issue_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT, actor TEXT,
            from_state TEXT, to_state TEXT, at TEXT, note TEXT);
        CREATE TABLE issue_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT, actor TEXT,
            kind TEXT, body TEXT, run_id TEXT, at TEXT);
        CREATE TABLE issue_leases (
            id TEXT PRIMARY KEY, root_id TEXT, taken_for TEXT, holder TEXT,
            run_id TEXT, taken_at TEXT, heartbeat_at TEXT, released_at TEXT,
            release_reason TEXT, released_by TEXT, note TEXT);
        CREATE UNIQUE INDEX ix_live_root ON issue_leases (root_id)
            WHERE released_at IS NULL;
        CREATE TABLE lease_refusals (
            id TEXT PRIMARY KEY, at TEXT, requested_item_id TEXT, product TEXT,
            requester TEXT, actor TEXT, blocking_lease_id TEXT,
            blocking_root_id TEXT, blocking_holder TEXT, blocks_by TEXT);
    """)
    prerequisites = json.dumps([{"text": CONDITION, "met": False}])
    conn.executemany(
        "INSERT INTO issue_items (id, product, title, type, state, severity, "
        "needs, created_by_kind, created_at, refs, parent_id, prerequisites) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [("epic", "vivy", "An epic", "epic", "open", "medium", "agent", "agent",
          _ts(days=2), "{}", None, None),
         ("task", "vivy", "A task", "task", "open", "low", "agent", "agent",
          _ts(days=2), "{}", "epic", prerequisites)])
    conn.commit()
    conn.close()

    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({AGENT: {"name": ACTOR, "products": ["vivy"]}}))
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    monkeypatch.setattr(lease, "TUNABLES", tmp_path / "absent.json")
    return TestClient(libapp.app)


def _h():
    return {"Authorization": f"Bearer {AGENT}",
            protocol.HEADER: str(protocol.CURRENT)}


def _db():
    conn = sqlite3.connect(store.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _lease_epic(holder, *, alive=True):
    stamp = _ts(seconds=5) if alive else _ts(days=3)
    conn = _db()
    conn.execute(
        "INSERT INTO issue_leases (id, root_id, taken_for, holder, taken_at, "
        "heartbeat_at) VALUES ('lease_epic','epic','task',?,?,?)",
        (holder, stamp, stamp))
    conn.commit()
    conn.close()


def _task():
    conn = _db()
    row = conn.execute("SELECT * FROM issue_items WHERE id='task'").fetchone()
    conn.close()
    return row


def _written():
    conn = _db()
    rows = conn.execute("SELECT * FROM issue_leases WHERE release_reason = ?",
                        (lease.LEASE_WRITTEN,)).fetchall()
    conn.close()
    return rows


# Each route, the body that changes the task, and how to tell it changed.
ROUTES = [
    ("resolve", {"resolution": "fixed"},
     lambda row: row["state"] == "fixed"),
    ("correct", {"severity": "high"},
     lambda row: row["severity"] == "high"),
    ("link-pr", {"pr": 7},
     lambda row: row["state"] == "pr-open"),
    ("prerequisites/verdict", {"text": CONDITION, "met": True},
     lambda row: json.loads(row["prerequisites"])[0]["met"] is True),
]
IDS = [r[0] for r in ROUTES]


def _post(client, path, body, **extra):
    return client.post(f"/items/task/{path}", json={**body, **extra},
                       headers=_h())


@pytest.mark.parametrize("path,body,changed", ROUTES, ids=IDS)
def test_another_holders_live_lease_refuses_the_change(client, path, body,
                                                       changed):
    _lease_epic("co-other-crew")
    r = _post(client, path, body)
    assert r.status_code == 409, r.json()
    assert r.json()["reason"] == "held"
    assert "co-other-crew" in r.json()["error"]
    assert not changed(_task())
    assert _written() == [], "a refused change still recorded a write lease"


@pytest.mark.parametrize("path,body,changed", ROUTES, ids=IDS)
def test_omitting_owner_is_judged_as_the_token_identity(client, path, body,
                                                         changed):
    """A caller that checked out under a session name and omits `owner` is
    judged as its token identity, which is not the holder. The refusal names
    the holder, so the fix is visible in the response."""
    _lease_epic("co-library-1154")
    r = _post(client, path, body)
    assert r.status_code == 409 and r.json()["reason"] == "held"
    assert "co-library-1154" in r.json()["error"]


@pytest.mark.parametrize("path,body,changed", ROUTES, ids=IDS)
def test_the_holders_own_lease_lets_the_change_through(client, path, body,
                                                       changed):
    _lease_epic("co-library-1154")
    r = _post(client, path, body, owner="co-library-1154")
    assert r.status_code == 200, r.json()
    assert changed(_task())
    assert _written() == [], "a change under a real checkout recorded a second one"


@pytest.mark.parametrize("path,body,changed", ROUTES, ids=IDS)
def test_with_no_lease_the_route_holds_the_item_for_its_own_write(
        client, path, body, changed):
    r = _post(client, path, body)
    assert r.status_code == 200, r.json()
    assert changed(_task())
    rows = _written()
    assert len(rows) == 1
    row = rows[0]
    assert (row["root_id"], row["taken_for"], row["holder"]) == (
        "epic", "task", ACTOR)
    assert row["released_at"] is not None and row["released_by"] == ACTOR
    conn = _db()
    live = conn.execute("SELECT COUNT(*) FROM issue_leases "
                        "WHERE released_at IS NULL").fetchone()[0]
    conn.close()
    assert live == 0, "the write lease was left fencing the epic"


@pytest.mark.parametrize("path,body,changed", ROUTES, ids=IDS)
def test_a_dead_lease_does_not_refuse_the_change(client, path, body, changed):
    """Its holder is gone, and reclaiming a dead lease needs no human."""
    _lease_epic("co-gone", alive=False)
    r = _post(client, path, body)
    assert r.status_code == 200, r.json()
    assert changed(_task())


def test_a_comment_needs_no_lease_even_while_someone_holds_the_item(client):
    """Commenting is how a non-holder asks the holder something, and it changes
    nothing on the item, so it stays outside the rule."""
    _lease_epic("co-other-crew")
    r = client.post("/items/task/comments", json={"body": "a question"},
                    headers=_h())
    assert r.status_code == 201, r.json()
