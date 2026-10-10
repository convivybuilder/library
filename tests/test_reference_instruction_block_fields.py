"""`binds_crews`, `binds_agents` and `binds_topics` on an `instruction-block`
document (issue_01M2MC21YAWT2V61QWSM247VRX, under the steward's epic
issue_01M2MBQ8Q7YX34DDF8H9YM5T58).

These are an `instruction-block`'s selector fields: they say which crews,
agents and topics MUST OBEY the block, where `audience` says who MAY READ
it. They live in `frontmatter_extra`, the same JSON blob that already carries
an `agent` document's `model`, `tools` and `trigger`
(`test_reference_agent_fields.py`), so this file follows that one's shape:
one fixture, every read surface the fields travel on, and the same
absent/empty/unparseable-`frontmatter_extra` cases.

A sibling PR against `convivy/knowledge` adds the `instruction-block` kind
and validates these three fields; it may not have landed when this file
runs, so nothing here depends on that validator existing — the index rows
below are built directly, the way the live indexer would eventually produce
them.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

import app as libapp
import identity
import protocol
import reference
import store

AGENT = "agent-token-vivy"

BLOCK_ID = "platform/blocks/verify-before-report"
BLOCK_EXTRA = {"binds_crews": ["platform"],
               "binds_agents": ["reviewer", "coder"],
               "binds_topics": ["verification"],
               # A field no caller asked for, alongside the three that
               # travel, matching `CODER_EXTRA["source_author"]` in
               # `test_reference_agent_fields.py`.
               "source_author": "steward"}

PARTIAL_ID = "platform/blocks/floor-only"
PARTIAL_EXTRA = {"binds_crews": ["platform", "vivy"]}  # no binds_agents/topics

DECISION_ID = "platform/decisions/0022-library-repo"
DECISION_EXTRA = {"source_author": "operator"}  # non-block; must not leak selectors

AGENT_ID = "agent-platform/agents/coder"
AGENT_DOC_EXTRA = {"model": "sonnet"}  # non-block; must not leak selectors

MALFORMED_ID = "platform/blocks/malformed-extra"
NULL_EXTRA_ID = "platform/blocks/null-extra"
SCALAR_EXTRA_ID = "platform/blocks/object-shaped-extra"


@pytest.fixture
def client(tmp_path, monkeypatch):
    control = tmp_path / "control.db"
    sqlite3.connect(control).close()

    index = tmp_path / "index.db"
    iconn = sqlite3.connect(index)
    iconn.executescript("""
        CREATE TABLE docs (
            id TEXT PRIMARY KEY, source_ref TEXT, kind TEXT, project TEXT,
            title TEXT, status TEXT, topics TEXT, audience TEXT,
            last_reviewed TEXT, updated_at TEXT, body TEXT, body_hash TEXT,
            frontmatter_extra TEXT);
        CREATE VIRTUAL TABLE docs_fts USING fts5(
            id, title, body, topics, content='docs', content_rowid='rowid');
    """)
    rows = [
        (BLOCK_ID, "instruction-block", "platform",
         "verify before you report", "state what you ran, not that you checked",
         json.dumps(BLOCK_EXTRA)),
        (PARTIAL_ID, "instruction-block", "platform",
         "the universal floor", "every agent reads this regardless of task",
         json.dumps(PARTIAL_EXTRA)),
        (DECISION_ID, "decision", "platform",
         "The Library gets its own repository",
         "the Library is a service with its own storage",
         json.dumps(DECISION_EXTRA)),
        (AGENT_ID, "agent", "agent-platform", "the coder agent",
         "the coder implements a spec on the substrate",
         json.dumps(AGENT_DOC_EXTRA)),
        (MALFORMED_ID, "instruction-block", "platform",
         "a row this service did not write",
         "an instruction-block with unparseable frontmatter_extra",
         "not valid json at all"),
        (NULL_EXTRA_ID, "instruction-block", "platform",
         "an older row", "indexed before frontmatter_extra existed", None),
        # VALID JSON, WRONG SHAPE for `binds_agents` — the case the sibling
        # validator rejects at CI but this read path must still serve
        # without guessing or crashing.
        (SCALAR_EXTRA_ID, "instruction-block", "platform",
         "a block with a malformed selector",
         "binds_agents is a bare string here, not a list",
         json.dumps({"binds_agents": "reviewer"})),
    ]
    for doc_id, kind, project, title, body, extra in rows:
        iconn.execute(
            "INSERT INTO docs (id, source_ref, kind, project, title, status, "
            "topics, audience, body, last_reviewed, updated_at, "
            "frontmatter_extra) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (doc_id, f"{doc_id}.md", kind, project, title, "current",
             "[]", '["team"]', body, "2026-08-01", "2026-08-02", extra))
    _reindex(iconn)
    iconn.close()

    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        AGENT: {"name": "vivy-agent", "products": ["vivy"]},
    }))

    monkeypatch.setattr(store, "DB_PATH", control)
    monkeypatch.setattr(reference, "INDEX_DB", index)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    return TestClient(libapp.app)


def _reindex(iconn):
    for doc_id, body in iconn.execute("SELECT id, body FROM docs").fetchall():
        iconn.execute("UPDATE docs SET body_hash = ? WHERE id = ?",
                      (hashlib.sha256((body or "").encode()).hexdigest(), doc_id))
    iconn.execute("INSERT INTO docs_fts(docs_fts) VALUES('rebuild')")
    iconn.commit()


def _h(token=AGENT):
    return {"Authorization": f"Bearer {token}",
            protocol.HEADER: str(protocol.CURRENT)}


def _doc(docs, doc_id):
    return next(d for d in docs if d["id"] == doc_id)


# --- every read surface serves the three fields on an instruction-block ------


def test_a_single_block_document_carries_the_three_selector_fields(client):
    r = client.get(f"/reference/documents/{BLOCK_ID}", headers=_h())
    assert r.status_code == 200
    body = r.json()
    assert body["binds_crews"] == ["platform"]
    assert body["binds_agents"] == ["reviewer", "coder"]
    assert body["binds_topics"] == ["verification"]
    # ...and the field nobody asked for stays off the wire.
    assert "source_author" not in body
    assert "frontmatter_extra" not in body
    # the pinning property the epic asks for already exists on this read.
    assert body["body_sha256"] == hashlib.sha256(
        body["body"].encode()).hexdigest()


def test_the_listing_carries_the_three_fields_on_block_rows(client):
    r = client.get("/reference/documents", headers=_h())
    assert r.status_code == 200
    doc = _doc(r.json()["documents"], BLOCK_ID)
    assert doc["binds_crews"] == ["platform"]
    assert doc["binds_agents"] == ["reviewer", "coder"]
    assert doc["binds_topics"] == ["verification"]
    assert "body_sha256" in doc


# --- a missing key is absent-as-null, never invented --------------------------


def test_a_selector_key_frontmatter_extra_does_not_carry_is_null(client):
    r = client.get(f"/reference/documents/{PARTIAL_ID}", headers=_h())
    assert r.status_code == 200
    body = r.json()
    assert body["binds_crews"] == ["platform", "vivy"]
    assert body["binds_agents"] is None
    assert body["binds_topics"] is None


# --- a non-block document gets none of the three -------------------------------


def test_a_decision_document_carries_none_of_the_selector_fields(client):
    r = client.get(f"/reference/documents/{DECISION_ID}", headers=_h())
    assert r.status_code == 200
    body = r.json()
    assert "binds_crews" not in body
    assert "binds_agents" not in body
    assert "binds_topics" not in body


def test_an_agent_document_carries_none_of_the_selector_fields(client):
    """An `agent` document's `frontmatter_extra` is a real, non-empty blob —
    the guard here is on `kind`, not on the column being absent."""
    r = client.get(f"/reference/documents/{AGENT_ID}", headers=_h())
    assert r.status_code == 200
    body = r.json()
    assert body["model"] == "sonnet"
    assert "binds_crews" not in body
    assert "binds_agents" not in body
    assert "binds_topics" not in body


def test_the_listing_leaves_the_three_fields_off_non_block_rows(client):
    r = client.get("/reference/documents", headers=_h())
    assert r.status_code == 200
    doc = _doc(r.json()["documents"], DECISION_ID)
    assert "binds_crews" not in doc
    assert "binds_agents" not in doc
    assert "binds_topics" not in doc


# --- a row this service cannot parse is still served, with none invented ------


def test_a_malformed_frontmatter_extra_is_served_without_the_three_fields(client):
    r = client.get(f"/reference/documents/{MALFORMED_ID}", headers=_h())
    assert r.status_code == 200
    body = r.json()
    assert "binds_crews" not in body
    assert "binds_agents" not in body
    assert "binds_topics" not in body


def test_a_null_frontmatter_extra_is_served_without_the_three_fields(client):
    r = client.get(f"/reference/documents/{NULL_EXTRA_ID}", headers=_h())
    assert r.status_code == 200
    body = r.json()
    assert "binds_crews" not in body
    assert "binds_agents" not in body
    assert "binds_topics" not in body


def test_a_malformed_or_null_row_does_not_break_the_listing_for_everyone(client):
    r = client.get("/reference/documents", headers=_h())
    assert r.status_code == 200
    ids = {d["id"] for d in r.json()["documents"]}
    assert {BLOCK_ID, MALFORMED_ID, NULL_EXTRA_ID} <= ids


# --- a selector value that is not a list of strings is passed through --------


def test_a_non_list_selector_value_is_served_as_is_not_coerced_or_dropped(client):
    """`binds_agents: "reviewer"` is a document the sibling validator refuses
    at CI, but this route reads an index built elsewhere and serves whatever
    the key holds rather than guessing or 500ing — the same passthrough
    `model`/`tools`/`trigger` already get on a malformed `agent` document."""
    r = client.get(f"/reference/documents/{SCALAR_EXTRA_ID}", headers=_h())
    assert r.status_code == 200
    body = r.json()
    assert body["binds_agents"] == "reviewer"


# --- an index with no frontmatter_extra column still serves -------------------


@pytest.fixture
def no_column_client(tmp_path, monkeypatch):
    control = tmp_path / "control.db"
    sqlite3.connect(control).close()

    index = tmp_path / "index.db"
    iconn = sqlite3.connect(index)
    iconn.executescript("""
        CREATE TABLE docs (
            id TEXT PRIMARY KEY, source_ref TEXT, kind TEXT, project TEXT,
            title TEXT, status TEXT, topics TEXT, audience TEXT,
            last_reviewed TEXT, updated_at TEXT, body TEXT, body_hash TEXT);
    """)
    iconn.execute(
        "INSERT INTO docs (id, source_ref, kind, project, title, status, "
        "topics, audience, body, last_reviewed, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (BLOCK_ID, f"{BLOCK_ID}.md", "instruction-block", "platform",
         "verify before you report", "current", "[]", '["team"]',
         "an old row from before the column existed",
         "2026-08-01", "2026-08-02"))
    for doc_id, body in iconn.execute("SELECT id, body FROM docs").fetchall():
        iconn.execute("UPDATE docs SET body_hash = ? WHERE id = ?",
                      (hashlib.sha256((body or "").encode()).hexdigest(), doc_id))
    iconn.commit()
    iconn.close()

    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        AGENT: {"name": "vivy-agent", "products": ["vivy"]},
    }))

    monkeypatch.setattr(store, "DB_PATH", control)
    monkeypatch.setattr(reference, "INDEX_DB", index)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    return TestClient(libapp.app)


def test_an_index_with_no_frontmatter_extra_column_still_serves(no_column_client):
    r = no_column_client.get(f"/reference/documents/{BLOCK_ID}", headers=_h())
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == BLOCK_ID
    assert "binds_crews" not in body
    assert "binds_agents" not in body
    assert "binds_topics" not in body
