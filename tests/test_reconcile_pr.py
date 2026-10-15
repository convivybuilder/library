"""Ending a `pr-open` item's wait on its own linked PR (`/reconcile-pr`).

`issue_01M2HRVPQN9X8WN3KA0W9S9P30`: `/link-pr` had a door in and none out. An
epic whose PR merged with work left in its body stayed `pr-open` forever —
`_do_release`'s `pr-open` branch deliberately drops only the fence, because the
PR is still open when a session releases mid-review, and nothing ever revisited
that premise once the PR itself reached an ending. `/reconcile-pr` is that
missing route, called by whatever noticed the PR's outcome (today,
agent-platform's `issue-pr-reconcile`) rather than by the item's own holder.

**`merged` and `closed` both return the item to `open`, the same shape
`lease.release_claim` already gives a `claimed` release**: `claimed_by` and
`claimed_at` clear, so a reader does not see a stale holder's name on a row
that reads as available. `refs` — `pr` and `url` — stays, as history.

**`abandoned` moves nothing.** The PR is still open, indistinguishable from one
still being worked, so freeing the item would race a second worker onto a live
branch. It only raises `needs` to `"operator"`.

**Hold discipline is `_hold_for_write`, not a `claimed_by` match** — the same
gate `/link-pr`, `/correct` and `/resolve` share, covered end to end for those
three in `test_hold_for_write.py`. A `pr-open` item's `claimed_by` is leftover
text once its own `/release` already dropped the fence (`lease.claim_status`
answers `(False, None)` for anything but `claimed`), so matching against it
would refuse the one caller this route exists for. What still has to hold is a
LIVE lease taken with this item as `root_id` — the shape a decomposed child's
own checkout leaves behind (`issue_01M2HRVPQN9X8WN3KA0W9S9P30`, comment #2673).
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
        "INSERT INTO issue_items (id, product, title, type, state, claimed_by, "
        "claimed_at, parent_id, created_at, refs) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [("epic_v", "vivy", "A vivy epic", "epic", "open", None, None, None,
          _ts(days=3), "{}"),
         # In review, under the epic, carrying the PR `/link-pr` would have
         # recorded — the row `/reconcile-pr` exists to unstick.
         ("task_a", "vivy", "A task in review", "task", "pr-open", "session-1",
          _ts(hours=2), "epic_v", _ts(days=2),
          json.dumps({"pr": 9, "url": "https://example/pull/9"})),
         ("open_v", "vivy", "Not yet claimed", "task", "open", None, None,
          None, _ts(days=1), "{}"),
         ("claimed_v", "vivy", "Mid-work", "task", "claimed", "session-1",
          _ts(hours=1), None, _ts(days=1), "{}"),
         ("done_v", "vivy", "Already resolved", "task", "fixed", None, None,
          None, _ts(days=1), "{}"),
         ("task_c", "constance", "Another product", "task", "pr-open",
          "session-1", _ts(hours=2), None, _ts(days=1), "{}")])
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


def _reconcile(client, item_id, outcome, **extra):
    return client.post(f"/items/{item_id}/reconcile-pr",
                       json={"outcome": outcome, **extra}, headers=_h())


# --- merged / closed: back to open, refs and history preserved -----------------


@pytest.mark.parametrize("outcome", ["merged", "closed"])
def test_returns_a_pr_open_item_to_open_with_refs_intact(client, outcome):
    r = _reconcile(client, "task_a", outcome)
    assert r.status_code == 200, r.json()
    body = r.json()
    assert body["state"] == "open"
    assert body["refs"] == {"pr": 9, "url": "https://example/pull/9"}


@pytest.mark.parametrize("outcome", ["merged", "closed"])
def test_reopening_clears_the_stale_claim(client, outcome):
    """A `pr-open` item's `claimed_by` is leftover text once its own `/release`
    already dropped the fence — the row should not read as claimed once it is
    back in the open pool."""
    r = _reconcile(client, "task_a", outcome)
    assert r.status_code == 200, r.json()
    body = r.json()
    assert body["claimed_by"] is None
    assert body["claimed_at"] is None


@pytest.mark.parametrize("outcome", ["merged", "closed"])
def test_reopening_writes_the_state_event(client, outcome):
    r = _reconcile(client, "task_a", outcome)
    assert r.status_code == 200, r.json()
    events = _events("task_a")
    assert len(events) == 1
    actor, from_state, to_state, note = events[0]
    assert (actor, from_state, to_state) == ("vivy-agent", "pr-open", "open")
    assert note and outcome in note.lower()


def test_merged_and_closed_notes_are_distinguishable(client):
    conn = _db()
    conn.execute(
        "INSERT INTO issue_items (id, product, title, type, state, refs, "
        "created_at) VALUES ('task_b','vivy','Also in review','task','pr-open', "
        "'{}', ?)", (_ts(days=1),))
    conn.commit()
    conn.close()

    _reconcile(client, "task_a", "merged")
    merged_note = _events("task_a")[0][3]
    _reconcile(client, "task_b", "closed")
    closed_note = _events("task_b")[0][3]

    assert merged_note != closed_note
    assert "merged" in merged_note.lower()
    assert "closed" in closed_note.lower()


# --- abandoned: no state change, needs flips to operator ------------------------


def test_abandoned_sets_needs_operator_without_moving_state(client):
    r = _reconcile(client, "task_a", "abandoned")
    assert r.status_code == 200, r.json()
    body = r.json()
    assert body["state"] == "pr-open"
    assert body["needs"] == "operator"


def test_abandoned_writes_no_state_event(client):
    """The item did not move, so there is nothing for the audit trail to
    explain — recording a `pr-open -> pr-open` event would be a transition
    that never happened."""
    _reconcile(client, "task_a", "abandoned")
    assert _events("task_a") == []


def test_abandoned_leaves_the_claim_and_refs_alone(client):
    r = _reconcile(client, "task_a", "abandoned")
    assert r.status_code == 200, r.json()
    body = r.json()
    assert body["claimed_by"] == "session-1"
    assert body["refs"] == {"pr": 9, "url": "https://example/pull/9"}


# --- refused from every state but pr-open ---------------------------------------


@pytest.mark.parametrize("item_id,outcome", [
    ("open_v", "merged"), ("open_v", "abandoned"),
    ("claimed_v", "merged"), ("claimed_v", "abandoned"),
    ("done_v", "merged"), ("done_v", "abandoned"),
])
def test_refused_from_every_state_but_pr_open(client, item_id, outcome):
    r = _reconcile(client, item_id, outcome, owner="session-1")
    assert r.status_code == 409, r.json()
    conn = _db()
    state = conn.execute("SELECT state FROM issue_items WHERE id = ?",
                         (item_id,)).fetchone()["state"]
    conn.close()
    assert state == {"open_v": "open", "claimed_v": "claimed",
                     "done_v": "fixed"}[item_id]


def test_a_terminal_item_names_its_own_resolution(client):
    r = _reconcile(client, "done_v", "merged")
    assert r.json()["error"] == "already fixed"


def test_an_active_but_not_pr_open_item_names_its_state(client):
    r = _reconcile(client, "open_v", "merged")
    assert "open" in r.json()["error"]


# --- outcome validation ----------------------------------------------------------


@pytest.mark.parametrize("outcome", [None, "", "in-review", 1, True, ["merged"]])
def test_a_malformed_outcome_is_refused(client, outcome):
    r = client.post("/items/task_a/reconcile-pr", json={"outcome": outcome},
                    headers=_h())
    assert r.status_code == 400, r.json()
    assert _item("task_a")["state"] == "pr-open"


# --- scope and the writes gate ----------------------------------------------------


def test_an_out_of_scope_item_is_404(client):
    assert _reconcile(client, "task_c", "merged").status_code == 404


def test_writes_are_refused_while_the_shadow_is_running(client, monkeypatch):
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    assert _reconcile(client, "task_a", "merged").status_code == 503


# --- hold discipline: `_hold_for_write`, the same gate /link-pr and /correct
# share (test_hold_for_write.py covers those three end to end; task_a starts
# `pr-open` here rather than `open`, which is why this route is not folded into
# that shared parametrisation — its ROUTES fixture item has to start `open` for
# `link-pr`'s own case, and the two starting states are mutually exclusive on
# one row).


def _lease_epic(holder, *, alive=True):
    stamp = _ts(seconds=5) if alive else _ts(days=3)
    conn = _db()
    conn.execute(
        "INSERT INTO issue_leases (id, root_id, taken_for, holder, taken_at, "
        "heartbeat_at) VALUES ('lease_epic','epic_v','task_a',?,?,?)",
        (holder, stamp, stamp))
    conn.commit()
    conn.close()


@pytest.mark.parametrize("outcome", ["merged", "abandoned"])
def test_another_holders_live_lease_refuses_the_reconcile(client, outcome):
    _lease_epic("co-other-crew")
    r = _reconcile(client, "task_a", outcome)
    assert r.status_code == 409, r.json()
    assert r.json()["reason"] == "held"
    assert "co-other-crew" in r.json()["error"]
    assert _item("task_a")["state"] == "pr-open"
    assert _item("task_a")["needs"] is None


@pytest.mark.parametrize("outcome", ["merged", "abandoned"])
def test_the_holders_own_lease_lets_the_reconcile_through(client, outcome):
    _lease_epic("co-library-1154")
    r = _reconcile(client, "task_a", outcome, owner="co-library-1154")
    assert r.status_code == 200, r.json()


@pytest.mark.parametrize("outcome", ["merged", "abandoned"])
def test_a_dead_lease_does_not_refuse_the_reconcile(client, outcome):
    _lease_epic("co-gone", alive=False)
    r = _reconcile(client, "task_a", outcome)
    assert r.status_code == 200, r.json()


def test_with_no_lease_the_route_holds_the_item_for_its_own_write(client):
    r = _reconcile(client, "task_a", "merged")
    assert r.status_code == 200, r.json()
    rows = _leases()
    assert len(rows) == 1
    row = rows[0]
    assert (row["root_id"], row["taken_for"]) == ("epic_v", "task_a")
    assert row["released_at"] is not None, "a one-off write lease was left live"


# --- concurrent-write safety, the shape the release race test uses -------------
#
# Two real threads, one held at `_hold_for_write` — the seam every request
# crosses after reading the item and before taking the write lock — so both are
# guaranteed to have judged the item's state before either writes. Mirrors
# `test_transitions.py`'s `_race` for `/link-pr`, which shares this route's
# `_hold_for_write` + `_transition` shape.


def test_two_concurrent_reconciles_do_not_double_write(client, monkeypatch):
    import threading

    barrier = threading.Barrier(2)
    real = libapp._hold_for_write

    def racer(*args, **kwargs):
        barrier.wait(timeout=20)
        return real(*args, **kwargs)

    monkeypatch.setattr(libapp, "_hold_for_write", racer)
    results = []
    lock = threading.Lock()

    def go(outcome):
        r = _reconcile(client, "task_a", outcome)
        with lock:
            results.append((outcome, r))

    threads = [threading.Thread(target=go, args=(o,))
              for o in ("merged", "closed")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 2, results
    codes = sorted(r.status_code for _, r in results)
    assert codes == [200, 409], [r.json() for _, r in results]
    loser = next(r for _, r in results if r.status_code == 409)
    assert loser.json()["error"] == (
        "the item is no longer pr-open; it is now open")
    events = _events("task_a")
    assert len(events) == 1, "the loser's transition was recorded too"


def test_a_reconcile_that_races_a_concurrent_resolve_is_serialized(client,
                                                                    monkeypatch):
    """Reproduces the shape `test_lease_release.py` uses for `/release` racing
    `/resolve`: real threads, one paused at a specific write seam and released
    only once the other has arrived, so an uncontended second write would land
    in the gap if the lock were not already held by the time the pause fires.

    `_hold_for_write` is `/reconcile-pr`'s first write-adjacent call, reached
    only after its own read; pausing there and firing `/resolve` at the same
    item proves the write lock this route takes protects the whole
    read-decide-write sequence, not just the final UPDATE.
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

    def do_reconcile():
        results["reconcile"] = client.post(
            "/items/task_a/reconcile-pr", json={"outcome": "merged"},
            headers=_h())

    def do_resolve():
        results["resolve"] = client.post(
            "/items/task_a/resolve",
            json={"resolution": "fixed", "owner": "session-1"}, headers=_h())

    reconcile_thread = threading.Thread(target=do_reconcile)
    reconcile_thread.start()
    assert paused.wait(timeout=20), "reconcile never reached its pause"

    resolve_thread = threading.Thread(target=do_resolve)
    resolve_thread.start()
    time.sleep(0.3)
    resume.set()

    reconcile_thread.join(timeout=20)
    resolve_thread.join(timeout=20)

    assert set(results) == {"reconcile", "resolve"}
    codes = sorted(r.status_code for r in results.values())
    assert codes == [200, 409], {k: v.json() for k, v in results.items()}

    row = _item("task_a")
    events = _events("task_a")
    assert len(events) == 1, "both writers landed a transition"
    if results["reconcile"].status_code == 200:
        assert row["state"] == "open"
        assert results["resolve"].json()["error"] == (
            "the item is no longer pr-open; it is now open")
    else:
        assert row["state"] == "fixed"
        assert results["reconcile"].json()["error"] == (
            "the item is no longer pr-open; it is now fixed")
