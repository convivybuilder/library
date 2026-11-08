"""The `project_registry` migration: what it builds, run for real (CQ·241).

**Driven through the real `alembic` CLI**, the same reasoning
`test_roster_and_profiles_migration.py` gives: the parts most likely to be
wrong are that the revision chains onto head and that its DDL actually
runs against SQLite, and an in-process `alembic.command` call would skip
both. Schema-only, so there is no data to assert here beyond the table
and its constraint.
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
            "PRAGMA table_info(project_registry)")}
    finally:
        conn.close()
    assert columns == {"id", "slug", "entry", "updated_at", "updated_by"}


def test_the_slug_column_is_unique(tmp_path):
    db = tmp_path / "library.db"
    assert alembic("upgrade", "head",
                   env_extra={"LIBRARY_DB": str(db)}).returncode == 0
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO project_registry (slug, entry, updated_at, updated_by) "
        "VALUES ('oldmill', '{}', '2026-01-01', 'x')")
    conn.commit()
    raised = False
    try:
        conn.execute(
            "INSERT INTO project_registry "
            "(slug, entry, updated_at, updated_by) "
            "VALUES ('oldmill', '{}', '2026-01-01', 'y')")
        conn.commit()
    except sqlite3.IntegrityError:
        raised = True
    finally:
        conn.close()
    assert raised, "two rows shared a slug — the unique constraint is gone"


def test_the_downgrade_drops_the_table(tmp_path):
    db = tmp_path / "library.db"
    assert alembic("upgrade", "head",
                   env_extra={"LIBRARY_DB": str(db)}).returncode == 0
    result = alembic("downgrade", "5cb0d7dc016f",
                     env_extra={"LIBRARY_DB": str(db)})
    assert result.returncode == 0, result.stderr
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        present = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='project_registry'")}
    finally:
        conn.close()
    assert present == set()
