"""POST /tokens/revoke, GET /tokens/{card_id}
(library/specs/library-card-credential-and-revocation-plan §4, tests 15-18).

Tests 15, 16, 17 and 18 from the plan's §7, the four this pull request
builds. Cards are inserted directly into `library_cards` rather than
minted through a redeemed slip — the behaviour under test is the revoke
and read routes' own logic, which starts from an existing row, and
`test_token_redeem.py` already covers how a row gets there.

**Test 17's "a card trying to revoke itself" half is not built here.** No
route accepts a library card as a bearer credential yet — `identity.
resolve` does not read `library_cards` until slice 3
(issue_01M12NE155ABV0K2K50P6CBWKJ) — so there is no way to construct a
request authenticated as a card at all. That half belongs with slice 3's
own tests. The half this file builds — a caller lacking `token.revoke` or
`token.read` gets 403 — is test 17's other half and does not depend on a
card credential existing.
"""
from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

import app as libapp
import identity
import library_cards
import protocol
import store

REVOKER = "revoker-token"
READER = "reader-token"
PLAIN_AGENT = "plain-agent-token"
OTHER_SCOPE = "other-scope-token"

H = {protocol.HEADER: str(protocol.CURRENT)}


def _headers(token):
    return {**H, "Authorization": f"Bearer {token}"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = tmp_path / "control.db"
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE library_cards (
            id TEXT PRIMARY KEY, actor TEXT, product TEXT, item_id TEXT,
            slip_id TEXT UNIQUE, protocol_version INTEGER, state TEXT,
            issued_at TEXT, revoked_at TEXT, secret_sha256 TEXT UNIQUE,
            bound_machine TEXT, crew TEXT, profile TEXT,
            revoked_cause TEXT, revoked_by TEXT, revocation_detail TEXT)
    """)
    conn.commit()
    conn.close()

    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        REVOKER: {"name": "bosun-revoker", "products": ["fennick"],
                  "grants": ["token.revoke"]},
        READER: {"name": "bosun-reader", "products": ["fennick"],
                 "grants": ["token.read"]},
        PLAIN_AGENT: {"name": "plain-agent", "products": ["fennick"]},
        OTHER_SCOPE: {"name": "marlow-agent", "products": ["marlow"],
                      "grants": ["token.revoke", "token.read"]},
    }))
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    return TestClient(libapp.app)


_SEQ = 0
SECRET_HASH_PREFIX = "stored-secret-hash-"
MACHINE = "bound-machine-name"


def _insert_card(db, *, product="fennick"):
    """A fresh active card row, with a unique id/slip_id/hash per call."""
    global _SEQ
    _SEQ += 1
    card_id = f"card_test{_SEQ}"
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO library_cards (id, actor, product, item_id, slip_id, "
        "protocol_version, state, issued_at, secret_sha256, bound_machine, "
        "crew) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (card_id, "fennick-agent", product, "task_a", f"slip_test{_SEQ}",
         protocol.CURRENT, library_cards.STATE_ACTIVE,
         "2026-11-01 00:00:00", f"{SECRET_HASH_PREFIX}{_SEQ}", MACHINE,
         "fennick-crew"))
    conn.commit()
    conn.close()
    return card_id


def _row(db, card_id):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM library_cards WHERE id = ?",
                       (card_id,)).fetchone()
    conn.close()
    return row


def _revoke(client, card_id, *, token=REVOKER, reason="the run ended"):
    return client.post("/tokens/revoke", headers=_headers(token),
                       json={"card_id": card_id, "reason": reason})


def _read(client, card_id, *, token=READER):
    return client.get(f"/tokens/{card_id}", headers=_headers(token))


# --- test 15: revoking twice -------------------------------------------------


def test_revoking_twice_both_answer_200_the_second_unchanged(client):
    card_id = _insert_card(store.DB_PATH)

    first = _revoke(client, card_id)
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["revoked"] is True
    assert first_body["already_revoked"] is False
    assert first_body["card"]["state"] == "revoked"
    assert first_body["card"]["revoked_cause"] == library_cards.CAUSE_REQUESTED
    assert first_body["card"]["revoked_by"] == "bosun-revoker"
    assert first_body["card"]["revocation_detail"] == {"reason": "the run ended"}
    assert first_body["card"]["revoked_at"] is not None

    second = _revoke(client, card_id, reason="a different reason")
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert second_body["revoked"] is True
    assert second_body["already_revoked"] is True
    # The first cause stands: the second call's own reason never lands.
    assert second_body["card"] == first_body["card"]


# --- test 16: scope and an invented id answer the same way ------------------


def test_out_of_scope_and_an_invented_id_get_one_404(client):
    card_id = _insert_card(store.DB_PATH, product="fennick")

    out_of_scope = _revoke(client, card_id, token=OTHER_SCOPE)
    invented = _revoke(client, "card_invented", token=OTHER_SCOPE)

    assert out_of_scope.status_code == 404
    assert invented.status_code == 404
    assert out_of_scope.json() == invented.json()
    assert _row(store.DB_PATH, card_id)["state"] == "active"


def test_reading_out_of_scope_and_an_invented_id_get_the_revoke_routes_404(client):
    card_id = _insert_card(store.DB_PATH, product="fennick")

    out_of_scope = _read(client, card_id, token=OTHER_SCOPE)
    invented = _read(client, "card_invented", token=OTHER_SCOPE)
    revoke_404 = _revoke(client, "card_invented", token=OTHER_SCOPE)

    assert out_of_scope.status_code == 404
    assert invented.status_code == 404
    assert out_of_scope.json() == invented.json() == revoke_404.json()


# --- GET /tokens/{card_id}: what the read route serves ----------------------


def test_reading_an_active_card_serves_null_revocation_fields(client):
    card_id = _insert_card(store.DB_PATH)

    r = _read(client, card_id)

    assert r.status_code == 200, r.text
    assert r.json() == {
        "card_id": card_id,
        "actor": "fennick-agent",
        "product": "fennick",
        "item_id": "task_a",
        "protocol_version": protocol.CURRENT,
        "state": "active",
        "issued_at": "2026-11-01 00:00:00",
        "crew": "fennick-crew",
        "revoked_at": None,
        "revoked_cause": None,
        "revoked_by": None,
        "revocation_detail": None,
    }


def test_reading_a_revoked_card_serves_what_revoking_it_answered(client):
    card_id = _insert_card(store.DB_PATH)
    revoked = _revoke(client, card_id)
    assert revoked.status_code == 200, revoked.text

    r = _read(client, card_id)

    assert r.status_code == 200, r.text
    assert r.json() == revoked.json()["card"]
    assert r.json()["revoked_cause"] == library_cards.CAUSE_REQUESTED
    assert r.json()["revocation_detail"] == {"reason": "the run ended"}


def test_neither_route_serves_the_secret_hash_or_the_machine(client):
    card_id = _insert_card(store.DB_PATH)
    assert _row(store.DB_PATH, card_id)["bound_machine"] == MACHINE

    for r in (_read(client, card_id), _revoke(client, card_id)):
        assert r.status_code == 200, r.text
        assert SECRET_HASH_PREFIX not in r.text
        assert MACHINE not in r.text


# --- test 17: the capability gate, on both routes ---------------------------


def test_a_caller_without_token_revoke_is_refused(client):
    card_id = _insert_card(store.DB_PATH)
    r = _revoke(client, card_id, token=PLAIN_AGENT)
    assert r.status_code == 403
    assert "token.revoke" in r.json()["error"]
    assert _row(store.DB_PATH, card_id)["state"] == "active"


def test_a_caller_without_token_read_is_refused(client):
    card_id = _insert_card(store.DB_PATH)
    r = client.get(f"/tokens/{card_id}", headers=_headers(PLAIN_AGENT))
    assert r.status_code == 403
    assert "token.read" in r.json()["error"]


# --- test 18: a caller cannot claim the hard-cap cause ----------------------


def test_a_body_naming_revoked_cause_is_refused(client):
    card_id = _insert_card(store.DB_PATH)
    r = client.post(
        "/tokens/revoke", headers=_headers(REVOKER),
        json={"card_id": card_id, "reason": "x",
              "revoked_cause": library_cards.CAUSE_HARD_CAP})
    assert r.status_code == 400
    assert "revoked_cause" in r.json()["error"]
    assert _row(store.DB_PATH, card_id)["state"] == "active"
