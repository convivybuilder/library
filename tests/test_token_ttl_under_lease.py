"""A call slip expires on its own TTL while the checkout lock it came from is
still live (issue_01M35RB7EKSS823B63GMC8MXZP, clause 5 of the Phase 5 epic
issue_01KZBWEYHJ1QCKBR5BTM6G6DQ9).

**ONE CHECKOUT, TWO EXPIRIES, AND THEY MUST NOT BE THE SAME CLOCK.** The slip's
`expires_at` is fixed at mint, `call_slips.TTL_SECONDS` after it. The lease's
end is re-derived from `taken_at` and `heartbeat_at` against the grace tunable
(`lease.expires_at`). A slip that borrowed the lease's life would stay
redeemable for hours after it leaked, which is the window the TTL bounds.

Both halves run through the real routes: `POST /items/{id}/checkout` mints the
slip and takes the lease in one transaction, and `POST /tokens/redeem` refuses
it over the mutual-TLS listener with the transport cross-check unstubbed. Only
the clock moves, by replacing the `datetime` `call_slips` reads its "now" from,
so the stored rows are exactly what checkout wrote.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import types

import pytest
from fastapi.testclient import TestClient

import app as libapp
import call_slips
import identity
import lease
import protocol
import store
import tls_ca
from test_token_redeem import MACHINE, _OverMTLS, _bind, _der, _register, _uri

AGENT = "agent-token-vivy"
# Past the slip's TTL by a margin, and nowhere near the lease's grace.
LATER = dt.timedelta(seconds=call_slips.TTL_SECONDS + 60)


def _ts(**delta):
    return (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
            - dt.timedelta(**delta)).isoformat(" ")


@pytest.fixture
def client(tmp_path, monkeypatch, vector_index, ledger_tables):
    """The checkout schema from `test_checkout.py`, plus the redemption and
    machine-identity tables from `test_token_redeem.py`, and a client on the
    mutual-TLS listener as MACHINE with AGENT bound to it."""
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
            id TEXT PRIMARY KEY, item_id TEXT, product TEXT,
            claim_owner_label TEXT, issued_at TEXT, expires_at TEXT,
            redeemed_at TEXT, grant_json TEXT);
        CREATE TABLE library_cards (
            id TEXT PRIMARY KEY, actor TEXT, product TEXT, item_id TEXT,
            slip_id TEXT UNIQUE, protocol_version INTEGER, state TEXT,
            issued_at TEXT, revoked_at TEXT);
        CREATE TABLE machine_certificates (
            id INTEGER PRIMARY KEY,
            machine TEXT NOT NULL,
            fingerprint_sha256 TEXT NOT NULL UNIQUE,
            not_after TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'revoked')),
            revoked_at TEXT,
            note TEXT,
            created_at TEXT NOT NULL,
            custody TEXT NOT NULL CHECK (custody IN ('hardware', 'file'))
        );
        CREATE TABLE credential_bindings (
            id INTEGER PRIMARY KEY,
            credential_sha256 TEXT NOT NULL UNIQUE,
            machine TEXT NOT NULL,
            note TEXT,
            created_at TEXT NOT NULL
        );
    """)
    conn.executemany(
        "INSERT INTO issue_items (id, product, title, type, state, needs, "
        "created_by_kind, parent_id, created_at, refs) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [("task_a", "vivy", "Task A", None, "open", "agent", "agent", None,
          _ts(days=1), "{}"),
         ("task_b", "vivy", "Task B", None, "open", "agent", "agent", None,
          _ts(days=1), "{}")])
    conn.commit()
    conn.close()

    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        AGENT: {"name": "vivy-agent", "products": ["vivy"]}}))
    vector_index(db)
    ledger_tables(db)
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    monkeypatch.setattr(lease, "TUNABLES", tmp_path / "absent.json")

    ca_cert, ca_key = tls_ca.make_ca(tmp_path)
    cert, _ = tls_ca.make_cert(tmp_path, MACHINE, ca_cert, ca_key,
                               uri_names=(_uri(MACHINE),))
    _register(db, MACHINE, _der(cert))
    _bind(db, AGENT, MACHINE)
    return TestClient(_OverMTLS(libapp.app, _der(cert), _uri(MACHINE)))


def _h():
    return {"Authorization": f"Bearer {AGENT}",
            protocol.HEADER: str(protocol.CURRENT)}


def _checkout(client, item):
    r = client.post(f"/items/{item}/checkout", json={"owner": "session-1"},
                    headers=_h())
    assert r.status_code == 200, r.json()
    return r.json()["slip_id"]


def _redeem(client, slip_id):
    return client.post("/tokens/redeem", json={"slip_id": slip_id},
                       headers=_h())


def _db():
    conn = sqlite3.connect(store.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _shift_slip_clock(monkeypatch, by):
    """`call_slips` reads "now" as `_dt.datetime.now(...)`. Hand it a
    `datetime` whose `now` runs `by` ahead, and leave every other name the
    module uses from `datetime` as it was."""
    class _Later(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime.now(tz) + by
    monkeypatch.setattr(call_slips, "_dt", types.SimpleNamespace(
        datetime=_Later, timedelta=dt.timedelta, timezone=dt.timezone))


def test_the_margin_sits_between_the_two_expiries():
    """The test below proves nothing unless LATER is past the slip's TTL and
    inside the lease's grace. Read from the code, so a change to either
    number fails here rather than quietly making the test vacuous."""
    assert LATER > dt.timedelta(seconds=call_slips.TTL_SECONDS)
    assert LATER < lease.DEFAULT_GRACE


def test_a_fresh_slip_from_a_checkout_redeems(client):
    """The positive control: the same checkout, redeemed at once, issues a
    card. Without it, a refusal below could come from the fixture rather
    than from the TTL."""
    r = _redeem(client, _checkout(client, "task_b"))
    assert r.status_code == 200, r.json()
    assert r.json()["card_id"].startswith("card_")


def test_the_slip_expires_while_its_checkout_lock_is_still_live(
        client, monkeypatch):
    slip_id = _checkout(client, "task_a")
    later = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + LATER

    # The lock reads live at the instant the slip is refused.
    conn = _db()
    item = conn.execute(
        "SELECT * FROM issue_items WHERE id = 'task_a'").fetchone()
    held = conn.execute(
        "SELECT * FROM issue_leases WHERE taken_for = 'task_a' "
        "AND released_at IS NULL").fetchone()
    expired, ends = lease.claim_status(conn, item, now=later)
    conn.close()
    assert held is not None, "the checkout took no lease"
    assert not expired, "the lease lapsed with the slip, which is one clock"
    assert ends > later

    _shift_slip_clock(monkeypatch, LATER)
    r = _redeem(client, slip_id)
    assert r.status_code == 409, r.json()
    assert "expired" in r.json()["error"]

    conn = _db()
    slip = conn.execute("SELECT * FROM call_slips WHERE id = ?",
                        (slip_id,)).fetchone()
    cards = conn.execute("SELECT * FROM library_cards").fetchall()
    conn.close()
    assert slip["redeemed_at"] is None
    assert cards == []
