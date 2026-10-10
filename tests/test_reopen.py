"""Undoing a wrong resolve (`/reopen`, `issue_01M3P06BYH15H2QVYFTHAMXHF4`).

Before this route, `/correct` refused `state` by name and `/revision` edited
content only, so an item closed by mistake — a bad PR reconcile, a wrong
multi-repo close — had no way back but filing a duplicate.

**Moves any `store.ITEM_TERMINAL` state back to `open`** — `fixed`, `wontfix`,
`duplicate` and `redirected` alike, not just the three the bug report named,
because the guard is the same constant `/resolve` checks itself against.

**Hold discipline is `_hold_for_write`, the same gate `/resolve`, `/correct`
and `/link-pr` share** (covered end to end for those three in
`test_hold_for_write.py`; not folded in here for the same reason
`/reconcile-pr` stands apart — this route needs its fixture item to start
terminal, and `test_hold_for_write.py`'s shared `task` row starts `open`).

**`resolution`, `resolved_by`, `resolved_at` and `outcome_note` are cleared**,
since they describe a resolve this item no longer carries; `refs` — `pr` and
`url` — is left alone, so the history still shows which PR the wrong close
came from. **`claimed_by` and `claimed_at` are cleared too**, the same shape
`/reconcile-pr`'s `merged`/`closed` outcomes already give a `pr-open` item
returned to `open`.
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
WIDE = "agent-token-wide"

TERMINAL_STATES = sorted(store.ITEM_TERMINAL)


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
    conn.executemany(
        "INSERT INTO issue_items (id, product, title, type, state, claimed_by, "
        "claimed_at, parent_id, created_at, refs, resolution, resolved_by, "
        "resolved_at, outcome_note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [("epic_v", "vivy", "A vivy epic", "epic", "open", None, None, None,
          _ts(days=3), "{}", None, None, None, None),
         # Closed by a bad reconcile, with a PR still on the row — the exact
         # shape the bug report names: `pr`/`pr_url` must survive the reopen.
         ("task_a", "vivy", "Wrongly closed", "task", "fixed", None, None,
          "epic_v", _ts(days=2),
          json.dumps({"pr": 9, "url": "https://example/pull/9"}),
          "fixed", "some-agent", _ts(hours=1), "closed by mistake"),
         ("open_v", "vivy", "Not yet claimed", "task", "open", None, None,
          None, _ts(days=1), "{}", None, None, None, None),
         ("claimed_v", "vivy", "Mid-work", "task", "claimed", "session-1",
          _ts(hours=1), None, _ts(days=1), "{}", None, None, None, None),
         ("pr_open_v", "vivy", "In review", "task", "pr-open", "session-1",
          _ts(hours=1), None, _ts(days=1), "{}", None, None, None, None),
         ("task_c", "constance", "Another product", "task", "fixed",
          None, None, None, _ts(days=1), "{}", "fixed", None, None, None)])
    conn.commit()
    conn.close()

    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        AGENT: {"name": "vivy-agent", "products": ["vivy"]},
        WIDE: {"name": "wide-agent", "products": ["*"]},
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


def _item(item_id="task_a"):
    conn = _db()
    row = conn.execute("SELECT * FROM issue_items WHERE id = ?",
                       (item_id,)).fetchone()
    conn.close()
    return row


def _events(item_id="task_a"):
    conn = _db()
    rows = conn.execute(
        "SELECT actor, from_state, to_state, note FROM issue_events "
        "WHERE item_id = ? ORDER BY id", (item_id,)).fetchall()
    conn.close()
    return [(r["actor"], r["from_state"], r["to_state"], r["note"])
            for r in rows]


def _leases():
    conn = _db()
    rows = conn.execute("SELECT * FROM issue_leases").fetchall()
    conn.close()
    return rows


def _reopen(client, item_id, reason="closed by mistake", **extra):
    body = {} if reason is None else {"reason": reason}
    return client.post(f"/items/{item_id}/reopen", json={**body, **extra},
                       headers=_h())


def _set_state(item_id, state):
    conn = _db()
    conn.execute("UPDATE issue_items SET state = ?, resolution = ? "
                "WHERE id = ?",
                (state, state if state in store.ITEM_TERMINAL else None,
                 item_id))
    conn.commit()
    conn.close()


# --- the happy path: any terminal state back to open, history preserved -------


@pytest.mark.parametrize("state", TERMINAL_STATES)
def test_reopening_a_terminal_item_returns_it_to_open(client, state):
    _set_state("task_a", state)
    r = _reopen(client, "task_a")
    assert r.status_code == 200, r.json()
    assert r.json()["state"] == "open"


def test_reopening_clears_the_resolve_fields(client):
    r = _reopen(client, "task_a")
    assert r.status_code == 200, r.json()
    body = r.json()
    assert body["resolution"] is None
    assert body["resolved_by"] is None
    assert body["resolved_at"] is None
    assert body["outcome_note"] is None


def test_reopening_clears_a_stale_claim(client):
    """Mirrors `test_reconcile_pr.py`'s `test_reopening_clears_the_stale_claim`:
    an item claimed before it was resolved still carries that claimant's name
    on `claimed_by`/`claimed_at` once terminal, and the reopen must not hand
    it back to the open pool still reading as held by them."""
    conn = _db()
    conn.execute(
        "UPDATE issue_items SET claimed_by = ?, claimed_at = ? WHERE id = ?",
        ("session-1", _ts(days=2), "task_a"))
    conn.commit()
    conn.close()
    r = _reopen(client, "task_a")
    assert r.status_code == 200, r.json()
    body = r.json()
    assert body["claimed_by"] is None
    assert body["claimed_at"] is None


def test_reopening_keeps_pr_and_pr_url(client):
    r = _reopen(client, "task_a")
    assert r.status_code == 200, r.json()
    assert r.json()["refs"] == {"pr": 9, "url": "https://example/pull/9"}


def test_reopening_writes_the_state_event_with_the_reason(client):
    r = _reopen(client, "task_a", reason="the multi-repo close was wrong")
    assert r.status_code == 200, r.json()
    events = _events("task_a")
    assert len(events) == 1
    actor, from_state, to_state, note = events[0]
    assert (actor, from_state, to_state) == ("vivy-agent", "fixed", "open")
    assert note == "the multi-repo close was wrong"


# --- reason is required and non-empty ------------------------------------------


@pytest.mark.parametrize("reason", [None, "", "   ", 1, True, ["a reason"]])
def test_a_missing_or_blank_reason_is_refused(client, reason):
    body = {} if reason is None else {"reason": reason}
    r = client.post("/items/task_a/reopen", json=body, headers=_h())
    assert r.status_code == 400, r.json()
    assert _item("task_a")["state"] == "fixed"
    assert _events("task_a") == []


# --- refused on anything but a terminal item -----------------------------------


@pytest.mark.parametrize("item_id", ["open_v", "claimed_v", "pr_open_v"])
def test_a_non_terminal_item_is_refused_and_names_its_state(client, item_id):
    state = _item(item_id)["state"]
    r = _reopen(client, item_id)
    assert r.status_code == 409, r.json()
    assert r.json()["error"] == (
        f"item is {state}, not terminal; only a terminal item can be reopened")
    assert _item(item_id)["state"] == state
    assert _events(item_id) == []


# --- scope and the writes gate --------------------------------------------------


def test_an_out_of_scope_item_is_404(client):
    assert _reopen(client, "task_c").status_code == 404


def test_writes_are_refused_while_the_shadow_is_running(client, monkeypatch):
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    assert _reopen(client, "task_a").status_code == 503


# --- hold discipline: `_hold_for_write`, the same gate /resolve, /correct and
# /link-pr share (test_hold_for_write.py covers those three end to end; this
# route needs its fixture item to start terminal, which is why it is not
# folded into that shared parametrisation).


def _lease_epic(holder, *, alive=True):
    stamp = _ts(seconds=5) if alive else _ts(days=3)
    conn = _db()
    conn.execute(
        "INSERT INTO issue_leases (id, root_id, taken_for, holder, taken_at, "
        "heartbeat_at) VALUES ('lease_epic','epic_v','task_a',?,?,?)",
        (holder, stamp, stamp))
    conn.commit()
    conn.close()


def test_another_holders_live_lease_refuses_the_reopen(client):
    _lease_epic("co-other-crew")
    r = _reopen(client, "task_a")
    assert r.status_code == 409, r.json()
    assert r.json()["reason"] == "held"
    assert "co-other-crew" in r.json()["error"]
    assert _item("task_a")["state"] == "fixed"


def test_the_holders_own_lease_lets_the_reopen_through(client):
    _lease_epic("co-library-1154")
    r = _reopen(client, "task_a", owner="co-library-1154")
    assert r.status_code == 200, r.json()


def test_a_dead_lease_does_not_refuse_the_reopen(client):
    _lease_epic("co-gone", alive=False)
    r = _reopen(client, "task_a")
    assert r.status_code == 200, r.json()


def test_with_no_lease_the_route_holds_the_item_for_its_own_write(client):
    r = _reopen(client, "task_a")
    assert r.status_code == 200, r.json()
    rows = _leases()
    assert len(rows) == 1
    row = rows[0]
    assert (row["root_id"], row["taken_for"]) == ("epic_v", "task_a")
    assert row["released_at"] is not None, "a one-off write lease was left live"


# --- concurrent-write safety ----------------------------------------------------


def test_two_concurrent_reopens_do_not_double_write(client, monkeypatch):
    """Mirrors `test_reconcile_pr.py`'s two-concurrent-reconciles test: real
    threads, both paused at `_hold_for_write` — the seam every request crosses
    after reading the item and before taking the write lock — so both are
    guaranteed to have judged the item terminal before either writes."""
    import threading

    barrier = threading.Barrier(2)
    real = libapp._hold_for_write

    def racer(*args, **kwargs):
        barrier.wait(timeout=20)
        return real(*args, **kwargs)

    monkeypatch.setattr(libapp, "_hold_for_write", racer)
    results = []
    lock = threading.Lock()

    def go(reason):
        r = _reopen(client, "task_a", reason=reason)
        with lock:
            results.append(r)

    threads = [threading.Thread(target=go, args=(reason,))
              for reason in ("first caller's reason", "second caller's reason")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 2, results
    codes = sorted(r.status_code for r in results)
    assert codes == [200, 409], [r.json() for r in results]
    loser = next(r for r in results if r.status_code == 409)
    assert loser.json()["error"] == (
        "the item is no longer fixed; it is now open")
    events = _events("task_a")
    assert len(events) == 1, "the loser's transition was recorded too"


def test_a_reopen_racing_a_resolve_loses_cleanly(client, monkeypatch):
    """The item is already terminal — the exact shape the bug report names.
    A resolve fired while the reopen is mid-flight reads the same terminal row
    the reopen is about to move and is refused on it; the reopen then
    completes and only its transition is recorded.

    Reproduces the shape `test_reconcile_pr.py` uses for `/reconcile-pr` racing
    `/resolve`: real threads, the reopen paused at `_hold_for_write` — the seam
    every request crosses after reading the item and before taking the write
    lock — and released only once the resolve, fired in the gap, has already
    read and been refused. `/resolve`'s own terminal check runs before it ever
    reaches `_hold_for_write`, so there is no write-lock contention to
    serialize here the way `/reconcile-pr` and `/resolve` have on a shared
    `pr-open` precondition; what this proves instead is that the resolve fired
    into that open window still sees the row as `fixed`, not as some
    in-between state the reopen's own transaction might have exposed.
    """
    import threading
    import time

    paused = threading.Event()
    resume = threading.Event()
    real = libapp._hold_for_write

    def hold_racer(*args, **kwargs):
        paused.set()
        assert resume.wait(timeout=20), "never told to continue"
        return real(*args, **kwargs)

    monkeypatch.setattr(libapp, "_hold_for_write", hold_racer)

    results = {}

    def do_reopen():
        results["reopen"] = _reopen(client, "task_a")

    def do_resolve():
        results["resolve"] = client.post(
            "/items/task_a/resolve",
            json={"resolution": "wontfix", "owner": "session-1"},
            headers=_h())

    reopen_thread = threading.Thread(target=do_reopen)
    reopen_thread.start()
    assert paused.wait(timeout=20), "reopen never reached its pause"

    resolve_thread = threading.Thread(target=do_resolve)
    resolve_thread.start()
    resolve_thread.join(timeout=20)
    time.sleep(0.1)
    resume.set()
    reopen_thread.join(timeout=20)

    assert set(results) == {"reopen", "resolve"}
    assert results["resolve"].status_code == 409, results["resolve"].json()
    assert results["resolve"].json()["error"] == "already fixed"
    assert results["reopen"].status_code == 200, results["reopen"].json()

    row = _item("task_a")
    assert row["state"] == "open"
    events = _events("task_a")
    assert len(events) == 1, "the resolve's refused attempt wrote an event too"
    assert events[0][1:3] == ("fixed", "open")
