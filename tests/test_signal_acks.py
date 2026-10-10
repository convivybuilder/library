"""ADR-0038's approved-client identity, exercised through the two ported
signal-ack routes — the first callers of `identity.attested_human`.

**These are HUMAN-RESERVED**, and the whole point of the decision under test is
that the control is the credential boundary rather than anything about the
request. So most of this file is: an `agent` or `system` token, however it is
dressed up, gets nowhere near these routes — and an `approved_client` token that
does not name a human gets nowhere either.

`httpx`'s `TestClient.delete()` takes no body, so every DELETE here goes through
`client.request("DELETE", ...)` instead — the escape hatch, not a workaround for
anything the server does differently.
"""
from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

import app as libapp
import identity
import protocol
import store

AGENT = "agent-token-vivy"
SYSTEM = "system-token"
CLIENT = "orient-token"


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = tmp_path / "control.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE signals (
            id TEXT PRIMARY KEY, source TEXT, stream TEXT, kind TEXT, actor TEXT,
            ingested_by TEXT, product TEXT, refs TEXT, body TEXT, meta TEXT,
            privacy_class TEXT, produced_at TEXT, ingested_at TEXT,
            embedded_at TEXT, dedup_key TEXT, acting_human TEXT);
        CREATE TABLE signal_acks (
            id TEXT PRIMARY KEY, signal_id TEXT, identity TEXT, acked_at TEXT);
        CREATE UNIQUE INDEX uq_signal_acks_signal_identity
            ON signal_acks (signal_id, identity);
    """)
    conn.executemany(
        "INSERT INTO signals (id, product, kind, body, privacy_class, "
        "ingested_at) VALUES (?,?,?,?,?,?)",
        [("sig_internal", "vivy", "note", "an internal note", "internal",
          "2026-08-01 07:30:00.000000"),
         ("sig_customer", "vivy", "note", "a customer wrote this", "customer",
          "2026-08-01 07:31:00.000000"),
         # A second product's own signal — model per `test_archives_isolation
         # .py`'s `two_products` fixture — for the credential below, scoped to
         # "vivy" only, to be refused against.
         ("sig_other_product", "constance", "note", "not vivy's", "internal",
          "2026-08-01 07:32:00.000000")])
    conn.commit()
    conn.close()

    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        AGENT: {"name": "vivy-agent", "products": ["vivy"]},
        SYSTEM: {"name": "capture", "products": ["*"], "kind": "system"},
    }))
    client_tokens = tmp_path / "client-tokens.json"
    client_tokens.write_text(json.dumps({
        CLIENT: {"name": "orient", "products": ["vivy"]},
    }))

    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(identity, "CLIENT_TOKENS", client_tokens)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    return TestClient(libapp.app)


def _h(token):
    return {"Authorization": f"Bearer {token}",
            protocol.HEADER: str(protocol.CURRENT)}


def _call(client, method, signal_id="sig_internal", token=CLIENT, **body):
    return client.request(method, f"/signals/{signal_id}/ack", json=body,
                          headers=_h(token))


def _acks(signal_id="sig_internal"):
    conn = sqlite3.connect(store.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT identity FROM signal_acks WHERE signal_id = ?",
            (signal_id,)).fetchall()
    finally:
        conn.close()


# --- the capability gate --------------------------------------------------


@pytest.mark.parametrize("method", ["POST", "DELETE"])
@pytest.mark.parametrize("token", [AGENT, SYSTEM])
def test_agent_and_system_are_refused_on_both_routes(client, method, token):
    r = _call(client, method, token=token, acting_human="operator@example.com")
    assert r.status_code == 403
    assert "signal.ack" in r.json()["error"]
    assert _acks() == []


# --- the attested human ----------------------------------------------------


@pytest.mark.parametrize("method", ["POST", "DELETE"])
def test_a_client_with_no_acting_human_is_refused_and_writes_nothing(
        client, method):
    r = _call(client, method)  # no acting_human in the body
    assert r.status_code != 200
    assert _acks() == []


def test_the_recorded_identity_is_the_human_never_the_client(client):
    r = _call(client, "POST", acting_human="operator@example.com")
    assert r.status_code == 200
    rows = _acks()
    assert len(rows) == 1
    assert rows[0]["identity"] == "operator@example.com"
    # The client is echoed, not recorded as the actor.
    assert r.json()["attested_by"] == "orient"


# --- idempotency -------------------------------------------------------------


def test_acking_twice_is_idempotent(client):
    first = _call(client, "POST", acting_human="operator@example.com")
    second = _call(client, "POST", acting_human="operator@example.com")
    assert first.status_code == 200
    assert second.status_code == 200
    assert len(_acks()) == 1


def test_unacking_removes_the_ack(client):
    _call(client, "POST", acting_human="operator@example.com")
    r = _call(client, "DELETE", acting_human="operator@example.com")
    assert r.status_code == 200
    assert r.json()["acknowledged"] is False
    assert _acks() == []


def test_unacking_twice_is_idempotent(client):
    _call(client, "POST", acting_human="operator@example.com")
    _call(client, "DELETE", acting_human="operator@example.com")
    r = _call(client, "DELETE", acting_human="operator@example.com")
    assert r.status_code == 200
    assert _acks() == []


# --- the customer wall -------------------------------------------------------


def test_a_customer_signal_is_invisible_without_the_capability(client, monkeypatch):
    monkeypatch.setattr(identity, "ROLE_CAPS", {
        **identity.ROLE_CAPS,
        "approved_client": identity.ROLE_CAPS["approved_client"]
                            - {"signal.customer"},
    })
    r = _call(client, "POST", signal_id="sig_customer",
             acting_human="operator@example.com")
    assert r.status_code == 404
    assert _acks("sig_customer") == []


def test_a_customer_signal_is_visible_with_the_capability(client):
    # `approved_client` inherits every `system` capability, `signal.customer`
    # included, so the ordinary token from the fixture already carries it.
    r = _call(client, "POST", signal_id="sig_customer",
             acting_human="operator@example.com")
    assert r.status_code == 200


# --- the product wall --------------------------------------------------------


@pytest.mark.parametrize("method", ["POST", "DELETE"])
def test_a_signal_in_another_product_is_refused_like_an_unknown_id(
        client, method):
    """`_ack_visible` calls `identity.in_scope` on the row's own product —
    drop that half of the check and this still passes against `sig_customer`,
    so the signal under test has to be out of the credential's product scope
    rather than merely customer-class."""
    other_product = _call(client, method, signal_id="sig_other_product",
                          acting_human="operator@example.com")
    unknown = _call(client, method, signal_id="sig_does_not_exist",
                    acting_human="operator@example.com")
    assert other_product.status_code == unknown.status_code == 404
    assert other_product.json() == unknown.json()
    assert _acks("sig_other_product") == []


def test_an_ack_on_another_product_is_not_removed_by_a_scoped_delete(client):
    """Planted directly in the vault, bypassing the write route's own scope
    check — the same reasoning `test_get_acks_omits_an_out_of_scope_signal_
    even_when_acked` gives for exercising the read side this way — so this
    proves the DELETE route's own gate, not that a caller can never ack it in
    the first place."""
    conn = sqlite3.connect(store.DB_PATH)
    conn.execute(
        "INSERT INTO signal_acks (id, signal_id, identity, acked_at) "
        "VALUES (?,?,?,?)",
        ("ack_other", "sig_other_product", "operator@example.com",
         "2026-08-01 07:33:00.000000"))
    conn.commit()
    conn.close()
    r = _call(client, "DELETE", signal_id="sig_other_product",
             acting_human="operator@example.com")
    assert r.status_code == 404
    rows = _acks("sig_other_product")
    assert len(rows) == 1
    assert rows[0]["identity"] == "operator@example.com"


# --- the client file is its own failure domain -------------------------------


def test_a_missing_client_token_file_leaves_agent_auth_working(
        client, monkeypatch, tmp_path):
    monkeypatch.setattr(identity, "CLIENT_TOKENS", tmp_path / "absent.json")
    r = client.get("/signals/sig_internal", headers=_h(AGENT))
    assert r.status_code == 200
    # And the approved-client path is the one that goes dark.
    assert _call(client, "POST", acting_human="operator@example.com").status_code == 401


# --- GET /signals/acks -------------------------------------------------------


def _get_acks(client, signal_ids, token=CLIENT, acting_human="operator@example.com"):
    params = [("signal_id", sid) for sid in signal_ids]
    if acting_human is not None:
        params.append(("acting_human", acting_human))
    return client.get("/signals/acks", params=params, headers=_h(token))


@pytest.mark.parametrize("token", [AGENT, SYSTEM])
def test_get_acks_requires_signal_ack_capability(client, token):
    r = _get_acks(client, ["sig_internal"], token=token)
    assert r.status_code == 403
    assert "signal.ack" in r.json()["error"]


def test_get_acks_with_no_acting_human_is_refused(client):
    r = _get_acks(client, ["sig_internal"], acting_human=None)
    assert r.status_code == 403


def test_get_acks_reports_only_ids_the_human_acked(client):
    _call(client, "POST", signal_id="sig_internal", acting_human="operator@example.com")
    r = _get_acks(client, ["sig_internal", "sig_customer"])
    assert r.status_code == 200
    assert r.json()["acked"] == ["sig_internal"]


def test_get_acks_omits_an_id_acked_by_a_different_human(client):
    _call(client, "POST", signal_id="sig_internal",
          acting_human="someone-else@example.com")
    r = _get_acks(client, ["sig_internal"])
    assert r.status_code == 200
    assert r.json()["acked"] == []


def test_get_acks_silently_omits_an_unknown_id(client):
    r = _get_acks(client, ["sig_does_not_exist"])
    assert r.status_code == 200
    assert r.json()["acked"] == []


def test_get_acks_with_no_signal_ids_returns_an_empty_list(client):
    r = _get_acks(client, [])
    assert r.status_code == 200
    assert r.json()["acked"] == []


def test_get_acks_omits_a_customer_signal_without_the_capability(client, monkeypatch):
    """Acked while the capability is held, then hidden once it is not — the
    same visibility rule the write pair enforces, applied here to a read."""
    _call(client, "POST", signal_id="sig_customer", acting_human="operator@example.com")
    monkeypatch.setattr(identity, "ROLE_CAPS", {
        **identity.ROLE_CAPS,
        "approved_client": identity.ROLE_CAPS["approved_client"]
                            - {"signal.customer"},
    })
    r = _get_acks(client, ["sig_customer"])
    assert r.status_code == 200
    assert r.json()["acked"] == []


def test_get_acks_omits_an_out_of_scope_signal_even_when_acked(client):
    """Acked directly in the vault, bypassing the write route's own scope
    check, so this exercises the read's own visibility gate rather than
    re-testing that a caller can never ack it in the first place."""
    conn = sqlite3.connect(store.DB_PATH)
    conn.execute(
        "INSERT INTO signals (id, product, kind, body, privacy_class, "
        "ingested_at) VALUES (?,?,?,?,?,?)",
        ("sig_other", "constance", "note", "not vivy", "internal",
         "2026-08-01 07:32:00.000000"))
    conn.execute(
        "INSERT INTO signal_acks (id, signal_id, identity, acked_at) "
        "VALUES (?,?,?,?)",
        ("ack_x", "sig_other", "operator@example.com", "2026-08-01 07:33:00.000000"))
    conn.commit()
    conn.close()
    r = _get_acks(client, ["sig_other"])
    assert r.status_code == 200
    assert r.json()["acked"] == [], "an out-of-scope signal's ack leaked through"


def test_get_acks_refuses_more_ids_than_the_cap(client):
    """Each id costs its own `_ack_visible` query, so the list is bounded the way
    every other list-shaped input in `app.py` is (both reviewers, PR #27).

    **Asserts a REFUSAL, not a truncation**, which is the whole point of the cap's
    shape: answering the first 200 of 201 would report the dropped id as un-acked,
    and `ops_reports_build` would render an acknowledged alert back into the live
    section with nothing in the response saying so. A wrong answer wearing the
    shape of a right one is worse than an error, the same reasoning `MAX_OFFSET`
    records for refusing rather than clamping."""
    r = _get_acks(client, [f"sig_{i}" for i in range(libapp.MAX_ACK_IDS + 1)])
    assert r.status_code == 400
    assert "acked" not in r.json(), "a refusal must not look like an empty result"


def test_get_acks_accepts_exactly_the_cap(client):
    """The boundary itself is allowed — an off-by-one here would refuse a caller
    asking about a full page of signals, which is the normal case rather than an
    abusive one, since `MAX_ACK_IDS` matches `MAX_PAGE` precisely so a full page
    of ids can be asked about in one call."""
    r = _get_acks(client, [f"sig_{i}" for i in range(libapp.MAX_ACK_IDS)])
    assert r.status_code == 200
    assert r.json()["acked"] == []
