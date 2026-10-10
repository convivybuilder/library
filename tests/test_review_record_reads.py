"""The review-record read routes (issue_01M2HS42EA7V4DEXQX1R1PWS9Y), the
Library's half of "All reviews are memory" after the schema
(issue_01M2HS394KTFMRATFK1N3G4ZNB) and the write route
(issue_01M2HS3Q36RE5F82QXRN4Z0D0K).

Built on a database from the real migration, `test_review_record_routes.py`'s
own reason: the behaviour under test includes the uniqueness key, the two
indexes and the `auth = 'run-verified'` CHECK, and a hand-written fixture
schema is a second copy that drifts.

Rows are seeded through `store.write_review_record` directly rather than
through `POST /review-records` — these tests exercise the read routes, not
the write route's `auth` derivation (already covered by
`test_review_record_routes.py`), and seeding through the store function is
the real store layer against the real migrated database, not a mock of it.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as libapp
import identity
import projects
import store

REPO = Path(__file__).resolve().parent.parent
SYSTEM = "system-token"
AGENT = "agent-token"
NARROW = "narrow-token"
H = {"Library-Protocol": "1"}


def _h(token=SYSTEM):
    return {**H, "Authorization": f"Bearer {token}"}


@pytest.fixture
def client(tmp_path, monkeypatch, vector_index):
    db = tmp_path / "reviews.db"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LIBRARY_DB": str(db)})
    assert result.returncode == 0, result.stderr
    vector_index(db)

    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        SYSTEM: {"name": "system", "products": ["*"], "kind": "system"},
        AGENT: {"name": "an-agent", "products": ["*"]},
        NARROW: {"name": "vivy-only", "products": ["vivy"], "kind": "system"}}))
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({
        "_meta": {}, "library": {"repos": ["library"]},
        "vivy": {"repos": ["vivy"]}}))

    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    monkeypatch.setattr(projects, "REGISTRY_PATH", registry)
    projects.reload()
    yield TestClient(libapp.app)
    projects.reload()


REPO_IDENTITY = "github:convivy/library"


def seed(*, repo_identity=REPO_IDENTITY, product="library", pr=None,
         reviewed_ref="a" * 40, role="reviewer", run_id=None, verdict="APPROVE",
         findings=None, prose="", paths=None, doc_ids=None, item_ids=None,
         auth="unverified", auth_reason="no run", source_comment_id=None):
    """One review record, written through the real store function —
    `write_review_record`'s own idempotent-or-supersede rule applies, so
    calling this twice on the same (repo_identity, run_id-or-content, role,
    reviewed_ref) supersedes rather than duplicating."""
    row = {
        "repo_identity": repo_identity, "product": product, "pr": pr,
        "reviewed_ref": reviewed_ref, "role": role, "run_id": run_id,
        "verdict": verdict, "findings": findings or [], "prose": prose,
        "paths": paths or [], "doc_ids": doc_ids or [], "item_ids": item_ids or [],
        "auth": auth, "auth_reason": auth_reason,
        "source_comment_id": source_comment_id,
    }
    return store.write_review_record(row)


def insert_malformed_paths_row(db_path, *, repo_identity, product,
                               reviewed_ref="f" * 40, role="reviewer"):
    """A `review_records` row whose `paths` column holds unparseable JSON,
    written directly with `sqlite3` — the shape a hand-edited or
    corrupted row could reach the table in, which no write route path
    produces."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO review_records "
            "(id, repo_identity, product, pr, reviewed_ref, role, run_id, "
            " identity_key, verdict, findings, prose, paths, doc_ids, "
            " item_ids, auth, auth_reason, content_sha, version, "
            " source_comment_id, signal_id, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,NULL,?,?)",
            (f"rev_{store.new_ulid()}", repo_identity, product, None,
             reviewed_ref, role, None, "content:malformed", "APPROVE", "[]",
             "", "{not valid json", "[]", "[]", "unverified", "no run",
             "deadbeef" * 8, None, "2026-09-15 00:00:00", "2026-09-15 00:00:00"))
        conn.commit()
    finally:
        conn.close()


# --- verdicts -----------------------------------------------------------------

def test_verdicts_by_pr(client):
    status, outcome, rec = seed(pr=42, reviewed_ref="a" * 40, role="reviewer")
    assert status == 201, outcome
    r = client.get("/review-records/verdicts",
                   params={"repo_identity": REPO_IDENTITY, "pr": 42},
                   headers=_h())
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["verdicts"]) == 1
    v = body["verdicts"][0]
    assert v["record_id"] == rec["id"]
    assert v["role"] == "reviewer"
    assert v["verdict"] == "APPROVE"
    assert v["pr"] == 42
    assert v["reviewed_ref"] == "a" * 40
    assert v["version"] == 1
    assert "prose" not in v
    assert "findings" not in v


def test_verdicts_by_commit_with_no_pr(client):
    status, outcome, rec = seed(pr=None, reviewed_ref="b" * 40, role="reviewer")
    assert status == 201, outcome
    r = client.get("/review-records/verdicts",
                   params={"repo_identity": REPO_IDENTITY, "reviewed_ref": "b" * 40},
                   headers=_h())
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["verdicts"]) == 1
    assert body["verdicts"][0]["record_id"] == rec["id"]
    assert body["verdicts"][0]["pr"] is None


def test_an_unverified_record_is_returned_and_marked(client):
    seed(pr=7, auth="unverified", auth_reason="no run")
    r = client.get("/review-records/verdicts",
                   params={"repo_identity": REPO_IDENTITY, "pr": 7}, headers=_h())
    assert r.status_code == 200, r.text
    v = r.json()["verdicts"][0]
    assert v["auth"] == "unverified"
    assert v["auth_reason"] == "no run"


def test_verdicts_requires_exactly_one_of_pr_or_reviewed_ref(client):
    neither = client.get("/review-records/verdicts",
                         params={"repo_identity": REPO_IDENTITY}, headers=_h())
    assert neither.status_code == 400

    both = client.get(
        "/review-records/verdicts",
        params={"repo_identity": REPO_IDENTITY, "pr": 1, "reviewed_ref": "a" * 40},
        headers=_h())
    assert both.status_code == 400


def test_a_malformed_repo_identity_is_refused(client):
    r = client.get("/review-records/verdicts",
                   params={"repo_identity": "not-a-valid-identity", "pr": 1},
                   headers=_h())
    assert r.status_code == 400


def test_verdicts_out_of_scope_is_not_found(client):
    seed(pr=9)
    r = client.get("/review-records/verdicts",
                   params={"repo_identity": REPO_IDENTITY, "pr": 9},
                   headers=_h(NARROW))
    assert r.status_code == 404


# --- retrieval by path, doc_id, item_id ---------------------------------------

def test_retrieval_by_path(client):
    status, outcome, rec = seed(reviewed_ref="c" * 40, paths=["app.py", "store.py"])
    assert status == 201, outcome
    r = client.get("/review-records", params={"path": "app.py"}, headers=_h())
    assert r.status_code == 200, r.text
    assert [x["id"] for x in r.json()["review_records"]] == [rec["id"]]


def test_a_substring_of_a_stored_path_does_not_match(client):
    seed(reviewed_ref="d" * 40, paths=["app.py"])
    r = client.get("/review-records", params={"path": "app"}, headers=_h())
    assert r.status_code == 200, r.text
    assert r.json()["review_records"] == []
    r2 = client.get("/review-records", params={"path": "pp.py"}, headers=_h())
    assert r2.json()["review_records"] == []


def test_retrieval_by_doc_id(client):
    status, outcome, rec = seed(reviewed_ref="e" * 40,
                                doc_ids=["projects/library/decisions/0001"])
    assert status == 201, outcome
    r = client.get("/review-records",
                   params={"doc_id": "projects/library/decisions/0001"},
                   headers=_h())
    assert r.status_code == 200, r.text
    assert [x["id"] for x in r.json()["review_records"]] == [rec["id"]]


def test_retrieval_by_item_id(client):
    status, outcome, rec = seed(reviewed_ref="1" * 40,
                                item_ids=["issue_01M2HS42EA7V4DEXQX1R1PWS9Y"])
    assert status == 201, outcome
    r = client.get("/review-records",
                   params={"item_id": "issue_01M2HS42EA7V4DEXQX1R1PWS9Y"},
                   headers=_h())
    assert r.status_code == 200, r.text
    assert [x["id"] for x in r.json()["review_records"]] == [rec["id"]]


def test_a_malformed_paths_row_does_not_break_the_listing(client):
    status, outcome, rec = seed(reviewed_ref="2" * 40, paths=["ok.py"])
    assert status == 201, outcome
    insert_malformed_paths_row(store.DB_PATH, repo_identity=REPO_IDENTITY,
                               product="library")
    r = client.get("/review-records", params={"path": "ok.py"}, headers=_h())
    assert r.status_code == 200, r.text
    assert [x["id"] for x in r.json()["review_records"]] == [rec["id"]]


# --- product scope --------------------------------------------------------------

def test_a_path_read_out_of_scope_returns_an_empty_list_not_another_products_rows(
        client):
    seed(reviewed_ref="3" * 40, paths=["shared-name.py"], product="library")
    r = client.get("/review-records", params={"path": "shared-name.py"},
                   headers=_h(NARROW))
    assert r.status_code == 200, r.text
    assert r.json()["review_records"] == []
    assert r.json()["total"] == 0


def test_repo_identity_read_out_of_scope_is_not_found(client):
    seed(reviewed_ref="4" * 40, pr=1)
    r = client.get("/review-records",
                   params={"repo_identity": REPO_IDENTITY, "pr": 1},
                   headers=_h(NARROW))
    assert r.status_code == 404


# --- one record, and its versions ------------------------------------------------

def test_include_versions_returns_a_superseded_version_and_omits_it_by_default(
        client):
    # A runless write's identity_key is content-derived, so two runless
    # writes with different verdicts are two records, not a supersede (the
    # migration's own rule) — a shared run_id is what makes the second write
    # land on the same key.
    status1, outcome1, rec1 = seed(reviewed_ref="5" * 40, verdict="APPROVE",
                                   run_id="run-versions-1")
    assert status1 == 201, outcome1
    status2, outcome2, rec2 = seed(reviewed_ref="5" * 40, verdict="REQUEST_CHANGES",
                                   run_id="run-versions-1")
    assert (status2, outcome2) == (200, "superseded")
    assert rec2["id"] == rec1["id"]
    assert rec2["version"] == 2

    default = client.get(f"/review-records/{rec2['id']}", headers=_h())
    assert default.status_code == 200, default.text
    assert "versions" not in default.json()

    included = client.get(f"/review-records/{rec2['id']}",
                          params={"include_versions": "true"}, headers=_h())
    assert included.status_code == 200, included.text
    versions = included.json()["versions"]
    assert len(versions) == 1
    assert versions[0]["version"] == 1
    assert versions[0]["verdict"] == "APPROVE"


def test_a_record_out_of_scope_is_not_found(client):
    status, outcome, rec = seed(reviewed_ref="6" * 40)
    assert status == 201, outcome
    r = client.get(f"/review-records/{rec['id']}", headers=_h(NARROW))
    assert r.status_code == 404


def test_an_unknown_record_id_is_not_found(client):
    r = client.get("/review-records/rev_does-not-exist", headers=_h())
    assert r.status_code == 404


# --- pagination -------------------------------------------------------------------

def test_limit_is_bounded_by_max_page_and_total_truncated_are_correct(client):
    n = libapp.MAX_PAGE + 5
    for i in range(n):
        status, outcome, _ = seed(reviewed_ref=format(i, "040x"),
                                  paths=["paginated.py"])
        assert status == 201, outcome
    r = client.get("/review-records",
                   params={"path": "paginated.py", "limit": 10_000}, headers=_h())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == libapp.MAX_PAGE
    assert body["total"] == n
    assert body["truncated"] is True


# --- selector validation -----------------------------------------------------------

def test_neither_selector_is_refused(client):
    r = client.get("/review-records", headers=_h())
    assert r.status_code == 400


def test_more_than_one_selector_is_refused(client):
    r = client.get("/review-records",
                   params={"repo_identity": REPO_IDENTITY, "path": "app.py"},
                   headers=_h())
    assert r.status_code == 400


def test_verdicts_malformed_reviewed_ref_is_refused(client):
    wrong_length = client.get(
        "/review-records/verdicts",
        params={"repo_identity": REPO_IDENTITY, "reviewed_ref": "a" * 39},
        headers=_h())
    assert wrong_length.status_code == 400
    assert "reviewed_ref" in wrong_length.json()["error"]

    uppercase = client.get(
        "/review-records/verdicts",
        params={"repo_identity": REPO_IDENTITY, "reviewed_ref": "A" * 40},
        headers=_h())
    assert uppercase.status_code == 400
    assert "reviewed_ref" in uppercase.json()["error"]


def test_verdicts_non_integer_pr_is_refused(client):
    r = client.get(
        "/review-records/verdicts",
        params={"repo_identity": REPO_IDENTITY, "pr": "not-a-number"},
        headers=_h())
    assert r.status_code == 400
    assert "pr" in r.json()["error"]


def test_list_malformed_reviewed_ref_is_refused(client):
    wrong_length = client.get(
        "/review-records",
        params={"repo_identity": REPO_IDENTITY, "reviewed_ref": "b" * 39},
        headers=_h())
    assert wrong_length.status_code == 400
    assert "reviewed_ref" in wrong_length.json()["error"]

    uppercase = client.get(
        "/review-records",
        params={"repo_identity": REPO_IDENTITY, "reviewed_ref": "B" * 40},
        headers=_h())
    assert uppercase.status_code == 400
    assert "reviewed_ref" in uppercase.json()["error"]


def test_list_non_integer_pr_is_refused(client):
    r = client.get(
        "/review-records",
        params={"repo_identity": REPO_IDENTITY, "pr": "not-a-number"},
        headers=_h())
    assert r.status_code == 400
    assert "pr" in r.json()["error"]


# --- repo_identity parse agreement between the write and read routes --------------

_REPO_IDENTITY_AGREEMENT_CASES = [
    ("github:convivy/library", True),
    ("git:" + "a" * 40, True),
    ("ftp:convivy/library", False),           # wrong scheme
    ("github:library", False),                # missing owner
    ("x" * (store.MAX_REVIEW_REPO_IDENTITY + 1), False),  # over-long
    ("git:" + "A" * 40, False),                # uppercase hex for git:
    ("github:convivy/library/", False),        # trailing slash
]


def test_repo_identity_validation_agrees_between_write_and_read():
    """The write route's payload parse (`_validate_review_record_payload`)
    and the read routes' query-param parse (`_validate_review_repo_identity`)
    both delegate to `_parse_repo_identity` — this feeds a table of values
    to both entry points and fails if either one ever accepts a value the
    other refuses, or vice versa."""
    for value, should_be_valid in _REPO_IDENTITY_AGREEMENT_CASES:
        payload = {"repo_identity": value, "reviewed_ref": "a" * 40,
                   "role": "reviewer", "verdict": "APPROVE"}
        _, write_err = libapp._validate_review_record_payload(payload)
        _, _, _, read_err = libapp._validate_review_repo_identity(value)
        write_ok, read_ok = write_err is None, read_err is None
        assert write_ok == read_ok == should_be_valid, (
            f"{value!r}: write_ok={write_ok} read_ok={read_ok} "
            f"expected valid={should_be_valid}")
