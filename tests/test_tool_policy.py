"""The per-(agent, crew, model, target) tool policy (ADR-0048).

**Read is open to every authenticated identity; write is Orient-only**, and
most of this file is proving that boundary holds from every angle it can be
approached: the capability, the unconditional kind check, the attested human,
and the class defaults a caller falls back to when no explicit row exists.
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
        CREATE TABLE tool_policy (
            id INTEGER PRIMARY KEY, agent TEXT NOT NULL, crew TEXT NOT NULL,
            model TEXT NOT NULL, target TEXT NOT NULL, decision TEXT NOT NULL,
            created_by TEXT NOT NULL, created_at TEXT NOT NULL,
            updated_by TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE (agent, crew, model, target));
        CREATE TABLE tool_policy_events (
            id INTEGER PRIMARY KEY, actor TEXT NOT NULL, agent TEXT NOT NULL,
            crew TEXT NOT NULL, model TEXT NOT NULL, target TEXT NOT NULL,
            decision TEXT NOT NULL, at TEXT NOT NULL);
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


def _grant(client, token=CLIENT, **over):
    body = {"agent": "scout", "crew": "cfo", "model": "sonnet",
            "target": "WebFetch", "decision": "allow",
            "acting_human": "operator"}
    body.update(over)
    return client.put("/policy", json=body, headers=_h(token))


def _events():
    conn = sqlite3.connect(store.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM tool_policy_events ORDER BY id").fetchall()
    finally:
        conn.close()


# --- read: open to every authenticated identity ------------------------------


@pytest.mark.parametrize("token", [AGENT, SYSTEM, CLIENT])
def test_every_identity_can_read(client, token):
    r = client.get("/policy/resolve",
                   params={"agent": "scout", "crew": "cfo", "model": "sonnet"},
                   headers=_h(token))
    assert r.status_code == 200


def test_unauthenticated_read_is_refused(client):
    r = client.get("/policy/resolve",
                   params={"agent": "scout", "crew": "cfo", "model": "sonnet"},
                   headers={protocol.HEADER: str(protocol.CURRENT)})
    assert r.status_code == 401


def test_resolve_carries_the_class_defaults(client):
    r = client.get("/policy/resolve",
                   params={"agent": "scout", "crew": "cfo", "model": "sonnet"},
                   headers=_h(AGENT))
    body = r.json()
    assert body["explicit"] == {}
    assert "claude_ai_Gmail" in body["connector_servers"]
    assert "WebFetch" in body["exfiltration_builtins"]
    assert body["default"] == "allow"
    assert body["version"] == 0


def test_resolve_required_fields(client):
    r = client.get("/policy/resolve", params={"agent": "scout", "crew": "cfo"},
                   headers=_h(AGENT))
    assert r.status_code == 400
    assert "model" in r.json()["error"]


# --- write: capability -------------------------------------------------------


@pytest.mark.parametrize("token", [AGENT, SYSTEM])
def test_agent_and_system_cannot_write(client, token):
    r = _grant(client, token=token)
    assert r.status_code == 403
    assert "policy.write" in r.json()["error"]


def test_an_agent_holding_policy_write_is_refused_by_the_kind_check(
        client, monkeypatch):
    """Granting `policy.write` to the agent role gets the request past the
    capability check, so the 403 below can only come from the route's
    `approved_client` check. That refusal reuses the capability wording, so
    the assertion pins the exact text, and the positive control proves the
    capability is held rather than assuming the monkeypatch took effect."""
    monkeypatch.setitem(identity.ROLE_CAPS, "agent",
                        identity.ROLE_CAPS["agent"] | {"policy.write"})
    assert identity.authorized(identity.resolve(f"Bearer {AGENT}"),
                               "policy.write")

    r = _grant(client, token=AGENT)
    assert r.status_code == 403
    assert r.json()["error"] == "this identity lacks 'policy.write'"

    # The refused write left nothing behind: no explicit row, no version bump,
    # no history row.
    resolved = client.get(
        "/policy/resolve",
        params={"agent": "scout", "crew": "cfo", "model": "sonnet"},
        headers=_h(AGENT)).json()
    assert resolved["explicit"] == {}
    assert resolved["version"] == 0
    assert _events() == []


def test_unauthenticated_write_is_refused(client):
    r = client.put("/policy", json={"agent": "scout", "crew": "cfo",
                                    "model": "sonnet", "target": "WebFetch",
                                    "decision": "allow"},
                   headers={protocol.HEADER: str(protocol.CURRENT)})
    assert r.status_code == 401


def test_approved_client_can_write(client):
    r = _grant(client)
    assert r.status_code == 200
    out = r.json()
    assert out["decision"] == "allow"
    assert out["acting_human"] == "operator"
    assert out["attested_by"] == "orient"


def test_write_without_acting_human_is_refused(client):
    r = client.put("/policy", json={"agent": "scout", "crew": "cfo",
                                    "model": "sonnet", "target": "WebFetch",
                                    "decision": "allow"}, headers=_h(CLIENT))
    assert r.status_code == 403
    assert "acting_human" in r.json()["error"]


def test_the_acting_human_is_recorded_never_the_client(client):
    _grant(client, acting_human="operator")
    events = _events()
    assert len(events) == 1
    assert events[0]["actor"] == "operator"
    assert events[0]["actor"] != "orient"


# --- write: validation --------------------------------------------------------


def test_unknown_decision_is_refused(client):
    r = _grant(client, decision="maybe")
    assert r.status_code == 400
    assert "decision" in r.json()["error"]


@pytest.mark.parametrize("field", ["agent", "crew", "model", "target"])
def test_missing_field_is_refused(client, field):
    r = _grant(client, **{field: ""})
    assert r.status_code == 400
    assert field in r.json()["error"]


@pytest.mark.parametrize("field", ["agent", "crew", "model", "target"])
def test_whitespace_only_field_is_refused_not_silently_accepted(client, field):
    r = _grant(client, **{field: "   "})
    assert r.status_code == 400
    assert field in r.json()["error"]


def test_a_non_string_field_refuses_cleanly_rather_than_500ing(client):
    """A truthy int reaching `len()` unguarded, or a truthy list/dict reaching
    `store.clip` (which silently turns a non-string into `""`), are both the
    same failure class the whitespace fix exists to close, just entered
    through a wrong-typed field instead of incidental whitespace (reviewer,
    PR #41, round 2)."""
    r = _grant(client, agent=12345)
    assert r.status_code == 400
    assert "agent" in r.json()["error"]


def test_a_container_field_refuses_cleanly_rather_than_storing_empty(client):
    r = _grant(client, agent=["scout"])
    assert r.status_code == 400
    assert "agent" in r.json()["error"]

    resolved = client.get(
        "/policy/resolve",
        params={"agent": "", "crew": "cfo", "model": "sonnet"},
        headers=_h(AGENT))
    # THE REFUSED WRITE MUST NOT HAVE LANDED as an agent="" row — the read
    # side already refuses an empty agent, so this would be an unreachable
    # write if it had.
    assert resolved.status_code == 400


def test_oversized_field_is_refused(client, monkeypatch):
    monkeypatch.setattr(store, "MAX_POLICY_FIELD", 4)
    r = _grant(client, agent="way-too-long")
    assert r.status_code == 400
    assert "agent" in r.json()["error"]


def test_writes_disabled_refuses_the_same_way(client, monkeypatch):
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    r = _grant(client)
    assert r.status_code == 503
    assert "write path" in r.json()["error"]


# --- state: revise in place, not append ---------------------------------------


def test_a_second_grant_on_the_same_combination_overwrites_the_first(client):
    _grant(client, decision="allow")
    r = _grant(client, decision="deny")
    assert r.status_code == 200
    assert r.json()["decision"] == "deny"

    resolved = client.get(
        "/policy/resolve",
        params={"agent": "scout", "crew": "cfo", "model": "sonnet"},
        headers=_h(AGENT)).json()
    assert resolved["explicit"] == {"WebFetch": "deny"}

    # ONE ROW IN THE STATE TABLE, TWO IN THE HISTORY — revised, not
    # duplicated, with the full history of the change preserved beside it.
    conn = sqlite3.connect(store.DB_PATH)
    try:
        assert conn.execute("SELECT COUNT(*) FROM tool_policy").fetchone()[0] == 1
    finally:
        conn.close()
    assert len(_events()) == 2


def test_version_bumps_on_every_write_and_is_cheap_to_check(client):
    before = client.get("/policy/version", headers=_h(AGENT)).json()["version"]
    assert before == 0
    _grant(client, target="WebFetch")
    after_one = client.get("/policy/version", headers=_h(AGENT)).json()["version"]
    assert after_one == 1
    _grant(client, target="RemoteTrigger")
    after_two = client.get("/policy/version", headers=_h(AGENT)).json()["version"]
    assert after_two == 2


def test_different_targets_on_the_same_combination_coexist(client):
    _grant(client, target="WebFetch", decision="allow")
    _grant(client, target="claude_ai_Gmail", decision="deny")
    resolved = client.get(
        "/policy/resolve",
        params={"agent": "scout", "crew": "cfo", "model": "sonnet"},
        headers=_h(AGENT)).json()
    assert resolved["explicit"] == {"WebFetch": "allow", "claude_ai_Gmail": "deny"}


def test_incidental_whitespace_does_not_create_an_unreachable_grant(client):
    """A trailing space in a written field must not produce a row a clean
    read can never find again (reviewer, PR #41)."""
    r = _grant(client, agent="scout ", crew=" cfo", decision="deny")
    assert r.status_code == 200
    assert r.json()["agent"] == "scout"
    assert r.json()["crew"] == "cfo"

    resolved = client.get(
        "/policy/resolve",
        params={"agent": " scout", "crew": "cfo ", "model": "sonnet"},
        headers=_h(AGENT)).json()
    assert resolved["explicit"] == {"WebFetch": "deny"}


def test_a_different_combination_is_unaffected(client):
    _grant(client, agent="scout")
    resolved = client.get(
        "/policy/resolve",
        params={"agent": "consultant", "crew": "cfo", "model": "sonnet"},
        headers=_h(AGENT)).json()
    assert resolved["explicit"] == {}


# --- policy_class_default: the pure function the routes fall back to ---------


@pytest.mark.parametrize("target", sorted(store.POLICY_CONNECTOR_SERVERS))
def test_every_connector_server_defaults_denied(target):
    assert store.policy_class_default(target) == store.POLICY_DENY


def test_claude_docs_connector_defaults_denied():
    # Named literally: the parametrized test above reads the set it checks, so
    # it passes whatever the set leaves out. Claude Docs was the one missing.
    assert store.policy_class_default("claude_ai_Claude_Docs") == store.POLICY_DENY


@pytest.mark.parametrize("target", sorted(store.POLICY_EXFIL_BUILTINS))
def test_every_exfiltration_builtin_defaults_denied(target):
    assert store.policy_class_default(target) == store.POLICY_DENY


@pytest.mark.parametrize("target", ["Read", "Bash", "kb", "ops", "Monitor"])
def test_everything_else_defaults_allowed(target):
    assert store.policy_class_default(target) == store.POLICY_ALLOW
