"""`init_cold.py`: the install-time command that creates the cold tier's
own database file (memory-retention epic clause 9,
issue_01M35R5QKX7QDCM822K3J18SWX).

**The schema is replayed from `main.signals`'s own `sqlite_master`, not a
DDL constant** — the opposite of `init_vectors.py`'s vec0 tables, which have
none to drift from. These tests run the real `alembic upgrade head` against
a scratch `LIBRARY_DB` so the replay is checked against what the migration
tree actually produces, not a second, independently-written copy of it.
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import init_cold
import store

REPO = Path(__file__).resolve().parent.parent


def _migrate(db_path: Path, env: dict | None = None) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(REPO), capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(REPO),
             "LIBRARY_DB": str(db_path), **(env or {})})
    assert result.returncode == 0, result.stderr


def _tables(db) -> set[str]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'")}
    finally:
        conn.close()


# --- creating the file, replaying main's own DDL -----------------------------


def _table_info(db, table: str) -> list[tuple]:
    """`PRAGMA table_info` rows as plain tuples — every column's name,
    declared type, nullability and default, not just its name."""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return conn.execute(f"PRAGMA table_info({table})").fetchall()
    finally:
        conn.close()


def test_a_fresh_cold_path_gets_a_signals_table_matching_main(
        tmp_path, monkeypatch):
    """`PRAGMA table_info` parity, not merely matching column names — the
    schema-parity test archives-cold-tier.md §2 asks for, catching a future
    migration that forgets the cold file."""
    db_path = tmp_path / "library.db"
    cold_path = tmp_path / "cold.db"
    _migrate(db_path)
    monkeypatch.setattr(store, "DB_PATH", db_path)

    created = init_cold.init(cold_path)

    assert created is True
    assert "signals" in _tables(cold_path)
    assert _table_info(cold_path, "signals") == _table_info(db_path, "signals")


def test_the_cold_file_carries_no_other_table(tmp_path, monkeypatch):
    """`signals` only — no vec0 tables, no `signal_acks`, no registry
    (archives-cold-tier.md §2: "There are no vec0 tables, no signal_acks
    and no registry")."""
    db_path = tmp_path / "library.db"
    cold_path = tmp_path / "cold.db"
    _migrate(db_path)
    monkeypatch.setattr(store, "DB_PATH", db_path)

    init_cold.init(cold_path)

    assert _tables(cold_path) == {"signals"}


def test_a_second_run_is_a_no_op(tmp_path, monkeypatch):
    db_path = tmp_path / "library.db"
    cold_path = tmp_path / "cold.db"
    _migrate(db_path)
    monkeypatch.setattr(store, "DB_PATH", db_path)

    assert init_cold.init(cold_path) is True
    conn = sqlite3.connect(cold_path)
    conn.execute(
        "INSERT INTO signals (id, source, kind, actor, ingested_by, "
        "product, refs, body, meta, privacy_class, ingested_at) VALUES "
        "('sig_x','t','note','a','a','vivy','{}','b','{}','internal',"
        "'2026-01-01 00:00:00')")
    conn.commit()
    conn.close()

    assert init_cold.init(cold_path) is False
    conn = sqlite3.connect(cold_path)
    n = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    conn.close()
    assert n == 1, "a no-op second run must not disturb existing rows"


# --- the refusals -------------------------------------------------------------


def test_unset_library_cold_db_refuses(tmp_path):
    result = subprocess.run(
        [sys.executable, str(REPO / "init_cold.py")], cwd=str(REPO),
        capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(REPO)})
    assert result.returncode != 0
    assert "LIBRARY_COLD_DB is not set" in result.stderr


def test_refuses_when_main_has_no_signals_table_yet(tmp_path):
    db_path = tmp_path / "library.db"
    sqlite3.connect(db_path).close()
    cold_path = tmp_path / "cold.db"
    result = subprocess.run(
        [sys.executable, str(REPO / "init_cold.py")], cwd=str(REPO),
        capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(REPO),
             "LIBRARY_DB": str(db_path), "LIBRARY_COLD_DB": str(cold_path)})
    assert result.returncode != 0
    assert "no `signals` table to replay" in result.stderr
    assert not cold_path.exists() or not _tables(cold_path)


def test_a_database_the_orient_guard_recognises_is_refused(tmp_path):
    """The same check `init_vectors.py` runs against the open handle before
    writing anything (ADR-0020) — a cold file pointed at Orient's control
    database must be refused, whatever its path claims."""
    db_path = tmp_path / "library.db"
    cold_path = tmp_path / "not-the-cold-file.db"
    conn = sqlite3.connect(str(cold_path))
    conn.execute(f"CREATE TABLE {store.ORIENT_ONLY_TABLES[0]} (id TEXT)")
    conn.commit()
    conn.close()

    result = subprocess.run(
        [sys.executable, str(REPO / "init_cold.py")], cwd=str(REPO),
        capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(REPO),
             "LIBRARY_DB": str(db_path), "LIBRARY_COLD_DB": str(cold_path)})

    assert result.returncode != 0
    assert store.ORIENT_ONLY_TABLES[0] in result.stderr
    assert "Orient" in result.stderr
    assert "signals" not in _tables(cold_path)


def test_main_reports_creation_then_presence_on_a_second_run(
        monkeypatch, tmp_path, capsys):
    db_path = tmp_path / "library.db"
    cold_path = tmp_path / "cold.db"
    _migrate(db_path)
    # `store.DB_PATH` resolves once, at import — already run by the time
    # this test sets the environment variable, so the module attribute
    # itself has to be the one patched (`conftest.py`'s own comment on
    # `LIBRARY_DB` explains why).
    monkeypatch.setattr(store, "DB_PATH", db_path)
    monkeypatch.setenv("LIBRARY_COLD_DB", str(cold_path))

    rc = init_cold.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "signals: created" in out

    rc = init_cold.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "signals: already present" in out
