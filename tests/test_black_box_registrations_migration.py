"""The `black_box_registrations` migration: what it builds, run for real.

**Driven through the real `alembic` CLI**, the same reasoning
`test_machine_certificates_migration.py` and
`test_roster_and_profiles_migration.py` give: the parts most likely to be
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
            "PRAGMA table_info(black_box_registrations)")}
    finally:
        conn.close()
    assert columns == {"id", "crew", "model", "gateway", "created_at",
                       "created_by", "updated_at", "updated_by"}


def test_the_crew_column_is_unique(tmp_path):
    db = tmp_path / "library.db"
    assert alembic("upgrade", "head",
                   env_extra={"LIBRARY_DB": str(db)}).returncode == 0
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO black_box_registrations "
        "(crew, model, gateway, created_at, created_by, updated_at, "
        " updated_by) VALUES ('cfo', 'sonnet', 'anthropic', '2026-01-01', "
        "'operator', '2026-01-01', 'operator')")
    conn.commit()
    try:
        conn.execute(
            "INSERT INTO black_box_registrations "
            "(crew, model, gateway, created_at, created_by, updated_at, "
            " updated_by) VALUES ('cfo', 'opus', 'bedrock', '2026-01-02', "
            "'operator', '2026-01-02', 'operator')")
        conn.commit()
        raised = False
    except sqlite3.IntegrityError:
        raised = True
    finally:
        conn.close()
    assert raised, "two rows shared a crew — the unique constraint is gone"


def test_the_downgrade_drops_the_table(tmp_path):
    db = tmp_path / "library.db"
    assert alembic("upgrade", "head",
                   env_extra={"LIBRARY_DB": str(db)}).returncode == 0
    result = alembic("downgrade", "52afccc56e4f",
                     env_extra={"LIBRARY_DB": str(db)})
    assert result.returncode == 0, result.stderr
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        present = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='black_box_registrations'").fetchone()
    finally:
        conn.close()
    assert present is None
