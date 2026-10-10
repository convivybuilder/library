"""`GET /roster`, `GET /profiles`, `GET /profiles/{project}/{variant}`
(issue_01M19QPV0J72EK1NDB0H8KT5EM) — the read surface `crew_profiles.py`
serves, and the DoD test that is this item's whole reason to exist:
comparing a caller's answer against the same profile resolved inside the
Library.

**Fixture built on `test_reference_reads.py`'s `client` fixture** — its
`docs` table and its `store.DB_PATH`/`reference.INDEX_DB`/`identity.TOKENS`
monkeypatch — extended with real `roster_agents`/`profiles` tables, built
from the same DDL `test_crew_registry.py` uses, and seeded through
`crew_registry`'s own write functions rather than by inserting rows
directly: a fixture that bypassed the write path could seed a shape
`crew_registry` itself would refuse.
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

AGENT = "agent-token-vivy"
ORCHESTRATOR = identity.Identity(actor="co-library-test", kind="orchestrator")

# id, kind, project, status, audience — chosen so every rule shape below has
# both a match and a non-match, the same discipline `test_reference_reads.py`
# uses for its own corpus.
DOCS = [
    ("deck/agents/scout", "agent", "deck", "current", ["daytime"]),
    ("hollis/agents/coder", "agent", "hollis", "current", []),
    ("vivy/agents/archived-thing", "agent", "vivy", "archived", ["daytime"]),
    ("vivy/agents/coder", "agent", "vivy", "current", ["daytime"]),
    ("vivy/agents/draft-thing", "agent", "vivy", "draft", []),
    ("vivy/agents/reviewer", "agent", "vivy", "current",
     ["daytime", "overnight"]),
    ("vivy/skills/dataviz", "skill", "vivy", "current", []),
]


def _hash_bodies(iconn):
    """`body_hash` for every fixture row, as `sha256(body)`.

    **Derived from the body rather than written beside it**, matching
    `lib/knowledge/indexer.py`, which hashes the body it is about to store. A
    fixture that carried a hand-written hash could assert a value that was
    never a digest of anything, and the test asserting `body_sha256` would
    pass against a constant.
    """
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
    """)
    for doc_id, kind, project, status, audience in DOCS:
        iconn.execute(
            "INSERT INTO docs (id, source_ref, kind, project, title, status, "
            "topics, audience, body, last_reviewed, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (doc_id, f"{doc_id}.md", kind, project, f"title of {doc_id}",
             status, "[]", json.dumps(audience), f"body of {doc_id}",
             "2026-08-01", "2026-08-02"))
    _hash_bodies(iconn)
    iconn.commit()
    iconn.close()

    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        AGENT: {"name": "vivy-agent", "products": ["vivy"]},
    }))

    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(reference, "INDEX_DB", index)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    return TestClient(libapp.app)


def _h(token=AGENT):
    return {"Authorization": f"Bearer {token}",
            protocol.HEADER: str(protocol.CURRENT)}


def _seed_profile(project, variant, include, claude_md_extras=None):
    return crew_registry.profile_put(
        ORCHESTRATOR, project, variant, include, claude_md_extras or [])


def _seed_roster(*names):
    for name in names:
        crew_registry.roster_add(ORCHESTRATOR, name)


# --- GET /roster ----------------------------------------------------------


def test_roster_lists_names_ordered_by_name(client):
    _seed_roster("zeta", "alpha", "mid")
    r = client.get("/roster", headers=_h())
    assert r.status_code == 200
    body = r.json()
    assert [a["name"] for a in body["agents"]] == ["alpha", "mid", "zeta"]
    assert body["count"] == 3


def test_roster_is_empty_before_load_crew_registry_has_run(client):
    """`load_crew_registry.py` is an operator step, out of scope for this
    route — an empty table is a true fact about the service's current
    state, not a failure to read it."""
    r = client.get("/roster", headers=_h())
    assert r.status_code == 200
    assert r.json() == {"agents": [], "count": 0}


def test_roster_refuses_without_roster_read(client, monkeypatch):
    monkeypatch.setattr(identity, "ROLE_CAPS", {
        **identity.ROLE_CAPS,
        "agent": identity.ROLE_CAPS["agent"] - {"roster.read"}})
    r = client.get("/roster", headers=_h())
    assert r.status_code == 403
    assert "roster.read" in r.json()["error"]


# --- GET /profiles ----------------------------------------------------------


def test_profiles_lists_project_variant_ordered_never_include(client):
    _seed_profile("vivy", "daytime", [{"kind": "agent", "project": ["vivy"]}])
    _seed_profile("hollis", "overnight",
                  [{"kind": "agent", "project": ["hollis"]}])
    r = client.get("/profiles", headers=_h())
    assert r.status_code == 200
    body = r.json()
    assert [(p["project"], p["variant"]) for p in body["profiles"]] == [
        ("hollis", "overnight"), ("vivy", "daytime")]
    assert body["count"] == 2
    assert "include" not in json.dumps(body)


def test_profiles_refuses_without_roster_read(client, monkeypatch):
    monkeypatch.setattr(identity, "ROLE_CAPS", {
        **identity.ROLE_CAPS,
        "agent": identity.ROLE_CAPS["agent"] - {"roster.read"}})
    r = client.get("/profiles", headers=_h())
    assert r.status_code == 403


# --- GET /profiles/{project}/{variant} -------------------------------------


def test_the_route_matches_an_independent_resolution_inside_the_library(
        client):
    """The definition of done: a caller's answer, compared against the same
    profile resolved a second time inside the Library, using the module the
    route itself calls rather than a copy of its logic."""
    include = [
        {"kind": "agent", "project": ["vivy", "hollis"]},
        {"kind": "agent", "audience_contains": ["daytime"]},
    ]
    _seed_profile("vivy", "daytime", include)

    r = client.get("/profiles/vivy/daytime", headers=_h())
    assert r.status_code == 200
    route_ids = [d["id"] for d in r.json()["documents"]]

    conn = store.connect()
    try:
        row = crew_profiles.profile_row(conn, "vivy", "daytime")
    finally:
        conn.close()
    index_conn = reference.connect()
    try:
        independent = crew_profiles.resolve(row, index_conn)
    finally:
        index_conn.close()

    assert route_ids == [d["id"] for d in independent]


def test_multi_rule_union_dedupes_overlap_and_orders_by_id(client):
    """Two rules whose matches overlap (`vivy/agents/coder` and
    `vivy/agents/reviewer` both carry `daytime` audience AND sit under
    `vivy`), plus a document each rule alone would miss: `deck/agents/scout`
    is reached only through `audience_contains`, and `vivy/agents/draft-
    thing` and `hollis/agents/coder` only through `project`."""
    include = [
        {"kind": "agent", "project": ["vivy", "hollis"]},
        {"kind": "agent", "audience_contains": ["daytime"]},
    ]
    _seed_profile("vivy", "daytime", include)
    r = client.get("/profiles/vivy/daytime", headers=_h())
    assert r.status_code == 200
    ids = [d["id"] for d in r.json()["documents"]]
    assert ids == [
        "deck/agents/scout",
        "hollis/agents/coder",
        "vivy/agents/coder",
        "vivy/agents/draft-thing",
        "vivy/agents/reviewer",
    ]
    assert r.json()["count"] == 5
    # Each rule's own count, before the union dedupes: the project rule
    # reaches four, the audience rule three, and the two overlapping
    # documents are counted under both, so the counts sum past the five
    # served.
    assert r.json()["rules"] == [{"position": 0, "matches": 4},
                                 {"position": 1, "matches": 3}]


def test_status_default_excludes_archived_and_a_named_status_reaches_it(
        client):
    _seed_profile("vivy", "no-status",
                  [{"kind": "agent", "project": ["vivy"]}])
    _seed_profile("vivy", "with-archived",
                  [{"kind": "agent", "project": ["vivy"],
                    "status": "archived"}])

    r1 = client.get("/profiles/vivy/no-status", headers=_h())
    ids1 = [d["id"] for d in r1.json()["documents"]]
    assert "vivy/agents/archived-thing" not in ids1

    r2 = client.get("/profiles/vivy/with-archived", headers=_h())
    ids2 = [d["id"] for d in r2.json()["documents"]]
    assert ids2 == ["vivy/agents/archived-thing"]


def test_audience_contains_matches_membership_any_of(client):
    """`overnight` matches `vivy/agents/reviewer` (which carries both
    `daytime` and `overnight`); `nonexistent` matches nothing — any-of, not
    all-of, over the two values in the rule."""
    _seed_profile("vivy", "audience-any",
                  [{"kind": "agent", "project": ["vivy"],
                    "audience_contains": ["overnight", "nonexistent"]}])
    r = client.get("/profiles/vivy/audience-any", headers=_h())
    assert r.status_code == 200
    ids = [d["id"] for d in r.json()["documents"]]
    assert ids == ["vivy/agents/reviewer"]


def test_audience_contains_does_not_match_a_substring(client):
    """`day` is a substring of the stored `daytime`, and membership is exact
    element equality, not substring — mirrors `test_reference_reads.py`'s
    `test_audience_does_not_match_a_substring_of_another_audience` for the
    single-value filter."""
    _seed_profile("vivy", "audience-substr",
                  [{"kind": "agent", "project": ["vivy"],
                    "audience_contains": ["day"]}])
    r = client.get("/profiles/vivy/audience-substr", headers=_h())
    assert r.status_code == 200
    assert r.json()["documents"] == []


@pytest.mark.parametrize("audience", [None, "", "not json at all",
                                       '{"a": "daytime"}', '"daytime"'])
def test_an_unusable_audience_row_neither_500s_nor_matches(client, tmp_path,
                                                            audience):
    """**The guard `reference.audience_clause` carries is what this route
    inherits, and this test is what stops a later edit from handing it an
    unguarded clause.** The resolution builds its own query rather than going
    through `GET /reference/documents`, so `test_reference_reads.py`'s version
    of this test says nothing about this path — the two share SQL today, and a
    shared shape with a test on only one side is a shape that can silently
    stop being shared.

    Both failure modes are covered by the same parametrize, because they are
    the two halves of one guard. `""` and `not json at all` are what
    `json_valid` catches: `json_each` raises `malformed JSON` on them, and one
    such row anywhere in the index would take this route down for every
    caller. `{"a": "daytime"}` and `"daytime"` are what the `json_type` shape
    check catches: both are valid JSON that `json_valid` alone would pass, and
    `json_each` on an object iterates its VALUES — so without the shape check
    an object row would MATCH a rule naming `daytime`, a wrong answer rather
    than a crash, which is the worse failure because nothing reports it.
    """
    iconn = sqlite3.connect(tmp_path / "index.db")
    iconn.execute(
        "INSERT INTO docs (id, source_ref, kind, project, title, status, "
        "topics, audience, body, last_reviewed, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("vivy/agents/unusable-audience", "x.md", "agent", "vivy",
         "unusable", "current", "[]", audience, "body", "2026-08-01",
         "2026-08-02"))
    iconn.commit()
    iconn.close()
    _seed_profile("vivy", "audience-guard",
                  [{"kind": "agent", "project": ["vivy"],
                    "audience_contains": ["daytime"]}])
    r = client.get("/profiles/vivy/audience-guard", headers=_h())
    assert r.status_code == 200
    ids = [d["id"] for d in r.json()["documents"]]
    assert "vivy/agents/unusable-audience" not in ids
    # The rule still resolves the documents it should, so a passing assertion
    # above cannot be an empty answer masquerading as a working guard.
    assert "vivy/agents/coder" in ids


def test_profile_with_zero_matching_documents_is_200_not_404(client):
    """A profile that exists and selects nothing is a different fact from a
    profile that does not exist — both must not collapse to the same
    answer."""
    _seed_profile("vivy", "empty", [{"kind": "runbook", "project": ["vivy"]}])
    r = client.get("/profiles/vivy/empty", headers=_h())
    assert r.status_code == 200
    body = r.json()
    assert body["documents"] == []
    assert body["count"] == 0
    assert body["rules"] == [{"position": 0, "matches": 0}]


def test_claude_md_extras_is_returned(client):
    """The one field this route returns that is content rather than a
    resolved document — withholding it would leave a caller with no way to
    obtain it at all, since nothing else in this service serves it."""
    _seed_profile("vivy", "with-extras",
                  [{"kind": "agent", "project": ["vivy"]}],
                  ["# some extra CLAUDE.local.md content"])
    r = client.get("/profiles/vivy/with-extras", headers=_h())
    assert r.status_code == 200
    assert r.json()["claude_md_extras"] == [
        "# some extra CLAUDE.local.md content"]


def test_404_on_an_unknown_project_and_variant(client):
    r = client.get("/profiles/nope/nope", headers=_h())
    assert r.status_code == 404


def test_404_on_a_known_project_with_an_unknown_variant(client):
    _seed_profile("vivy", "daytime", [{"kind": "agent", "project": ["vivy"]}])
    r = client.get("/profiles/vivy/overnight", headers=_h())
    assert r.status_code == 404


def test_resolve_refuses_without_roster_read(client, monkeypatch):
    _seed_profile("vivy", "daytime", [{"kind": "agent", "project": ["vivy"]}])
    monkeypatch.setattr(identity, "ROLE_CAPS", {
        **identity.ROLE_CAPS,
        "agent": identity.ROLE_CAPS["agent"] - {"roster.read"}})
    r = client.get("/profiles/vivy/daytime", headers=_h())
    assert r.status_code == 403
    assert "roster.read" in r.json()["error"]


def test_resolve_refuses_503_when_the_index_is_unreadable(
        client, monkeypatch, tmp_path):
    """The exception handler that covers the Reference Room's own six
    routes (`reference.ReferenceIndexUnavailable`) is registered once for
    the whole app; this confirms `GET /profiles/{project}/{variant}`,
    the one new route that calls `reference.connect()`, is covered by it
    rather than needing a second one."""
    _seed_profile("vivy", "daytime", [{"kind": "agent", "project": ["vivy"]}])
    monkeypatch.setattr(reference, "INDEX_DB", tmp_path / "gone.db")
    r = client.get("/profiles/vivy/daytime", headers=_h())
    assert r.status_code == 503
    assert "knowledge index" in r.json()["error"]


# --- no caller ever sees the selection rule ---------------------------------


def test_include_never_appears_in_any_route_response(client):
    _seed_profile("vivy", "daytime",
                  [{"kind": "agent", "project": ["vivy"],
                    "audience_contains": ["daytime"]}],
                  ["some extra content"])
    responses = [
        client.get("/roster", headers=_h()),
        client.get("/profiles", headers=_h()),
        client.get("/profiles/vivy/daytime", headers=_h()),
    ]
    for r in responses:
        assert r.status_code == 200
        assert "include" not in json.dumps(r.json())
