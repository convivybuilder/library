"""The `credential_bindings` migration: what it builds, run for real.

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


def test_the_migration_creates_the_table_at_head(tmp_path):
    db = tmp_path / "library.db"
    result = alembic("upgrade", "head", env_extra={"LIBRARY_DB": str(db)})
    assert result.returncode == 0, result.stderr

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        columns = {row[1] for row in conn.execute(
            "PRAGMA table_info(credential_bindings)")}
    finally:
        conn.close()
    assert columns == {"id", "credential_sha256", "machine", "note",
                       "created_at"}


def test_the_credential_hash_column_is_unique(tmp_path):
    db = tmp_path / "library.db"
    assert alembic("upgrade", "head",
                   env_extra={"LIBRARY_DB": str(db)}).returncode == 0
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO credential_bindings "
        "(credential_sha256, machine, created_at) "
        "VALUES ('deadbeef', 'a', '2026-01-01')")
    conn.commit()
    try:
        conn.execute(
            "INSERT INTO credential_bindings "
            "(credential_sha256, machine, created_at) "
            "VALUES ('deadbeef', 'b', '2026-01-01')")
        conn.commit()
        raised = False
    except sqlite3.IntegrityError:
        raised = True
    finally:
        conn.close()
    assert raised, ("two rows shared a credential hash — the unique "
                    "constraint is gone")


def test_machine_is_required(tmp_path):
    db = tmp_path / "library.db"
    assert alembic("upgrade", "head",
                   env_extra={"LIBRARY_DB": str(db)}).returncode == 0
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO credential_bindings "
            "(credential_sha256, created_at) VALUES ('deadbeef', '2026-01-01')")
        conn.commit()
        raised = False
    except sqlite3.IntegrityError:
        raised = True
    finally:
        conn.close()
    assert raised, "a row with no machine value was accepted"


def test_the_downgrade_drops_the_table(tmp_path):
    db = tmp_path / "library.db"
    assert alembic("upgrade", "head",
                   env_extra={"LIBRARY_DB": str(db)}).returncode == 0
    result = alembic("downgrade", "f2f766662793",
                     env_extra={"LIBRARY_DB": str(db)})
    assert result.returncode == 0, result.stderr
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        present = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='credential_bindings'").fetchone()
    finally:
        conn.close()
    assert present is None
