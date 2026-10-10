"""The `found_during` migration, run for real
(issue_01M3BFN34Y8P8K39RWS6MM89ZT).

Driven through the `alembic` CLI, as `test_item_model_migration.py` is. The
upgrade runs against a COPY of a database that already holds work items, so
the additive claim is checked on data rather than on an empty schema: every
existing row survives untouched and reads NULL in the new column.
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PREVIOUS_HEAD = "d70d450ef8c7"
COLUMN = "found_during"


def alembic(*args, db):
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(REPO), capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(REPO),
             "LIBRARY_DB": str(db)})


def _columns(db):
    conn = sqlite3.connect(db)
    try:
        return [r[1] for r in conn.execute("PRAGMA table_info(issue_items)")]
    finally:
        conn.close()


def test_upgrade_is_additive_on_a_database_that_holds_items(tmp_path):
    db = tmp_path / "library.db"
    assert alembic("upgrade", PREVIOUS_HEAD, db=db).returncode == 0
    before = _columns(db)
    assert COLUMN not in before
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO issue_items (id, product, title, body, refs, severity, state, "
        "type, created_by, created_at) VALUES ('i1','vivy','a title','b','{}',"
        "'low','open','task','t','2026-01-01')")
    conn.commit()
    conn.close()

    # A copy of the populated database is what the upgrade runs against.
    copy = tmp_path / "copy.db"
    copy.write_bytes(db.read_bytes())
    result = alembic("upgrade", "head", db=copy)
    assert result.returncode == 0, result.stderr

    after = _columns(copy)
    assert after == before + [COLUMN]
    conn = sqlite3.connect(copy)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM issue_items WHERE id = 'i1'").fetchone()
    conn.close()
    assert row["title"] == "a title"
    assert row[COLUMN] is None


def test_downgrade_drops_exactly_the_one_column(tmp_path):
    db = tmp_path / "library.db"
    assert alembic("upgrade", "head", db=db).returncode == 0
    assert COLUMN in _columns(db)
    result = alembic("downgrade", PREVIOUS_HEAD, db=db)
    assert result.returncode == 0, result.stderr
    assert COLUMN not in _columns(db)
