"""The `merge_admission_settings` migration: what it builds, run for real
(CQ·219, issue_01M5FM2W1JV82A3XH3P0R5TX8J).

**Driven through the real `alembic` CLI**, the same way
`test_machine_certificates_migration.py` drives its own new table — the
parts most likely to be wrong are that the revision chains onto head and
that its DDL actually runs against SQLite.
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PREVIOUS_HEAD = "495cb5bcc41f"


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
            "PRAGMA table_info(merge_admission_settings)")}
    finally:
        conn.close()
    assert columns == {"id", "enforced", "set_by_client", "acting_human",
                       "reason", "set_at"}


def test_the_table_is_empty_on_a_fresh_upgrade(tmp_path):
    """Additive: a fresh vault's table starts empty, which is itself the
    enforced-by-default state `store.merge_admission_enforced` reads."""
    db = tmp_path / "library.db"
    assert alembic("upgrade", "head",
                   env_extra={"LIBRARY_DB": str(db)}).returncode == 0
    conn = sqlite3.connect(db)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM merge_admission_settings").fetchone()[0]
    finally:
        conn.close()
    assert count == 0


def test_a_row_can_be_inserted_and_read_back(tmp_path):
    db = tmp_path / "library.db"
    assert alembic("upgrade", "head",
                   env_extra={"LIBRARY_DB": str(db)}).returncode == 0
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO merge_admission_settings "
        "(enforced, set_by_client, acting_human, reason, set_at) "
        "VALUES (0, 'deck', 'op@example.com', 'incident', '2026-01-01 00:00:00')")
    conn.commit()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM merge_admission_settings").fetchone()
    conn.close()
    assert row["enforced"] == 0
    assert row["set_by_client"] == "deck"
    assert row["reason"] == "incident"


def test_reason_may_be_null(tmp_path):
    db = tmp_path / "library.db"
    assert alembic("upgrade", "head",
                   env_extra={"LIBRARY_DB": str(db)}).returncode == 0
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO merge_admission_settings "
        "(enforced, set_by_client, acting_human, set_at) "
        "VALUES (1, 'deck', 'op@example.com', '2026-01-01 00:00:00')")
    conn.commit()
    conn.close()


def test_set_by_client_is_required(tmp_path):
    db = tmp_path / "library.db"
    assert alembic("upgrade", "head",
                   env_extra={"LIBRARY_DB": str(db)}).returncode == 0
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO merge_admission_settings "
            "(enforced, acting_human, set_at) "
            "VALUES (1, 'op@example.com', '2026-01-01 00:00:00')")
        conn.commit()
        raised = False
    except sqlite3.IntegrityError:
        raised = True
    finally:
        conn.close()
    assert raised, "a row with no set_by_client was accepted"


def test_the_downgrade_drops_the_table(tmp_path):
    db = tmp_path / "library.db"
    assert alembic("upgrade", "head",
                   env_extra={"LIBRARY_DB": str(db)}).returncode == 0
    result = alembic("downgrade", PREVIOUS_HEAD, env_extra={"LIBRARY_DB": str(db)})
    assert result.returncode == 0, result.stderr
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        present = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='merge_admission_settings'").fetchone()
    finally:
        conn.close()
    assert present is None
