"""Scoping every `/reference/*` read (and the Reference Room half of `GET
/search`) to a credential's bound dispatch profile, server-side
(issue_01M35QG0VANA5P1XE3PH6A7PS4).

**The DoD property**: for a bound credential, the set of document ids
reachable through any Reference Room read route equals
`crew_profiles.resolve()` for that credential's profile — the exact function
`GET /profiles/{project}/{variant}` already calls, so there is no second
evaluator for a reviewer to find disagreeing with the first.

**Fixture built on `test_crew_read_surface.py`'s shape**: `roster_agents` and
`profiles` seeded through `crew_registry.profile_put` (never by inserting
rows directly, so the fixture cannot seed a shape `crew_registry` itself
would refuse), `docs` and `docs_fts` built the way `test_search.py` builds
them, so `GET /reference/search` and the reference half of `GET /search`
both have a real FTS index to query.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

import app as libapp
import crew_profiles
import crew_registry
import identity
import protocol
import reference
import store

AGENT = "agent-token-unbound"
BOUND_VIVY = "agent-token-bound-vivy"
BOUND_COMPANY = "agent-token-bound-company"
BOUND_EMPTY = "agent-token-bound-empty"
BOUND_UNRESOLVED = "agent-token-bound-unresolved"

ORCHESTRATOR = identity.Identity(actor="co-library-test", kind="orchestrator")

PHRASE = "zylofoam gasket"

# id, kind, project, status, audience, topics, body. Every body carries
# PHRASE so one search query reaches the whole corpus, and every id's own
# project/kind/status/audience is chosen so the two profiles below each have
# both a match and a non-match to narrow away.
DOCS = [
    ("vivy/agents/coder", "agent", "vivy", "current", ["daytime"], ["widgets"]),
    ("vivy/agents/reviewer", "agent", "vivy", "current", [], ["widgets"]),
    ("vivy/agents/draft-thing", "agent", "vivy", "draft", [], ["widgets"]),
    # Archived: excluded by the vivy/daytime profile's own rule (no explicit
    # `status`, so it defaults to `reference.DEFAULT_STATUSES`) even though
    # its project and kind match.
    ("vivy/agents/archived-thing", "agent", "vivy", "archived", [], ["widgets"]),
    # Right kind, wrong project — narrowed away by the vivy/daytime profile's
    # `project` rule.
    ("hollis/agents/other", "agent", "hollis", "current", [], ["widgets"]),
    # Right project, wrong kind — narrowed away by the vivy/daytime profile's
    # `kind` rule.
    ("vivy/skills/dataviz", "skill", "vivy", "current", [], ["widgets"]),
    # The company/contractor profile's three cases: right audience, and two
    # kinds of wrong (a different audience; the right audience under the
    # wrong project is not represented since this is the only project it
    # pulls from, matching "a profile that admits company reference only for
    # one audience").
    ("company/reference/contractor-ok", "reference", "company", "current",
     ["contractor"], ["policy"]),
    ("company/reference/public", "reference", "company", "current",
     ["public"], ["policy"]),
    ("company/reference/other-audience", "reference", "company", "current",
     ["other"], ["policy"]),
]


def _hash_bodies(iconn):
    for doc_id, body in iconn.execute("SELECT id, body FROM docs").fetchall():
        iconn.execute("UPDATE docs SET body_hash = ? WHERE id = ?",
                      (hashlib.sha256((body or "").encode()).hexdigest(), doc_id))


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
        CREATE TABLE reference_checkouts (
            id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, holder TEXT NOT NULL,
            run_id TEXT, taken_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL,
            released_at TEXT, release_reason TEXT, released_by TEXT);
        CREATE UNIQUE INDEX ix_reference_checkouts_live_doc
            ON reference_checkouts (doc_id) WHERE released_at IS NULL;
    """)
    conn.commit()
    conn.close()

    index = tmp_path / "index.db"
    iconn = sqlite3.connect(index)
    iconn.executescript("""
        CREATE TABLE docs (
            id TEXT PRIMARY KEY, source_ref TEXT, kind TEXT, project TEXT,
            title TEXT, status TEXT, topics TEXT, audience TEXT,
            last_reviewed TEXT, updated_at TEXT, body TEXT, body_hash TEXT);
        CREATE VIRTUAL TABLE docs_fts USING fts5(
            id, title, body, topics, content='docs', content_rowid='rowid');
    """)
    for doc_id, kind, project, status, audience, topics in DOCS:
        iconn.execute(
            "INSERT INTO docs (id, source_ref, kind, project, title, status, "
            "topics, audience, body, last_reviewed, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (doc_id, f"{doc_id}.md", kind, project, f"title of {doc_id}",
             status, json.dumps(topics), json.dumps(audience),
             f"body of {doc_id}, the {PHRASE}", "2026-08-01", "2026-08-02"))
    iconn.execute("INSERT INTO docs_fts(rowid, id, title, body, topics) "
                 "SELECT rowid, id, title, body, topics FROM docs")
    _hash_bodies(iconn)
    iconn.commit()
    iconn.close()

    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        AGENT: {"name": "unbound-agent", "products": ["*"]},
        BOUND_VIVY: {"name": "vivy-crew", "products": ["*"],
                    "profile": "vivy/daytime"},
        BOUND_COMPANY: {"name": "company-crew", "products": ["*"],
                        "profile": "company/contractor"},
        BOUND_EMPTY: {"name": "empty-crew", "products": ["*"],
                     "profile": "vivy/empty"},
        BOUND_UNRESOLVED: {"name": "unresolved-crew", "products": ["*"],
                           "profile": "nope/nope"},
    }))

    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(reference, "INDEX_DB", index)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)

    crew_registry.profile_put(
        ORCHESTRATOR, "vivy", "daytime",
        [{"kind": "agent", "project": ["vivy"]}], [])
    crew_registry.profile_put(
        ORCHESTRATOR, "company", "contractor",
        [{"kind": "reference", "project": ["company"],
          "audience_contains": ["contractor"]}], [])
    crew_registry.profile_put(ORCHESTRATOR, "vivy", "empty", [], [])
    return TestClient(libapp.app)


def _h(token):
    return {"Authorization": f"Bearer {token}",
            protocol.HEADER: str(protocol.CURRENT)}


def _resolved_ids(profile_project, profile_variant):
    """`crew_profiles.resolve()`'s own answer for one profile — the
    independent computation every DoD assertion compares a route's answer
    against."""
    conn = store.connect()
    try:
        row = crew_profiles.profile_row(conn, profile_project, profile_variant)
    finally:
        conn.close()
    index_conn = reference.connect()
    try:
        return {d["id"] for d in crew_profiles.resolve(row, index_conn)}
    finally:
        index_conn.close()


ALL_IDS = {d[0] for d in DOCS}


# --- the DoD: every route's reach equals crew_profiles.resolve() -----------


@pytest.mark.parametrize("token, project, variant", [
    (BOUND_VIVY, "vivy", "daytime"),
    (BOUND_COMPANY, "company", "contractor"),
])
def test_listing_reach_equals_resolve(client, token, project, variant):
    expected = _resolved_ids(project, variant)
    r = client.get("/reference/documents", params={"limit": 100},
                    headers=_h(token))
    assert r.status_code == 200
    assert {d["id"] for d in r.json()["documents"]} == expected
    assert r.json()["total"] == len(expected)


@pytest.mark.parametrize("token, project, variant", [
    (BOUND_VIVY, "vivy", "daytime"),
    (BOUND_COMPANY, "company", "contractor"),
])
def test_search_reach_equals_resolve(client, token, project, variant):
    expected = _resolved_ids(project, variant)
    r = client.get("/reference/search", params={"q": PHRASE, "limit": 100},
                    headers=_h(token))
    assert r.status_code == 200
    assert {d["id"] for d in r.json()["documents"]} == expected
    assert r.json()["total"] == len(expected)


@pytest.mark.parametrize("token, project, variant", [
    (BOUND_VIVY, "vivy", "daytime"),
    (BOUND_COMPANY, "company", "contractor"),
])
def test_all_vault_search_reach_equals_resolve(client, token, project, variant):
    expected = _resolved_ids(project, variant)
    r = client.get("/search", params={"q": PHRASE, "vault": "reference",
                                      "limit": 100}, headers=_h(token))
    assert r.status_code == 200
    got = {row["id"] for row in r.json()["results"] if row["vault"] == "reference"}
    assert got == expected


@pytest.mark.parametrize("token, project, variant", [
    (BOUND_VIVY, "vivy", "daytime"),
    (BOUND_COMPANY, "company", "contractor"),
])
def test_single_document_reach_equals_resolve(client, token, project, variant):
    expected = _resolved_ids(project, variant)
    for doc_id in ALL_IDS:
        r = client.get(f"/reference/documents/{doc_id}", headers=_h(token))
        if doc_id in expected:
            assert r.status_code == 200, doc_id
            assert r.json()["id"] == doc_id
        else:
            assert r.status_code == 404, doc_id


@pytest.mark.parametrize("token, project, variant", [
    (BOUND_VIVY, "vivy", "daytime"),
    (BOUND_COMPANY, "company", "contractor"),
])
def test_checkout_status_reach_equals_resolve(client, token, project, variant):
    expected = _resolved_ids(project, variant)
    for doc_id in ALL_IDS:
        r = client.get(f"/reference/documents/{doc_id}/checkout",
                        headers=_h(token))
        if doc_id in expected:
            assert r.status_code == 200, doc_id
        else:
            assert r.status_code == 404, doc_id


@pytest.mark.parametrize("token, project, variant", [
    (BOUND_VIVY, "vivy", "daytime"),
    (BOUND_COMPANY, "company", "contractor"),
])
def test_checkout_post_reach_equals_resolve(client, token, project, variant):
    expected = _resolved_ids(project, variant)
    for doc_id in ALL_IDS:
        r = client.post(f"/reference/documents/{doc_id}/checkout", json={},
                        headers=_h(token))
        if doc_id in expected:
            assert r.status_code == 200, doc_id
        else:
            assert r.status_code == 404, doc_id


def test_topics_reach_matches_the_scoped_corpus(client):
    """`GET /reference/topics` counts only documents the bound profile
    selects — the `widgets` topic lives only on `vivy/agents/*` documents,
    none of which the company profile's own scope ever reaches."""
    r = client.get("/reference/topics", headers=_h(BOUND_COMPANY))
    assert r.status_code == 200
    topics = {t["topic"] for t in r.json()["topics"]}
    assert "widgets" not in topics
    assert "policy" in topics

    r = client.get("/reference/topics", headers=_h(BOUND_VIVY))
    assert r.status_code == 200
    topics = {t["topic"] for t in r.json()["topics"]}
    assert "widgets" in topics
    assert "policy" not in topics


# --- targeted cases named in the task -----------------------------------


def test_an_out_of_scope_id_404s_identically_to_a_missing_id(client):
    """The same body, byte for byte, as a genuinely absent id — an
    enumeration oracle is a document outside the scope reading differently
    from one that does not exist at all."""
    missing_body = client.get("/reference/documents/does/not/exist",
                              headers=_h(BOUND_VIVY)).json()
    for path in ("/reference/documents/{}", "/reference/documents/{}/checkout"):
        out_of_scope = client.get(path.format("company/reference/contractor-ok"),
                                  headers=_h(BOUND_VIVY))
        missing = client.get(path.format("does/not/exist"), headers=_h(BOUND_VIVY))
        assert out_of_scope.status_code == missing.status_code == 404
        assert out_of_scope.json() == missing.json() == missing_body

    out_of_scope_post = client.post(
        "/reference/documents/company/reference/contractor-ok/checkout",
        json={}, headers=_h(BOUND_VIVY))
    missing_post = client.post("/reference/documents/does/not/exist/checkout",
                               json={}, headers=_h(BOUND_VIVY))
    assert out_of_scope_post.status_code == missing_post.status_code == 404
    assert out_of_scope_post.json() == missing_post.json()


def test_a_filter_outside_scope_narrows_to_nothing_rather_than_widening(client):
    """`project=company&kind=reference` on a credential bound to the
    vivy/daytime profile, which never selects company's `reference` kind —
    the filter must not reach past the profile's own ceiling."""
    r = client.get("/reference/documents",
                   params={"project": "company", "kind": "reference"},
                   headers=_h(BOUND_VIVY))
    assert r.status_code == 200
    assert r.json()["documents"] == []
    assert r.json()["total"] == 0

    r = client.get("/reference/search",
                   params={"q": PHRASE, "project": "company", "kind": "reference"},
                   headers=_h(BOUND_VIVY))
    assert r.status_code == 200
    assert r.json()["documents"] == []


@pytest.mark.parametrize("token", [BOUND_EMPTY, BOUND_UNRESOLVED])
def test_an_unresolvable_binding_gets_nothing_on_every_route(client, token):
    """A profile with no `include` rules (`BOUND_EMPTY`) and a profile naming
    a `<project>/<variant>` nothing has written (`BOUND_UNRESOLVED`) are both
    the fail-closed case: empty, never the unscoped corpus."""
    listing = client.get("/reference/documents", headers=_h(token))
    assert listing.status_code == 200
    assert listing.json()["documents"] == []
    assert listing.json()["total"] == 0

    search = client.get("/reference/search", params={"q": PHRASE},
                        headers=_h(token))
    assert search.status_code == 200
    assert search.json()["documents"] == []
    assert search.json()["total"] == 0

    all_vault = client.get("/search", params={"q": PHRASE, "vault": "reference"},
                           headers=_h(token))
    assert all_vault.status_code == 200
    assert [r for r in all_vault.json()["results"] if r["vault"] == "reference"] == []

    topics = client.get("/reference/topics", headers=_h(token))
    assert topics.status_code == 200
    assert topics.json()["topics"] == []

    for doc_id in ALL_IDS:
        assert client.get(f"/reference/documents/{doc_id}",
                          headers=_h(token)).status_code == 404
        assert client.get(f"/reference/documents/{doc_id}/checkout",
                          headers=_h(token)).status_code == 404
        assert client.post(f"/reference/documents/{doc_id}/checkout", json={},
                           headers=_h(token)).status_code == 404


def test_an_unbound_credential_is_unaffected(client):
    """No `profile` field at all — the contract's additive promise: every
    route answers exactly as it did before this existed."""
    listing = client.get("/reference/documents", headers=_h(AGENT))
    assert listing.status_code == 200
    assert {d["id"] for d in listing.json()["documents"]} == ALL_IDS - {
        "vivy/agents/archived-thing"}  # excluded by the default status filter

    search = client.get("/reference/search", params={"q": PHRASE, "limit": 100},
                        headers=_h(AGENT))
    assert search.status_code == 200
    assert search.json()["total"] == len(ALL_IDS) - 1

    for doc_id in ALL_IDS:
        assert client.get(f"/reference/documents/{doc_id}",
                          headers=_h(AGENT)).status_code == 200
