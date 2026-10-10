"""The `administrators`/`bootstrap_record` migration (issue_01M2RSEKRDQS0YQDKVEAMNHGN2).

Driven through the real `alembic` CLI, as `test_people_migration.py` is and for
the same reason: the parts most likely to be wrong are that the revision chains
onto head and that its DDL runs against SQLite.

The singleton constraint is tested here, at the schema, separately from the
command. The command's guard IS that constraint, so a command test alone could
pass on a code path that happened to check first while the schema admitted a
second row.
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

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


def _record(conn, id_):
    conn.execute(
        "INSERT INTO bootstrap_record (id, person_id, credential_sha256, "
        "performed_at, performed_by) VALUES (?, 'person_a', ?, '2026-01-01', 'x')",
        (id_, "0" * 64))


def test_the_revision_chains_onto_the_people_migration(tmp_path):
    db = _migrated(tmp_path)
    # Head is deliberately not asserted here: it moves with every later slice,
    # and `test_person_product_changes_migration.py` owns "what head is now".
    result = alembic("history", db=db)
    assert "76bfd9912739 -> b59b16050865" in result.stdout, result.stdout


def test_the_migration_creates_both_tables_at_head(tmp_path):
    db = _migrated(tmp_path)
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        admin_columns = {r[1] for r in conn.execute(
            "PRAGMA table_info(administrators)")}
        record_columns = {r[1] for r in conn.execute(
            "PRAGMA table_info(bootstrap_record)")}
    finally:
        conn.close()
    assert admin_columns == {"person_id", "granted_at", "granted_by"}
    assert record_columns == {"id", "person_id", "credential_sha256",
                              "performed_at", "performed_by"}


def test_the_record_refuses_a_second_row_whatever_its_id(tmp_path):
    db = _migrated(tmp_path)
    conn = sqlite3.connect(db)
    try:
        _record(conn, 1)
        conn.commit()
        for id_ in (1, 2, 0):
            try:
                _record(conn, id_)
            except sqlite3.IntegrityError:
                continue
            raise AssertionError(f"a second row with id {id_} was admitted")
        assert conn.execute(
            "SELECT COUNT(*) FROM bootstrap_record").fetchone()[0] == 1
    finally:
        conn.close()


def test_the_record_does_not_depend_on_people_or_administrators(tmp_path):
    """The schema-level statement of the guard's independence: a record row
    survives every person-shaped table being emptied, and can be written with
    none of them populated."""
    db = _migrated(tmp_path)
    conn = sqlite3.connect(db)
    try:
        _record(conn, 1)
        conn.commit()
        for table in ("people", "administrators", "person_products"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
        assert conn.execute(
            "SELECT COUNT(*) FROM bootstrap_record").fetchone()[0] == 1
    finally:
        conn.close()


def test_an_administrator_is_recorded_once_per_person(tmp_path):
    db = _migrated(tmp_path)
    conn = sqlite3.connect(db)
    try:
        conn.execute("INSERT INTO administrators (person_id, granted_at, "
                     "granted_by) VALUES ('person_a', '2026-01-01', 'x')")
        conn.commit()
        try:
            conn.execute("INSERT INTO administrators (person_id, granted_at, "
                         "granted_by) VALUES ('person_a', '2026-01-02', 'y')")
        except sqlite3.IntegrityError:
            return
        raise AssertionError("a person was made an administrator twice")
    finally:
        conn.close()


def test_the_downgrade_drops_both_tables_and_leaves_people(tmp_path):
    db = _migrated(tmp_path)
    result = alembic("downgrade", "76bfd9912739", db=db)
    assert result.returncode == 0, result.stderr
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        present = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "('administrators', 'bootstrap_record', 'people', 'person_products')")}
    finally:
        conn.close()
    assert present == {"people", "person_products"}
