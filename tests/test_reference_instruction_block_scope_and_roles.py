"""`scope` and `binds_roles` on an `instruction-block` document
(issue_01M4AMQ15DGKET6NKXFZM0QS7X), for the Bosun's `knowledge-ctx use`
(agent-platform#801) to read instruction blocks from the Library instead of
a local index.

These join `binds_crews`, `binds_agents` and `binds_topics`
(`test_reference_instruction_block_fields.py`, issue_01M2MC21YAWT2V61QWSM247VRX)
as the fifth and fourth fields `doc_dict` reads out of the same
`frontmatter_extra` blob, on the same `_frontmatter_extra_dict` contract, so
this file follows that one's shape: one fixture, the read surfaces the two
new fields travel on, and the same absent/unparseable-`frontmatter_extra`
cases, run against `origin/main` first to confirm each fails there.
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
               "scope": "fleet",
               "binds_roles": ["coder", "reviewer"],
               # A field no caller asked for, alongside the five that
               # travel, matching `CODER_EXTRA["source_author"]` in
               # `test_reference_agent_fields.py`.
               "source_author": "steward"}

PARTIAL_ID = "platform/blocks/floor-only"
PARTIAL_EXTRA = {"binds_crews": ["platform", "vivy"]}  # no scope/binds_roles

DECISION_ID = "platform/decisions/0022-library-repo"
DECISION_EXTRA = {"source_author": "operator"}  # non-block; must not leak the two

MALFORMED_ID = "platform/blocks/malformed-extra"


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
        (MALFORMED_ID, "instruction-block", "platform",
         "a row this service did not write",
         "an instruction-block with unparseable frontmatter_extra",
         "not valid json at all"),
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


def test_a_block_carrying_both_fields_serves_them_as_written(client):
    r = client.get(f"/reference/documents/{BLOCK_ID}", headers=_h())
    assert r.status_code == 200
    body = r.json()
    assert body["scope"] == "fleet"
    assert body["binds_roles"] == ["coder", "reviewer"]
    # the three sibling selector fields still travel alongside them.
    assert body["binds_crews"] == ["platform"]
    assert body["binds_agents"] == ["reviewer", "coder"]
    assert body["binds_topics"] == ["verification"]
    assert "source_author" not in body


def test_a_block_without_the_two_fields_serves_none_for_each(client):
    r = client.get(f"/reference/documents/{PARTIAL_ID}", headers=_h())
    assert r.status_code == 200
    body = r.json()
    assert body["scope"] is None
    assert body["binds_roles"] is None


def test_a_malformed_frontmatter_extra_drops_all_five_fields(client):
    r = client.get(f"/reference/documents/{MALFORMED_ID}", headers=_h())
    assert r.status_code == 200
    body = r.json()
    assert "scope" not in body
    assert "binds_roles" not in body
    assert "binds_crews" not in body
    assert "binds_agents" not in body
    assert "binds_topics" not in body


def test_a_decision_document_carries_neither_field(client):
    r = client.get(f"/reference/documents/{DECISION_ID}", headers=_h())
    assert r.status_code == 200
    body = r.json()
    assert "scope" not in body
    assert "binds_roles" not in body
