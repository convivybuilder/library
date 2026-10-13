"""`POST /people/by-email/projects` (issue_01M4QQZ7VTANJ5ZGF630DCB0Y7) —
Orient reads a person's projects by their sign-in email, in exactly the
shape `GET /people/{person_id}/projects` already answers it in.

Fixture built the way `test_person_projects_route.py`'s own is: a fresh
`tmp_path` database carrying only the tables this route reads, plus
`people.sign_in_email` and `administrators` for the two cases that route
did not need. Every email in this file is a synthetic `example.com` address.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as libapp
import identity
import projects
import protocol
import store

ORIENT = "orient-token"
SCOPED = "scoped-client-token"
NOSCOPE_CLIENT = "noscope-client-token"
AGENT = "agent-token"

MULTI = "person_multi"
EMPTY = "person_empty"
ADMIN_PERSON = "person_admin"


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = tmp_path / "control.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE people (
            id TEXT PRIMARY KEY, display_name TEXT NOT NULL,
            created_at TEXT NOT NULL, created_by TEXT NOT NULL,
            updated_at TEXT NOT NULL, updated_by TEXT NOT NULL,
            sign_in_email TEXT
        );
        CREATE UNIQUE INDEX ix_people_sign_in_email ON people (sign_in_email);
        CREATE TABLE person_products (
            person_id TEXT NOT NULL, product TEXT NOT NULL,
            granted_at TEXT NOT NULL, granted_by TEXT NOT NULL,
            PRIMARY KEY (person_id, product)
        );
        CREATE TABLE person_product_removals (
            person_id TEXT NOT NULL, product TEXT NOT NULL,
            removed_at TEXT NOT NULL, removed_by TEXT NOT NULL,
            PRIMARY KEY (person_id, product)
        );
        CREATE TABLE administrators (
            person_id TEXT PRIMARY KEY, granted_at TEXT NOT NULL,
            granted_by TEXT NOT NULL
        );
    """)
    conn.execute(
        "INSERT INTO people (id, display_name, created_at, created_by, "
        "updated_at, updated_by, sign_in_email) VALUES (?, 'Empty Person', "
        "'2026-01-01', 'x', '2026-01-01', 'x', 'empty@example.com')",
        (EMPTY,))
    conn.execute(
        "INSERT INTO people (id, display_name, created_at, created_by, "
        "updated_at, updated_by, sign_in_email) VALUES (?, 'Multi Person', "
        "'2026-01-01', 'x', '2026-01-01', 'x', 'multi@example.com')",
        (MULTI,))
    conn.execute(
        "INSERT INTO people (id, display_name, created_at, created_by, "
        "updated_at, updated_by, sign_in_email) VALUES (?, 'Ada Admin', "
        "'2026-01-01', 'x', '2026-01-01', 'x', 'ada@example.com')",
        (ADMIN_PERSON,))
    conn.execute(
        "INSERT INTO administrators (person_id, granted_at, granted_by) "
        "VALUES (?, '2026-01-01', 'x')", (ADMIN_PERSON,))
    conn.executemany(
        "INSERT INTO person_products (person_id, product, granted_at, "
        "granted_by) VALUES (?, ?, '2026-01-01', 'x')",
        [(MULTI, "vivy"), (MULTI, "constance"), (MULTI, "hollis")])
    conn.commit()
    conn.close()

    tokens = tmp_path / "client-tokens.json"
    tokens.write_text(json.dumps({
        ORIENT: {"name": "orient", "products": ["*"]},
        SCOPED: {"name": "a-scoped-client", "products": ["vivy", "hollis"]},
        NOSCOPE_CLIENT: {"name": "a-scopeless-client", "products": []},
    }))
    agent_tokens = tmp_path / "api-tokens.json"
    agent_tokens.write_text(json.dumps({
        AGENT: {"name": "an-agent", "products": ["*"]},
    }))

    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "CLIENT_TOKENS", tokens)
    monkeypatch.setattr(identity, "TOKENS", agent_tokens)
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({"vivy": {}, "constance": {}, "hollis": {}}))
    monkeypatch.setattr(projects, "REGISTRY_PATH", registry)
    projects.reload()
    yield TestClient(libapp.app)
    projects.reload()


def _h(token):
    return {"Authorization": f"Bearer {token}", protocol.HEADER: str(protocol.CURRENT)}


def _post(client, token, email):
    return client.post("/people/by-email/projects", json={"email": email},
                       headers=_h(token))


# --- the shape matches the id route exactly ----------------------------------


def test_a_project_outside_the_callers_scope_is_absent(client):
    r = _post(client, SCOPED, "multi@example.com")
    assert r.status_code == 200
    body = r.json()
    assert body["projects"] == ["hollis", "vivy"]
    assert body["count"] == 2
    assert "constance" not in json.dumps(body)


def test_a_wildcard_credential_sees_every_granted_project(client):
    r = _post(client, ORIENT, "multi@example.com")
    assert r.status_code == 200
    assert r.json() == {"projects": ["constance", "hollis", "vivy"], "count": 3}


def test_matches_the_id_route_field_for_field(client):
    by_email = _post(client, ORIENT, "multi@example.com")
    by_id = client.get(f"/people/{MULTI}/projects", headers=_h(ORIENT))
    assert by_email.status_code == by_id.status_code == 200
    assert by_email.json() == by_id.json()


def test_the_email_is_normalised_the_same_way_a_write_would_normalise_it(
        client):
    r = _post(client, ORIENT, "  Multi@Example.COM  ")
    assert r.status_code == 200
    assert r.json() == {"projects": ["constance", "hollis", "vivy"], "count": 3}


# --- unknown email reads exactly like an empty person -----------------------


def test_unknown_email_and_empty_set_person_are_identical(client):
    known_empty = _post(client, ORIENT, "empty@example.com")
    unknown = _post(client, ORIENT, "nobody@example.com")
    assert known_empty.status_code == unknown.status_code == 200
    assert known_empty.json() == unknown.json() == {"projects": [], "count": 0}
    assert (known_empty.headers[protocol.HEADER]
            == unknown.headers[protocol.HEADER])


def test_unknown_email_and_empty_set_person_are_identical_under_a_scoped_client(
        client):
    known_empty = _post(client, SCOPED, "empty@example.com")
    unknown = _post(client, SCOPED, "nobody@example.com")
    assert known_empty.status_code == unknown.status_code == 200
    assert known_empty.json() == unknown.json() == {"projects": [], "count": 0}


def test_the_route_never_discloses_a_display_name(client):
    r = _post(client, ORIENT, "empty@example.com")
    assert "Empty Person" not in json.dumps(r.json())


# --- an administrator's answer is every project the caller's scope reaches --


def test_an_administrator_sees_every_project_the_registry_knows(client):
    r = _post(client, ORIENT, "ada@example.com")
    assert r.status_code == 200
    assert r.json() == {"projects": ["constance", "hollis", "vivy"], "count": 3}


def test_an_administrators_answer_is_still_bounded_by_the_callers_scope(client):
    r = _post(client, SCOPED, "ada@example.com")
    assert r.status_code == 200
    assert r.json() == {"projects": ["hollis", "vivy"], "count": 2}
    assert "constance" not in json.dumps(r.json())


def test_a_scopeless_client_reading_an_administrator_sees_nothing(client):
    r = _post(client, NOSCOPE_CLIENT, "ada@example.com")
    assert r.status_code == 200
    assert r.json() == {"projects": [], "count": 0}


def test_the_client_never_learns_the_administrator_role_from_the_shape(client):
    """Same response shape for an administrator and an ordinary person —
    the role is resolved inside the Library and never surfaced as a field a
    client could branch on."""
    admin = _post(client, ORIENT, "ada@example.com")
    ordinary = _post(client, ORIENT, "multi@example.com")
    assert set(admin.json()) == set(ordinary.json()) == {"projects", "count"}


def test_the_registry_being_unavailable_refuses_an_administrators_read(
        client, monkeypatch):
    monkeypatch.setattr(projects, "REGISTRY_PATH", Path("/does/not/exist"))
    projects.reload()
    r = _post(client, ORIENT, "ada@example.com")
    assert r.status_code == 503
    projects.reload()


# --- validation ---------------------------------------------------------------


@pytest.mark.parametrize("raw", [
    "no-at-sign.example.com", "two@at@example.com", "has space@example.com",
    "", "   ", 5, None, ["a@example.com"],
])
def test_an_implausible_or_wrong_typed_email_is_a_400(client, raw):
    r = _post(client, ORIENT, raw)
    assert r.status_code == 400, (raw, r.text)


def test_a_body_that_is_not_an_object_is_a_400(client):
    r = client.post("/people/by-email/projects", content=b"[1]",
                    headers={**_h(ORIENT), "Content-Type": "application/json"})
    assert r.status_code == 400


# --- capability gating, same as the id route ---------------------------------


def test_refuses_without_person_read(client, monkeypatch):
    monkeypatch.setattr(identity, "ROLE_CAPS", {
        **identity.ROLE_CAPS,
        "approved_client": identity.ROLE_CAPS["approved_client"]
        - {"person.read"}})
    r = _post(client, ORIENT, "multi@example.com")
    assert r.status_code == 403
    assert "person.read" in r.json()["error"]


def test_an_ordinary_agent_credential_cannot_reach_the_route(client):
    r = _post(client, AGENT, "multi@example.com")
    assert r.status_code == 403
    assert "person.read" in r.json()["error"]


# --- routing: the static path wins over the dynamic one ---------------------


def test_the_by_email_path_is_not_swallowed_by_the_id_route(client):
    """A regression test for the registration-order hazard the route's own
    docstring names: if `by-email` were ever consumed as a `person_id` by
    `POST /people/{person_id}/projects`, this would come back 404 (no such
    administrator) rather than a projects listing."""
    r = _post(client, ORIENT, "multi@example.com")
    assert r.status_code == 200
    assert "count" in r.json()
