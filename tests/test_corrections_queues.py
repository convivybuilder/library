"""`GET /corrections-queues` and `GET /corrections-queues/{id}`, and the
lease root once a leaf's parent can name a queue rather than an epic
(issue_01M12NE0RFPJY12TT8CF1169V1, E18·2 — library/specs/one-home-per-
work-item, PR 1).

**The two routes are scoped exactly like the item routes they sit beside**:
`GET /corrections-queues` takes the same product filter and the same
`_scope_clause` bound `GET /items` does, and `GET /corrections-queues/{id}`
answers an out-of-scope id with the identical 404 a missing one gets, so
neither discloses which it was.

**The lease test is exercised at the domain layer**, the way
`test_checkout.py`'s force-override test is, because nothing in this PR
ever writes a queue id into `issue_items.parent_id` through a route — the
backfill that does is PR 2's, gated on its own soak window. What has to
hold here is narrower and checkable without it: a lease taken while an
item's parent is NULL still resolves to the same root once that parent
names a queue, which is the shape every row looks like the instant PR 2's
backfill runs.
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
            severity TEXT, state TEXT, claimed_by TEXT, claimed_at TEXT,
            resolution TEXT, resolved_by TEXT, resolved_at TEXT, outcome_note TEXT,
            once_key TEXT UNIQUE, created_by TEXT, created_at TEXT, run_id TEXT,
            type TEXT, created_by_kind TEXT, needs TEXT, park_reason TEXT,
            unpark_condition TEXT, parked_at TEXT, embedded_at TEXT,
            parent_id TEXT, topics TEXT, maturity TEXT, specification TEXT,
            definition_of_done TEXT, prerequisites TEXT,
            model_choice TEXT, model_reason TEXT, model_set_by_client TEXT,
            model_acting_human TEXT, model_set_at TEXT, found_during TEXT, item_key TEXT);
        CREATE TABLE corrections_queues (
            id TEXT PRIMARY KEY, product TEXT UNIQUE, created_at TEXT);
    """)
    conn.executemany(
        "INSERT INTO corrections_queues (id, product, created_at) "
        "VALUES (?,?,?)",
        [("cq_vivy", "vivy", "2026-08-01 00:00:00.000000"),
         ("cq_constance", "constance", "2026-08-02 00:00:00.000000")])
    conn.commit()
    conn.close()

    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        AGENT: {"name": "vivy-agent", "products": ["vivy"]},
        WIDE: {"name": "wide-agent", "products": ["*"]},
    }))
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    return TestClient(libapp.app)


def _h(token=AGENT):
    return {"Authorization": f"Bearer {token}",
            protocol.HEADER: str(protocol.CURRENT)}


# --- GET /corrections-queues -----------------------------------------------


def test_list_is_scoped_to_the_callers_products(client):
    r = client.get("/corrections-queues", headers=_h(AGENT))
    assert r.status_code == 200, r.text
    products = {q["product"] for q in r.json()["corrections_queues"]}
    assert products == {"vivy"}


def test_a_wide_credential_sees_every_queue(client):
    r = client.get("/corrections-queues", headers=_h(WIDE))
    assert r.status_code == 200
    products = {q["product"] for q in r.json()["corrections_queues"]}
    assert products == {"vivy", "constance"}
    assert r.json()["count"] == 2


def test_the_product_filter_narrows_within_scope(client):
    r = client.get("/corrections-queues?product=constance", headers=_h(WIDE))
    assert r.status_code == 200
    assert [q["product"] for q in r.json()["corrections_queues"]] == ["constance"]


def test_no_credential_is_refused(client):
    r = client.get("/corrections-queues",
                   headers={protocol.HEADER: str(protocol.CURRENT)})
    assert r.status_code == 401


# --- GET /corrections-queues/{id} -------------------------------------------


def test_get_returns_the_queue(client):
    r = client.get("/corrections-queues/cq_vivy", headers=_h(AGENT))
    assert r.status_code == 200, r.text
    assert r.json()["id"] == "cq_vivy"
    assert r.json()["product"] == "vivy"
    assert r.json()["created_at"].endswith("Z")


def test_an_out_of_scope_queue_reads_exactly_like_a_missing_one(client):
    """Same status, same body — a caller scoped to vivy must not learn that
    `cq_constance` exists."""
    missing = client.get("/corrections-queues/cq_nope", headers=_h(AGENT))
    foreign = client.get("/corrections-queues/cq_constance", headers=_h(AGENT))
    assert missing.status_code == foreign.status_code == 404
    assert missing.json() == foreign.json()


# --- the lease root, once a leaf's parent can name a queue ------------------


def _lease_db(tmp_path):
    db = tmp_path / "lease.db"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE issue_items (
            id TEXT PRIMARY KEY, product TEXT, type TEXT, state TEXT,
            claimed_by TEXT, claimed_at TEXT, parent_id TEXT);
        CREATE TABLE issue_leases (
            id TEXT PRIMARY KEY, root_id TEXT, taken_for TEXT, holder TEXT,
            run_id TEXT, taken_at TEXT, heartbeat_at TEXT, released_at TEXT,
            release_reason TEXT, released_by TEXT, note TEXT);
        CREATE UNIQUE INDEX ix_live_root ON issue_leases (root_id)
            WHERE released_at IS NULL;
    """)
    return conn


def _row(conn, item_id):
    return conn.execute("SELECT * FROM issue_items WHERE id = ?",
                       (item_id,)).fetchone()


def test_two_corrections_in_the_same_product_lease_independently(tmp_path):
    """Neither is rooted on the other, or on anything shared, so two holders
    checking out two corrections in the same product at once both succeed —
    the same independence a parentless row has always had."""
    conn = _lease_db(tmp_path)
    conn.executemany(
        "INSERT INTO issue_items (id, product, type, state, claimed_by, "
        "claimed_at, parent_id) VALUES (?,?,?,?,?,?,?)",
        [("c1", "vivy", "bug", "claimed", "h1", _ts(minutes=1), None),
         ("c2", "vivy", "bug", "claimed", "h2", _ts(minutes=1), None)])
    conn.commit()
    one, two = _row(conn, "c1"), _row(conn, "c2")
    lease.take(conn, one, holder="h1", actor="a")
    lease.take(conn, two, holder="h2", actor="a")
    conn.commit()
    live = conn.execute(
        "SELECT root_id FROM issue_leases WHERE released_at IS NULL").fetchall()
    assert {r["root_id"] for r in live} == {"c1", "c2"}


def test_a_lease_taken_while_the_parent_was_null_still_reads_as_claimed_once_the_parent_is_a_queue(tmp_path):
    """The shape every correction looks like the instant PR 2's backfill
    runs: `parent_id` moves from NULL to a queue id, with no other column
    touched and the lease row never rewritten. `root_id_for` must resolve
    the SAME root before and after, because a queue is not a lease root any
    more than NULL is."""
    conn = _lease_db(tmp_path)
    conn.execute(
        "INSERT INTO issue_items (id, product, type, state, claimed_by, "
        "claimed_at, parent_id) VALUES (?,?,?,?,?,?,?)",
        ("c1", "vivy", "bug", "claimed", "h1", _ts(minutes=1), None))
    conn.commit()
    before = _row(conn, "c1")
    lease.take(conn, before, holder="h1", actor="a")
    conn.commit()
    expired_before, _ = lease.claim_status(conn, before)
    assert expired_before is False

    # The backfill: parent_id alone moves, nothing else — simulated here as
    # PR 2's own migration will do it, without re-touching the lease.
    conn.execute("UPDATE issue_items SET parent_id = ? WHERE id = ?",
                 ("cq_vivy", "c1"))
    conn.commit()
    after = _row(conn, "c1")
    assert lease.root_id_for(conn, before) == lease.root_id_for(conn, after)

    expired_after, _ = lease.claim_status(conn, after)
    assert expired_after is False
    live = conn.execute(
        "SELECT * FROM issue_leases WHERE root_id = ? AND released_at IS NULL",
        ("c1",)).fetchone()
    assert live is not None and live["holder"] == "h1"
