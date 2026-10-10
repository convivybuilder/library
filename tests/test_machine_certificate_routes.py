"""`POST /machines/{machine}/certificates` and `POST /machines/{machine}/
revoke` (platform/decisions/0066): the approved-client-gated write path,
the second of the two registry write paths the ADR's Handoff specifies.

**Read is `machine_identity.MachineIdentityGate`, already built and
untouched here.** This file is the write side only, and most of it is
proving the same boundary `test_tool_policy.py` proves for `policy.write`:
the capability, the unconditional kind check, and the attested human.
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
VERIFY = "bosun-verify-token"
MACHINE = "macbook"
FP = "a" * 64
NOT_AFTER = "2026-12-01T00:00:00"


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = tmp_path / "control.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
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
    """)
    conn.commit()
    conn.close()

    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        AGENT: {"name": "vivy-agent", "products": ["vivy"]},
        SYSTEM: {"name": "capture", "products": ["*"], "kind": "system"},
        VERIFY: {"name": "bosun", "products": ["*"],
                 "grants": ["machine.verify"]},
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
    body = {"fingerprint_sha256": FP, "not_after": NOT_AFTER,
            "custody": "hardware", "acting_human": "operator"}
    body.update(over)
    return body


def _rows(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in
                conn.execute("SELECT * FROM machine_certificates").fetchall()]
    finally:
        conn.close()


# --- write: approved-client-only, matching write_policy's shape --------------


def test_orient_can_issue_a_certificate(client):
    r = client.post(f"/machines/{MACHINE}/certificates",
                    json=_issue_body(), headers=_h(CLIENT))
    assert r.status_code == 200
    body = r.json()
    assert body["machine"] == MACHINE
    assert body["status"] == "active"
    assert body["acting_human"] == "operator"
    assert body["attested_by"] == "orient"


def test_an_agent_cannot_issue_a_certificate(client):
    r = client.post(f"/machines/{MACHINE}/certificates",
                    json=_issue_body(), headers=_h(AGENT))
    assert r.status_code == 403
    assert "machine.write" in r.json()["error"]


def test_a_system_identity_cannot_issue_a_certificate(client):
    """`system` sits below `approved_client` in the role hierarchy and does
    not inherit its human-reserved capabilities — the same boundary
    `policy.write` draws."""
    r = client.post(f"/machines/{MACHINE}/certificates",
                    json=_issue_body(), headers=_h(SYSTEM))
    assert r.status_code == 403


def test_an_unauthenticated_request_is_401(client):
    r = client.post(f"/machines/{MACHINE}/certificates", json=_issue_body(),
                    headers={protocol.HEADER: str(protocol.CURRENT)})
    assert r.status_code == 401


def test_holding_signing_write_but_not_machine_write_is_refused(
        client, tmp_path, monkeypatch):
    """`signing.write` and `machine.write` are separate capabilities
    (identity.py's comment beside `signing.write` says why); a client
    trusted with one must not reach the other's routes."""
    token = "signing-write-only-token"
    client_tokens = tmp_path / "client-tokens.json"
    client_tokens.write_text(json.dumps({
        CLIENT: {"name": "orient", "products": ["*"]},
        token: {"name": "narrowed", "products": ["*"],
               "holds_reserved": ["signing.write"]},
    }))
    monkeypatch.setattr(identity, "CLIENT_TOKENS", client_tokens)
    r = client.post(f"/machines/{MACHINE}/certificates", json=_issue_body(),
                    headers=_h(token))
    assert r.status_code == 403
    assert "machine.write" in r.json()["error"]
    r = client.post(f"/machines/{MACHINE}/revoke",
                    json={"acting_human": "operator"}, headers=_h(token))
    assert r.status_code == 403
    assert "machine.write" in r.json()["error"]


def test_issuing_without_acting_human_is_refused(client):
    body = _issue_body()
    del body["acting_human"]
    r = client.post(f"/machines/{MACHINE}/certificates", json=body,
                    headers=_h(CLIENT))
    assert r.status_code == 403
    assert "acting_human" in r.json()["error"]


# --- issue: validation --------------------------------------------------------


def test_a_missing_fingerprint_is_refused(client):
    body = _issue_body()
    del body["fingerprint_sha256"]
    r = client.post(f"/machines/{MACHINE}/certificates", json=body,
                    headers=_h(CLIENT))
    assert r.status_code == 400
    assert "fingerprint_sha256" in r.json()["error"]


def test_an_invalid_fingerprint_is_refused(client):
    r = client.post(f"/machines/{MACHINE}/certificates",
                    json=_issue_body(fingerprint_sha256="not-hex"),
                    headers=_h(CLIENT))
    assert r.status_code == 400


def test_an_invalid_custody_is_refused(client):
    r = client.post(f"/machines/{MACHINE}/certificates",
                    json=_issue_body(custody="usb-stick"), headers=_h(CLIENT))
    assert r.status_code == 400


def test_a_non_string_custody_is_refused_rather_than_crashing(client):
    """`custody not in CUSTODY_VALUES` hashes the probe; a caller-supplied
    list must answer 400, not 500."""
    r = client.post(f"/machines/{MACHINE}/certificates",
                    json=_issue_body(custody=["hardware"]), headers=_h(CLIENT))
    assert r.status_code == 400


def test_an_unparseable_not_after_is_refused(client):
    r = client.post(f"/machines/{MACHINE}/certificates",
                    json=_issue_body(not_after="not a date"),
                    headers=_h(CLIENT))
    assert r.status_code == 400
    assert "not_after" in r.json()["error"]


def test_a_whitespace_only_machine_name_is_refused(client):
    r = client.post("/machines/%20/certificates", json=_issue_body(),
                    headers=_h(CLIENT))
    assert r.status_code == 400


def test_a_non_string_note_is_refused(client):
    r = client.post(f"/machines/{MACHINE}/certificates",
                    json=_issue_body(note=12345), headers=_h(CLIENT))
    assert r.status_code == 400


def test_a_duplicate_fingerprint_is_400_not_500(client):
    client.post(f"/machines/{MACHINE}/certificates", json=_issue_body(),
               headers=_h(CLIENT))
    r = client.post("/machines/deck/certificates", json=_issue_body(),
                    headers=_h(CLIENT))
    assert r.status_code == 400
    assert "already registered" in r.json()["error"]


def test_a_fingerprint_already_enrolled_as_a_signing_certificate_is_400(client):
    """The cross-table refusal through the route, in this direction: a
    signing certificate cannot then be enrolled as a machine's mTLS
    certificate. The first write is `POST /signing-certificates`."""
    r = client.post("/signing-certificates", json={
        "fingerprint_sha256": FP, "person": "operator@example.com",
        "machine": MACHINE, "not_after": "2026-12-01T00:00:00",
        "acting_human": "operator"}, headers=_h(CLIENT))
    assert r.status_code == 200, r.text
    r = client.post(f"/machines/{MACHINE}/certificates", json=_issue_body(),
                    headers=_h(CLIENT))
    assert r.status_code == 400
    assert "signing certificate" in r.json()["error"]
    conn = sqlite3.connect(store.DB_PATH)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM machine_certificates").fetchone()[0] == 0
    finally:
        conn.close()


# --- issue: the shadow gate ---------------------------------------------------


def test_issue_refuses_while_writes_are_disabled(client, monkeypatch):
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    r = client.post(f"/machines/{MACHINE}/certificates",
                    json=_issue_body(), headers=_h(CLIENT))
    assert r.status_code == 503


# --- issue: re-issuing revokes the previous row -------------------------------


def test_re_issuing_revokes_the_previous_certificate(client):
    client.post(f"/machines/{MACHINE}/certificates", json=_issue_body(),
               headers=_h(CLIENT))
    client.post(f"/machines/{MACHINE}/certificates",
               json=_issue_body(fingerprint_sha256="b" * 64),
               headers=_h(CLIENT))
    rows = {r["fingerprint_sha256"]: r for r in _rows(store.DB_PATH)}
    assert rows[FP]["status"] == "revoked"
    assert rows["b" * 64]["status"] == "active"


# --- revoke --------------------------------------------------------------------


def test_orient_can_revoke_a_certificate(client):
    client.post(f"/machines/{MACHINE}/certificates", json=_issue_body(),
               headers=_h(CLIENT))
    r = client.post(f"/machines/{MACHINE}/revoke",
                    json={"acting_human": "operator", "note": "lost the key"},
                    headers=_h(CLIENT))
    assert r.status_code == 200
    assert r.json()["status"] == "revoked"


def test_revoking_a_machine_with_no_active_certificate_is_404(client):
    r = client.post(f"/machines/{MACHINE}/revoke",
                    json={"acting_human": "operator"}, headers=_h(CLIENT))
    assert r.status_code == 404


def test_an_agent_cannot_revoke_a_certificate(client):
    client.post(f"/machines/{MACHINE}/certificates", json=_issue_body(),
               headers=_h(CLIENT))
    r = client.post(f"/machines/{MACHINE}/revoke",
                    json={"acting_human": "operator"}, headers=_h(AGENT))
    assert r.status_code == 403


def test_revoke_refuses_while_writes_are_disabled(client, monkeypatch):
    client.post(f"/machines/{MACHINE}/certificates", json=_issue_body(),
               headers=_h(CLIENT))
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    r = client.post(f"/machines/{MACHINE}/revoke",
                    json={"acting_human": "operator"}, headers=_h(CLIENT))
    assert r.status_code == 503


# --- read: GET /machine-certificates/{fingerprint_sha256} --------------------
# `machine.verify` (platform/decisions/0066): granted only through an entry's
# own `grants`, so only `VERIFY` above — modelling the Bosun's dedicated
# credential — holds it; an ordinary `agent` or `system` credential does not.


def test_an_unknown_fingerprint_is_200_not_404(client):
    r = client.get(f"/machine-certificates/{FP}", headers=_h(VERIFY))
    assert r.status_code == 200
    assert r.json() == {"fingerprint_sha256": FP, "status": "unknown",
                        "active": False}


def test_an_active_certificate_reads_active(client):
    client.post(f"/machines/{MACHINE}/certificates", json=_issue_body(),
               headers=_h(CLIENT))
    r = client.get(f"/machine-certificates/{FP}", headers=_h(VERIFY))
    assert r.status_code == 200
    assert r.json() == {"machine": MACHINE, "fingerprint_sha256": FP,
                        "status": "active", "active": True,
                        "custody": "hardware", "not_after": "2026-12-01 00:00:00"}


def test_a_revoked_certificate_reads_revoked(client):
    client.post(f"/machines/{MACHINE}/certificates", json=_issue_body(),
               headers=_h(CLIENT))
    client.post(f"/machines/{MACHINE}/revoke",
               json={"acting_human": "operator"}, headers=_h(CLIENT))
    r = client.get(f"/machine-certificates/{FP}", headers=_h(VERIFY))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "revoked"
    assert body["active"] is False


def test_an_expired_certificate_reads_expired(client):
    client.post(f"/machines/{MACHINE}/certificates",
               json=_issue_body(not_after="2020-01-01T00:00:00"),
               headers=_h(CLIENT))
    r = client.get(f"/machine-certificates/{FP}", headers=_h(VERIFY))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "expired"
    assert body["active"] is False


def test_a_malformed_fingerprint_is_400(client):
    r = client.get("/machine-certificates/not-hex", headers=_h(VERIFY))
    assert r.status_code == 400


def test_an_ordinary_agent_credential_is_403(client):
    """`machine.verify` sits on no role — an `agent` credential with no
    `grants` entry naming it must not reach this route."""
    r = client.get(f"/machine-certificates/{FP}", headers=_h(AGENT))
    assert r.status_code == 403
    assert "machine.verify" in r.json()["error"]


def test_an_ordinary_system_credential_is_403(client):
    r = client.get(f"/machine-certificates/{FP}", headers=_h(SYSTEM))
    assert r.status_code == 403
    assert "machine.verify" in r.json()["error"]


def test_a_credential_holding_the_grant_can_read(client):
    client.post(f"/machines/{MACHINE}/certificates", json=_issue_body(),
               headers=_h(CLIENT))
    r = client.get(f"/machine-certificates/{FP}", headers=_h(VERIFY))
    assert r.status_code == 200


def test_an_unauthenticated_read_is_401(client):
    r = client.get(f"/machine-certificates/{FP}",
                   headers={protocol.HEADER: str(protocol.CURRENT)})
    assert r.status_code == 401
