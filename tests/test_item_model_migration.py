"""The `item_model_choice` migration, run for real
(issue_01M38WTBNAS3NBGEYR8349RR52).

Driven through the `alembic` CLI, as `test_bundle_tokens_migration.py` is. The
upgrade runs against a COPY of a database that already holds work items, so the
additive claim is checked on data rather than on an empty schema: every
existing row survives untouched and reads NULL in the five new columns.
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PREVIOUS_HEAD = "3e8a5c1d7b40"
# **THIS MIGRATION'S OWN REVISION, NOT `"head"`.** `issue_items` is a shared
# table, and `d70d450ef8c7` (issue_01M2P5CBY5JY8SK5W344CV82AE) adds its own
# column to it further up the chain — upgrading to `"head"` from here would
# make this test about every migration between `PREVIOUS_HEAD` and whatever
# the chain currently ends at, not about the one this file is named for.
THIS_REVISION = "a8d2c5e91f37"
COLUMNS = {"model_choice", "model_reason", "model_set_by_client",
           "model_acting_human", "model_set_at"}


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
    assert not COLUMNS & set(before)
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
    result = alembic("upgrade", THIS_REVISION, db=copy)
    assert result.returncode == 0, result.stderr

    after = _columns(copy)
    # `after[:len(before)] == before` rather than the stronger `after ==
    # before + [...]` this used to assert: a later migration (`found_during`,
    # `ee33c883acc4`) also lands between `PREVIOUS_HEAD` and `head` now, so
    # "head" carries columns beyond this one's own five. What this migration
    # itself promises is narrower and still checked here — every existing
    # column survives untouched and in place, and its own five arrive.
    assert after[:len(before)] == before
    assert COLUMNS <= set(after)
    conn = sqlite3.connect(copy)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM issue_items WHERE id = 'i1'").fetchone()
    conn.close()
    assert row["title"] == "a title"
    assert all(row[c] is None for c in COLUMNS)


def test_downgrade_drops_exactly_the_five_columns(tmp_path):
    db = tmp_path / "library.db"
    assert alembic("upgrade", THIS_REVISION, db=db).returncode == 0
    assert COLUMNS <= set(_columns(db))
    result = alembic("downgrade", PREVIOUS_HEAD, db=db)
    assert result.returncode == 0, result.stderr
    assert not COLUMNS & set(_columns(db))
