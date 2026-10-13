"""`POST /signing-certificates`, `POST /signing-certificates/{fingerprint}/
revoke` and `GET /signing-certificates/{fingerprint}` (platform/contracts/
library-protocol §2.2): the write side on its own capability,
`signing.write`, separate from `machine.write`, and the read side on its
own capability, `signing.verify`.

**Read is per-request, no caching.** This file is what
`test_machine_certificate_routes.py` is for the sibling registry, plus the
read route §2.2 adds.
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
PERSON = "operator@example.com"
MACHINE = "macbook"
FP = "a" * 64
NOT_AFTER = "2026-12-01T00:00:00"


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = tmp_path / "control.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE signing_certificates (
            id INTEGER PRIMARY KEY,
            fingerprint_sha256 TEXT NOT NULL UNIQUE,
            person TEXT NOT NULL,
            machine TEXT NOT NULL,
            not_after TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'revoked')),
            issued_at TEXT NOT NULL,
            revoked_at TEXT,
            note TEXT
        );
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
        CREATE INDEX ix_machine_certificates_lookup
            ON machine_certificates (machine, fingerprint_sha256);
    """)
    conn.commit()
    conn.close()

    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        AGENT: {"name": "vivy-agent", "products": ["vivy"]},
        SYSTEM: {"name": "capture", "products": ["*"], "kind": "system"},
    }))
    client_tokens = tmp_path / "client-tokens.json"
    client_tokens.write_text(json.dumps({
        CLIENT: {"name": "orient", "products": ["*"]},
    }))

    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(identity, "CLIENT_TOKENS", client_tokens)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    return TestClient(libapp.app)


def _h(token):
    return {"Authorization": f"Bearer {token}",
            protocol.HEADER: str(protocol.CURRENT)}


def _issue_body(**over):
    body = {"fingerprint_sha256": FP, "person": PERSON, "machine": MACHINE,
            "not_after": NOT_AFTER, "acting_human": "operator"}
    body.update(over)
    return body


def _rows(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in
                conn.execute("SELECT * FROM signing_certificates").fetchall()]
    finally:
        conn.close()


# --- write: approved-client-only, on signing.write ----------------------------


def test_orient_can_issue_a_certificate(client):
    r = client.post("/signing-certificates", json=_issue_body(),
                    headers=_h(CLIENT))
    assert r.status_code == 200
    body = r.json()
    assert body["person"] == PERSON
    assert body["machine"] == MACHINE
    assert body["status"] == "active"
    assert body["acting_human"] == "operator"
    assert body["attested_by"] == "orient"


def test_an_agent_cannot_issue_a_certificate(client):
    r = client.post("/signing-certificates", json=_issue_body(),
                    headers=_h(AGENT))
    assert r.status_code == 403
    assert "signing.write" in r.json()["error"]


def test_a_system_identity_cannot_issue_a_certificate(client):
    r = client.post("/signing-certificates", json=_issue_body(),
                    headers=_h(SYSTEM))
    assert r.status_code == 403


# --- write: signing.write is not machine.write, and the reverse --------------


def _write_client_tokens(tmp_path, monkeypatch, **entries):
    client_tokens = tmp_path / "client-tokens.json"
    client_tokens.write_text(json.dumps({
        CLIENT: {"name": "orient", "products": ["*"]},
        **entries,
    }))
    monkeypatch.setattr(identity, "CLIENT_TOKENS", client_tokens)


def test_holding_machine_write_but_not_signing_write_is_refused(
        client, tmp_path, monkeypatch):
    """The finding this split fixes: `machine.write` alone must not reach
    the signing registry's write routes."""
    token = "machine-write-only-token"
    _write_client_tokens(tmp_path, monkeypatch, **{
        token: {"name": "narrowed", "products": ["*"],
               "holds_reserved": ["machine.write"]}})
    r = client.post("/signing-certificates", json=_issue_body(),
                    headers=_h(token))
    assert r.status_code == 403
    assert "signing.write" in r.json()["error"]
    r = client.post(f"/signing-certificates/{FP}/revoke",
                    json={"acting_human": "operator"}, headers=_h(token))
    assert r.status_code == 403
    assert "signing.write" in r.json()["error"]


def test_an_approved_client_narrowed_to_no_reserved_caps_is_refused(
        client, tmp_path, monkeypatch):
    token = "holds-nothing-token"
    _write_client_tokens(tmp_path, monkeypatch, **{
        token: {"name": "bosun", "products": ["*"], "holds_reserved": []}})
    r = client.post("/signing-certificates", json=_issue_body(),
                    headers=_h(token))
    assert r.status_code == 403
    assert "signing.write" in r.json()["error"]
    r = client.post(f"/signing-certificates/{FP}/revoke",
                    json={"acting_human": "operator"}, headers=_h(token))
    assert r.status_code == 403
    assert "signing.write" in r.json()["error"]


def test_an_unauthenticated_request_is_401(client):
    r = client.post("/signing-certificates", json=_issue_body(),
                    headers={protocol.HEADER: str(protocol.CURRENT)})
    assert r.status_code == 401


def test_issuing_without_acting_human_is_refused(client):
    body = _issue_body()
    del body["acting_human"]
    r = client.post("/signing-certificates", json=body, headers=_h(CLIENT))
    assert r.status_code == 403
    assert "acting_human" in r.json()["error"]


# --- issue: validation --------------------------------------------------------


def test_a_missing_fingerprint_is_refused(client):
    body = _issue_body()
    del body["fingerprint_sha256"]
    r = client.post("/signing-certificates", json=body, headers=_h(CLIENT))
    assert r.status_code == 400
    assert "fingerprint_sha256" in r.json()["error"]


def test_an_invalid_fingerprint_is_refused(client):
    r = client.post("/signing-certificates",
                    json=_issue_body(fingerprint_sha256="not-hex"),
                    headers=_h(CLIENT))
    assert r.status_code == 400


def test_a_missing_person_is_refused(client):
    body = _issue_body()
    del body["person"]
    r = client.post("/signing-certificates", json=body, headers=_h(CLIENT))
    assert r.status_code == 400
    assert "person" in r.json()["error"]


def test_a_missing_machine_is_refused(client):
    body = _issue_body()
    del body["machine"]
    r = client.post("/signing-certificates", json=body, headers=_h(CLIENT))
    assert r.status_code == 400
    assert "machine" in r.json()["error"]


def test_an_unparseable_not_after_is_refused(client):
    r = client.post("/signing-certificates",
                    json=_issue_body(not_after="not a date"),
                    headers=_h(CLIENT))
    assert r.status_code == 400
    assert "not_after" in r.json()["error"]


def test_a_non_string_note_is_refused(client):
    r = client.post("/signing-certificates",
                    json=_issue_body(note=12345), headers=_h(CLIENT))
    assert r.status_code == 400


def test_a_duplicate_fingerprint_is_400_not_500(client):
    client.post("/signing-certificates", json=_issue_body(), headers=_h(CLIENT))
    r = client.post("/signing-certificates",
                    json=_issue_body(machine="deck"), headers=_h(CLIENT))
    assert r.status_code == 400
    assert "already registered" in r.json()["error"]


def test_a_fingerprint_already_enrolled_as_a_machine_certificate_is_400(
        client):
    """The cross-table refusal through the route: a machine's mTLS
    certificate, enrolled first through `POST /machines/...`, cannot then be
    registered as a signing certificate."""
    machine_body = {"fingerprint_sha256": FP, "custody": "hardware",
                    "not_after": NOT_AFTER, "acting_human": "operator"}
    r = client.post(f"/machines/{MACHINE}/certificates", json=machine_body,
                    headers=_h(CLIENT))
    assert r.status_code == 200, r.text
    r = client.post("/signing-certificates", json=_issue_body(),
                    headers=_h(CLIENT))
    assert r.status_code == 400
    assert "machine certificate" in r.json()["error"]
    assert _rows(store.DB_PATH) == []


def test_a_person_can_be_issued_two_certificates(client):
    client.post("/signing-certificates", json=_issue_body(), headers=_h(CLIENT))
    r = client.post("/signing-certificates",
                    json=_issue_body(machine="deck",
                                     fingerprint_sha256="b" * 64),
                    headers=_h(CLIENT))
    assert r.status_code == 200
    assert len(_rows(store.DB_PATH)) == 2


# --- issue: the shadow gate ---------------------------------------------------


def test_issue_refuses_while_writes_are_disabled(client, monkeypatch):
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    r = client.post("/signing-certificates", json=_issue_body(),
                    headers=_h(CLIENT))
    assert r.status_code == 503


# --- revoke --------------------------------------------------------------------


def test_orient_can_revoke_a_certificate(client):
    client.post("/signing-certificates", json=_issue_body(), headers=_h(CLIENT))
    r = client.post(f"/signing-certificates/{FP}/revoke",
                    json={"acting_human": "operator", "note": "lost the key"},
                    headers=_h(CLIENT))
    assert r.status_code == 200
    assert r.json()["status"] == "revoked"


def test_revoking_an_unknown_fingerprint_is_404(client):
    r = client.post(f"/signing-certificates/{FP}/revoke",
                    json={"acting_human": "operator"}, headers=_h(CLIENT))
    assert r.status_code == 404


def test_an_agent_cannot_revoke_a_certificate(client):
    client.post("/signing-certificates", json=_issue_body(), headers=_h(CLIENT))
    r = client.post(f"/signing-certificates/{FP}/revoke",
                    json={"acting_human": "operator"}, headers=_h(AGENT))
    assert r.status_code == 403


def test_revoking_one_certificate_leaves_the_persons_other_one_active(client):
    client.post("/signing-certificates", json=_issue_body(), headers=_h(CLIENT))
    client.post("/signing-certificates",
               json=_issue_body(machine="deck", fingerprint_sha256="b" * 64),
               headers=_h(CLIENT))
    client.post(f"/signing-certificates/{FP}/revoke",
               json={"acting_human": "operator"}, headers=_h(CLIENT))
    rows = {r["fingerprint_sha256"]: r for r in _rows(store.DB_PATH)}
    assert rows[FP]["status"] == "revoked"
    assert rows["b" * 64]["status"] == "active"


def test_revoke_refuses_while_writes_are_disabled(client, monkeypatch):
    client.post("/signing-certificates", json=_issue_body(), headers=_h(CLIENT))
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    r = client.post(f"/signing-certificates/{FP}/revoke",
                    json={"acting_human": "operator"}, headers=_h(CLIENT))
    assert r.status_code == 503


# --- read: GET /signing-certificates/{fingerprint} ---------------------------


def test_an_active_certificate_reads_active(client):
    client.post("/signing-certificates", json=_issue_body(), headers=_h(CLIENT))
    r = client.get(f"/signing-certificates/{FP}", headers=_h(AGENT))
    assert r.status_code == 200
    assert r.json() == {"fingerprint_sha256": FP, "status": "active",
                        "active": True}


def test_a_revoked_certificate_reads_revoked(client):
    client.post("/signing-certificates", json=_issue_body(), headers=_h(CLIENT))
    client.post(f"/signing-certificates/{FP}/revoke",
               json={"acting_human": "operator"}, headers=_h(CLIENT))
    r = client.get(f"/signing-certificates/{FP}", headers=_h(AGENT))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "revoked"
    assert body["active"] is False


def test_an_expired_certificate_reads_expired(client):
    client.post("/signing-certificates",
               json=_issue_body(not_after="2020-01-01T00:00:00"),
               headers=_h(CLIENT))
    r = client.get(f"/signing-certificates/{FP}", headers=_h(AGENT))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "expired"
    assert body["active"] is False


def test_an_unknown_fingerprint_is_200_not_404(client):
    """The contract's own rule: a fingerprint of a certificate the caller
    already holds reveals nothing worth protecting."""
    r = client.get(f"/signing-certificates/{FP}", headers=_h(AGENT))
    assert r.status_code == 200
    assert r.json() == {"fingerprint_sha256": FP, "status": "unknown",
                        "active": False}


def test_read_refuses_any_parameter(client):
    r = client.get(f"/signing-certificates/{FP}?person=someone",
                   headers=_h(AGENT))
    assert r.status_code == 400
    assert r.json()["unknown"] == ["person"]
    assert r.json()["supported"] == []


def test_a_malformed_fingerprint_is_400(client):
    r = client.get("/signing-certificates/not-hex", headers=_h(AGENT))
    assert r.status_code == 400


def test_a_system_identity_can_read(client):
    """`signing.verify` sits on `system` too — `kb apply` may authenticate
    as either."""
    client.post("/signing-certificates", json=_issue_body(), headers=_h(CLIENT))
    r = client.get(f"/signing-certificates/{FP}", headers=_h(SYSTEM))
    assert r.status_code == 200


def test_an_approved_client_can_read(client):
    """`signing.verify` is not human-reserved, but `approved_client` derives
    from `system` and so holds it too."""
    client.post("/signing-certificates", json=_issue_body(), headers=_h(CLIENT))
    r = client.get(f"/signing-certificates/{FP}", headers=_h(CLIENT))
    assert r.status_code == 200


def test_a_credential_without_signing_verify_is_403(client, tmp_path,
                                                     monkeypatch):
    """Every role in `ROLE_CAPS` grants `signing.verify` (the contract puts
    it on `agent` and `system` and every other role derives from one of
    them), so the only way to hold a credential without it is an entry's own
    `withholds` list — the mechanism `epic.approve`'s comment describes for
    narrowing an `approved_client` below its role's default set."""
    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        "no-cap-token": {"name": "narrowed-agent", "products": ["*"],
                         "withholds": ["signing.verify"]},
    }))
    monkeypatch.setattr(identity, "TOKENS", tokens)
    r = client.get(f"/signing-certificates/{FP}",
                   headers=_h("no-cap-token"))
    assert r.status_code == 403
    assert "signing.verify" in r.json()["error"]


def test_an_unauthenticated_read_is_401(client):
    r = client.get(f"/signing-certificates/{FP}",
                   headers={protocol.HEADER: str(protocol.CURRENT)})
    assert r.status_code == 401
