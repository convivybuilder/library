"""The `roster_agents`/`profiles` migration: what it builds, run for real.

**Driven through the real `alembic` CLI**, the same reasoning
`test_credential_bindings_migration.py` and
`test_machine_certificates_migration.py` give: the parts most likely to be
wrong are that the revision chains onto head and that its DDL actually runs
against SQLite, and an in-process `alembic.command` call would skip both.
This migration is schema-only (see its own docstring for why the data load
is `load_crew_registry.py` instead), so there is no data to assert here —
only that the two tables and their constraints exist at head.
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
        roster_columns = {row[1] for row in conn.execute(
            "PRAGMA table_info(roster_agents)")}
        profile_columns = {row[1] for row in conn.execute(
            "PRAGMA table_info(profiles)")}
    finally:
        conn.close()
    assert roster_columns == {"id", "name", "created_at", "created_by",
                              "updated_at", "updated_by"}
    assert profile_columns == {"id", "project", "variant", "include",
                               "claude_md_extras", "created_at",
                               "created_by", "updated_at", "updated_by"}


def test_the_roster_name_column_is_unique(tmp_path):
    db = tmp_path / "library.db"
    assert alembic("upgrade", "head",
                   env_extra={"LIBRARY_DB": str(db)}).returncode == 0
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO roster_agents "
        "(name, created_at, created_by, updated_at, updated_by) "
        "VALUES ('coder', '2026-01-01', 'x', '2026-01-01', 'x')")
    conn.commit()
    raised = False
    try:
        conn.execute(
            "INSERT INTO roster_agents "
            "(name, created_at, created_by, updated_at, updated_by) "
            "VALUES ('coder', '2026-01-01', 'y', '2026-01-01', 'y')")
        conn.commit()
    except sqlite3.IntegrityError:
        raised = True
    finally:
        conn.close()
    assert raised, "two rows shared a roster name — the unique constraint is gone"


def test_the_profile_project_variant_pair_is_unique(tmp_path):
    db = tmp_path / "library.db"
    assert alembic("upgrade", "head",
                   env_extra={"LIBRARY_DB": str(db)}).returncode == 0
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO profiles "
        "(project, variant, include, claude_md_extras, created_at, "
        " created_by, updated_at, updated_by) "
        "VALUES ('hollis', 'daytime', '[]', '[]', '2026-01-01', 'x', "
        "        '2026-01-01', 'x')")
    conn.commit()
    raised = False
    try:
        conn.execute(
            "INSERT INTO profiles "
            "(project, variant, include, claude_md_extras, created_at, "
            " created_by, updated_at, updated_by) "
            "VALUES ('hollis', 'daytime', '[]', '[]', '2026-01-01', 'y', "
            "        '2026-01-01', 'y')")
        conn.commit()
    except sqlite3.IntegrityError:
        raised = True
    finally:
        conn.close()
    assert raised, ("two rows shared a (project, variant) pair — the "
                    "unique constraint is gone")


def test_a_different_variant_of_the_same_project_is_allowed(tmp_path):
    db = tmp_path / "library.db"
    assert alembic("upgrade", "head",
                   env_extra={"LIBRARY_DB": str(db)}).returncode == 0
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO profiles "
        "(project, variant, include, claude_md_extras, created_at, "
        " created_by, updated_at, updated_by) "
        "VALUES ('hollis', 'daytime', '[]', '[]', '2026-01-01', 'x', "
        "        '2026-01-01', 'x')")
    conn.execute(
        "INSERT INTO profiles "
        "(project, variant, include, claude_md_extras, created_at, "
        " created_by, updated_at, updated_by) "
        "VALUES ('hollis', 'overnight', '[]', '[]', '2026-01-01', 'x', "
        "        '2026-01-01', 'x')")
    conn.commit()
    count = conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0]
    conn.close()
    assert count == 2


def test_the_downgrade_drops_both_tables(tmp_path):
    db = tmp_path / "library.db"
    assert alembic("upgrade", "head",
                   env_extra={"LIBRARY_DB": str(db)}).returncode == 0
    result = alembic("downgrade", "4d80ea9474e4",
                     env_extra={"LIBRARY_DB": str(db)})
    assert result.returncode == 0, result.stderr
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        present = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('roster_agents', 'profiles')")}
    finally:
        conn.close()
    assert present == set()
