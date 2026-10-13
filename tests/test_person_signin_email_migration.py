"""The `ad6a0a903c09` migration (issue_01M4QQZ7VTANJ5ZGF630DCB0Y7): a
person's sign-in email, nullable and unique without regard to case, plus the
`person_email_changes` history table.

Driven through the real `alembic` CLI, as `test_person_product_changes_migration.py`
is and for the same reason: the parts most likely to be wrong are that the
revision chains onto head and that its DDL runs against SQLite.
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def alembic(*args, db):
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(REPO), capture_output=True, text=True, check=False,
        env={"PATH": "/usr/bin:/bin", "HOME": str(REPO),
             "LIBRARY_DB": str(db)})


def _migrated(tmp_path):
    db = tmp_path / "library.db"
    result = alembic("upgrade", "head", db=db)
    assert result.returncode == 0, result.stderr
    return db


def _people_columns(db):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(people)")}
    finally:
        conn.close()


def test_the_revision_chains_onto_the_removals_migration(tmp_path):
    db = _migrated(tmp_path)
    result = alembic("heads", db=db)
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("(head)") == 1, result.stdout
    result = alembic("history", db=db)
    assert "fc50f714c340 -> ad6a0a903c09" in result.stdout, result.stdout


def test_the_migration_adds_a_nullable_column_and_a_unique_index(tmp_path):
    db = _migrated(tmp_path)
    assert "sign_in_email" in _people_columns(db)
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO people (id, display_name, created_at, created_by, "
            "updated_at, updated_by, sign_in_email) VALUES "
            "('person_a', 'A', '2026-01-01', 'x', '2026-01-01', 'x', NULL)")
        conn.execute(
            "INSERT INTO people (id, display_name, created_at, created_by, "
            "updated_at, updated_by, sign_in_email) VALUES "
            "('person_b', 'B', '2026-01-01', 'x', '2026-01-01', 'x', NULL)")
        conn.commit()
    finally:
        conn.close()


def test_two_people_may_not_hold_the_same_email(tmp_path):
    db = _migrated(tmp_path)
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO people (id, display_name, created_at, created_by, "
            "updated_at, updated_by, sign_in_email) VALUES "
            "('person_a', 'A', '2026-01-01', 'x', '2026-01-01', 'x', "
            "'ada@example.com')")
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO people (id, display_name, created_at, "
                "created_by, updated_at, updated_by, sign_in_email) VALUES "
                "('person_b', 'B', '2026-01-01', 'x', '2026-01-01', 'x', "
                "'ada@example.com')")
    finally:
        conn.close()


def test_the_migration_creates_the_changes_table(tmp_path):
    db = _migrated(tmp_path)
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        columns = {r[1] for r in conn.execute(
            "PRAGMA table_info(person_email_changes)")}
    finally:
        conn.close()
    assert columns == {"id", "person_id", "email", "changed_at", "changed_by"}


def test_downgrade_removes_only_this_slice(tmp_path):
    db = _migrated(tmp_path)
    result = alembic("downgrade", "fc50f714c340", db=db)
    assert result.returncode == 0, result.stderr
    assert "sign_in_email" not in _people_columns(db)
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        conn.close()
    assert "person_email_changes" not in tables
    assert {"people", "person_products", "person_product_removals",
            "administrators"} <= tables


def test_upgrade_downgrade_upgrade_on_the_full_chain(tmp_path):
    """The chain this stacks on, both ways, against a scratch database
    rather than a fixture — the item's own instruction."""
    db = _migrated(tmp_path)
    result = alembic("downgrade", "fc50f714c340", db=db)
    assert result.returncode == 0, result.stderr
    result = alembic("upgrade", "head", db=db)
    assert result.returncode == 0, result.stderr
    assert "sign_in_email" in _people_columns(db)
