"""A credential that may do exactly one thing: register a crew.

`registrar` (CQ·246) is the credential agent-platform's `agentctl crew
register` and `agentctl crew create` present to `POST /registry/crews`. Like
`review_record_writer`, it is a literal one-capability set, not a union over
`agent`, so a capability later added to `agent` cannot reach it.

Fixture slugs are synthetic (`oldmill`, `harrow`) — no real crew name.
"""
from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

import app as libapp
import identity
import project_registry
import protocol
import store

ACTOR = "test, 2026-10-26"
REGISTRAR = "registrar-token"
HARROW = {"slug": "harrow", "display": "Harrow", "repos": ["harrow-app"]}


def test_the_role_holds_exactly_one_capability():
    assert identity.ROLE_CAPS["registrar"] == frozenset({"registry.write"})


def test_the_role_is_nameable_by_a_token_file():
    assert "registrar" in identity.NAMEABLE_KINDS


def test_an_entry_naming_registry_write_in_grants_is_still_refused(tmp_path, monkeypatch):
    """`registrar` holds `registry.write` by role. The `grants` refusal for
    every kind but `orchestrator` is unchanged, so an agent entry still cannot
    name its way to the capability."""
    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        "agent-token": {"name": "plain-agent", "products": ["*"],
                        "grants": ["registry.write"]}}))
    monkeypatch.setattr(identity, "TOKENS", tokens)
    assert identity.resolve("Bearer agent-token") is None


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = tmp_path / "control.db"
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE project_registry (
            id INTEGER PRIMARY KEY,
            slug TEXT NOT NULL UNIQUE,
            entry TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            updated_by TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()
    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        REGISTRAR: {"name": "the-registrar", "products": ["*"],
                    "kind": "registrar"}}))
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    project_registry.replace({"oldmill": {"repos": ["oldmill-app"]}}, actor=ACTOR)
    return TestClient(libapp.app)


def _h():
    return {"Authorization": f"Bearer {REGISTRAR}",
            protocol.HEADER: str(protocol.CURRENT)}


def test_a_registrar_registers_a_crew_under_its_own_name(client):
    r = client.post("/registry/crews", headers=_h(), json=HARROW)
    assert r.status_code == 201
    assert r.json()["updated_by"] == "the-registrar"


def test_a_registrar_gets_the_rows_own_refusal_for_an_invalid_row(client):
    r = client.post("/registry/crews", headers=_h(),
                    json={"slug": "Harrow!", "display": "Harrow",
                          "repos": ["harrow-app"]})
    assert r.status_code == 400
    assert "slug" in r.json()["error"]


@pytest.mark.parametrize("path", ["/registry", "/roster"])
def test_a_registrar_reads_nothing(client, path):
    r = client.get(path, headers=_h())
    assert r.status_code == 403
