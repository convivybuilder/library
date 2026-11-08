"""The `project_registry_edits` migration: what it builds, run for real
(CQ·249, §6.8 of knowledge#1062).

**Driven through the real `alembic` CLI**, the same reasoning
`test_project_registry_migration.py` gives for its own table: the parts
most likely to be wrong are that the revision chains onto head and that
its DDL actually runs against SQLite, and an in-process `alembic.command`
call would skip both. Schema-only, so there is no data to assert here
beyond the table and its columns.
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
            "PRAGMA table_info(project_registry_edits)")}
    finally:
        conn.close()
    assert columns == {"id", "slug", "actor", "at", "changes"}


def test_the_downgrade_drops_the_table(tmp_path):
    db = tmp_path / "library.db"
    assert alembic("upgrade", "head",
                   env_extra={"LIBRARY_DB": str(db)}).returncode == 0
    result = alembic("downgrade", "a63c8a7f1b85",
                     env_extra={"LIBRARY_DB": str(db)})
    assert result.returncode == 0, result.stderr
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        present = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='project_registry_edits'")}
    finally:
        conn.close()
    assert present == set()
