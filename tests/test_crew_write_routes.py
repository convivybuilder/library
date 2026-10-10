"""`PUT`/`DELETE /roster/{name}` (issue_01M34STNFA2CSD78PW5Y9XHX1M) — the
roster's write surface, gated on `roster.write` so an orchestrator's
credential can change crew composition and a dispatched worker's cannot.

**Presented as real credentials over the HTTP routes, not asserted from
`identity.ROLE_CAPS`** — the epic's own DoD language
(issue_01M092R482KSQHKKVG4XJ8X257): "A dispatched worker's credential is
refused when it tries to change a crew's roster, and an orchestrator's is
accepted. Tested by presenting each rather than by reading the capability
table." `crew_registry.roster_add`/`roster_remove`'s own capability check
is already pinned at the function boundary by `test_crew_registry.py`; this
file is the route's translation of what those functions raise into an HTTP
refusal, exercised with a `TestClient` and three real token-file entries —
`orchestrator`, `agent` and `system` — the same fixture shape
`test_crew_read_surface.py` uses for its read routes.
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

ORCHESTRATOR = "orchestrator-token"
AGENT = "agent-token"
SYSTEM = "system-token"


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = tmp_path / "control.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE roster_agents (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            created_by TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            updated_by TEXT NOT NULL
        );
        CREATE TABLE profiles (
            id INTEGER PRIMARY KEY,
            project TEXT NOT NULL,
            variant TEXT NOT NULL,
            include TEXT NOT NULL,
            claude_md_extras TEXT NOT NULL,
            created_at TEXT NOT NULL,
            created_by TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            updated_by TEXT NOT NULL,
            UNIQUE (project, variant)
        );
    """)
    conn.commit()
    conn.close()

    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        ORCHESTRATOR: {"name": "co-library", "products": ["*"],
                       "kind": "orchestrator"},
        AGENT: {"name": "a-worker", "products": ["*"]},
        SYSTEM: {"name": "capture", "products": ["*"], "kind": "system"},
    }))

    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    return TestClient(libapp.app)


def _h(token):
    return {"Authorization": f"Bearer {token}",
            protocol.HEADER: str(protocol.CURRENT)}


def _roster_names(client):
    r = client.get("/roster", headers=_h(AGENT))
    assert r.status_code == 200
    return {a["name"] for a in r.json()["agents"]}


# --- an orchestrator may add and remove ------------------------------------


def test_orchestrator_adds_a_name_and_it_appears_in_get_roster(client):
    r = client.put("/roster/scout", headers=_h(ORCHESTRATOR))
    assert r.status_code == 200
    assert r.json()["name"] == "scout"
    assert "scout" in _roster_names(client)


def test_orchestrator_add_is_idempotent(client):
    r1 = client.put("/roster/scout", headers=_h(ORCHESTRATOR))
    r2 = client.put("/roster/scout", headers=_h(ORCHESTRATOR))
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert _roster_names(client) == {"scout"}


def test_orchestrator_removes_a_name_and_it_disappears(client):
    client.put("/roster/scout", headers=_h(ORCHESTRATOR))
    assert "scout" in _roster_names(client)
    r = client.delete("/roster/scout", headers=_h(ORCHESTRATOR))
    assert r.status_code == 200
    assert r.json()["name"] == "scout"
    assert "scout" not in _roster_names(client)


def test_removing_an_absent_name_is_404(client):
    r = client.delete("/roster/nonexistent", headers=_h(ORCHESTRATOR))
    assert r.status_code == 404


def test_an_invalid_name_is_400_on_add_and_on_remove(client):
    too_long = "x" * (store.MAX_IDENT + 1)
    r_add = client.put(f"/roster/{too_long}", headers=_h(ORCHESTRATOR))
    assert r_add.status_code == 400
    r_remove = client.delete(f"/roster/{too_long}", headers=_h(ORCHESTRATOR))
    assert r_remove.status_code == 400
    assert _roster_names(client) == set()


# --- a worker's credential is refused, and the roster is unchanged ---------


@pytest.mark.parametrize("token", [AGENT, SYSTEM])
def test_a_non_orchestrator_credential_is_refused_403_on_add(client, token):
    r = client.put("/roster/scout", headers=_h(token))
    assert r.status_code == 403
    assert "roster.write" in r.json()["error"]
    assert _roster_names(client) == set()


@pytest.mark.parametrize("token", [AGENT, SYSTEM])
def test_a_non_orchestrator_credential_is_refused_403_on_remove(
        client, token):
    client.put("/roster/scout", headers=_h(ORCHESTRATOR))
    r = client.delete("/roster/scout", headers=_h(token))
    assert r.status_code == 403
    assert "roster.write" in r.json()["error"]
    assert _roster_names(client) == {"scout"}


# --- the shadow gate still refuses through this route -----------------------


def test_add_refuses_503_while_writes_are_disabled(client, monkeypatch):
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    r = client.put("/roster/scout", headers=_h(ORCHESTRATOR))
    assert r.status_code == 503


def test_remove_refuses_503_while_writes_are_disabled(client, monkeypatch):
    client.put("/roster/scout", headers=_h(ORCHESTRATOR))
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    r = client.delete("/roster/scout", headers=_h(ORCHESTRATOR))
    assert r.status_code == 503
