"""`body_sha256` on every reference the Library returns
(issue_01M19QQCKXW818SKY5A4G3XPHT).

**The claim is about EVERY reference-returning surface, so the fixture carries
all four in one file rather than testing each where its own fixture already
lives.** Search, the listing, a single document and a resolved profile are four
routes and one `reference.doc_dict`; a claim quantified over "every reference"
that was checked on whichever surfaces happened to be convenient is the shape
that passes while the uncovered one ships without a hash. That costs this file
a wider fixture — FTS5 for search, a control database for the profile — and the
width is the point.

**The definition-of-done test is `test_a_document_changing_between_the_two_calls_is_detectable`.**
The epic words it as "content that changed between the reference being issued
and the fetch is detectable rather than silent. Tested by changing a document
between those two calls", and that is literally what it does.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

import app as libapp
import crew_registry
import identity
import protocol
import reference
import store

AGENT = "agent-token-vivy"
ORCHESTRATOR = identity.Identity(actor="co-library-test", kind="orchestrator")

DOC_ID = "platform/decisions/0022-library-repo"
DOC_BODY = "the Library is a service with its own storage"

# Two documents with an IDENTICAL body and different everything else, which pin
# the hash's documented scope: it covers the body the index holds, not the
# source file, so these must hash the same.
TWIN_A = "platform/decisions/0031-twin-a"
TWIN_B = "vivy/specs/twin-b"
TWIN_BODY = "one body, two documents, deliberately"


@pytest.fixture
def client(tmp_path, monkeypatch):
    control = tmp_path / "control.db"
    conn = sqlite3.connect(control)
    conn.executescript("""
        CREATE TABLE roster_agents (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL, created_by TEXT NOT NULL,
            updated_at TEXT NOT NULL, updated_by TEXT NOT NULL);
        CREATE TABLE profiles (
            id INTEGER PRIMARY KEY, project TEXT NOT NULL, variant TEXT NOT NULL,
            include TEXT NOT NULL, claude_md_extras TEXT NOT NULL,
            created_at TEXT NOT NULL, created_by TEXT NOT NULL,
            updated_at TEXT NOT NULL, updated_by TEXT NOT NULL,
            UNIQUE (project, variant));
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
    # SHAPED LIKE THE REAL INDEX, FTS5 included, because search is one of the
    # four surfaces this file's claim covers and a fixture without `docs_fts`
    # would quietly leave it untested.
    iconn.executescript("""
        CREATE TABLE docs (
            id TEXT PRIMARY KEY, source_ref TEXT, kind TEXT, project TEXT,
            title TEXT, status TEXT, topics TEXT, audience TEXT,
            last_reviewed TEXT, updated_at TEXT, body TEXT, body_hash TEXT);
        CREATE VIRTUAL TABLE docs_fts USING fts5(
            id, title, body, topics, content='docs', content_rowid='rowid');
    """)
    rows = [
        (DOC_ID, "decision", "platform", "The Library gets its own repository",
         DOC_BODY),
        (TWIN_A, "decision", "platform", "Twin A", TWIN_BODY),
        (TWIN_B, "spec", "vivy", "Twin B", TWIN_BODY),
    ]
    for doc_id, kind, project, title, body in rows:
        iconn.execute(
            "INSERT INTO docs (id, source_ref, kind, project, title, status, "
            "topics, audience, body, last_reviewed, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (doc_id, f"{doc_id}.md", kind, project, title, "current",
             "[]", '["team"]', body, "2026-08-01", "2026-08-02"))
    _reindex(iconn)
    iconn.close()

    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        AGENT: {"name": "vivy-agent", "products": ["vivy"]},
    }))

    monkeypatch.setattr(store, "DB_PATH", control)
    monkeypatch.setattr(reference, "INDEX_DB", index)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    return TestClient(libapp.app)


def _reindex(iconn):
    """`body_hash` and the FTS shadow, both derived from the body just written.

    **Derived rather than written beside the row**, matching
    `lib/knowledge/indexer.py`, which hashes the body it is about to store. A
    fixture carrying a hand-written hash could assert a value that was never a
    digest of anything, and every assertion below would pass against a
    constant.
    """
    for doc_id, body in iconn.execute("SELECT id, body FROM docs").fetchall():
        iconn.execute("UPDATE docs SET body_hash = ? WHERE id = ?",
                      (hashlib.sha256((body or "").encode()).hexdigest(), doc_id))
    # `docs_fts` is an EXTERNAL-CONTENT table (`content='docs'`), so it owns no
    # copy of the rows and a plain `DELETE` against it raises "database disk
    # image is malformed". `rebuild` is FTS5's own re-sync from the content
    # table, and it is what makes `_edit` below able to change a body and have
    # search agree — which the real indexer achieves by writing both together.
    iconn.execute("INSERT INTO docs_fts(docs_fts) VALUES('rebuild')")
    iconn.commit()


def _h(token=AGENT):
    return {"Authorization": f"Bearer {token}",
            protocol.HEADER: str(protocol.CURRENT)}


def _edit(index_path, doc_id, new_body):
    """Change a document's body the way the indexer would — body and hash
    together — so the fixture cannot manufacture a mismatch the real indexer
    never produces."""
    iconn = sqlite3.connect(index_path)
    iconn.execute("UPDATE docs SET body = ? WHERE id = ?", (new_body, doc_id))
    _reindex(iconn)
    iconn.close()


def _seed_profile(project, variant, include):
    return crew_registry.profile_put(ORCHESTRATOR, project, variant, include, [])


# --- every surface -----------------------------------------------------------


def test_the_listing_carries_a_hash_on_every_document(client):
    r = client.get("/reference/documents", headers=_h())
    assert r.status_code == 200
    docs = r.json()["documents"]
    assert docs and all(d["body_sha256"] for d in docs)


def test_search_carries_a_hash_on_every_hit(client):
    r = client.get("/reference/search", params={"q": "library storage"},
                   headers=_h())
    assert r.status_code == 200
    docs = r.json()["documents"]
    assert docs and all(d["body_sha256"] for d in docs)


def test_a_single_document_carries_a_hash(client):
    r = client.get(f"/reference/documents/{DOC_ID}", headers=_h())
    assert r.status_code == 200
    assert r.json()["body_sha256"]


def test_a_resolved_profile_carries_a_hash_on_every_reference(client):
    """The surface added most recently, and the one a Individuator actually
    reads — a profile resolves to references it will fetch later, which is the
    exact gap the hash exists to close."""
    _seed_profile("platform", "daytime", [{"kind": "decision"}])
    r = client.get("/profiles/platform/daytime", headers=_h())
    assert r.status_code == 200
    docs = r.json()["documents"]
    assert docs and all(d["body_sha256"] for d in docs)


# --- what the hash means -----------------------------------------------------


def test_the_hash_matches_the_body_the_same_service_returns(client):
    """**The round trip is what makes the hash usable**: a caller handed a
    reference must be able to verify the body it later fetches against it,
    using only what the Library gave it. If the hash covered the source file
    rather than the indexed body, this comparison would be impossible from
    outside."""
    ref = next(d for d in client.get("/reference/documents", headers=_h())
               .json()["documents"] if d["id"] == DOC_ID)
    fetched = client.get(f"/reference/documents/{DOC_ID}", headers=_h()).json()
    assert fetched["body_sha256"] == ref["body_sha256"]
    assert hashlib.sha256(fetched["body"].encode()).hexdigest() == ref["body_sha256"]


def test_two_documents_with_the_same_body_hash_identically(client):
    """The documented limit, asserted rather than only described: the hash
    covers the body the index holds, not the file, so two documents differing
    only in id, kind, project and title carry the same digest. A caller must
    not read `body_sha256` as a document identity."""
    docs = {d["id"]: d for d in
            client.get("/reference/documents", headers=_h()).json()["documents"]}
    assert docs[TWIN_A]["body_sha256"] == docs[TWIN_B]["body_sha256"]


# --- the definition of done --------------------------------------------------


def test_a_document_changing_between_the_two_calls_is_detectable(client, tmp_path):
    """**The epic's definition-of-done clause, tested the way it is worded.**
    The Bosun asks what an agent needs and the Individuator fetches it
    afterwards; this changes the document in between and shows the change is
    visible rather than silent.

    It asserts BOTH directions, because only one of them is evidence. That the
    hash moved shows the change was noticed; that the pre-edit reference no
    longer matches the fetched body is what a Individuator would actually act
    on — and a test asserting only the first would pass on a hash that changed
    for any reason at all.
    """
    reference_issued = next(
        d for d in client.get("/reference/documents", headers=_h())
        .json()["documents"] if d["id"] == DOC_ID)

    _edit(tmp_path / "index.db", DOC_ID, "an edit that arrived mid-flight")

    fetched = client.get(f"/reference/documents/{DOC_ID}", headers=_h()).json()
    assert fetched["body_sha256"] != reference_issued["body_sha256"]
    assert (hashlib.sha256(fetched["body"].encode()).hexdigest()
            != reference_issued["body_sha256"])


def test_an_unchanged_document_still_matches_after_a_second_call(client):
    """The other half of the check above, and the reason it is not vacuous: a
    hash that differed on every call would satisfy the mismatch assertion while
    telling a Individuator nothing. Fetching twice with no edit must agree."""
    first = next(d for d in client.get("/reference/documents", headers=_h())
                 .json()["documents"] if d["id"] == DOC_ID)
    second = client.get(f"/reference/documents/{DOC_ID}", headers=_h()).json()
    assert second["body_sha256"] == first["body_sha256"]
