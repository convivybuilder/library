"""Linking a PR, and the retired park routes.

`/park` and `/unpark` are gone, and `parked` with them (ADR
platform/decisions/0074): a blocker is a `prerequisite` now, covered by
`test_prerequisites.py`. The park-history columns stay on the schema below
because the rows that were once parked still carry them.
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
            severity TEXT, state TEXT, claimed_by TEXT, claimed_by_crew TEXT, claimed_at TEXT,
            resolution TEXT, resolved_by TEXT, resolved_at TEXT, outcome_note TEXT,
            once_key TEXT UNIQUE, created_by TEXT, created_at TEXT, run_id TEXT,
            type TEXT, created_by_kind TEXT, needs TEXT, park_reason TEXT,
            unpark_condition TEXT, parked_at TEXT, embedded_at TEXT,
            parent_id TEXT, topics TEXT, maturity TEXT, specification TEXT,
            definition_of_done TEXT, prerequisites TEXT,
            model_choice TEXT, model_reason TEXT, model_set_by_client TEXT,
            model_acting_human TEXT, model_set_at TEXT, found_during TEXT, item_key TEXT, short_description TEXT);
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
            id TEXT PRIMARY KEY, root_id TEXT, taken_for TEXT, holder TEXT, holder_crew TEXT,
            run_id TEXT, taken_at TEXT, heartbeat_at TEXT, released_at TEXT,
            release_reason TEXT, released_by TEXT, note TEXT);
        CREATE UNIQUE INDEX ix_live_root ON issue_leases (root_id)
            WHERE released_at IS NULL;
        CREATE TABLE lease_refusals (
            id TEXT PRIMARY KEY, at TEXT, requested_item_id TEXT, product TEXT,
            requester TEXT, actor TEXT, blocking_lease_id TEXT,
            blocking_root_id TEXT, blocking_holder TEXT, blocks_by TEXT, blocking_taken_for TEXT);
    """)
    conn.executemany(
        "INSERT INTO issue_items (id, product, title, type, state, needs, "
        "created_by_kind, created_at, refs) VALUES (?,?,?,?,?,?,?,?,?)",
        [("task", "vivy", "A task", "task", "open", "agent", "agent",
          _ts(days=2), "{}"),
         ("done", "vivy", "Done", "task", "fixed", "agent", "agent",
          _ts(days=2), "{}")])
    conn.commit()
    conn.close()

    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        AGENT: {"name": "vivy-agent", "products": ["vivy"]},
    }))
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    monkeypatch.setattr(lease, "TUNABLES", tmp_path / "absent.json")
    return TestClient(libapp.app)


def _h(token=AGENT):
    return {"Authorization": f"Bearer {token}",
            protocol.HEADER: str(protocol.CURRENT)}


def _db():
    conn = sqlite3.connect(store.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _refs(item_id):
    conn = _db()
    raw = conn.execute("SELECT refs FROM issue_items WHERE id = ?",
                       (item_id,)).fetchone()["refs"]
    conn.close()
    return json.loads(raw)


def _state(item_id):
    conn = _db()
    state = conn.execute("SELECT state FROM issue_items WHERE id = ?",
                         (item_id,)).fetchone()["state"]
    conn.close()
    return state


# --- the park routes are gone --------------------------------------------------


@pytest.mark.parametrize("path,body", [
    ("park", {"reason": "blocked", "unpark_condition": "vendor replies"}),
    ("unpark", {}),
])
def test_the_park_routes_are_gone(client, path, body):
    """A caller still sending them is told the route does not exist, and the
    item does not move."""
    r = client.post(f"/items/task/{path}", json=body, headers=_h())
    assert r.status_code in (404, 405), r.text
    assert _state("task") == "open"


def test_parked_is_no_longer_a_state():
    assert not hasattr(store, "ITEM_STATE_PARKED")
    assert not hasattr(store, "ITEM_HELD")
    assert "parked" not in store.ITEM_ACTIVE | store.ITEM_TERMINAL


def test_a_row_in_a_retired_state_is_not_moved(client):
    """Every stored state is active or terminal, so this is a row no route
    writes any more. The guard still names it rather than moving it."""
    conn = _db()
    conn.execute("UPDATE issue_items SET state = 'parked' WHERE id = 'task'")
    conn.commit()
    conn.close()
    r = client.post("/items/task/link-pr", json={"pr": 1}, headers=_h())
    assert r.status_code == 409
    assert r.json()["error"] == "state parked is not one work can move"
    assert _state("task") == "parked"


# --- link-pr -------------------------------------------------------------------


@pytest.mark.parametrize("pr", [5, "5", "convivy/library#5"])
def test_a_pr_may_be_a_number_or_a_string(client, pr):
    r = client.post("/items/task/link-pr", json={"pr": pr}, headers=_h())
    assert r.status_code == 200 and r.json()["state"] == "pr-open"
    assert _refs("task")["pr"] == pr


@pytest.mark.parametrize("pr", [None, "", "   ", True, {"n": 1}])
def test_a_malformed_pr_is_refused(client, pr):
    """`True` is in this list on purpose: it is an int in Python, so a bare
    isinstance check accepts it and stores a boolean where a PR number belongs."""
    assert client.post("/items/task/link-pr", json={"pr": pr},
                       headers=_h()).status_code == 400


def test_linking_a_pr_to_a_terminal_item_is_refused(client):
    assert client.post("/items/done/link-pr", json={"pr": 1},
                       headers=_h()).status_code == 409


# --- every transition leaves a trail -------------------------------------------


def test_a_state_change_always_appends_an_event(client):
    """A state change with no event is a row the audit trail cannot explain."""
    client.post("/items/task/link-pr", json={"pr": 1}, headers=_h())
    conn = _db()
    ev = conn.execute("SELECT actor, from_state, to_state FROM issue_events "
                      "WHERE item_id='task' ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    assert (ev["actor"], ev["from_state"], ev["to_state"]) == (
        "vivy-agent", "open", "pr-open")


def test_writes_are_refused_while_the_shadow_is_running(client, monkeypatch):
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    assert client.post("/items/task/link-pr", json={"pr": 1},
                       headers=_h()).status_code == 503


# --- a losing concurrent writer is refused, not silently overwritten ------------
#
# link-pr writes through the shared `_transition`, where the compare-and-swap
# lives. Two real threads are synchronised on `_hold_for_write` — the seam
# every request crosses AFTER reading the item and BEFORE taking the write
# lock, exactly once — so both are guaranteed to have judged the item's state
# before either writes. A seam after the lock would hold one thread there with
# the lock taken, and the other could never arrive.


def _race(client, path, bodies, monkeypatch):
    """Run one request per body in its own thread, both held at
    `_hold_for_write` until both arrive, and return `[(body, response)]`."""
    import threading

    barrier = threading.Barrier(2)
    real = libapp._hold_for_write

    def racer(*args, **kwargs):
        barrier.wait(timeout=20)
        return real(*args, **kwargs)

    monkeypatch.setattr(libapp, "_hold_for_write", racer)
    results = []
    lock = threading.Lock()

    def go(body):
        response = client.post(path, json=body, headers=_h())
        with lock:
            results.append((body, response))

    threads = [threading.Thread(target=go, args=(body,)) for body in bodies]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def test_a_link_pr_that_loses_a_concurrent_race_is_rolled_back(client,
                                                               monkeypatch):
    results = _race(client, "/items/task/link-pr",
                    [{"pr": 11}, {"pr": 12}], monkeypatch)
    assert len(results) == 2, results
    codes = sorted(r.status_code for _, r in results)
    assert codes == [200, 409], [r.json() for _, r in results]
    loser = next(r for _, r in results if r.status_code == 409)
    assert loser.json()["error"] == (
        "the item is no longer open; it is now pr-open")
    winner = next(body for body, r in results if r.status_code == 200)
    conn = _db()
    row = conn.execute("SELECT state, refs FROM issue_items WHERE id='task'"
                       ).fetchone()
    events = conn.execute("SELECT from_state, to_state FROM issue_events "
                          "WHERE item_id='task'").fetchall()
    conn.close()
    assert row["state"] == "pr-open"
    assert json.loads(row["refs"])["pr"] == winner["pr"]
    assert [(e["from_state"], e["to_state"]) for e in events] == [
        ("open", "pr-open")], "the loser's link was recorded"
