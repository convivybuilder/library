"""`GET /people/{person_id}/projects` (issue_01M2RSE08WRRB1RJY1ZCSMD6EG) — the
first slice of person identity in the Library.

**Four design points the item's spec settles rather than leaves to this
route's author, each pinned by its own test below:** the person is a path
parameter and never `acting_human`; the answer is bounded by the CALLER's own
product scope, in SQL; a project outside a person's set is absent rather than
present-and-marked; and an unknown person is indistinguishable from a person
with an empty set, field for field, including status code.

Fixture built the way `test_crew_read_surface.py` builds its own: a fresh
`tmp_path` database carrying only the one table this route reads, and
`identity.CLIENT_TOKENS` (not `identity.TOKENS`) pointed at a fixture file,
since every caller of this route is `approved_client`.
"""
from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

import app as libapp
import identity
import projects
import protocol
import store

ORIENT = "orient-token"
DECK = "deck-token"
BOSUN = "bosun-token"
SCOPED = "scoped-client-token"
NOSCOPE_CLIENT = "noscope-client-token"
AGENT = "agent-token"


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = tmp_path / "control.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE people (
            id TEXT PRIMARY KEY, display_name TEXT NOT NULL,
            created_at TEXT NOT NULL, created_by TEXT NOT NULL,
            updated_at TEXT NOT NULL, updated_by TEXT NOT NULL
        );
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
    """)
    conn.execute(
        "INSERT INTO people (id, display_name, created_at, created_by, "
        "updated_at, updated_by) VALUES ('person_empty', 'Empty Person', "
        "'2026-01-01', 'x', '2026-01-01', 'x')")
    conn.executemany(
        "INSERT INTO person_products (person_id, product, granted_at, "
        "granted_by) VALUES (?, ?, '2026-01-01', 'x')",
        [("person_multi", "vivy"),
         ("person_multi", "constance"),
         ("person_multi", "hollis")])
    conn.commit()
    conn.close()

    tokens = tmp_path / "client-tokens.json"
    tokens.write_text(json.dumps({
        ORIENT: {"name": "orient", "products": ["*"]},
        DECK: {"name": "deck", "products": ["*"]},
        BOSUN: {"name": "bosun", "products": ["*"]},
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
    # A synthetic registry naming no creator, so every default is empty and
    # these tests keep asserting on explicit grants alone. Without one the
    # routes fail closed with 503 wherever no agent-platform checkout exists,
    # which is CI.
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({"vivy": {}, "constance": {}, "hollis": {}}))
    monkeypatch.setattr(projects, "REGISTRY_PATH", registry)
    projects.reload()
    yield TestClient(libapp.app)
    projects.reload()


def _h(token):
    return {"Authorization": f"Bearer {token}", protocol.HEADER: str(protocol.CURRENT)}


# --- design point 2/3: bounded by the caller's own scope, absent not withheld --


def test_a_project_outside_the_callers_scope_is_absent(client):
    r = client.get("/people/person_multi/projects", headers=_h(SCOPED))
    assert r.status_code == 200
    body = r.json()
    assert body["projects"] == ["hollis", "vivy"]
    assert body["count"] == 2
    assert "constance" not in body["projects"]
    assert "constance" not in json.dumps(body), (
        "a project outside scope must be absent, never present and marked "
        "withheld")


def test_a_wildcard_credential_sees_every_granted_project(client):
    r = client.get("/people/person_multi/projects", headers=_h(ORIENT))
    assert r.status_code == 200
    body = r.json()
    assert body["projects"] == ["constance", "hollis", "vivy"]
    assert body["count"] == 3


def test_a_scopeless_client_sees_nothing_even_for_a_person_with_grants(client):
    """`1 = 0` scoping (`_scope_clause`) rather than `products: []` reading
    as unrestricted — an authenticated client scoped to nothing must see
    nothing, not everything."""
    r = client.get("/people/person_multi/projects", headers=_h(NOSCOPE_CLIENT))
    assert r.status_code == 200
    assert r.json() == {"projects": [], "count": 0}


def test_the_bound_holds_for_a_narrow_client_not_only_for_the_wildcard_one(
        client):
    """The property the item's done-when names explicitly: the scope bound
    must hold by construction, demonstrated against a credential that is
    NOT `orient`'s `products: ["*"]` — the one configuration under which an
    unscoped route would already look safe."""
    wide = client.get("/people/person_multi/projects", headers=_h(ORIENT)).json()
    narrow = client.get("/people/person_multi/projects",
                        headers=_h(SCOPED)).json()
    assert set(narrow["projects"]) < set(wide["projects"])


# --- design point 4: unknown person and empty-set person are identical -----


def test_unknown_person_and_empty_set_person_are_identical(client):
    """`person_empty` has a `people` row and no grants; `person_nonexistent`
    has neither. Both must produce the exact same response — compared field
    by field, including status code, not merely both non-disclosing."""
    known_empty = client.get("/people/person_empty/projects", headers=_h(ORIENT))
    unknown = client.get("/people/person_nonexistent/projects",
                         headers=_h(ORIENT))
    assert known_empty.status_code == unknown.status_code == 200
    assert known_empty.json() == unknown.json() == {"projects": [], "count": 0}
    assert (known_empty.headers[protocol.HEADER]
            == unknown.headers[protocol.HEADER])


def test_unknown_person_and_empty_set_person_are_identical_under_a_scoped_client(
        client):
    """The same comparison under a non-wildcard credential — the identical
    shape must not depend on the caller happening to see everything."""
    known_empty = client.get("/people/person_empty/projects", headers=_h(SCOPED))
    unknown = client.get("/people/person_nonexistent/projects",
                         headers=_h(SCOPED))
    assert known_empty.status_code == unknown.status_code == 200
    assert known_empty.json() == unknown.json() == {"projects": [], "count": 0}


def test_the_route_never_discloses_a_display_name(client):
    """A consequence of the identical-response rule rather than a separate
    feature: `person_empty` has a real display name on its `people` row, and
    it must never appear here, because an unknown person has none to match
    it with."""
    r = client.get("/people/person_empty/projects", headers=_h(ORIENT))
    assert "Empty Person" not in json.dumps(r.json())


# --- design point 1: acting_human is never read on this route --------------


def test_acting_human_is_refused_rather_than_silently_ignored(client):
    """`acting_human` is never read on this route at all (the docstring's own
    words) — so passing it is now a 400, naming it, rather than a parameter
    that silently had no effect (issue_01M1MTCTH3PW0DWDEXAZA5KFET)."""
    r = client.get(
        "/people/person_multi/projects?acting_human=a-scoped-client",
        headers=_h(SCOPED))
    assert r.status_code == 400
    assert r.json()["unknown"] == ["acting_human"]
    assert r.json()["supported"] == []


# --- capability gating -------------------------------------------------------


def test_refuses_without_person_read(client, monkeypatch):
    monkeypatch.setattr(identity, "ROLE_CAPS", {
        **identity.ROLE_CAPS,
        "approved_client": identity.ROLE_CAPS["approved_client"]
        - {"person.read"}})
    r = client.get("/people/person_multi/projects", headers=_h(ORIENT))
    assert r.status_code == 403
    assert "person.read" in r.json()["error"]


def test_an_ordinary_agent_credential_cannot_reach_the_route(client):
    """Only `approved_client` holds `person.read` (`identity.py`) — an
    ordinary agent credential, however widely scoped, is refused."""
    r = client.get("/people/person_multi/projects", headers=_h(AGENT))
    assert r.status_code == 403
    assert "person.read" in r.json()["error"]


# --- the three named callers, each under its own credential -----------------


def test_orient_deck_and_bosun_each_read_under_their_own_credential(client):
    for token in (ORIENT, DECK, BOSUN):
        r = client.get("/people/person_multi/projects", headers=_h(token))
        assert r.status_code == 200
        assert r.json() == {"projects": ["constance", "hollis", "vivy"],
                            "count": 3}
