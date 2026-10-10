"""`model`, `tools` and `trigger` on every agent reference the Library returns
(issue_01M1RYSP06C98MN55ZAC47NVNA, under the content-move epic
issue_01M092R482KSQHKKVG4XJ8X257).

**Measured against the live Library at 451ad71:** `GET
/reference/documents/agent-platform/agents/coder` served id, source_ref, kind,
project, title, status, topics, audience, last_reviewed, updated_at, body and
body_sha256 — and no `model` or `tools`, for every agent. The knowledge index
holds them in the `frontmatter_extra` column as canonical JSON
(agent-platform#517); `reference.doc_dict` built its output from a fixed field
list and never read that column. No dispatch could get an agent's tool policy
or model from the Library, which the epic's definition of done requires.

**One fixture, all four call sites**, matching `test_reference_content_hash
.py`'s shape for the same reason: a claim quantified over "every reference"
that was checked on whichever surface was convenient is exactly the shape
that ships with the fifth surface uncovered.
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

CODER_ID = "agent-platform/agents/coder"
CODER_EXTRA = {"model": "sonnet",
               "tools": ["Read", "Edit", "Write", "Bash(git:*)", "Bash(test:*)",
                         "Grep", "Glob"],
               "trigger": "when implementing a spec on the agent-platform substrate",
               "role": "coder",
               "model_floor": "deepseek",
               "builtins": ["Bash", "Read"],
               # A FIELD NO CALLER HAS ASKED FOR, alongside the three that
               # travel — the fixture that proves the wire only carries
               # `model`/`tools`/`trigger` needs one of these sitting beside
               # them in the same blob.
               "source_author": "platform-architect"}

WRITER_ID = "agent-platform/agents/commercial-writer"
WRITER_EXTRA = {"model": "opus", "tools": ["Read", "Write"]}  # no `trigger`, no `builtins`

EMPTY_BUILTINS_ID = "agent-platform/agents/empty-builtins"
EMPTY_BUILTINS_EXTRA = {"model": "opus", "builtins": []}

DECISION_ID = "platform/decisions/0022-library-repo"
DECISION_EXTRA = {"source_author": "operator", "role": "not-an-agent-role",
                  "model_floor": "deepseek",
                  "builtins": ["Read"]}  # non-agent; must not leak model/tools/builtins

SKILL_ID = "agent-platform/skills/write-spec"
# A skill's blob holding agent-only keys too, so the tests can show only
# `trigger` travels on a skill.
SKILL_EXTRA = {"trigger": "when writing a work item's specification",
               "model": "opus", "role": "coder"}
BARE_SKILL_ID = "agent-platform/skills/no-trigger"
MALFORMED_SKILL_ID = "agent-platform/skills/malformed-extra"

MALFORMED_ID = "agent-platform/agents/malformed-extra"
NULL_EXTRA_ID = "agent-platform/agents/null-extra"


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
    """)
    conn.commit()
    conn.close()

    index = tmp_path / "index.db"
    iconn = sqlite3.connect(index)
    # SHAPED LIKE THE REAL INDEX, `frontmatter_extra` included
    # (agent-platform#517) — FTS5 too, since search is one of the four
    # surfaces this file's claim covers.
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
        (CODER_ID, "agent", "agent-platform", "the coder agent",
         "the coder implements a spec on the substrate",
         json.dumps(CODER_EXTRA)),
        (WRITER_ID, "agent", "agent-platform", "the commercial-writer agent",
         "the commercial writer drafts marketing copy",
         json.dumps(WRITER_EXTRA)),
        (EMPTY_BUILTINS_ID, "agent", "agent-platform", "an agent with no built-ins",
         "an agent document whose builtins is an explicit empty list",
         json.dumps(EMPTY_BUILTINS_EXTRA)),
        (DECISION_ID, "decision", "platform",
         "The Library gets its own repository",
         "the Library is a service with its own storage",
         json.dumps(DECISION_EXTRA)),
        (SKILL_ID, "skill", "agent-platform", "write-spec",
         "how to write a work item specification", json.dumps(SKILL_EXTRA)),
        (BARE_SKILL_ID, "skill", "agent-platform", "no-trigger",
         "a skill whose frontmatter sets no trigger", json.dumps({})),
        (MALFORMED_SKILL_ID, "skill", "agent-platform", "malformed skill",
         "a skill row with unparseable frontmatter_extra", "not valid json"),
        (MALFORMED_ID, "agent", "agent-platform", "a row this service did not write",
         "an agent document with unparseable frontmatter_extra",
         "not valid json at all"),
        (NULL_EXTRA_ID, "agent", "agent-platform", "an older row",
         "an agent document indexed before frontmatter_extra was backfilled",
         None),
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
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
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


def _seed_profile(project, variant, include):
    return crew_registry.profile_put(ORCHESTRATOR, project, variant, include, [])


def _doc(docs, doc_id):
    return next(d for d in docs if d["id"] == doc_id)


# --- every surface serves the three fields on an agent document --------------


def test_a_single_agent_document_carries_model_tools_and_trigger(client):
    r = client.get(f"/reference/documents/{CODER_ID}", headers=_h())
    assert r.status_code == 200
    body = r.json()
    assert body["model"] == "sonnet"
    assert body["tools"] == CODER_EXTRA["tools"]
    assert body["trigger"] == CODER_EXTRA["trigger"]
    # ...and the field nobody asked for stays off the wire.
    assert "source_author" not in body
    assert "frontmatter_extra" not in body


def test_the_listing_carries_the_three_fields_on_agent_rows(client):
    r = client.get("/reference/documents", headers=_h())
    assert r.status_code == 200
    doc = _doc(r.json()["documents"], CODER_ID)
    assert doc["model"] == "sonnet"
    assert doc["tools"] == CODER_EXTRA["tools"]
    assert doc["trigger"] == CODER_EXTRA["trigger"]


def test_search_carries_the_three_fields_on_an_agent_hit(client):
    r = client.get("/reference/search", params={"q": "coder implements substrate"},
                   headers=_h())
    assert r.status_code == 200
    doc = _doc(r.json()["documents"], CODER_ID)
    assert doc["model"] == "sonnet"
    assert doc["tools"] == CODER_EXTRA["tools"]


def test_a_resolved_profile_carries_the_three_fields_on_its_agent_documents(
        client):
    _seed_profile("agent-platform", "daytime", [{"kind": "agent"}])
    r = client.get("/profiles/agent-platform/daytime", headers=_h())
    assert r.status_code == 200
    doc = _doc(r.json()["documents"], CODER_ID)
    assert doc["model"] == "sonnet"
    assert doc["tools"] == CODER_EXTRA["tools"]
    assert doc["trigger"] == CODER_EXTRA["trigger"]


# --- a missing key is absent or null, never invented --------------------------


def test_a_key_frontmatter_extra_does_not_carry_is_null_not_invented(client):
    """`commercial-writer` has no `trigger` in its frontmatter — the field must
    read `None`, not be fabricated and not silently disappear."""
    r = client.get(f"/reference/documents/{WRITER_ID}", headers=_h())
    assert r.status_code == 200
    body = r.json()
    assert body["model"] == "opus"
    assert body["tools"] == ["Read", "Write"]
    assert body["trigger"] is None


# --- a non-agent document gets none of the three -------------------------------


def test_a_non_agent_document_carries_none_of_the_three_fields(client):
    """`platform/decisions/0022...` carries `source_author` in its own
    `frontmatter_extra` — an agent's tool policy and model are not a fact
    about a decision record, so nothing here should leak even though the
    column itself has content."""
    r = client.get(f"/reference/documents/{DECISION_ID}", headers=_h())
    assert r.status_code == 200
    body = r.json()
    assert "model" not in body
    assert "tools" not in body
    assert "trigger" not in body


def test_the_listing_leaves_the_three_fields_off_non_agent_rows(client):
    r = client.get("/reference/documents", headers=_h())
    assert r.status_code == 200
    doc = _doc(r.json()["documents"], DECISION_ID)
    assert "model" not in doc and "tools" not in doc and "trigger" not in doc


# --- a row this service cannot parse is still served ---------------------------


def test_a_malformed_frontmatter_extra_is_served_without_the_three_fields(client):
    r = client.get(f"/reference/documents/{MALFORMED_ID}", headers=_h())
    assert r.status_code == 200
    body = r.json()
    assert "model" not in body
    assert "tools" not in body
    assert "trigger" not in body


def test_a_null_frontmatter_extra_is_served_without_the_three_fields(client):
    r = client.get(f"/reference/documents/{NULL_EXTRA_ID}", headers=_h())
    assert r.status_code == 200
    body = r.json()
    assert "model" not in body
    assert "tools" not in body
    assert "trigger" not in body


def test_a_malformed_or_null_row_does_not_break_the_listing_for_everyone(client):
    """One bad row must not take the whole route down for every caller —
    the failure mode `audience_clause`'s own guard exists to prevent, and the
    same discipline this column needs."""
    r = client.get("/reference/documents", headers=_h())
    assert r.status_code == 200
    ids = {d["id"] for d in r.json()["documents"]}
    assert {CODER_ID, MALFORMED_ID, NULL_EXTRA_ID} <= ids


# --- an index built before agent-platform#517 has no such column --------------


@pytest.fixture
def no_column_client(tmp_path, monkeypatch):
    """The knowledge index as it existed before agent-platform#517 —
    `frontmatter_extra` never added, not merely empty. `doc_dict` must not
    raise `IndexError` reading a column that is not there."""
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
        (CODER_ID, f"{CODER_ID}.md", "agent", "agent-platform", "the coder agent",
         "current", "[]", '["team"]', "an old row from before the column existed",
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
    r = no_column_client.get(f"/reference/documents/{CODER_ID}", headers=_h())
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == CODER_ID
    assert "model" not in body
    assert "tools" not in body
    assert "trigger" not in body


def test_the_listing_still_serves_with_no_frontmatter_extra_column(
        no_column_client):
    r = no_column_client.get("/reference/documents", headers=_h())
    assert r.status_code == 200
    doc = _doc(r.json()["documents"], CODER_ID)
    assert "model" not in doc and "tools" not in doc and "trigger" not in doc


# --- `role` (issue_01M38TY0ES13YXCATFDG87C7B4) --------------------------------


def test_a_single_agent_document_carries_its_role(client):
    r = client.get(f"/reference/documents/{CODER_ID}", headers=_h())
    assert r.json()["role"] == "coder"


def test_the_listing_search_and_profile_carry_role_on_agent_rows(client):
    listing = client.get("/reference/documents", headers=_h())
    assert _doc(listing.json()["documents"], CODER_ID)["role"] == "coder"
    hit = client.get("/reference/search",
                     params={"q": "coder implements substrate"}, headers=_h())
    assert _doc(hit.json()["documents"], CODER_ID)["role"] == "coder"
    _seed_profile("agent-platform", "daytime", [{"kind": "agent"}])
    prof = client.get("/profiles/agent-platform/daytime", headers=_h())
    assert _doc(prof.json()["documents"], CODER_ID)["role"] == "coder"


def test_an_agent_without_a_role_key_reads_null_not_invented(client):
    r = client.get(f"/reference/documents/{WRITER_ID}", headers=_h())
    assert r.json()["role"] is None


def test_a_non_agent_document_has_no_role_key_even_when_its_blob_holds_one(client):
    r = client.get(f"/reference/documents/{DECISION_ID}", headers=_h())
    assert "role" not in r.json()
    listing = client.get("/reference/documents", headers=_h())
    assert "role" not in _doc(listing.json()["documents"], DECISION_ID)


def test_a_malformed_or_null_frontmatter_extra_serves_no_role(client):
    for doc_id in (MALFORMED_ID, NULL_EXTRA_ID):
        r = client.get(f"/reference/documents/{doc_id}", headers=_h())
        assert "role" not in r.json()


# --- `model_floor` (issue_01M35KPG11KD8BZTEVKWAMQX26) -------------------------


def test_a_single_agent_document_carries_its_model_floor(client):
    r = client.get(f"/reference/documents/{CODER_ID}", headers=_h())
    assert r.json()["model_floor"] == "deepseek"


def test_the_listing_search_and_profile_carry_model_floor_on_agent_rows(client):
    listing = client.get("/reference/documents", headers=_h())
    assert _doc(listing.json()["documents"], CODER_ID)["model_floor"] == "deepseek"
    hit = client.get("/reference/search",
                     params={"q": "coder implements substrate"}, headers=_h())
    assert _doc(hit.json()["documents"], CODER_ID)["model_floor"] == "deepseek"
    _seed_profile("agent-platform", "daytime", [{"kind": "agent"}])
    prof = client.get("/profiles/agent-platform/daytime", headers=_h())
    assert _doc(prof.json()["documents"], CODER_ID)["model_floor"] == "deepseek"


def test_an_agent_without_a_model_floor_reads_null_not_invented(client):
    r = client.get(f"/reference/documents/{WRITER_ID}", headers=_h())
    assert r.json()["model_floor"] is None


def test_a_non_agent_document_has_no_model_floor_even_when_its_blob_holds_one(client):
    r = client.get(f"/reference/documents/{DECISION_ID}", headers=_h())
    assert "model_floor" not in r.json()


def test_a_malformed_or_null_frontmatter_extra_serves_no_model_floor(client):
    for doc_id in (MALFORMED_ID, NULL_EXTRA_ID):
        r = client.get(f"/reference/documents/{doc_id}", headers=_h())
        assert "model_floor" not in r.json()


# --- `builtins` (agent-platform#818) -------------------------------------------


def test_a_single_agent_document_carries_its_builtins_list(client):
    r = client.get(f"/reference/documents/{CODER_ID}", headers=_h())
    assert r.json()["builtins"] == ["Bash", "Read"]


def test_the_listing_search_and_profile_carry_builtins_on_agent_rows(client):
    listing = client.get("/reference/documents", headers=_h())
    assert _doc(listing.json()["documents"], CODER_ID)["builtins"] == ["Bash", "Read"]
    hit = client.get("/reference/search",
                     params={"q": "coder implements substrate"}, headers=_h())
    assert _doc(hit.json()["documents"], CODER_ID)["builtins"] == ["Bash", "Read"]
    _seed_profile("agent-platform", "daytime", [{"kind": "agent"}])
    prof = client.get("/profiles/agent-platform/daytime", headers=_h())
    assert _doc(prof.json()["documents"], CODER_ID)["builtins"] == ["Bash", "Read"]


def test_an_agent_without_a_builtins_key_reads_null_not_invented(client):
    """`None` means "no override" to `agentctl`'s `resolve_agent` — it must
    never collapse to `[]`, which means "load no built-in tools" instead."""
    r = client.get(f"/reference/documents/{WRITER_ID}", headers=_h())
    assert r.json()["builtins"] is None


def test_an_agent_with_an_explicit_empty_builtins_list_reads_the_empty_list(client):
    """The opposite failure: `[]` must be served as `[]`, not silently
    widened to `None` — the two mean opposite things to `resolve_agent`."""
    r = client.get(f"/reference/documents/{EMPTY_BUILTINS_ID}", headers=_h())
    assert r.json()["builtins"] == []


def test_a_non_agent_document_has_no_builtins_key_even_when_its_blob_holds_one(client):
    r = client.get(f"/reference/documents/{DECISION_ID}", headers=_h())
    assert "builtins" not in r.json()


def test_a_malformed_or_null_frontmatter_extra_serves_no_builtins(client):
    for doc_id in (MALFORMED_ID, NULL_EXTRA_ID):
        r = client.get(f"/reference/documents/{doc_id}", headers=_h())
        assert "builtins" not in r.json()


# --- `trigger` on a skill (issue_01M34SV3BDRHBTXZPZ8C4AH9HD) ------------------


def test_a_single_skill_document_carries_its_trigger_and_no_agent_field(client):
    body = client.get(f"/reference/documents/{SKILL_ID}", headers=_h()).json()
    assert body["trigger"] == SKILL_EXTRA["trigger"]
    for key in ("model", "tools", "role", "model_floor", "builtins"):
        assert key not in body


def test_the_listing_search_and_profile_carry_trigger_on_skill_rows(client):
    listing = client.get("/reference/documents", headers=_h())
    want = SKILL_EXTRA["trigger"]
    assert _doc(listing.json()["documents"], SKILL_ID)["trigger"] == want
    hit = client.get("/reference/search",
                     params={"q": "work item specification"}, headers=_h())
    assert _doc(hit.json()["documents"], SKILL_ID)["trigger"] == want
    _seed_profile("agent-platform", "skills", [{"kind": "skill"}])
    prof = client.get("/profiles/agent-platform/skills", headers=_h())
    assert _doc(prof.json()["documents"], SKILL_ID)["trigger"] == want


def test_a_skill_without_a_trigger_reads_null_not_invented(client):
    r = client.get(f"/reference/documents/{BARE_SKILL_ID}", headers=_h())
    assert r.json()["trigger"] is None


def test_a_malformed_skill_frontmatter_extra_serves_no_trigger(client):
    r = client.get(f"/reference/documents/{MALFORMED_SKILL_ID}", headers=_h())
    assert r.status_code == 200
    assert "trigger" not in r.json()
