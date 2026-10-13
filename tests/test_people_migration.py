"""The `people`/`person_products` migration (issue_01M2RSE08WRRB1RJY1ZCSMD6EG):
what it builds, run for real, plus the persistence properties the item's
done-when names — a stored grant surviving a Library restart and a subsequent
`alembic upgrade head`.

**Driven through the real `alembic` CLI**, matching
`test_roster_and_profiles_migration.py`'s own reasoning: the parts most
likely to be wrong are that the revision chains onto head and that its DDL
actually runs against SQLite, and an in-process `alembic.command` call would
skip both.
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


def test_the_migration_creates_both_tables_at_head(tmp_path):
    db = tmp_path / "library.db"
    result = alembic("upgrade", "head", env_extra={"LIBRARY_DB": str(db)})
    assert result.returncode == 0, result.stderr

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        people_columns = {row[1] for row in conn.execute(
            "PRAGMA table_info(people)")}
        grant_columns = {row[1] for row in conn.execute(
            "PRAGMA table_info(person_products)")}
    finally:
        conn.close()
    # `sign_in_email` arrives at head with ad6a0a903c09 (issue_01M4QQZ7VTANJ5ZGF630DCB0Y7).
    assert people_columns == {"id", "display_name", "created_at",
                              "created_by", "updated_at", "updated_by",
                              "sign_in_email"}
    assert grant_columns == {"person_id", "product", "granted_at",
                             "granted_by"}


def test_a_person_may_not_be_granted_the_same_product_twice(tmp_path):
    db = tmp_path / "library.db"
    assert alembic("upgrade", "head",
                   env_extra={"LIBRARY_DB": str(db)}).returncode == 0
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO person_products (person_id, product, granted_at, "
        "granted_by) VALUES ('person_a', 'vivy', '2026-01-01', 'x')")
    conn.commit()
    raised = False
    try:
        conn.execute(
            "INSERT INTO person_products (person_id, product, granted_at, "
            "granted_by) VALUES ('person_a', 'vivy', '2026-01-01', 'y')")
        conn.commit()
    except sqlite3.IntegrityError:
        raised = True
    finally:
        conn.close()
    assert raised, ("two rows shared a (person_id, product) pair — the "
                    "primary key is gone")


def test_the_same_product_may_be_granted_to_a_different_person(tmp_path):
    db = tmp_path / "library.db"
    assert alembic("upgrade", "head",
                   env_extra={"LIBRARY_DB": str(db)}).returncode == 0
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO person_products (person_id, product, granted_at, "
        "granted_by) VALUES ('person_a', 'vivy', '2026-01-01', 'x')")
    conn.execute(
        "INSERT INTO person_products (person_id, product, granted_at, "
        "granted_by) VALUES ('person_b', 'vivy', '2026-01-01', 'x')")
    conn.commit()
    count = conn.execute(
        "SELECT COUNT(*) FROM person_products").fetchone()[0]
    conn.close()
    assert count == 2


def test_the_downgrade_drops_both_tables(tmp_path):
    db = tmp_path / "library.db"
    assert alembic("upgrade", "head",
                   env_extra={"LIBRARY_DB": str(db)}).returncode == 0
    result = alembic("downgrade", "e4894432e1aa",
                     env_extra={"LIBRARY_DB": str(db)})
    assert result.returncode == 0, result.stderr
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        present = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('people', 'person_products')")}
    finally:
        conn.close()
    assert present == set()


# --- the item's done-when: survives a restart, and a subsequent upgrade ----


def test_a_grant_survives_a_library_restart(tmp_path):
    """"Restart" for a service that keeps no in-memory state and reopens a
    fresh SQLite connection per request means exactly this: a row written
    through one connection is read back correctly through an entirely new
    one opened later, against the same file. `store.connect()`/
    `connect_rw()` never hold a connection open across requests, so this is
    the whole of what a Library restart could threaten.
    """
    db = tmp_path / "library.db"
    assert alembic("upgrade", "head",
                   env_extra={"LIBRARY_DB": str(db)}).returncode == 0

    write_conn = sqlite3.connect(db)
    write_conn.execute(
        "INSERT INTO people (id, display_name, created_at, created_by, "
        "updated_at, updated_by) VALUES ('person_a', 'Ada', '2026-01-01', "
        "'x', '2026-01-01', 'x')")
    write_conn.execute(
        "INSERT INTO person_products (person_id, product, granted_at, "
        "granted_by) VALUES ('person_a', 'vivy', '2026-01-01', 'x')")
    write_conn.commit()
    write_conn.close()

    reopened = sqlite3.connect(db)
    try:
        row = reopened.execute(
            "SELECT display_name FROM people WHERE id = 'person_a'").fetchone()
        grants = reopened.execute(
            "SELECT product FROM person_products "
            "WHERE person_id = 'person_a'").fetchall()
    finally:
        reopened.close()
    assert row == ("Ada",)
    assert grants == [("vivy",)]


def test_a_grant_survives_a_subsequent_alembic_upgrade(tmp_path):
    """The other half of the done-when: running `alembic upgrade head` again
    against a database that already holds data must not disturb it — the
    property a later revision landing on top of this one depends on."""
    db = tmp_path / "library.db"
    assert alembic("upgrade", "head",
                   env_extra={"LIBRARY_DB": str(db)}).returncode == 0

    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO person_products (person_id, product, granted_at, "
        "granted_by) VALUES ('person_a', 'vivy', '2026-01-01', 'x')")
    conn.commit()
    conn.close()

    result = alembic("upgrade", "head", env_extra={"LIBRARY_DB": str(db)})
    assert result.returncode == 0, result.stderr

    conn = sqlite3.connect(db)
    try:
        grants = conn.execute(
            "SELECT product FROM person_products "
            "WHERE person_id = 'person_a'").fetchall()
    finally:
        conn.close()
    assert grants == [("vivy",)]
