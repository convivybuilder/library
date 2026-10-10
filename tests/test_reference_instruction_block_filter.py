"""`GET /reference/documents?binds_crews=...&binds_agents=...&binds_topics=...`
(issue_01M2MC21YAWT2V61QWSM247VRX), the listing route's half of the
instruction-block work: filter by the same membership semantics `audience`
already has, AND several selectors together, and never 500 on a
`frontmatter_extra` this service did not write.

Fixture shaped like `test_reference_reads.py`'s audience-filter corpus —
matches and non-matches for every filter, plus the malformed/empty/null/
wrong-shape rows `audience_clause`'s own tests already cover for the
`audience` column, reproduced here for `frontmatter_extra` because the
selector fields read a different guard (`app.py`'s `_selector_clause`, which
guards the extraction as well as the extracted value).
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

# id, extra dict or raw string (None means no frontmatter_extra at all)
BLOCK_REVIEWER_PLATFORM = "platform/blocks/review-invariants"
BLOCK_CODER_VIVY = "platform/blocks/shared-checkout"
BLOCK_SECURITY_PLATFORM = "platform/blocks/dependency-tiering"

DOCS = [
    (BLOCK_REVIEWER_PLATFORM, "instruction-block", "platform",
     json.dumps({"binds_crews": ["platform"],
                 "binds_agents": ["reviewer", "coder"],
                 "binds_topics": ["review"]})),
    (BLOCK_CODER_VIVY, "instruction-block", "vivy",
     json.dumps({"binds_crews": ["vivy"],
                 "binds_agents": ["coder"],
                 "binds_topics": ["shared-checkout"]})),
    (BLOCK_SECURITY_PLATFORM, "instruction-block", "platform",
     json.dumps({"binds_crews": ["platform"],
                 "binds_agents": ["security-reviewer"],
                 "binds_topics": ["dependencies"]})),
    ("platform/decisions/0022-library-repo", "decision", "platform", None),
]

# **`frontmatter_extra` VALUES THIS SERVICE DOES NOT CONTROL**, the same
# reasoning `RAW_AUDIENCE_DOCS` in `test_reference_reads.py` gives for the
# `audience` column, reproduced here because the selector fields are read
# through a second, independent guard (the extraction itself, not only the
# extracted value — see `app.py._selector_clause`).
RAW_EXTRA_DOCS = [
    ("platform/blocks/null-extra", "instruction-block", "platform", None),
    ("platform/blocks/empty-extra", "instruction-block", "platform", ""),
    ("platform/blocks/broken-extra", "instruction-block", "platform",
     "not-json"),
    # valid JSON, wrong top-level shape: `frontmatter_extra` itself an array.
    ("platform/blocks/array-extra", "instruction-block", "platform", "[1,2]"),
    # valid JSON object, but `binds_agents` is an object rather than an array
    # — `json_each` on an object iterates its VALUES, so an unguarded test
    # would make `binds_agents=coder` match a document whose `binds_agents`
    # was never `["coder"]`.
    ("platform/blocks/object-shaped-agents", "instruction-block", "platform",
     json.dumps({"binds_agents": {"a": "coder"}})),
    ("platform/blocks/scalar-agents", "instruction-block", "platform",
     json.dumps({"binds_agents": "coder"})),
]


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
    """)
    for doc_id, kind, project, extra in DOCS + RAW_EXTRA_DOCS:
        iconn.execute(
            "INSERT INTO docs (id, source_ref, kind, project, title, status, "
            "topics, audience, body, last_reviewed, updated_at, "
            "frontmatter_extra) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (doc_id, f"{doc_id}.md", kind, project, f"title of {doc_id}",
             "current", "[]", '["team"]', f"body of {doc_id}",
             "2026-08-01", "2026-08-02", extra))
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


@pytest.fixture
def no_column_client(tmp_path, monkeypatch):
    """An index whose `docs` table predates `frontmatter_extra` entirely.

    Not hypothetical: `reference._frontmatter_extra_dict` handles this state
    per row because an index built before agent-platform#517 has no such
    column, and this service does not build the index. A `WHERE` clause naming
    the column would be a parse error rather than a miss, so the filter needs
    its own guard and this fixture is what holds it (reviewer, PR #122).
    """
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
    for doc_id, kind, project, _extra in DOCS:
        iconn.execute(
            "INSERT INTO docs (id, source_ref, kind, project, title, status, "
            "topics, audience, body, last_reviewed, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (doc_id, f"{doc_id}.md", kind, project, f"title of {doc_id}",
             "current", "[]", '["team"]', f"body of {doc_id}",
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


def _h(token=AGENT):
    return {"Authorization": f"Bearer {token}",
            protocol.HEADER: str(protocol.CURRENT)}


def _list(client, **params):
    return client.get("/reference/documents", params=params, headers=_h())


def _ids(r):
    return [d["id"] for d in r.json()["documents"]]


# --- membership, not equality --------------------------------------------------


def test_binds_agents_matches_membership_not_equality(client):
    """`coder` appears in `BLOCK_REVIEWER_PLATFORM`'s two-agent list and as
    `BLOCK_CODER_VIVY`'s only entry — a naive `=` would only find the second."""
    got = set(_ids(_list(client, binds_agents="coder")))
    assert got == {BLOCK_REVIEWER_PLATFORM, BLOCK_CODER_VIVY}


def test_binds_agents_does_not_match_a_substring(client):
    assert _ids(_list(client, binds_agents="code")) == []


# --- several selectors AND -----------------------------------------------------


def test_two_selectors_and(client):
    """`binds_agents=coder` alone matches two blocks; adding `binds_crews=vivy`
    narrows to the one block naming both, matching how `kind`+`project`
    already compose on this route."""
    got = set(_ids(_list(client, binds_agents="coder", binds_crews="vivy")))
    assert got == {BLOCK_CODER_VIVY}


def test_all_three_selectors_and(client):
    got = set(_ids(_list(client, binds_crews="platform",
                          binds_agents="reviewer", binds_topics="review")))
    assert got == {BLOCK_REVIEWER_PLATFORM}


# --- a selector matching nothing is an empty list, not an error ---------------


def test_a_selector_matching_nothing_returns_an_empty_list(client):
    r = _list(client, binds_agents="nonexistent-agent")
    assert r.status_code == 200
    assert _ids(r) == []


# --- filters compose with the existing ones ------------------------------------


def test_a_selector_narrows_alongside_kind(client):
    got = set(_ids(_list(client, kind="instruction-block", binds_crews="platform")))
    assert got == {BLOCK_REVIEWER_PLATFORM, BLOCK_SECURITY_PLATFORM}


# --- a document with no selector fields is simply excluded, not an error -----


def test_a_non_block_document_never_matches_a_selector_filter(client):
    got = _ids(_list(client, binds_crews="platform"))
    assert "platform/decisions/0022-library-repo" not in got


# --- malformed `frontmatter_extra` anywhere in the corpus does not 500 -------


def test_a_malformed_frontmatter_extra_does_not_500_the_filtered_listing(client):
    r = _list(client, binds_agents="coder")
    assert r.status_code == 200
    got = set(_ids(r))
    assert got == {BLOCK_REVIEWER_PLATFORM, BLOCK_CODER_VIVY}


def test_rows_with_unusable_frontmatter_extra_match_no_selector_filter(client):
    for doc_id, *_ in RAW_EXTRA_DOCS:
        got = _ids(_list(client, binds_agents="coder"))
        assert doc_id not in got


def test_a_malformed_row_is_still_listed_unfiltered(client):
    assert "platform/blocks/broken-extra" in _ids(_list(client))


def test_a_valid_but_object_shaped_binds_agents_does_not_match(client):
    """`json_each` on an object iterates its VALUES — `{"a": "coder"}` must
    not make `binds_agents=coder` match a document whose `binds_agents` was
    never `["coder"]`."""
    got = _ids(_list(client, binds_agents="coder"))
    assert "platform/blocks/object-shaped-agents" not in got
    assert "platform/blocks/scalar-agents" not in got


# --- unaffected filters and fields keep working --------------------------------


def test_audience_filter_still_works_alongside_selector_fields(client):
    got = set(_ids(_list(client, audience="team")))
    all_ids = {doc_id for doc_id, *_ in DOCS + RAW_EXTRA_DOCS}
    assert got == all_ids


def test_listing_rows_still_carry_body_sha256(client):
    r = _list(client)
    assert r.status_code == 200
    for doc in r.json()["documents"]:
        assert doc["body_sha256"] == hashlib.sha256(
            f"body of {doc['id']}".encode()).hexdigest()


# --- an index with no frontmatter_extra column at all --------------------------


def test_a_selector_filter_does_not_500_an_index_without_the_column(
        no_column_client):
    """The blocking finding on this PR: `_selector_clause` named
    `d.frontmatter_extra` unconditionally, so this raised
    `OperationalError: no such column` and 500'd the request rather than
    answering it.
    """
    r = _list(no_column_client, binds_agents="coder")
    assert r.status_code == 200


def test_a_selector_filter_matches_nothing_without_the_column(no_column_client):
    """No column means no document can carry a selector, so the honest answer
    to 'which blocks bind this agent' is none — not an error, and not the
    unfiltered corpus, which would be the far worse failure of silently
    ignoring the filter."""
    r = _list(no_column_client, binds_agents="coder")
    assert _ids(r) == []
    assert r.json()["total"] == 0


def test_two_selectors_without_the_column_still_match_nothing(no_column_client):
    """Guards the params/clause alignment: the absent-column branch appends a
    clause but no bound parameter, so a second selector must not shift the
    remaining parameters out of position."""
    r = _list(no_column_client, binds_agents="coder", binds_crews="library")
    assert r.status_code == 200
    assert _ids(r) == []


def test_a_selector_and_a_bound_filter_stay_aligned_without_the_column(
        no_column_client):
    """The same alignment risk with a parameterised filter AFTER the
    unparameterised one: `audience` binds a value, so a mismatch here would
    surface as a wrong answer rather than an error."""
    r = _list(no_column_client, binds_agents="coder", audience="team")
    assert r.status_code == 200
    assert _ids(r) == []


def test_unfiltered_listing_still_works_without_the_column(no_column_client):
    """The guard must not cost the ordinary listing anything."""
    r = _list(no_column_client)
    assert r.status_code == 200
    assert len(_ids(r)) == len(DOCS)
