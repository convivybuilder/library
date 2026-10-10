"""`store.py`'s review_records column list, row helper, and canonical-content
hash — the schema half of issue_01M2HS394KTFMRATFK1N3G4ZNB. No write route
exists yet, so rows are inserted directly against a real migrated database,
the same way `test_ledger_store.py` builds its fixture.
"""
from __future__ import annotations

import datetime
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import store

REPO = Path(__file__).resolve().parent.parent


def make_db(tmp_path):
    db = tmp_path / "library.db"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
             "LIBRARY_DB": str(db)})
    assert result.returncode == 0, result.stderr
    return db


def test_a_row_round_trips_through_the_store_helper(tmp_path):
    db = make_db(tmp_path)
    findings = [{"severity": "high", "location": "store.py:12"},
               {"severity": "low", "location": "app.py:99"}]
    row = {
        "id": "rvw_a", "repo_identity": "github:convivy/library",
        "product": "library", "pr": 42, "reviewed_ref": "deadbeef",
        "role": "reviewer", "run_id": "run-1", "identity_key": "run-1",
        "verdict": "REQUEST_CHANGES",
        "findings": json.dumps(findings),
        "prose": "two findings, one blocking",
        "paths": json.dumps(["store.py", "app.py"]),
        "doc_ids": json.dumps(["doc_1", "doc_2"]),
        "item_ids": json.dumps(["issue_1"]),
        "auth": "run-verified", "content_sha": "b" * 64, "version": 1,
        "source_comment_id": "3061234567", "signal_id": None,
        "created_at": "2026-09-15 12:00:00", "updated_at": "2026-09-15 12:00:00",
    }
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        cols = list(row.keys())
        conn.execute(
            f"INSERT INTO review_records ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' for _ in cols)})",
            [row[c] for c in cols])
        conn.commit()
        fetched = conn.execute(
            f"SELECT {', '.join(store.REVIEW_RECORD_COLUMNS)} "
            f"FROM review_records WHERE id = ?", ("rvw_a",)).fetchone()
    finally:
        conn.close()

    out = store.review_record_dict(fetched)
    assert out["findings"] == findings
    assert out["prose"] == "two findings, one blocking"
    assert out["paths"] == ["store.py", "app.py"]
    assert out["doc_ids"] == ["doc_1", "doc_2"]
    assert out["item_ids"] == ["issue_1"]
    assert out["verdict"] == "REQUEST_CHANGES"
    assert out["pr"] == 42
    assert out["source_comment_id"] == "3061234567"


BASE_CONTENT = dict(
    verdict="APPROVE",
    findings=[{"severity": "low", "location": "a.py:1"}],
    prose="looks fine",
    paths=["a.py", "b.py"],
    doc_ids=["doc_1"],
    item_ids=["issue_1"],
    auth="run-verified",
)


def test_review_content_sha_is_stable_across_key_order():
    a = store.review_content_sha(**BASE_CONTENT)
    reordered = {
        "auth": BASE_CONTENT["auth"],
        "item_ids": BASE_CONTENT["item_ids"],
        "doc_ids": BASE_CONTENT["doc_ids"],
        "paths": BASE_CONTENT["paths"],
        "prose": BASE_CONTENT["prose"],
        "findings": [dict(reversed(list(BASE_CONTENT["findings"][0].items())))],
        "verdict": BASE_CONTENT["verdict"],
    }
    b = store.review_content_sha(
        verdict=reordered["verdict"], findings=reordered["findings"],
        prose=reordered["prose"], paths=reordered["paths"],
        doc_ids=reordered["doc_ids"], item_ids=reordered["item_ids"],
        auth=reordered["auth"])
    assert a == b


def test_review_content_sha_changes_with_verdict():
    a = store.review_content_sha(**BASE_CONTENT)
    b = store.review_content_sha(**{**BASE_CONTENT, "verdict": "REQUEST_CHANGES"})
    assert a != b


def test_review_content_sha_changes_with_findings():
    a = store.review_content_sha(**BASE_CONTENT)
    b = store.review_content_sha(**{**BASE_CONTENT, "findings": []})
    assert a != b


def test_review_content_sha_changes_with_prose():
    a = store.review_content_sha(**BASE_CONTENT)
    b = store.review_content_sha(**{**BASE_CONTENT, "prose": "different"})
    assert a != b


def test_review_content_sha_changes_with_paths():
    a = store.review_content_sha(**BASE_CONTENT)
    b = store.review_content_sha(**{**BASE_CONTENT, "paths": ["c.py"]})
    assert a != b


def test_review_content_sha_changes_with_doc_ids():
    a = store.review_content_sha(**BASE_CONTENT)
    b = store.review_content_sha(**{**BASE_CONTENT, "doc_ids": ["doc_2"]})
    assert a != b


def test_review_content_sha_changes_with_item_ids():
    a = store.review_content_sha(**BASE_CONTENT)
    b = store.review_content_sha(**{**BASE_CONTENT, "item_ids": ["issue_2"]})
    assert a != b


def test_review_content_sha_changes_with_auth():
    a = store.review_content_sha(**BASE_CONTENT)
    b = store.review_content_sha(**{**BASE_CONTENT, "auth": "unverified"})
    assert a != b


def _write_review_row(**over):
    row = {
        "repo_identity": "github:convivy/library", "product": "library",
        "pr": 1, "reviewed_ref": "a" * 40, "role": "reviewer",
        "run_id": "run-1", "verdict": "APPROVE", "findings": [],
        "prose": "fine", "paths": [], "doc_ids": [], "item_ids": [],
        "auth": "unverified", "auth_reason": None, "source_comment_id": None,
    }
    row.update(over)
    return row


def test_write_review_record_stamps_datetime_now_when_at_is_not_passed(
        tmp_path, monkeypatch):
    """`POST /review-records` never passes `at` — this is what proves the
    default stays byte-for-byte what it was before that parameter existed."""
    db = make_db(tmp_path)
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)

    before = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    _status, _outcome, out = store.write_review_record(_write_review_row())
    after = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)

    created_at = datetime.datetime.strptime(out["created_at"], "%Y-%m-%dT%H:%M:%SZ")
    assert before - datetime.timedelta(seconds=2) <= created_at
    assert created_at <= after + datetime.timedelta(seconds=2)
    assert out["created_at"] == out["updated_at"]


def test_write_review_record_honors_at_when_given(tmp_path, monkeypatch):
    """The backfill's whole reason for this parameter: a record dated from
    its own review comment rather than from when the backfill ran."""
    db = make_db(tmp_path)
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)

    _status, _outcome, out = store.write_review_record(
        _write_review_row(), at="2026-06-20 10:00:00")

    assert out["created_at"] == "2026-06-20T10:00:00Z"
    assert out["updated_at"] == "2026-06-20T10:00:00Z"


def test_write_review_record_still_supersedes_run_verified_when_called_like_the_route(
        tmp_path, monkeypatch):
    """`POST /review-records` calls this with neither `skip_if_verified` nor
    `only_if_after` — round 6's two backfill-only guards must default to
    today's plain three-way decision, not to the backfill's guarded one, so
    a live re-post through the route still supersedes its own prior
    run-verified record exactly as it does today."""
    db = make_db(tmp_path)
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)

    _status, outcome, first = store.write_review_record(
        _write_review_row(auth="run-verified"))
    assert outcome == store.REVIEW_OUTCOME_CREATED
    assert first["auth"] == "run-verified"

    _status, outcome, second = store.write_review_record(
        _write_review_row(auth="run-verified", verdict="REQUEST_CHANGES"))
    assert outcome == store.REVIEW_OUTCOME_SUPERSEDED
    assert second["id"] == first["id"]
    assert second["version"] == 2
    assert second["verdict"] == "REQUEST_CHANGES"


def test_write_review_record_skip_if_verified_refuses_to_supersede(
        tmp_path, monkeypatch):
    db = make_db(tmp_path)
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)

    _status, _outcome, verified = store.write_review_record(
        _write_review_row(auth="run-verified"))

    _status, outcome, out = store.write_review_record(
        _write_review_row(verdict="REQUEST_CHANGES"), skip_if_verified=True)

    assert outcome == store.REVIEW_OUTCOME_LEFT_VERIFIED
    assert out["id"] == verified["id"]
    assert out["version"] == 1
    assert out["verdict"] == "APPROVE"
    assert out["auth"] == "run-verified"


def test_write_review_record_only_if_after_refuses_an_at_or_before_position(
        tmp_path, monkeypatch):
    db = make_db(tmp_path)
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)

    _status, _outcome, first = store.write_review_record(
        _write_review_row(), at="2026-06-20 10:00:00")
    store.set_review_record_signal(first["id"], first["version"], "sig_first")

    # Equal position: refused.
    _status, outcome, out = store.write_review_record(
        _write_review_row(verdict="REQUEST_CHANGES"), at="2026-06-20 10:00:00",
        only_if_after=("2026-06-20 10:00:00", "sig_first"))
    assert outcome == store.REVIEW_OUTCOME_ALREADY_CURRENT
    assert out["version"] == 1
    assert out["verdict"] == "APPROVE"

    # Strictly earlier position: refused too.
    _status, outcome, out = store.write_review_record(
        _write_review_row(verdict="REQUEST_CHANGES"), at="2026-06-20 09:00:00",
        only_if_after=("2026-06-20 09:00:00", "sig_earlier"))
    assert outcome == store.REVIEW_OUTCOME_ALREADY_CURRENT
    assert out["version"] == 1
    assert out["verdict"] == "APPROVE"


def test_write_review_record_only_if_after_allows_a_later_position(
        tmp_path, monkeypatch):
    db = make_db(tmp_path)
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)

    _status, _outcome, first = store.write_review_record(
        _write_review_row(), at="2026-06-20 10:00:00")
    store.set_review_record_signal(first["id"], first["version"], "sig_first")

    _status, outcome, out = store.write_review_record(
        _write_review_row(verdict="REQUEST_CHANGES"), at="2026-06-20 10:05:00",
        only_if_after=("2026-06-20 10:05:00", "sig_later"))

    assert outcome == store.REVIEW_OUTCOME_SUPERSEDED
    assert out["id"] == first["id"]
    assert out["version"] == 2
    assert out["verdict"] == "REQUEST_CHANGES"
