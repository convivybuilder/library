"""`POST /black-box/registrations` (issue_01M19QQNTWD4RD9MSZZRKHYM7N): a
black-box crew confirms it belongs to a black-box project and declares the
tool permissions, model and gateway bindings it wants.

**Most of this file is proving the single-mechanism claim**: the declared
permissions land in `tool_policy` through `store.upsert_policy`, the exact
function `PUT /policy` uses, rather than a second store for the same fact —
and proving the closed-set audit ("sees no content") from every angle a
black box's own content could try to reach the Library through: an unknown
top-level field, an unknown permission field, and an over-long identifier.
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
        CREATE TABLE black_box_registrations (
            id INTEGER PRIMARY KEY, crew TEXT NOT NULL UNIQUE,
            model TEXT NOT NULL, gateway TEXT NOT NULL,
            created_by TEXT NOT NULL, created_at TEXT NOT NULL,
            updated_by TEXT NOT NULL, updated_at TEXT NOT NULL);
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


def _body(**over):
    body = {
        "acting_human": "operator", "crew": "cfo", "model": "sonnet",
        "gateway": "anthropic",
        "permissions": [
            {"agent": "scout", "model": "sonnet", "target": "WebFetch",
             "decision": "allow"},
        ],
    }
    body.update(over)
    return body


def _register(client, token=CLIENT, **over):
    return client.post("/black-box/registrations", json=_body(**over),
                       headers=_h(token))


def _registrations():
    conn = sqlite3.connect(store.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in
                conn.execute("SELECT * FROM black_box_registrations").fetchall()]
    finally:
        conn.close()


def _policy_events():
    conn = sqlite3.connect(store.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM tool_policy_events ORDER BY id").fetchall()
    finally:
        conn.close()


# --- write: approved-client-only, matching write_policy's shape --------------


def test_an_agent_cannot_register(client):
    r = _register(client, token=AGENT)
    assert r.status_code == 403
    assert "policy.write" in r.json()["error"]


def test_a_system_identity_cannot_register(client):
    """`system` sits below `approved_client` in the role hierarchy and does
    not inherit its human-reserved capabilities — the same boundary
    `policy.write` draws for `write_policy` itself."""
    r = _register(client, token=SYSTEM)
    assert r.status_code == 403
    assert "policy.write" in r.json()["error"]


def test_an_unauthenticated_request_is_401(client):
    r = client.post("/black-box/registrations", json=_body(),
                    headers={protocol.HEADER: str(protocol.CURRENT)})
    assert r.status_code == 401


def test_acting_human_is_required(client):
    body = _body()
    del body["acting_human"]
    r = client.post("/black-box/registrations", json=body, headers=_h(CLIENT))
    assert r.status_code == 403
    assert "acting_human" in r.json()["error"]


def test_acting_human_is_recorded_separately_from_attested_by(client):
    r = _register(client)
    assert r.status_code == 200
    out = r.json()
    assert out["acting_human"] == "operator"
    assert out["attested_by"] == "orient"
    assert out["acting_human"] != out["attested_by"]


# --- write: the closed set, refusing every content-shaped field --------------


@pytest.mark.parametrize("field,value", [
    ("note", "a note about this crew"),
    ("description", "what this crew does"),
    ("path", "/some/worktree/path"),
    ("body", "free text"),
    ("totally_unknown_field", "x"),
])
def test_a_content_shaped_top_level_field_is_refused(client, field, value):
    r = _register(client, **{field: value})
    assert r.status_code == 400
    assert field in r.json()["error"]


def test_a_content_shaped_permission_field_is_refused(client):
    r = _register(client, permissions=[
        {"agent": "scout", "model": "sonnet", "target": "WebFetch",
         "decision": "allow", "note": "why this grant exists"},
    ])
    assert r.status_code == 400
    assert "note" in r.json()["error"]


# --- write: identifier bounds -------------------------------------------------


def test_an_over_long_identifier_is_refused_at_max_ident(client):
    r = _register(client, crew="x" * (store.MAX_IDENT + 1))
    assert r.status_code == 400
    assert "crew" in r.json()["error"]


def test_an_over_long_permission_field_is_refused_at_max_ident(client):
    r = _register(client, permissions=[
        {"agent": "x" * (store.MAX_IDENT + 1), "model": "sonnet",
         "target": "WebFetch", "decision": "allow"},
    ])
    assert r.status_code == 400
    assert "agent" in r.json()["error"]


def test_an_unknown_decision_in_a_permission_is_refused(client):
    r = _register(client, permissions=[
        {"agent": "scout", "model": "sonnet", "target": "WebFetch",
         "decision": "maybe"},
    ])
    assert r.status_code == 400
    assert "decision" in r.json()["error"]


def test_the_permissions_list_bound_is_enforced(client, monkeypatch):
    monkeypatch.setattr(store, "MAX_BLACK_BOX_PERMISSIONS", 2)
    r = _register(client, permissions=[
        {"agent": "scout", "model": "sonnet", "target": "WebFetch",
         "decision": "allow"},
        {"agent": "consultant", "model": "sonnet", "target": "claude_ai_Gmail",
         "decision": "deny"},
        {"agent": "librarian", "model": "sonnet", "target": "Read",
         "decision": "allow"},
    ])
    assert r.status_code == 400
    assert "permissions" in r.json()["error"]


# --- the single mechanism: permissions land in tool_policy, not a new store --


def test_permissions_land_in_tool_policy_readable_through_resolve(client):
    r = _register(client, permissions=[
        {"agent": "scout", "model": "sonnet", "target": "WebFetch",
         "decision": "allow"},
        {"agent": "consultant", "model": "sonnet", "target": "claude_ai_Gmail",
         "decision": "deny"},
    ])
    assert r.status_code == 200
    assert r.json()["permissions_count"] == 2

    scout = client.get(
        "/policy/resolve",
        params={"agent": "scout", "crew": "cfo", "model": "sonnet"},
        headers=_h(AGENT)).json()
    assert scout["explicit"] == {"WebFetch": "allow"}

    consultant = client.get(
        "/policy/resolve",
        params={"agent": "consultant", "crew": "cfo", "model": "sonnet"},
        headers=_h(AGENT)).json()
    assert consultant["explicit"] == {"claude_ai_Gmail": "deny"}


def test_tool_policy_events_gains_a_row_per_permission(client):
    _register(client, permissions=[
        {"agent": "scout", "model": "sonnet", "target": "WebFetch",
         "decision": "allow"},
        {"agent": "consultant", "model": "sonnet", "target": "claude_ai_Gmail",
         "decision": "deny"},
        {"agent": "librarian", "model": "sonnet", "target": "Read",
         "decision": "allow"},
    ])
    events = _policy_events()
    assert len(events) == 3
    assert {e["actor"] for e in events} == {"operator"}


# --- upsert on re-registration, not a duplicate row ---------------------------


def test_re_registering_the_same_crew_upserts(client):
    r1 = _register(client, model="sonnet", gateway="anthropic")
    assert r1.status_code == 200
    r2 = _register(client, model="opus", gateway="bedrock")
    assert r2.status_code == 200
    assert r2.json()["model"] == "opus"
    assert r2.json()["gateway"] == "bedrock"

    rows = _registrations()
    assert len(rows) == 1
    assert rows[0]["model"] == "opus"
    assert rows[0]["gateway"] == "bedrock"


# --- the Ask case: an unregistered crew registers successfully ---------------


def test_a_crew_the_registry_does_not_know_registers_successfully(client):
    """`projects.private_repos()` narrows to `{'cfo', 'ea'}`-shaped registry
    entries and ADR-0043 is explicit that Ask is deliberately UNREGISTERED —
    it is a black box with no registry entry at all. This route must not
    gate on that registry, or it would refuse the registration of a crew
    that genuinely is a black box. `ask` here stands in for exactly that
    case."""
    r = _register(client, crew="ask")
    assert r.status_code == 200
    assert r.json()["crew"] == "ask"


# --- the shadow gate -----------------------------------------------------------


def test_registration_refuses_while_writes_are_disabled(client, monkeypatch):
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    r = _register(client)
    assert r.status_code == 503


@pytest.mark.parametrize("bad", [{}, [], 7, True, None])
@pytest.mark.parametrize("field", ["crew", "model", "gateway"])
def test_a_non_string_top_level_field_is_a_400_not_a_crash(client, field, bad):
    """**The "sees no content" property has to hold against a body that is not
    shaped like text at all**, not only against text in the wrong field. The
    code already guards with `isinstance`; nothing pinned it, and an
    unguarded one of these is how a dict reaches a store that promised to
    hold identifiers (reviewer, PR #104)."""
    r = _register(client, **{field: bad})
    assert r.status_code == 400


@pytest.mark.parametrize("bad", [{}, [], 7])
def test_an_unhashable_decision_is_a_400_not_a_500(client, bad):
    """**`decision` was the one field that skipped its type check**, and a
    dict or list is UNHASHABLE — `x not in frozenset(...)` raises
    `TypeError` rather than answering False, so `{"decision": []}` reached
    the client as an unhandled 500 (reviewer, PR #104).

    Parametrized over an int as well, which IS hashable and merely absent
    from the set: both must give the same clean refusal, so the fix cannot
    be mistaken for special-casing the crash.
    """
    r = _register(client, permissions=[{"agent": "a", "model": "m",
                                        "target": "t", "decision": bad}])
    assert r.status_code == 400
    assert "decision" in r.json()["error"]


@pytest.mark.parametrize("bad", [{}, []])
def test_the_sibling_policy_route_refuses_an_unhashable_decision_too(client, bad):
    """The identical crash lived in `PUT /policy`, which this PR did not add.
    Fixed in both places: the same defect two hundred lines apart with one
    side repaired reads as a deliberate difference rather than an oversight."""
    r = client.put("/policy", json={
        "acting_human": "operator", "agent": "a", "crew": "c", "model": "m",
        "target": "t", "decision": bad}, headers=_h(CLIENT))
    assert r.status_code == 400


# --- read: a registration's declared model and gateway read back exactly ----
# (issue_01M34SVJK9AVJX7VM8Y8A23HR7)

REGISTRATION_KEYS = {"crew", "model", "gateway", "created_by", "created_at",
                     "updated_by", "updated_at"}


def test_a_registered_crews_model_and_gateway_read_back_exactly(client):
    r = _register(client, crew="cfo", model="sonnet", gateway="anthropic")
    assert r.status_code == 200

    got = client.get("/black-box/registrations/cfo", headers=_h(CLIENT))
    assert got.status_code == 200
    body = got.json()
    assert body["crew"] == "cfo"
    assert body["model"] == "sonnet"
    assert body["gateway"] == "anthropic"


def test_the_single_registration_key_set_is_exactly_the_documented_set(client):
    _register(client, crew="cfo")
    got = client.get("/black-box/registrations/cfo", headers=_h(CLIENT))
    assert set(got.json().keys()) == REGISTRATION_KEYS


def test_an_unregistered_crew_is_404(client):
    got = client.get("/black-box/registrations/nobody-registered-this",
                     headers=_h(CLIENT))
    assert got.status_code == 404


def test_a_caller_without_policy_read_is_refused(client, monkeypatch):
    monkeypatch.setattr(identity, "ROLE_CAPS",
                        {**identity.ROLE_CAPS,
                         "approved_client":
                             identity.ROLE_CAPS["approved_client"]
                             - {"policy.read"}})
    _register(client, crew="cfo")
    got = client.get("/black-box/registrations/cfo", headers=_h(CLIENT))
    assert got.status_code == 403
    assert "policy.read" in got.json()["error"]

    listed = client.get("/black-box/registrations", headers=_h(CLIENT))
    assert listed.status_code == 403
    assert "policy.read" in listed.json()["error"]


def test_an_agent_kind_credential_is_refused_on_both_routes(client):
    # `policy.read` is held by `agent` by default (see `identity.ROLE_CAPS`),
    # so this exercises the `ident.kind != "approved_client"` refusal, not
    # the capability check above it — security-reviewer, PR #174 round 1.
    _register(client, crew="cfo")

    got = client.get("/black-box/registrations/cfo", headers=_h(AGENT))
    assert got.status_code == 403
    assert "policy.read" in got.json()["error"]

    listed = client.get("/black-box/registrations", headers=_h(AGENT))
    assert listed.status_code == 403
    assert "policy.read" in listed.json()["error"]


def test_a_system_kind_credential_is_refused_on_both_routes(client):
    _register(client, crew="cfo")

    got = client.get("/black-box/registrations/cfo", headers=_h(SYSTEM))
    assert got.status_code == 403
    assert "policy.read" in got.json()["error"]

    listed = client.get("/black-box/registrations", headers=_h(SYSTEM))
    assert listed.status_code == 403
    assert "policy.read" in listed.json()["error"]


def test_an_approved_client_reads_both_routes(client):
    _register(client, crew="cfo", model="sonnet", gateway="anthropic")

    got = client.get("/black-box/registrations/cfo", headers=_h(CLIENT))
    assert got.status_code == 200
    assert got.json()["crew"] == "cfo"

    listed = client.get("/black-box/registrations", headers=_h(CLIENT))
    assert listed.status_code == 200
    assert listed.json()["count"] == 1


def test_list_registrations_refuses_any_parameter(client):
    r = client.get("/black-box/registrations?crew=cfo", headers=_h(CLIENT))
    assert r.status_code == 400
    assert r.json()["unknown"] == ["crew"]
    assert r.json()["supported"] == []


def test_get_registration_refuses_any_parameter(client):
    _register(client, crew="cfo", model="sonnet", gateway="anthropic")
    r = client.get("/black-box/registrations/cfo?model=opus", headers=_h(CLIENT))
    assert r.status_code == 400
    assert r.json()["unknown"] == ["model"]
    assert r.json()["supported"] == []


def test_the_list_route_returns_each_registered_crew_once(client):
    _register(client, crew="cfo", model="sonnet", gateway="anthropic")
    _register(client, crew="ea", model="opus", gateway="bedrock")
    _register(client, crew="cfo", model="opus", gateway="bedrock")

    got = client.get("/black-box/registrations", headers=_h(CLIENT))
    assert got.status_code == 200
    body = got.json()
    assert body["count"] == 2
    crews = [r["crew"] for r in body["registrations"]]
    assert sorted(crews) == ["cfo", "ea"]
    assert len(crews) == len(set(crews))

    cfo = next(r for r in body["registrations"] if r["crew"] == "cfo")
    assert cfo["model"] == "opus"


def test_a_crew_named_with_a_slash_is_reachable_through_the_path_converter(client):
    # `_clean_black_box_ident` puts no character restriction on `crew`
    # (reviewer, PR #174 round 1) — without `{crew:path}` this would 404
    # before the handler ran, indistinguishable from "never registered".
    r = _register(client, crew="finance/cfo", model="sonnet",
                  gateway="anthropic")
    assert r.status_code == 200

    got = client.get("/black-box/registrations/finance/cfo",
                     headers=_h(CLIENT))
    assert got.status_code == 200
    assert got.json()["crew"] == "finance/cfo"


def test_an_over_long_crew_path_value_is_refused(client):
    got = client.get(
        f"/black-box/registrations/{'x' * (store.MAX_POLICY_FIELD + 1)}",
        headers=_h(CLIENT))
    assert got.status_code == 400
    assert "crew" in got.json()["error"]
