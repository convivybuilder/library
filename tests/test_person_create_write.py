"""`POST /people` — the administrator route that creates a person record.

Orient is moving its project authorization from its local `roles.json` to
the Library's person records (Orient's E19·1). A contributor who holds
every product in `roles.json` today has no Library person record, and
nothing short of `bootstrap_library.py` (which refuses to run a second
time) could create one. This route is that gap closed: an administrator
creates the record, then grants projects and sets the email through the
existing routes.

**Gated exactly as `POST /people/{person_id}/projects` and `PUT
/people/{person_id}/email` are**, so this file's fixture and its first
block of tests are those two routes' own counterpart: the credential comes
only from the client-token file, and a non-administrator's refusal is
byte-identical across every cause.

Every email in this file is a synthetic `example.com` address.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as libapp
import identity
import projects
import protocol
import store

REPO = Path(__file__).resolve().parent.parent

ADMIN = "person_admin"
PLAIN = "person_plain"
OTHER = "person_other"

ADMIN_TOKEN = "admin-client-token"
PLAIN_TOKEN = "plain-person-client-token"
ORIENT_TOKEN = "orient-client-token"
AGENT_TOKEN = "agent-token"
FORGED_AGENT_TOKEN = "agent-token-named-for-an-admin"


def _migrate(db):
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=str(REPO),
        capture_output=True, text=True, check=False,
        env={"PATH": "/usr/bin:/bin", "HOME": str(REPO), "LIBRARY_DB": str(db)})
    assert result.returncode == 0, result.stderr


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = tmp_path / "library.db"
    _migrate(db)
    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO people (id, display_name, created_at, created_by, "
        "updated_at, updated_by) VALUES (?, ?, '2026-01-01', 'x', "
        "'2026-01-01', 'x')",
        [(ADMIN, "Ada Admin"), (PLAIN, "Pat Plain"), (OTHER, "Oleg Other")])
    conn.execute(
        "INSERT INTO administrators (person_id, granted_at, granted_by) "
        "VALUES (?, '2026-01-01', 'x')", (ADMIN,))
    conn.execute(
        "UPDATE people SET sign_in_email = ? WHERE id = ?",
        ("oleg@example.com", OTHER))
    conn.commit()
    conn.close()

    client_tokens = tmp_path / "client-tokens.json"
    client_tokens.write_text(json.dumps({
        ADMIN_TOKEN: {"name": ADMIN, "products": ["*"]},
        PLAIN_TOKEN: {"name": PLAIN, "products": ["*"]},
        ORIENT_TOKEN: {"name": "orient", "products": ["*"]},
    }))
    agent_tokens = tmp_path / "api-tokens.json"
    agent_tokens.write_text(json.dumps({
        AGENT_TOKEN: {"name": "an-agent", "products": ["*"]},
        FORGED_AGENT_TOKEN: {"name": ADMIN, "products": ["*"]},
    }))

    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "CLIENT_TOKENS", client_tokens)
    monkeypatch.setattr(identity, "TOKENS", agent_tokens)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({"vivy": {}}))
    monkeypatch.setattr(projects, "REGISTRY_PATH", registry)
    projects.reload()
    test_client = TestClient(libapp.app)
    test_client.db = db
    yield test_client
    projects.reload()


def _h(token=None):
    headers = {protocol.HEADER: str(protocol.CURRENT)}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _post(client, token, body):
    return client.post("/people", json=body, headers=_h(token))


def _wire(response):
    return (response.status_code, response.content, dict(response.headers))


def _people(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM people ORDER BY created_at, id").fetchall()]
    finally:
        conn.close()


def _email_changes(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM person_email_changes ORDER BY id").fetchall()]
    finally:
        conn.close()


# --- refusals disclose nothing -----------------------------------------------


def test_every_refusal_before_the_body_is_one_response(client):
    body = {"display_name": "Tara Target"}
    reference = _wire(_post(client, PLAIN_TOKEN, body))
    callers = {
        "no credential": _post(client, None, body),
        "an unknown token": _post(client, "not-a-token", body),
        "an approved client that is nobody's": _post(client, ORIENT_TOKEN, body),
        "a person who is not an administrator": _post(client, PLAIN_TOKEN, body),
        "an agent credential": _post(client, AGENT_TOKEN, body),
        "an agent-file entry named for an administrator":
            _post(client, FORGED_AGENT_TOKEN, body),
    }
    for name, response in callers.items():
        assert _wire(response) == reference, name
    assert len(_people(client.db)) == 3


def test_a_malformed_body_never_tells_a_refused_caller_from_an_administrator(
        client):
    for raw in (b"not json", b"[]", b'{"display_name": 5}', b"{}"):
        refused = client.post("/people", content=raw,
                              headers={**_h(PLAIN_TOKEN),
                                       "Content-Type": "application/json"})
        reference = client.post(
            "/people", json={"display_name": "Tara Target"},
            headers=_h(PLAIN_TOKEN))
        assert _wire(refused) == _wire(reference), raw


# --- creation -----------------------------------------------------------------


def test_an_administrator_creates_a_person_without_an_email(client):
    before = len(_people(client.db))
    r = _post(client, ADMIN_TOKEN, {"display_name": "Tara Target"})
    assert r.status_code == 201
    body = r.json()
    assert body["display_name"] == "Tara Target"
    assert body["sign_in_email"] is None
    assert body["created_by"] == ADMIN
    assert body["person_id"].startswith("person_")
    rows = _people(client.db)
    assert len(rows) == before + 1
    row = [r for r in rows if r["id"] == body["person_id"]][0]
    assert row["display_name"] == "Tara Target"
    assert row["created_by"] == ADMIN
    assert row["updated_by"] == ADMIN
    assert row["sign_in_email"] is None


def test_an_administrator_creates_a_person_with_an_email(client):
    r = _post(client, ADMIN_TOKEN,
              {"display_name": "Tara Target", "email": "  Tara@Example.COM  "})
    assert r.status_code == 201
    body = r.json()
    assert body["sign_in_email"] == "tara@example.com"
    rows = _people(client.db)
    row = [r for r in rows if r["id"] == body["person_id"]][0]
    assert row["sign_in_email"] == "tara@example.com"
    changes = _email_changes(client.db)
    assert len(changes) == 1
    assert changes[0]["person_id"] == body["person_id"]
    assert changes[0]["email"] == "tara@example.com"
    assert changes[0]["changed_by"] == ADMIN


def test_two_people_may_not_hold_the_same_email_under_any_casing(client):
    before = len(_people(client.db))
    r = _post(client, ADMIN_TOKEN,
              {"display_name": "Tara Target", "email": "OLEG@EXAMPLE.COM"})
    assert r.status_code == 409
    assert OTHER not in json.dumps(r.json())
    assert "Oleg" not in json.dumps(r.json())
    assert len(_people(client.db)) == before
    assert _email_changes(client.db) == []


# --- display_name validation --------------------------------------------------


@pytest.mark.parametrize("raw", [5, None, [], {}, ""])
def test_a_wrong_typed_or_empty_display_name_is_a_400(client, raw):
    before = len(_people(client.db))
    r = _post(client, ADMIN_TOKEN, {"display_name": raw})
    assert r.status_code == 400, (raw, r.text)
    assert len(_people(client.db)) == before


def test_a_body_missing_display_name_entirely_is_a_400(client):
    before = len(_people(client.db))
    r = _post(client, ADMIN_TOKEN, {"email": "tara@example.com"})
    assert r.status_code == 400
    assert len(_people(client.db)) == before


def test_a_whitespace_only_display_name_is_a_400(client):
    r = _post(client, ADMIN_TOKEN, {"display_name": "   "})
    assert r.status_code == 400


def test_surrounding_whitespace_is_trimmed_rather_than_refused(client):
    r = _post(client, ADMIN_TOKEN, {"display_name": "  Tara Target  "})
    assert r.status_code == 201
    assert r.json()["display_name"] == "Tara Target"


def test_a_display_name_over_the_bound_is_a_400(client):
    too_long = "a" * (store.MAX_DISPLAY_NAME + 1)
    before = len(_people(client.db))
    r = _post(client, ADMIN_TOKEN, {"display_name": too_long})
    assert r.status_code == 400
    assert len(_people(client.db)) == before


def test_a_display_name_at_the_bound_is_accepted(client):
    at_bound = "a" * store.MAX_DISPLAY_NAME
    r = _post(client, ADMIN_TOKEN, {"display_name": at_bound})
    assert r.status_code == 201


@pytest.mark.parametrize("raw", [
    "no-at-sign.example.com", "two@at@example.com", "has space@example.com",
    "", "   ", 5, ["a@example.com"],
])
def test_an_implausible_or_wrong_typed_email_is_a_400(client, raw):
    before = len(_people(client.db))
    r = _post(client, ADMIN_TOKEN,
              {"display_name": "Tara Target", "email": raw})
    assert r.status_code == 400, (raw, r.text)
    assert len(_people(client.db)) == before


def test_an_email_over_the_bound_is_a_400(client):
    too_long = "a" * (store.MAX_EMAIL - len("@example.com") + 1) + "@example.com"
    assert len(too_long) > store.MAX_EMAIL
    before = len(_people(client.db))
    r = _post(client, ADMIN_TOKEN,
              {"display_name": "Tara Target", "email": too_long})
    assert r.status_code == 400
    assert len(_people(client.db)) == before


# --- the new person is reachable through the existing routes -----------------


def test_the_new_person_reads_back_with_an_empty_project_set(client):
    created = _post(client, ADMIN_TOKEN, {"display_name": "Tara Target"}).json()
    r = client.get(f"/people/{created['person_id']}/projects",
                   headers=_h(ADMIN_TOKEN))
    assert r.status_code == 200
    assert r.json() == {"projects": [], "count": 0}


def test_the_new_person_is_found_by_email(client):
    created = _post(client, ADMIN_TOKEN, {
        "display_name": "Tara Target", "email": "tara@example.com"}).json()
    r = client.post("/people/by-email/projects",
                    json={"email": "TARA@example.com"},
                    headers=_h(ADMIN_TOKEN))
    assert r.status_code == 200
    assert r.json() == {"projects": [], "count": 0}
    assert created["person_id"]  # sanity: creation itself succeeded


def test_projects_may_be_granted_to_the_new_person(client):
    created = _post(client, ADMIN_TOKEN, {"display_name": "Tara Target"}).json()
    pid = created["person_id"]
    r = client.post(f"/people/{pid}/projects", json={"grant": ["vivy"]},
                    headers=_h(ADMIN_TOKEN))
    assert r.status_code == 200
    assert r.json()["granted"] == ["vivy"]
    r = client.get(f"/people/{pid}/projects", headers=_h(ADMIN_TOKEN))
    assert r.json() == {"projects": ["vivy"], "count": 1}


def test_the_email_may_be_set_on_the_new_person_afterwards(client):
    created = _post(client, ADMIN_TOKEN, {"display_name": "Tara Target"}).json()
    pid = created["person_id"]
    r = client.put(f"/people/{pid}/email", json={"email": "tara@example.com"},
                   headers=_h(ADMIN_TOKEN))
    assert r.status_code == 200
    assert r.json() == {"person_id": pid, "email": "tara@example.com",
                        "changed_by": ADMIN}


# --- the read-only shadow -----------------------------------------------------


def test_the_write_is_refused_while_the_library_is_read_only(client, monkeypatch):
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    before = len(_people(client.db))
    r = _post(client, ADMIN_TOKEN, {"display_name": "Tara Target"})
    assert r.status_code == 503
    assert len(_people(client.db)) == before


# --- the store function directly ----------------------------------------------


def test_the_store_function_refuses_a_non_administrator(client):
    assert store.create_person(PLAIN, "Tara Target") is None
    assert len(_people(client.db)) == 3


def test_the_store_function_refuses_a_held_email_and_writes_nothing(client):
    before = len(_people(client.db))
    assert store.create_person(
        ADMIN, "Tara Target", "oleg@example.com") is store.EMAIL_TAKEN
    assert len(_people(client.db)) == before
    assert _email_changes(client.db) == []
