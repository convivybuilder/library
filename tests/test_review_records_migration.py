"""The `review_records` migration: what it builds, run for real.

**Driven through the real `alembic` CLI**, the same reasoning
`test_machine_certificates_migration.py` gives: the parts most likely to be
wrong are that the revision chains onto head and that its DDL actually runs
against SQLite, and an in-process `alembic.command` call would skip both.
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def alembic(*args, env_extra=None):
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(REPO), capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(REPO), **(env_extra or {})})


def upgraded(tmp_path):
    db = tmp_path / "library.db"
    result = alembic("upgrade", "head", env_extra={"LIBRARY_DB": str(db)})
    assert result.returncode == 0, result.stderr
    return db


BASE_ROW = {
    "id": "rvw_a", "repo_identity": "github:convivy/library", "product": "library",
    "pr": 1, "reviewed_ref": "deadbeef", "role": "reviewer", "run_id": "run-1",
    "identity_key": "run-1", "verdict": "APPROVE", "findings": "[]", "prose": "",
    "paths": "[]", "doc_ids": "[]", "item_ids": "[]", "auth": "run-verified",
    "auth_reason": None, "content_sha": "a" * 64, "version": 1,
    "source_comment_id": None, "signal_id": None,
    "created_at": "2026-01-01 00:00:00", "updated_at": "2026-01-01 00:00:00",
}


def insert(conn, **over):
    row = {**BASE_ROW, **over}
    cols = list(row.keys())
    conn.execute(
        f"INSERT INTO review_records ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' for _ in cols)})",
        [row[c] for c in cols])
    conn.commit()


def test_the_migration_creates_review_records_at_head(tmp_path):
    """Run at `head`, not at `468a1789d29e` alone — `e4894432e1aa` adds
    `auth_reason` on top of this table, and this is the shape a caller of
    this repo's `head` actually sees."""
    db = upgraded(tmp_path)
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        columns = {row[1] for row in conn.execute(
            "PRAGMA table_info(review_records)")}
    finally:
        conn.close()
    assert columns == {
        "id", "repo_identity", "product", "pr", "reviewed_ref", "role",
        "run_id", "identity_key", "verdict", "findings", "prose", "paths",
        "doc_ids", "item_ids", "auth", "auth_reason", "content_sha", "version",
        "source_comment_id", "signal_id", "created_at", "updated_at"}


def test_the_migration_creates_review_record_versions_at_head(tmp_path):
    db = upgraded(tmp_path)
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        columns = {row[1] for row in conn.execute(
            "PRAGMA table_info(review_record_versions)")}
    finally:
        conn.close()
    assert columns == {
        "record_id", "version", "product", "pr", "verdict", "findings",
        "prose", "paths", "doc_ids", "item_ids", "auth", "auth_reason",
        "content_sha", "run_id", "source_comment_id", "signal_id", "written_at"}


def test_the_unique_key_refuses_an_exact_duplicate(tmp_path):
    db = upgraded(tmp_path)
    conn = sqlite3.connect(db)
    try:
        insert(conn)
        try:
            insert(conn, id="rvw_b")
            raised = False
        except sqlite3.IntegrityError:
            raised = True
        assert raised, ("a second row with the same (repo_identity, "
                        "identity_key, role, reviewed_ref) was accepted")
    finally:
        conn.close()


def test_the_unique_key_accepts_a_row_differing_in_repo_identity(tmp_path):
    db = upgraded(tmp_path)
    conn = sqlite3.connect(db)
    try:
        insert(conn)
        insert(conn, id="rvw_b", repo_identity="github:convivy/selector")
    finally:
        conn.close()


def test_the_unique_key_accepts_a_row_differing_in_identity_key(tmp_path):
    db = upgraded(tmp_path)
    conn = sqlite3.connect(db)
    try:
        insert(conn)
        insert(conn, id="rvw_b", identity_key="run-2", run_id="run-2")
    finally:
        conn.close()


def test_the_unique_key_accepts_a_row_differing_in_role(tmp_path):
    db = upgraded(tmp_path)
    conn = sqlite3.connect(db)
    try:
        insert(conn)
        insert(conn, id="rvw_b", role="security-reviewer")
    finally:
        conn.close()


def test_the_unique_key_accepts_a_row_differing_in_reviewed_ref(tmp_path):
    db = upgraded(tmp_path)
    conn = sqlite3.connect(db)
    try:
        insert(conn)
        insert(conn, id="rvw_b", reviewed_ref="cafef00d")
    finally:
        conn.close()


def test_a_runless_review_key_is_not_null(tmp_path):
    """`identity_key` is NOT NULL — a runless review keys on
    `content:<sha256>` rather than relying on SQLite's null-distinctness in
    the unique index (comment 2748 on issue_01M2HS394KTFMRATFK1N3G4ZNB)."""
    db = upgraded(tmp_path)
    conn = sqlite3.connect(db)
    try:
        try:
            insert(conn, run_id=None, identity_key=None)
            raised = False
        except sqlite3.IntegrityError:
            raised = True
        assert raised, "a row with a NULL identity_key was accepted"
    finally:
        conn.close()


def test_auth_rejects_a_fourth_value(tmp_path):
    db = upgraded(tmp_path)
    conn = sqlite3.connect(db)
    try:
        try:
            insert(conn, auth="revoked")
            raised = False
        except sqlite3.IntegrityError:
            raised = True
        assert raised, "an auth value outside the epic's three was accepted"
    finally:
        conn.close()


def test_run_verified_without_a_run_id_is_refused(tmp_path):
    db = upgraded(tmp_path)
    conn = sqlite3.connect(db)
    try:
        try:
            insert(conn, auth="run-verified", run_id=None,
                   identity_key="content:" + "a" * 64)
            raised = False
        except sqlite3.IntegrityError:
            raised = True
        assert raised, "a run-verified row with no run_id was accepted"
    finally:
        conn.close()


def test_run_verified_with_a_run_id_is_accepted(tmp_path):
    db = upgraded(tmp_path)
    conn = sqlite3.connect(db)
    try:
        insert(conn, auth="run-verified", run_id="run-1", identity_key="run-1")
    finally:
        conn.close()


def test_unverified_with_no_run_id_is_accepted(tmp_path):
    db = upgraded(tmp_path)
    conn = sqlite3.connect(db)
    try:
        insert(conn, auth="unverified", run_id=None,
               identity_key="content:" + "b" * 64)
    finally:
        conn.close()


def test_both_indexes_are_there(tmp_path):
    db = upgraded(tmp_path)
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        indexes = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='review_records'")}
    finally:
        conn.close()
    assert "ix_review_records_repo_pr" in indexes
    assert "ix_review_records_repo_ref" in indexes


def test_review_record_versions_refuses_a_duplicate_record_and_version(tmp_path):
    db = upgraded(tmp_path)
    conn = sqlite3.connect(db)
    version_row = {
        "record_id": "rvw_a", "version": 1, "product": "library",
        "verdict": "APPROVE", "findings": "[]", "prose": "", "paths": "[]",
        "doc_ids": "[]", "item_ids": "[]", "auth": "run-verified",
        "content_sha": "a" * 64, "run_id": "run-1",
        "written_at": "2026-01-01 00:00:00",
    }
    try:
        insert(conn)
        cols = list(version_row.keys())
        conn.execute(
            f"INSERT INTO review_record_versions ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' for _ in cols)})",
            [version_row[c] for c in cols])
        conn.commit()
        try:
            conn.execute(
                f"INSERT INTO review_record_versions ({', '.join(cols)}) "
                f"VALUES ({', '.join('?' for _ in cols)})",
                [version_row[c] for c in cols])
            conn.commit()
            raised = False
        except sqlite3.IntegrityError:
            raised = True
        assert raised, ("a second review_record_versions row with the same "
                        "(record_id, version) was accepted")
    finally:
        conn.close()


RECORD_KEY_COLUMNS = {"repo_identity", "identity_key", "role", "reviewed_ref"}
RECORD_ONLY_COLUMNS = {"id", "version", "created_at", "updated_at"}


def test_a_version_carries_every_record_column_but_the_key_and_record_only_ones(
        tmp_path):
    """A version is its own write (comment 2748's supersede contract), not a
    fact about the record's identity — so it carries everything about that
    write except the four columns that make up the record's uniqueness key
    (identical across every version of one record by construction) and the
    four columns that belong to the record row itself, not to a version."""
    db = upgraded(tmp_path)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        actual_columns = {row[1] for row in conn.execute(
            "PRAGMA table_info(review_record_versions)")}
        assert actual_columns == {
            "record_id", "version", "product", "pr", "verdict", "findings",
            "prose", "paths", "doc_ids", "item_ids", "auth", "auth_reason",
            "content_sha", "run_id", "source_comment_id", "signal_id",
            "written_at"}

        insert(conn)
        carried_cols = [c for c in BASE_ROW
                        if c not in RECORD_KEY_COLUMNS | RECORD_ONLY_COLUMNS]
        version_row = {"record_id": "rvw_a", "version": 1,
                       **{c: BASE_ROW[c] for c in carried_cols},
                       "written_at": "2026-01-01 00:00:00"}
        cols = list(version_row.keys())
        conn.execute(
            f"INSERT INTO review_record_versions ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' for _ in cols)})",
            [version_row[c] for c in cols])
        conn.commit()

        fetched = conn.execute(
            "SELECT * FROM review_record_versions "
            "WHERE record_id = ? AND version = ?", ("rvw_a", 1)).fetchone()
        for col in carried_cols:
            assert fetched[col] == BASE_ROW[col], (
                f"{col} did not round-trip through review_record_versions")
    finally:
        conn.close()


def test_the_downgrade_drops_both_tables(tmp_path):
    db = upgraded(tmp_path)
    result = alembic("downgrade", "b1a7f3e6c9d2", env_extra={"LIBRARY_DB": str(db)})
    assert result.returncode == 0, result.stderr
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        present = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('review_records', 'review_record_versions')"
        ).fetchall()
    finally:
        conn.close()
    assert present == []
