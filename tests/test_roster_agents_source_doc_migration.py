"""The `roster_agents.source_doc` migration, run for real
(issue_01M5H8RPFPM6VBVJCPBND1XVBT).

Driven through the `alembic` CLI, matching `test_short_description_
migration.py`'s own shape for the same kind of change: one nullable column
added to a table that already holds rows. The upgrade runs against a COPY of
a database that already holds a roster name, so the additive claim is
checked on data: the existing row survives untouched and reads NULL in the
new column.
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PREVIOUS_HEAD = "a4644bf94fab"
THIS_REVISION = "ebb81eb799bc"
COLUMN = "source_doc"


def alembic(*args, db):
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(REPO), capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(REPO),
             "LIBRARY_DB": str(db)})


def _columns(db):
    conn = sqlite3.connect(db)
    try:
        return [r[1] for r in conn.execute("PRAGMA table_info(roster_agents)")]
    finally:
        conn.close()


def test_upgrade_is_additive_on_a_database_that_holds_a_roster_name(tmp_path):
    db = tmp_path / "library.db"
    assert alembic("upgrade", PREVIOUS_HEAD, db=db).returncode == 0
    before = _columns(db)
    assert COLUMN not in before
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO roster_agents (name, created_at, created_by, "
        "updated_at, updated_by) VALUES ('coder', '2026-01-01', 'x', "
        "'2026-01-01', 'x')")
    conn.commit()
    conn.close()

    copy = tmp_path / "copy.db"
    copy.write_bytes(db.read_bytes())
    result = alembic("upgrade", THIS_REVISION, db=copy)
    assert result.returncode == 0, result.stderr

    assert _columns(copy) == before + [COLUMN]
    conn = sqlite3.connect(copy)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM roster_agents WHERE name = 'coder'").fetchone()
    conn.close()
    assert row["created_by"] == "x"
    assert row[COLUMN] is None


def test_downgrade_drops_exactly_the_one_column(tmp_path):
    db = tmp_path / "library.db"
    assert alembic("upgrade", THIS_REVISION, db=db).returncode == 0
    before = _columns(db)
    result = alembic("downgrade", PREVIOUS_HEAD, db=db)
    assert result.returncode == 0, result.stderr
    assert _columns(db) == [c for c in before if c != COLUMN]
