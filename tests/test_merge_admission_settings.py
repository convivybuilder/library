"""`merge_admission_settings` — the append-only enforcement switch, and how
`store.py` reads it (CQ·219, issue_01M5FM2W1JV82A3XH3P0R5TX8J).

**No alembic here.** The hand-built schema below matches the migration
(`5cb0d7dc016f`) exactly, and `test_merge_admission_settings_migration.py`
is what proves the real migration builds it — this file is about what
`store.py` does with the table, not about the DDL.
"""
from __future__ import annotations

import sqlite3

import pytest

import store

SCHEMA = """
    CREATE TABLE merge_admission_settings (
        id INTEGER PRIMARY KEY, enforced BOOLEAN NOT NULL,
        set_by_client TEXT NOT NULL, acting_human TEXT NOT NULL,
        reason TEXT, set_at TEXT NOT NULL);
"""


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "control.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    return path


def _conn(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


def _insert(db, *, enforced, set_by_client="deck", acting_human="op@example.com",
           reason=None, set_at="2026-01-01 00:00:00"):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO merge_admission_settings "
        "(enforced, set_by_client, acting_human, reason, set_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (1 if enforced else 0, set_by_client, acting_human, reason, set_at))
    conn.commit()
    conn.close()


# --- merge_admission_enforced: an empty table enforces, the newest row wins --


def test_an_empty_table_enforces(db):
    conn = _conn(db)
    try:
        assert store.merge_admission_enforced(conn) is True
    finally:
        conn.close()


def test_a_single_row_is_read_back(db):
    _insert(db, enforced=False)
    conn = _conn(db)
    try:
        assert store.merge_admission_enforced(conn) is False
    finally:
        conn.close()


def test_the_newest_row_by_id_wins_even_against_an_earlier_timestamp(db):
    """`id`, never `set_at` — two writes could tie on wall-clock time, and
    `id` cannot."""
    _insert(db, enforced=False, set_at="2026-01-02 00:00:00")
    _insert(db, enforced=True, set_at="2026-01-01 00:00:00")
    conn = _conn(db)
    try:
        assert store.merge_admission_enforced(conn) is True
    finally:
        conn.close()


# --- set_merge_admission_enforcement: append-only ----------------------------


def test_set_merge_admission_enforcement_appends_rather_than_overwrites(
        db, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    store.set_merge_admission_enforcement(
        actor="deck", enforced=False, acting_human="op@example.com",
        reason="incident")
    store.set_merge_admission_enforcement(
        actor="orient", enforced=True, acting_human="op2@example.com",
        reason=None)
    conn = _conn(db)
    try:
        rows = conn.execute(
            "SELECT * FROM merge_admission_settings ORDER BY id").fetchall()
        assert len(rows) == 2
        assert store.merge_admission_enforced(conn) is True
    finally:
        conn.close()


def test_set_merge_admission_enforcement_refuses_while_writes_are_disabled(
        db, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    with pytest.raises(store.WritesDisabled):
        store.set_merge_admission_enforcement(
            actor="deck", enforced=False, acting_human="op@example.com",
            reason=None)


# --- merge_admission_boot_state: the startup line and the /health cache seed -


def test_boot_state_says_enforced_for_an_empty_table(db, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", db)
    health, line = store.merge_admission_boot_state()
    assert health == store.MERGE_ADMISSION_DEFAULT_HEALTH
    assert line == "ENFORCED (default — merge_admission_settings is empty)"


def test_boot_state_names_the_client_when_not_enforced(db, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", db)
    _insert(db, enforced=False, set_by_client="deck",
           set_at="2026-01-01 00:00:00")
    health, line = store.merge_admission_boot_state()
    assert health == {"enforced": False, "changed_at": "2026-01-01 00:00:00",
                      "changed_by_client": "deck", "reason": None}
    assert "NOT enforced" in line
    assert "deck" in line
    assert "2026-01-01 00:00:00" in line


def test_boot_state_says_enforced_for_the_newest_row(db, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", db)
    _insert(db, enforced=False, set_at="2026-01-01 00:00:00")
    _insert(db, enforced=True, set_at="2026-01-02 00:00:00")
    health, line = store.merge_admission_boot_state()
    assert health["enforced"] is True
    assert line == "ENFORCED"


def test_boot_state_reports_unreadable_rather_than_raising(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "does-not-exist.db")
    health, line = store.merge_admission_boot_state()
    assert health == store.MERGE_ADMISSION_DEFAULT_HEALTH
    assert "UNREADABLE" in line
