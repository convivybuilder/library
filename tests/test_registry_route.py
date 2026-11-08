"""`GET /registry` (CQ·241) and `POST /registry/crews` (CQ·242): the whole
project registry, as the Chart House holds it, and the route that adds one
crew to it.

**Fixture built on `test_crew_read_surface.py`'s shape**: a hand-built
`control.db` with the one table this route reads, seeded through
`project_registry.replace` rather than by inserting rows directly, so the
fixture cannot seed a shape the write path itself would refuse.

Fixture slugs are synthetic (`oldmill`, `quill`, `harrow`) — no real crew
name.
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

ACTOR = "test, 2026-10-25"
AGENT = "agent-token-oldmill"
SYSTEM = "system-token-oldmill"
# The same kinds with no `grants`: `registry.read` is on no role set, so
# none of these reads the registry (security-reviewer, PR #332, round 1).
AGENT_UNGRANTED = "agent-token-ungranted"
SYSTEM_UNGRANTED = "system-token-ungranted"
CUSTOMER = "customer-reader-token"
ORCHESTRATOR = "orchestrator-token"
REGISTRAR = "registrar-token"

REGISTRY = {
    "oldmill": {"repos": ["oldmill-app"], "private": False},
    "quill": {"repos": [], "private": True, "aliases": ["scribe"]},
    "_scopes": {"platform": {}},
}


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
    conn.execute("""
        CREATE TABLE project_registry_edits (
            id INTEGER PRIMARY KEY,
            slug TEXT NOT NULL,
            actor TEXT NOT NULL,
            at TEXT NOT NULL,
            changes TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()

    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        AGENT: {"name": "oldmill-agent", "products": ["oldmill"],
                "grants": ["registry.read"]},
        SYSTEM: {"name": "oldmill-system", "products": ["oldmill"],
                 "kind": "system", "grants": ["registry.read"]},
        AGENT_UNGRANTED: {"name": "plain-agent", "products": ["oldmill"]},
        SYSTEM_UNGRANTED: {"name": "plain-system", "products": ["oldmill"],
                           "kind": "system"},
        CUSTOMER: {"name": "customer", "products": ["oldmill"],
                   "kind": "customer_reader"},
        ORCHESTRATOR: {"name": "co", "products": ["oldmill"],
                       "kind": "orchestrator"},
        REGISTRAR: {"name": "registrar-co", "products": ["oldmill"],
                   "kind": "registrar"},
    }))

    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    return TestClient(libapp.app)


def _h(token=AGENT):
    return {"Authorization": f"Bearer {token}",
            protocol.HEADER: str(protocol.CURRENT)}


def _seed(document=None):
    project_registry.replace(document if document is not None else REGISTRY,
                             actor=ACTOR)


def test_an_agent_credential_reads_the_document(client):
    _seed()
    r = client.get("/registry", headers=_h(AGENT))
    assert r.status_code == 200
    assert r.json() == REGISTRY


def test_a_system_credential_reads_the_document(client):
    _seed()
    r = client.get("/registry", headers=_h(SYSTEM))
    assert r.status_code == 200
    assert r.json() == REGISTRY


def test_the_response_carries_a_private_crew(client):
    """Agent-platform needs the private crews to hide them, so this route
    withholds nothing."""
    _seed()
    r = client.get("/registry", headers=_h())
    assert r.json()["quill"]["private"] is True


@pytest.mark.parametrize("token", [AGENT_UNGRANTED, SYSTEM_UNGRANTED,
                                   CUSTOMER, ORCHESTRATOR])
def test_refuses_every_kind_whose_entry_does_not_grant_it(client, token):
    _seed()
    r = client.get("/registry", headers=_h(token))
    assert r.status_code == 403
    assert "registry.read" in r.json()["error"]
    assert "quill" not in r.text


def test_registry_read_is_on_no_role_set():
    assert "registry.read" in identity.GRANT_ONLY_CAPS
    assert not any("registry.read" in caps
                   for caps in identity.ROLE_CAPS.values())


def test_refuses_without_any_transport_authentication(client):
    _seed()
    r = client.get("/registry", headers={protocol.HEADER: str(protocol.CURRENT)})
    assert r.status_code == 401


def test_refuses_an_unresolvable_token(client):
    _seed()
    r = client.get("/registry", headers=_h("not-a-real-token"))
    assert r.status_code == 401


def test_empty_table_refuses_503_naming_the_empty_registry(client):
    r = client.get("/registry", headers=_h())
    assert r.status_code == 503
    assert "empty" in r.json()["error"]


def test_refuses_any_parameter(client):
    _seed()
    r = client.get("/registry?slug=oldmill", headers=_h())
    assert r.status_code == 400
    assert r.json()["unknown"] == ["slug"]


# --- POST /registry/crews (CQ·242) --------------------------------------------


HARROW = {"slug": "harrow", "display": "Harrow", "repos": ["harrow-app"]}


def test_an_orchestrator_credential_registers_a_public_crew(client):
    _seed()
    r = client.post("/registry/crews", headers=_h(ORCHESTRATOR), json=HARROW)
    assert r.status_code == 201, r.text
    assert r.json()["slug"] == "harrow"
    assert r.json()["updated_by"] == "co"


def test_an_orchestrator_credential_registers_a_private_crew(client):
    _seed()
    payload = {"slug": "wick", "display": "Wick",
               "repos": ["wick-app"], "private": True, "kb": "local"}
    r = client.post("/registry/crews", headers=_h(ORCHESTRATOR), json=payload)
    assert r.status_code == 201, r.text
    assert r.json()["private"] is True


def test_a_registered_crew_is_served_back_by_get_registry(client):
    _seed()
    client.post("/registry/crews", headers=_h(ORCHESTRATOR), json=HARROW)
    r = client.get("/registry", headers=_h())
    assert r.json()["harrow"]["display"] == "Harrow"


@pytest.fixture
def table_mode(monkeypatch):
    """`projects.py` in table mode (CQ·243): `REGISTRY_PATH` cleared so its
    reads go through `store`, which `client` has already pointed at this
    fixture's own `control.db`."""
    import projects
    monkeypatch.setattr(projects, "REGISTRY_PATH", None)
    projects.reload()
    yield projects
    projects.reload()


def test_a_registered_private_crew_resolves_without_restart_or_reload(
        client, table_mode):
    """CQ·243's done-when: a registration through `POST /registry/crews`
    reaches `projects.resolve` and a privacy reader (`private_repos`) with
    no call to `projects.reload()` anywhere in this test."""
    _seed()
    assert table_mode.resolve("wick") is None

    payload = {"slug": "wick", "display": "Wick",
               "repos": ["wick-app"], "private": True, "kb": "local"}
    r = client.post("/registry/crews", headers=_h(ORCHESTRATOR), json=payload)
    assert r.status_code == 201, r.text

    assert table_mode.resolve("wick") == "wick"
    assert "wick-app" in table_mode.private_repos()


def test_refuses_a_private_crew_with_central_kb(client):
    _seed()
    payload = {"slug": "wick", "display": "Wick",
               "repos": ["wick-app"], "private": True}
    r = client.post("/registry/crews", headers=_h(ORCHESTRATOR), json=payload)
    assert r.status_code == 400
    assert "private" in r.json()["error"]


def test_refuses_a_private_crew_with_a_kb_project(client):
    _seed()
    payload = {"slug": "wick", "display": "Wick",
               "repos": ["wick-app"], "private": True, "kb": "local",
               "kb_project": "wick"}
    r = client.post("/registry/crews", headers=_h(ORCHESTRATOR), json=payload)
    assert r.status_code == 400
    assert "kb_project" in r.json()["error"]


@pytest.mark.parametrize("field,value", [
    ("slug", "oldmill"), ("aliases", ["scribe"]), ("repos", ["oldmill-app"])])
def test_refuses_a_colliding_slug_alias_or_repo(client, field, value):
    _seed()
    payload = dict(HARROW)
    payload[field] = value
    r = client.post("/registry/crews", headers=_h(ORCHESTRATOR), json=payload)
    assert r.status_code == 409
    assert str(value if isinstance(value, str) else value[0]) in r.json()["error"]


@pytest.mark.parametrize("token", [AGENT, SYSTEM, AGENT_UNGRANTED,
                                   SYSTEM_UNGRANTED, CUSTOMER])
def test_refuses_every_credential_without_registry_write(client, token):
    _seed()
    r = client.post("/registry/crews", headers=_h(token), json=HARROW)
    assert r.status_code == 403
    assert "registry.write" in r.json()["error"]


def test_refuses_without_any_transport_authentication_on_post(client):
    _seed()
    r = client.post("/registry/crews",
                    headers={protocol.HEADER: str(protocol.CURRENT)},
                    json=HARROW)
    assert r.status_code == 401


def test_refuses_a_malformed_body(client):
    _seed()
    r = client.post("/registry/crews", headers=_h(ORCHESTRATOR),
                    content=b"not json")
    assert r.status_code == 400


@pytest.mark.parametrize("content", [b"not json", b"[]"])
def test_refuses_a_malformed_body_with_403_before_400_without_registry_write(
        client, content):
    _seed()
    r = client.post("/registry/crews", headers=_h(AGENT), content=content)
    assert r.status_code == 403


def test_refuses_a_slug_alias_or_repo_colliding_in_another_case(client):
    _seed()
    payload = dict(HARROW)
    payload["aliases"] = ["Scribe"]
    r = client.post("/registry/crews", headers=_h(ORCHESTRATOR), json=payload)
    assert r.status_code == 409


def test_refuses_a_missing_required_field(client):
    _seed()
    r = client.post("/registry/crews", headers=_h(ORCHESTRATOR),
                    json={"slug": "harrow", "repos": ["harrow-app"]})
    assert r.status_code == 400
    assert "display" in r.json()["error"]


def test_refuses_while_writes_are_disabled(client, monkeypatch):
    _seed()
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    r = client.post("/registry/crews", headers=_h(ORCHESTRATOR), json=HARROW)
    assert r.status_code == 503


def test_refuses_registration_against_an_empty_table(client):
    r = client.post("/registry/crews", headers=_h(ORCHESTRATOR), json=HARROW)
    assert r.status_code == 503


# --- PATCH /registry/crews/{slug} (CQ·249) ------------------------------------
#
# `REGISTRY`'s raw partial entries are fine for `GET /registry` and for
# `POST /registry/crews`'s own collision fixtures, but `PATCH` rebuilds its
# target through `build_crew_row`, so a patch target must already be one of
# that function's own outputs. `_seed_built` seeds exactly that.


def _seed_built(*payloads):
    document = {}
    for payload in payloads:
        slug, row = project_registry.build_crew_row(payload)
        document[slug] = row
    project_registry.replace(document, actor=ACTOR)


OLDMILL_BUILT = {"slug": "oldmill", "display": "Old Mill",
                 "repos": ["oldmill-app"], "aliases": ["mill"]}
QUILL_BUILT = {"slug": "quill", "display": "Quill", "repos": ["quill-app"],
              "aliases": ["scribe"], "private": True, "kb": "local"}


def test_an_orchestrator_credential_patches_a_field(client):
    _seed_built(OLDMILL_BUILT, QUILL_BUILT)
    r = client.patch("/registry/crews/oldmill", headers=_h(ORCHESTRATOR),
                     json={"display": "Old Mill Works"})
    assert r.status_code == 200, r.text
    assert set(r.json()) == {"slug", "changed", "updated_at", "updated_by"}
    assert r.json()["changed"] == ["display"]
    assert r.json()["updated_by"] == "co"


def test_a_registrar_credential_patches_a_field(client):
    _seed_built(OLDMILL_BUILT, QUILL_BUILT)
    r = client.patch("/registry/crews/oldmill", headers=_h(REGISTRAR),
                     json={"display": "Old Mill Works"})
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("token", [AGENT, SYSTEM, AGENT_UNGRANTED,
                                   SYSTEM_UNGRANTED, CUSTOMER])
def test_patch_refuses_every_credential_without_registry_write(client, token):
    _seed_built(OLDMILL_BUILT, QUILL_BUILT)
    r = client.patch("/registry/crews/oldmill", headers=_h(token),
                     json={"display": "New"})
    assert r.status_code == 403
    assert "registry.write" in r.json()["error"]


def test_patch_refuses_without_any_transport_authentication(client):
    _seed_built(OLDMILL_BUILT, QUILL_BUILT)
    r = client.patch("/registry/crews/oldmill",
                     headers={protocol.HEADER: str(protocol.CURRENT)},
                     json={"display": "New"})
    assert r.status_code == 401


def test_patch_refuses_a_malformed_body(client):
    _seed_built(OLDMILL_BUILT, QUILL_BUILT)
    r = client.patch("/registry/crews/oldmill", headers=_h(ORCHESTRATOR),
                     content=b"not json")
    assert r.status_code == 400


def test_patch_answers_403_before_judging_a_malformed_body(client):
    _seed_built(OLDMILL_BUILT, QUILL_BUILT)
    r = client.patch("/registry/crews/oldmill", headers=_h(AGENT),
                     content=b"not json")
    assert r.status_code == 403


def test_patch_refuses_naming_slug(client):
    _seed_built(OLDMILL_BUILT, QUILL_BUILT)
    r = client.patch("/registry/crews/oldmill", headers=_h(ORCHESTRATOR),
                     json={"slug": "newname"})
    assert r.status_code == 400
    assert "slug" in r.json()["error"]


def test_patch_refuses_an_undeclared_field(client):
    _seed_built(OLDMILL_BUILT, QUILL_BUILT)
    r = client.patch("/registry/crews/oldmill", headers=_h(ORCHESTRATOR),
                     json={"made_up": 1})
    assert r.status_code == 400
    assert "made_up" in r.json()["error"]


def test_patch_refuses_null_on_an_always_carried_field(client):
    _seed_built(OLDMILL_BUILT, QUILL_BUILT)
    r = client.patch("/registry/crews/oldmill", headers=_h(ORCHESTRATOR),
                     json={"display": None})
    assert r.status_code == 400
    assert "display" in r.json()["error"]


def test_patch_removes_an_optional_field_with_null(client):
    slug, row = project_registry.build_crew_row(
        {"slug": "oldmill", "display": "Old Mill", "repos": ["oldmill-app"],
         "note": "a note"})
    project_registry.replace({slug: row}, actor=ACTOR)
    r = client.patch("/registry/crews/oldmill", headers=_h(ORCHESTRATOR),
                     json={"note": None})
    assert r.status_code == 200, r.text
    assert r.json()["changed"] == ["note"]
    assert "note" not in client.get("/registry", headers=_h()).json()["oldmill"]


def test_patch_refuses_the_404_naming_the_slug(client):
    _seed_built(OLDMILL_BUILT, QUILL_BUILT)
    r = client.patch("/registry/crews/harrow", headers=_h(ORCHESTRATOR),
                     json={"display": "Harrow"})
    assert r.status_code == 404
    assert "harrow" in r.json()["error"]


def test_patch_refuses_the_404_for_a_metadata_key(client):
    _seed_built(OLDMILL_BUILT, QUILL_BUILT)
    r = client.patch("/registry/crews/_scopes", headers=_h(ORCHESTRATOR),
                     json={"display": "x"})
    assert r.status_code == 404


def test_patch_refuses_a_collision_with_another_entrys_alias(client):
    _seed_built(OLDMILL_BUILT, QUILL_BUILT)
    r = client.patch("/registry/crews/oldmill", headers=_h(ORCHESTRATOR),
                     json={"aliases": ["scribe"]})
    assert r.status_code == 409
    assert "scribe" in r.json()["error"]


def test_patch_refuses_making_a_private_crew_public(client):
    _seed_built(OLDMILL_BUILT, QUILL_BUILT)
    r = client.patch("/registry/crews/quill", headers=_h(ORCHESTRATOR),
                     json={"private": False})
    assert r.status_code == 400
    assert r.json()["error"] == "'private' is an operator change"


def test_patch_refuses_redirecting_a_repos_github_owner(client):
    _seed_built(OLDMILL_BUILT, QUILL_BUILT)
    r = client.patch("/registry/crews/oldmill", headers=_h(REGISTRAR),
                     json={"repo_owners": {"oldmill-app": "someone-else"}})
    assert r.status_code == 400
    assert r.json()["error"] == "'repo_owners' is an operator change"
    assert "repo_owners" not in client.get(
        "/registry", headers=_h()).json()["oldmill"]


def test_patch_a_noop_changes_nothing_and_answers_200(client):
    _seed_built(OLDMILL_BUILT, QUILL_BUILT)
    r = client.patch("/registry/crews/oldmill", headers=_h(ORCHESTRATOR),
                     json={"aliases": ["mill"]})
    assert r.status_code == 200, r.text
    assert r.json()["changed"] == []


def test_patch_refuses_while_writes_are_disabled(client, monkeypatch):
    _seed_built(OLDMILL_BUILT, QUILL_BUILT)
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    r = client.patch("/registry/crews/oldmill", headers=_h(ORCHESTRATOR),
                     json={"display": "New"})
    assert r.status_code == 503


def test_patch_refuses_against_an_empty_table(client):
    r = client.patch("/registry/crews/oldmill", headers=_h(ORCHESTRATOR),
                     json={"display": "New"})
    assert r.status_code == 503
    assert "empty" in r.json()["error"]


def test_a_patched_crew_is_served_back_by_get_registry(client):
    _seed_built(OLDMILL_BUILT, QUILL_BUILT)
    client.patch("/registry/crews/oldmill", headers=_h(ORCHESTRATOR),
                 json={"display": "Old Mill Works"})
    r = client.get("/registry", headers=_h())
    assert r.json()["oldmill"]["display"] == "Old Mill Works"


def test_a_patched_alias_resolves_without_restart_or_reload(client, table_mode):
    """§6.8's own close: the Library rereads its registry after the commit,
    so the next request it serves sees the edit — the same CQ·243 done-when
    `POST /registry/crews`'s own test above exercises."""
    _seed_built(OLDMILL_BUILT, QUILL_BUILT)
    assert table_mode.resolve("millworks") is None

    r = client.patch("/registry/crews/oldmill", headers=_h(ORCHESTRATOR),
                     json={"aliases": ["mill", "millworks"]})
    assert r.status_code == 200, r.text

    assert table_mode.resolve("millworks") == "oldmill"
