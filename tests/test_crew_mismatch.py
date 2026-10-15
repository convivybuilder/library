"""A credential of a different crew cannot release, resolve, reopen, check out
or claim another crew's hold by typing the holder's own `owner` string back
(CQ·10, the operator's ruling of 2026-10-15, comment 5378 on
issue_01KZMQCBXQ5KF2DTJCV3A9GMF7).

Every hold `owner` string is self-declared and unbound to the credential that
took it — that is the defect these items describe, and it is still true after
this change. What is new is that a work item's claim and an epic lease's
holder now ALSO carry which crew took them, and a non-human credential whose
own crew differs is refused 409 `reason: "crew_mismatch"` regardless of what
`owner` it types. The refusal names neither crew.

`CREW_A1` and `CREW_A2` are two credentials of one crew, `crew-alpha` — the
accepted gap this change does not close, pinned here so a later change that
does close it is deliberate. `CREW_B` is `crew-bravo`. `UNCREWED` carries no
`crew` field at all, which the ruling treats as its own crew for this
purpose: refused on a crewed hold, exactly like `CREW_B`.
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

CREW_A1 = "token-crew-a1"
CREW_A2 = "token-crew-a2"
CREW_B = "token-crew-b"
UNCREWED = "token-uncrewed"


def _ts(**delta):
    return (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
            - dt.timedelta(**delta)).isoformat(" ")


@pytest.fixture
def client(tmp_path, monkeypatch, vector_index, ledger_tables):
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
        CREATE TABLE call_slips (
            id TEXT PRIMARY KEY, item_id TEXT, product TEXT, claim_owner_label TEXT,
            issued_at TEXT, expires_at TEXT, grant_json TEXT);
        CREATE TABLE issue_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT, actor TEXT,
            kind TEXT, body TEXT, run_id TEXT, at TEXT);
        CREATE TABLE corrections_queues (
            id TEXT PRIMARY KEY, product TEXT UNIQUE, created_at TEXT);
    """)
    # An epic with two children, for the checkout/claim cases — a second
    # child under the same held epic is what makes those routes reachable at
    # all: the item itself stays `open`, the epic's own lease is what a
    # different crew's checkout would silently refresh if its `owner` string
    # happened to match.
    conn.executemany(
        "INSERT INTO issue_items (id, product, title, type, state, needs, "
        "created_by_kind, parent_id, created_at, refs) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [("epic1", "vivy", "An epic", "epic", "open", "agent", "agent",
          None, _ts(days=2), "{}"),
         ("task1", "vivy", "First task", "task", "open", "agent", "agent",
          "epic1", _ts(days=2), "{}"),
         ("task2", "vivy", "Second task", "task", "open", "agent", "agent",
          "epic1", _ts(days=2), "{}"),
         ("lone1", "vivy", "Parentless one", "task", "open", "agent", "agent",
          None, _ts(days=1), "{}"),
         ("lone2", "vivy", "Parentless two", "task", "open", "agent", "agent",
          None, _ts(days=1), "{}"),
         # Nested epics lease themselves, so `sub` and `top` have different
         # roots: the shape the conflict scan, not the refresh branch, judges.
         ("top", "vivy", "Top epic", "epic", "open", "agent", "agent",
          None, _ts(days=2), "{}"),
         ("sub", "vivy", "Sub epic", "epic", "open", "agent", "agent",
          "top", _ts(days=2), "{}"),
         ("leaf", "vivy", "Leaf", "task", "open", "agent", "agent",
          "sub", _ts(days=2), "{}")])
    conn.commit()
    conn.close()

    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        CREW_A1: {"name": "co-a1", "products": ["vivy"], "crew": "crew-alpha"},
        CREW_A2: {"name": "co-a2", "products": ["vivy"], "crew": "crew-alpha"},
        CREW_B: {"name": "co-b1", "products": ["vivy"], "crew": "crew-bravo"},
        UNCREWED: {"name": "co-x", "products": ["vivy"]},
    }))
    vector_index(db)
    ledger_tables(db)
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    monkeypatch.setattr(lease, "TUNABLES", tmp_path / "absent.json")
    return TestClient(libapp.app)


def _h(token):
    return {"Authorization": f"Bearer {token}",
            protocol.HEADER: str(protocol.CURRENT)}


def _co(client, item, owner="session-1", token=CREW_A1, **extra):
    return client.post(f"/items/{item}/checkout",
                       json={"owner": owner, **extra}, headers=_h(token))


def _claim(client, item, owner="session-1", token=CREW_A1):
    return client.post(f"/items/{item}/claim", json={"owner": owner},
                       headers=_h(token))


def _release(client, item, owner="session-1", token=CREW_A1):
    return client.post(f"/items/{item}/release", json={"owner": owner},
                       headers=_h(token))


def _resolve(client, item, owner="session-1", token=CREW_A1,
            resolution="fixed"):
    return client.post(f"/items/{item}/resolve",
                       json={"owner": owner, "resolution": resolution},
                       headers=_h(token))


def _reopen(client, item, owner="session-1", token=CREW_A1,
           reason="wrong call"):
    return client.post(f"/items/{item}/reopen",
                       json={"owner": owner, "reason": reason},
                       headers=_h(token))


def _body_leaks_no_crew(body: dict) -> bool:
    text = json.dumps(body)
    return "crew-alpha" not in text and "crew-bravo" not in text


# --- release: refused across crews, even naming the real holder ------------


def test_release_is_refused_across_crews_even_with_the_right_owner_name(client):
    assert _co(client, "lone1", owner="session-1", token=CREW_A1).status_code == 200
    r = _release(client, "lone1", owner="session-1", token=CREW_B)
    assert r.status_code == 409
    body = r.json()
    assert body["reason"] == "crew_mismatch"
    assert _body_leaks_no_crew(body)


def test_release_of_a_lease_only_hold_is_refused_across_crews(client):
    """The `open`-state branch: a lease-only hold with no claim to match it,
    scoped the same way the claimed branch is."""
    conn = sqlite3.connect(store.DB_PATH)
    conn.execute(
        "INSERT INTO issue_leases (id, root_id, taken_for, holder, "
        "holder_crew, taken_at, heartbeat_at) VALUES "
        "('lease_x','lone1','lone1','session-1','crew-alpha',?,?)",
        (_ts(minutes=1), _ts(minutes=1)))
    conn.commit()
    conn.close()
    r = _release(client, "lone1", owner="session-1", token=CREW_B)
    assert r.status_code == 409
    body = r.json()
    assert body["reason"] == "crew_mismatch"
    assert _body_leaks_no_crew(body)


def test_releasing_its_own_crews_hold_succeeds(client):
    assert _co(client, "lone1", owner="session-1", token=CREW_A1).status_code == 200
    r = _release(client, "lone1", owner="session-1", token=CREW_A1)
    assert r.status_code == 200, r.text


def test_a_second_credential_of_the_same_crew_may_release(client):
    """The accepted gap (CQ·10): two sessions of one crew are not told
    apart, pinned here so closing it later is a deliberate change."""
    assert _co(client, "lone1", owner="session-1", token=CREW_A1).status_code == 200
    r = _release(client, "lone1", owner="session-1", token=CREW_A2)
    assert r.status_code == 200, r.text


def test_an_uncrewed_credential_is_refused_on_a_crewed_claim(client):
    assert _co(client, "lone1", owner="session-1", token=CREW_A1).status_code == 200
    r = _release(client, "lone1", owner="session-1", token=UNCREWED)
    assert r.status_code == 409
    assert r.json()["reason"] == "crew_mismatch"


def test_an_uncrewed_claim_is_releasable_by_anyone_as_today(client):
    """Items held before a crew exists, and anything taken through an
    uncrewed credential, keep today's behaviour exactly."""
    assert _co(client, "lone1", owner="session-1", token=UNCREWED).status_code == 200
    r = _release(client, "lone1", owner="session-1", token=CREW_B)
    assert r.status_code == 200, r.text


# --- resolve: refused across crews, even naming the real holder ------------


def test_resolve_is_refused_across_crews_even_with_the_right_owner_name(client):
    assert _co(client, "lone1", owner="session-1", token=CREW_A1).status_code == 200
    r = _resolve(client, "lone1", owner="session-1", token=CREW_B)
    assert r.status_code == 409
    body = r.json()
    assert body["reason"] == "crew_mismatch"
    assert _body_leaks_no_crew(body)


def test_resolving_its_own_crews_hold_succeeds(client):
    assert _co(client, "lone1", owner="session-1", token=CREW_A1).status_code == 200
    r = _resolve(client, "lone1", owner="session-1", token=CREW_A1)
    assert r.status_code == 200, r.text


def test_a_second_credential_of_the_same_crew_may_resolve(client):
    assert _co(client, "lone1", owner="session-1", token=CREW_A1).status_code == 200
    r = _resolve(client, "lone1", owner="session-1", token=CREW_A2)
    assert r.status_code == 200, r.text


def test_an_uncrewed_credential_is_refused_on_a_crewed_claim_at_resolve(client):
    assert _co(client, "lone1", owner="session-1", token=CREW_A1).status_code == 200
    r = _resolve(client, "lone1", owner="session-1", token=UNCREWED)
    assert r.status_code == 409
    assert r.json()["reason"] == "crew_mismatch"


# --- reopen: refused across crews, off the item's own claimed_by_crew ------


def test_reopen_is_refused_across_crews_even_with_the_right_owner_name(client):
    """The lease is already gone by reopen time — `/resolve` released it —
    so this has to read `claimed_by_crew` off the item itself, which a
    resolve does not clear."""
    assert _co(client, "lone1", owner="session-1", token=CREW_A1).status_code == 200
    assert _resolve(client, "lone1", owner="session-1",
                    token=CREW_A1).status_code == 200
    r = _reopen(client, "lone1", owner="session-1", token=CREW_B)
    assert r.status_code == 409
    body = r.json()
    assert body["reason"] == "crew_mismatch"
    assert _body_leaks_no_crew(body)


def test_reopening_its_own_crews_resolve_succeeds(client):
    assert _co(client, "lone1", owner="session-1", token=CREW_A1).status_code == 200
    assert _resolve(client, "lone1", owner="session-1",
                    token=CREW_A1).status_code == 200
    r = _reopen(client, "lone1", owner="session-1", token=CREW_A1)
    assert r.status_code == 200, r.text


def test_a_second_credential_of_the_same_crew_may_reopen(client):
    assert _co(client, "lone1", owner="session-1", token=CREW_A1).status_code == 200
    assert _resolve(client, "lone1", owner="session-1",
                    token=CREW_A1).status_code == 200
    r = _reopen(client, "lone1", owner="session-1", token=CREW_A2)
    assert r.status_code == 200, r.text


# --- checkout and claim: refused by a sibling's live, differently-crewed ---
# --- epic lease, even naming the real holder -------------------------------


def test_checkout_of_a_sibling_is_refused_across_crews_even_with_the_right_owner_name(
        client):
    """`task1` and `task2` share `epic1`'s lease. B checking out `task2`
    with A's own `owner` string would otherwise hit `lease.take`'s refresh
    branch — the one case a plain holder-string conflict scan cannot catch,
    because it treats a matching string as the caller's own."""
    assert _co(client, "task1", owner="session-1", token=CREW_A1).status_code == 200
    r = _co(client, "task2", owner="session-1", token=CREW_B)
    assert r.status_code == 409
    body = r.json()
    assert body["reason"] == "crew_mismatch"
    assert _body_leaks_no_crew(body)


def test_claim_of_a_sibling_is_refused_across_crews_even_with_the_right_owner_name(
        client):
    assert _co(client, "task1", owner="session-1", token=CREW_A1).status_code == 200
    r = _claim(client, "task2", owner="session-1", token=CREW_B)
    assert r.status_code == 409
    body = r.json()
    assert body["reason"] == "crew_mismatch"
    assert _body_leaks_no_crew(body)


def test_checkout_of_a_sibling_by_the_same_crew_succeeds(client):
    assert _co(client, "task1", owner="session-1", token=CREW_A1).status_code == 200
    r = _co(client, "task2", owner="session-1", token=CREW_A2)
    assert r.status_code == 200, r.text


# --- revision: the lease-authorized text edit (reviewer, PR 287 round 1) ----


def _revise(client, item, owner="session-1", token=CREW_A1, title="New title"):
    return client.post(f"/items/{item}/revision",
                       json={"owner": owner, "title": title}, headers=_h(token))


def test_revision_is_refused_across_crews_even_with_the_right_owner_name(client):
    assert _co(client, "lone1", owner="session-1", token=CREW_A1).status_code == 200
    r = _revise(client, "lone1", owner="session-1", token=CREW_B)
    assert r.status_code == 409
    body = r.json()
    assert body["reason"] == "crew_mismatch"
    assert _body_leaks_no_crew(body)
    conn = sqlite3.connect(store.DB_PATH)
    title = conn.execute("SELECT title FROM issue_items WHERE id = 'lone1'"
                         ).fetchone()[0]
    conn.close()
    assert title == "Parentless one"


def test_revision_by_the_holding_crew_succeeds(client):
    assert _co(client, "lone1", owner="session-1", token=CREW_A1).status_code == 200
    r = _revise(client, "lone1", owner="session-1", token=CREW_A2)
    assert r.status_code == 200, r.text


# --- the holder match is crew-aware everywhere (security-reviewer, PR 287 ---
# --- round 1): the conflict scan, and re-parenting -------------------------


def _live_leases():
    conn = sqlite3.connect(store.DB_PATH)
    rows = conn.execute("SELECT root_id, holder_crew FROM issue_leases "
                        "WHERE released_at IS NULL ORDER BY root_id").fetchall()
    conn.close()
    return rows


def test_a_containing_epic_is_refused_across_crews_even_with_the_right_owner_name(
        client):
    """Crew B checking out `top` with A's name used to skip A's lease on `sub`
    in the downward scan and plant an overlapping lease above it."""
    assert _co(client, "sub", owner="session-1", token=CREW_A1).status_code == 200
    r = _co(client, "top", owner="session-1", token=CREW_B)
    assert r.status_code == 409
    assert r.json()["reason"] == "held"
    assert _live_leases() == [("sub", "crew-alpha")]


def test_a_containing_epic_is_taken_by_the_same_crew(client):
    assert _co(client, "sub", owner="session-1", token=CREW_A1).status_code == 200
    r = _co(client, "top", owner="session-1", token=CREW_A2)
    assert r.status_code == 200, r.text


def test_a_re_parent_into_another_crews_fence_is_refused_with_its_name(client):
    assert _co(client, "sub", owner="session-1", token=CREW_A1).status_code == 200
    assert _co(client, "lone2", owner="session-1", token=CREW_B).status_code == 200
    r = client.post("/items/lone2/correct",
                    json={"owner": "session-1", "parent_id": "sub"},
                    headers=_h(CREW_B))
    assert r.status_code == 409
    assert r.json()["reason"] == "held"
    conn = sqlite3.connect(store.DB_PATH)
    parent = conn.execute("SELECT parent_id FROM issue_items WHERE id = 'lone2'"
                          ).fetchone()[0]
    conn.close()
    assert parent is None


def test_a_re_parent_carrying_another_crews_lease_into_a_fence_is_refused(client):
    """The downward half of /correct's re-parent check (reviewer, PR 287 round
    2). The two overlapping leases are planted, since the conflict scan now
    refuses to create them; a vault written before this change can hold them."""
    conn = sqlite3.connect(store.DB_PATH)
    conn.executemany(
        "INSERT INTO issue_leases (id, root_id, taken_for, holder, holder_crew, "
        "taken_at, heartbeat_at) VALUES (?,?,?,?,?,?,?)",
        [("lease_top", "top", "top", "session-1", "crew-bravo",
          _ts(minutes=1), _ts(minutes=1)),
         ("lease_sub", "sub", "sub", "session-1", "crew-alpha",
          _ts(minutes=1), _ts(minutes=1))])
    conn.commit()
    conn.close()
    assert _co(client, "epic1", owner="session-1", token=CREW_B).status_code == 200
    r = client.post("/items/top/correct",
                    json={"owner": "session-1", "parent_id": "epic1"},
                    headers=_h(CREW_B))
    assert r.status_code == 409, r.text
    conn = sqlite3.connect(store.DB_PATH)
    parent = conn.execute("SELECT parent_id FROM issue_items WHERE id = 'top'"
                          ).fetchone()[0]
    conn.close()
    assert parent is None


def test_held_by_needs_the_crew_on_a_crewed_lease():
    row = {"holder": "session-1", "holder_crew": "crew-alpha"}
    assert lease.held_by(row, "session-1", crew="crew-alpha")
    assert not lease.held_by(row, "session-1", crew="crew-bravo")
    assert not lease.held_by(row, "session-1")
    assert lease.held_by(row, "session-1", crew_exempt=True)
    uncrewed = {"holder": "session-1", "holder_crew": None}
    assert lease.held_by(uncrewed, "session-1")
    assert lease.held_by(uncrewed, "session-1", crew="crew-bravo")


# --- an exempt refresh keeps the recorded crew (reviewer, PR 287 round 1) ----


def _lease_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE issue_items (id TEXT PRIMARY KEY, type TEXT, "
                 "parent_id TEXT)")
    conn.execute("CREATE TABLE issue_leases (id TEXT PRIMARY KEY, "
                 "root_id TEXT, taken_for TEXT, holder TEXT, holder_crew TEXT, "
                 "run_id TEXT, taken_at TEXT, heartbeat_at TEXT, "
                 "released_at TEXT, release_reason TEXT, released_by TEXT, "
                 "note TEXT)")
    conn.execute("CREATE UNIQUE INDEX ix_live_root ON issue_leases (root_id) "
                 "WHERE released_at IS NULL")
    conn.execute("INSERT INTO issue_items (id, type, parent_id) VALUES "
                 "('lone', 'task', NULL)")
    conn.commit()
    return conn, conn.execute("SELECT * FROM issue_items WHERE id = 'lone'"
                              ).fetchone()


def test_an_exempt_refresh_keeps_the_recorded_crew():
    """An approved client carries no crew. Stamping its crew on refresh wrote
    NULL over a crewed lease, after which any uncrewed credential typing the
    holder's name passed the crew check."""
    conn, item = _lease_db()
    lease.take(conn, item, holder="session-1", actor="a", holder_crew="crew-alpha")
    conn.commit()
    held = lease.take(conn, item, holder="session-1", actor="deck",
                      holder_crew=None, crew_exempt=True)
    conn.commit()
    assert held["holder_crew"] == "crew-alpha"
    with pytest.raises(lease.CrewMismatch):
        lease.take(conn, item, holder="session-1", actor="x", holder_crew=None)


# --- the human force path still breaks a crewed lease -----------------------
#
# Exercised at the domain layer because the route cannot reach it: `force` is
# refused outright for every credential, `approved_client` included, since
# this service issues no human identity over HTTP today
# (`test_checkout.py::test_a_live_lease_cannot_be_broken_because_force_is_refused`
# pins that refusal). `lease.take`'s own override loop, and
# `lease.release_for`'s `force` branch, are where ADR-0026's reserved act
# would run once a human-attested caller exists, and neither reads
# `holder_crew` at all — the crew check this change adds lives only on the
# REFRESH branch `take` takes for a caller re-presenting the same `owner`
# string, which is exactly what `crew_exempt=True` (an approved-client
# caller) skips.


def test_take_with_force_overrides_a_crewed_lease_regardless_of_crew():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE issue_items (id TEXT PRIMARY KEY, type TEXT, "
                "parent_id TEXT)")
    conn.execute("CREATE TABLE issue_leases (id TEXT PRIMARY KEY, "
                "root_id TEXT, taken_for TEXT, holder TEXT, holder_crew TEXT, "
                "run_id TEXT, taken_at TEXT, heartbeat_at TEXT, "
                "released_at TEXT, release_reason TEXT, released_by TEXT, "
                "note TEXT)")
    conn.execute("CREATE UNIQUE INDEX ix_live_root ON issue_leases (root_id) "
                "WHERE released_at IS NULL")
    conn.execute("INSERT INTO issue_items (id, type, parent_id) VALUES "
                "('lone', 'task', NULL)")
    conn.commit()
    item = conn.execute("SELECT * FROM issue_items WHERE id = 'lone'").fetchone()

    lease.take(conn, item, holder="session-1", actor="a", holder_crew="crew-alpha")
    conn.commit()

    held = lease.take(conn, item, holder="human-override", actor="human",
                      holder_crew=None, crew_exempt=True, force=True)
    conn.commit()
    assert held["holder"] == "human-override"
    assert held["release_reason"] is None


def test_release_for_with_force_overrides_a_crewed_lease_regardless_of_crew():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE issue_items (id TEXT PRIMARY KEY, type TEXT, "
                "parent_id TEXT)")
    conn.execute("CREATE TABLE issue_leases (id TEXT PRIMARY KEY, "
                "root_id TEXT, taken_for TEXT, holder TEXT, holder_crew TEXT, "
                "run_id TEXT, taken_at TEXT, heartbeat_at TEXT, "
                "released_at TEXT, release_reason TEXT, released_by TEXT, "
                "note TEXT)")
    conn.execute("CREATE UNIQUE INDEX ix_live_root ON issue_leases (root_id) "
                "WHERE released_at IS NULL")
    conn.execute("INSERT INTO issue_items (id, type, parent_id) VALUES "
                "('lone', 'task', NULL)")
    conn.commit()
    item = conn.execute("SELECT * FROM issue_items WHERE id = 'lone'").fetchone()

    lease.take(conn, item, holder="session-1", actor="a", holder_crew="crew-alpha")
    conn.commit()

    released = lease.release_for(conn, item, holder="someone-else",
                                 actor="human", force=True)
    conn.commit()
    assert released is not None
    assert released["release_reason"] == lease.LEASE_RELEASED
