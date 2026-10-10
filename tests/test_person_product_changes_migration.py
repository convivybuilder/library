"""The `person_product_changes` migration (issue_01M2RSF4MMRT7PBHJJPQ2BKEC7).

Driven through the real `alembic` CLI, as `test_bootstrap_migration.py` is and
for the same reason: the parts most likely to be wrong are that the revision
chains onto head and that its DDL runs against SQLite. The route's own tests
exercise the table through the service; this file pins the schema itself.
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


def test_the_revision_chains_onto_the_bootstrap_migration(tmp_path):
    db = _migrated(tmp_path)
    result = alembic("heads", db=db)
    assert result.returncode == 0, result.stderr
    # ONE head, not THIS head: pinning the revision as head made every later
    # migration fail this test, which is about how this revision chains and
    # not about which migration is newest. The `history` line below is the
    # chain assertion, unchanged.
    assert result.stdout.count("(head)") == 1, result.stdout
    result = alembic("history", db=db)
    assert "b59b16050865 -> 0652cc6b2cee" in result.stdout, result.stdout


def test_the_migration_creates_the_table_at_head(tmp_path):
    db = _migrated(tmp_path)
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        columns = {r[1] for r in conn.execute(
            "PRAGMA table_info(person_product_changes)")}
    finally:
        conn.close()
    assert columns == {"id", "person_id", "product", "action", "changed_at",
                       "changed_by"}


def test_the_table_refuses_an_action_other_than_grant_or_revoke(tmp_path):
    db = _migrated(tmp_path)
    conn = sqlite3.connect(db)
    try:
        for action in ("grant", "revoke"):
            conn.execute(
                "INSERT INTO person_product_changes (person_id, product, "
                "action, changed_at, changed_by) VALUES "
                "('person_a', 'vivy', ?, '2026-01-01', 'x')", (action,))
        for action in ("GRANT", "delete", ""):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO person_product_changes (person_id, product, "
                    "action, changed_at, changed_by) VALUES "
                    "('person_a', 'vivy', ?, '2026-01-01', 'x')", (action,))
    finally:
        conn.close()


def test_the_same_change_may_be_recorded_twice(tmp_path):
    """History is a list of events, so a grant, a revoke and a second grant of
    the same product for the same person are three rows and never a conflict."""
    db = _migrated(tmp_path)
    conn = sqlite3.connect(db)
    try:
        for action in ("grant", "revoke", "grant"):
            conn.execute(
                "INSERT INTO person_product_changes (person_id, product, "
                "action, changed_at, changed_by) VALUES "
                "('person_a', 'vivy', ?, '2026-01-01', 'x')", (action,))
        assert conn.execute(
            "SELECT COUNT(*) FROM person_product_changes").fetchone()[0] == 3
    finally:
        conn.close()


def test_downgrade_removes_only_this_table(tmp_path):
    db = _migrated(tmp_path)
    result = alembic("downgrade", "b59b16050865", db=db)
    assert result.returncode == 0, result.stderr
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        conn.close()
    assert "person_product_changes" not in tables
    assert {"people", "person_products", "administrators",
            "bootstrap_record"} <= tables
