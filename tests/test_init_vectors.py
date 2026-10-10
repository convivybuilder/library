"""`init_vectors.py`: the install-time command that creates the vector
index's own database file (memory-retention epic clause 7,
issue_01M35R5GA1SKRG2CMQRF189NDE).

**Independent of `LIBRARY_DB` and `alembic` entirely.** The vec0 schema
references no ordinary table, so unlike the single-file design this
replaced (issue_01M3JMHPN98Z3WD8GSSC841VM7's bug report, where a fresh
`alembic upgrade head` produced neither vec0 table), nothing here needs the
vault database to exist first at all.

**The DDL constants are checked against the live database's own words**,
`sqlite_master.sql`, rather than against a copy of the string kept in this
file — a constant and a test both transcribed from the same wrong string
would agree with each other and not with what this command actually
produces.
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import init_vectors
import store
import vectors

sqlite_vec = pytest.importorskip("sqlite_vec")

REPO = Path(__file__).resolve().parent.parent

LIBRARY_VECTORS_SQL = (
    "CREATE VIRTUAL TABLE library_vectors USING vec0(embedding "
    "float[384], vault text, +item_id text)")
SIGNAL_VECTORS_SQL = (
    "CREATE VIRTUAL TABLE signal_vectors USING vec0(embedding "
    "float[384], +signal_id text)")


def _tables(db) -> set[str]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'")}
    finally:
        conn.close()


def _sql_for(db, name: str) -> str | None:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = ?", (name,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _row_count(db, table: str) -> int:
    conn = init_vectors.connect_rw(db)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


# --- creating the file --------------------------------------------------


def test_a_fresh_path_has_no_file_until_init_runs(tmp_path):
    path = tmp_path / "vectors.db"
    assert not path.exists()
    init_vectors.init(path)
    assert path.is_file()


def test_init_makes_vectors_available_report_ready(tmp_path, monkeypatch):
    path = tmp_path / "vectors.db"
    library_db = tmp_path / "library.db"
    sqlite3.connect(library_db).close()
    monkeypatch.setattr(vectors, "VECTORS_DB_PATH", path)

    before_ok, before_reason = vectors.available(library_db)
    assert before_ok is False

    init_vectors.init(path)
    assert vectors.available(library_db) == (True, "ready")


def test_init_creates_both_tables_the_bug_report_names(tmp_path):
    path = tmp_path / "vectors.db"
    created = dict(init_vectors.init(path))
    assert created == {"library_vectors": True, "signal_vectors": True}
    assert {"library_vectors", "signal_vectors"} <= _tables(path)


# --- the canonical DDL -------------------------------------------------------


def test_the_ddl_constants_match_the_live_databases_schema(tmp_path):
    path = tmp_path / "vectors.db"
    init_vectors.init(path)
    assert _sql_for(path, "library_vectors") == LIBRARY_VECTORS_SQL
    assert _sql_for(path, "signal_vectors") == SIGNAL_VECTORS_SQL


def test_the_vectors_module_constants_equal_the_canonical_strings():
    """The constants themselves, with no database involved — `sqlite_master`
    strips `IF NOT EXISTS`, so this is the one place the literal constant
    text (which keeps it, for idempotency) is checked at all."""
    assert vectors.VEC_TABLE_DDL == (
        "CREATE VIRTUAL TABLE IF NOT EXISTS library_vectors USING "
        "vec0(embedding float[384], vault text, +item_id text)")
    assert vectors.SIGNAL_VEC_TABLE_DDL == (
        "CREATE VIRTUAL TABLE IF NOT EXISTS signal_vectors USING "
        "vec0(embedding float[384], +signal_id text)")


def test_split_vectors_and_vectors_name_the_same_vec0_tables():
    """`split_vectors.py`'s migration and `init_vectors.py`'s fresh-install
    path are two different ways of arriving at the same schema — a table
    renamed in one and not the other would make the migration produce a
    database `vectors.py` cannot read."""
    import split_vectors
    assert set(split_vectors.VEC_TABLES) == {
        vectors.VEC_TABLE, vectors.SIGNAL_VEC_TABLE}


# --- idempotence -------------------------------------------------------------


def test_a_second_run_is_a_no_op_and_leaves_existing_rows_intact(tmp_path):
    path = tmp_path / "vectors.db"
    init_vectors.init(path)
    conn = init_vectors.connect_rw(path)
    try:
        conn.execute(
            "INSERT INTO library_vectors(embedding, vault, item_id) VALUES "
            "(?, ?, ?)", (b"\x00" * (384 * 4), "chart_house", "item_1"))
        conn.execute(
            "INSERT INTO signal_vectors(embedding, signal_id) VALUES (?, ?)",
            (b"\x00" * (384 * 4), "sig_1"))
        conn.commit()
    finally:
        conn.close()

    before_library_sql = _sql_for(path, "library_vectors")
    before_signal_sql = _sql_for(path, "signal_vectors")

    second = dict(init_vectors.init(path))

    assert second == {"library_vectors": False, "signal_vectors": False}
    assert _sql_for(path, "library_vectors") == before_library_sql
    assert _sql_for(path, "signal_vectors") == before_signal_sql
    assert _row_count(path, "library_vectors") == 1
    assert _row_count(path, "signal_vectors") == 1


# --- the refusals ------------------------------------------------------------


def _run_script(env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(REPO / "init_vectors.py")], cwd=str(REPO),
        capture_output=True, text=True, check=False, env=env)


def test_unset_library_vectors_db_refuses(tmp_path):
    result = _run_script({"PATH": "/usr/bin:/bin", "HOME": str(REPO)})
    assert result.returncode != 0
    assert "LIBRARY_VECTORS_DB is not set" in result.stderr


def test_refuses_when_it_names_the_same_file_as_library_db(tmp_path):
    same = tmp_path / "library.db"
    sqlite3.connect(same).close()
    result = _run_script({
        "PATH": "/usr/bin:/bin", "HOME": str(REPO),
        "LIBRARY_DB": str(same), "LIBRARY_VECTORS_DB": str(same)})
    assert result.returncode != 0
    assert "same file as LIBRARY_DB" in result.stderr
    # And nothing was written — the vec0 tables never landed in library.db.
    assert not _tables(same) & {"library_vectors", "signal_vectors"}


def test_library_db_unset_with_orient_control_db_skips_the_same_file_check(
        tmp_path):
    """The read-only shadow's own configuration — `ORIENT_CONTROL_DB` set,
    `LIBRARY_DB` not — satisfies `store`'s own import-time check with no
    `LIBRARY_DB` path to compare this command's own refusal against, and that
    is not an error either."""
    path = tmp_path / "vectors.db"
    env = {"PATH": "/usr/bin:/bin", "HOME": str(REPO),
           "LIBRARY_VECTORS_DB": str(path),
           "ORIENT_CONTROL_DB": str(tmp_path / "orient-control.db")}
    result = _run_script(env)
    assert result.returncode == 0, result.stderr
    assert {"library_vectors", "signal_vectors"} <= _tables(path)


def test_a_database_the_orient_guard_recognises_is_refused(tmp_path):
    """A file that holds one of `store.ORIENT_ONLY_TABLES`, whatever its path
    claims, is Orient's rather than the Library's, and a second writer to it
    is the ADR-0020 failure.

    **`store`'s own import-time check cannot catch this on its own**, unlike
    the equivalent case for `LIBRARY_DB`: it resolves `LIBRARY_DB`, and this
    poisons `LIBRARY_VECTORS_DB`, a path `store`'s import-time check never
    looks at. `connect_rw`'s own fingerprint check on the open handle is the
    only thing standing between this file and a `CREATE VIRTUAL TABLE`
    against Orient's live control database.
    """
    db = tmp_path / "not-the-vectors-file.db"
    conn = sqlite3.connect(str(db))
    conn.execute(f"CREATE TABLE {store.ORIENT_ONLY_TABLES[0]} (id TEXT)")
    conn.commit()
    conn.close()

    result = _run_script({
        "PATH": "/usr/bin:/bin", "HOME": str(REPO),
        "LIBRARY_DB": str(tmp_path / "placeholder-library.db"),
        "LIBRARY_VECTORS_DB": str(db)})

    assert result.returncode != 0
    assert store.ORIENT_ONLY_TABLES[0] in result.stderr
    assert "Orient" in result.stderr
    assert not _tables(db) & {"library_vectors", "signal_vectors"}


def test_connect_rws_own_fingerprint_check_refuses_in_process(tmp_path):
    """The in-process counterpart to
    `test_a_database_the_orient_guard_recognises_is_refused` above, calling
    `connect_rw` directly rather than through a subprocess — both reach the
    same check, since nothing upstream of `connect_rw` looks at
    `LIBRARY_VECTORS_DB` at all.
    """
    db = tmp_path / "not-the-vectors-file.db"
    conn = sqlite3.connect(str(db))
    conn.execute(f"CREATE TABLE {store.ORIENT_ONLY_TABLES[0]} (id TEXT)")
    conn.commit()
    conn.close()

    with pytest.raises(init_vectors.Refused, match=store.ORIENT_ONLY_TABLES[0]):
        init_vectors.connect_rw(db)

    assert not _tables(db) & {"library_vectors", "signal_vectors"}


def test_a_normal_vectors_file_is_accepted(tmp_path):
    """The negative control: a fresh, ordinary path holds none of
    `store.ORIENT_ONLY_TABLES`, so the fingerprint check finds nothing and
    `connect_rw` returns a working handle rather than refusing everything."""
    path = tmp_path / "vectors.db"
    conn = init_vectors.connect_rw(path)
    try:
        conn.execute(vectors.VEC_TABLE_DDL)
        conn.execute(vectors.SIGNAL_VEC_TABLE_DDL)
        conn.commit()
    finally:
        conn.close()
    assert {"library_vectors", "signal_vectors"} <= _tables(path)


# --- connect_rw closes on every error path -----------------------------


def test_connect_rw_closes_the_handle_when_the_extension_load_fails(
        tmp_path, monkeypatch):
    """`connect_rw` used to close its connection only on the branch where the
    Orient fingerprint query found a match; this asserts the other branch —
    any failure after `sqlite3.connect`, here `sqlite_vec.load` itself
    raising, must not leave the handle open either. Spy on `sqlite3.connect`
    to keep the connection `connect_rw` opens reachable after it raises, then
    check that a later operation on it gets `sqlite3.ProgrammingError`, the
    error sqlite3 gives for a closed connection, rather than running against
    a database nothing else can then open exclusively.
    """
    path = tmp_path / "vectors.db"
    opened: list[sqlite3.Connection] = []
    real_connect = sqlite3.connect

    def _spy_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", _spy_connect)
    monkeypatch.setattr(sqlite_vec, "load",
                         lambda conn: (_ for _ in ()).throw(
                             RuntimeError("sqlite-vec load failed")))

    with pytest.raises(RuntimeError, match="sqlite-vec load failed"):
        init_vectors.connect_rw(path)

    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")


def test_main_reports_creation_then_presence_on_a_second_run(monkeypatch,
                                                               tmp_path, capsys):
    path = tmp_path / "vectors.db"
    monkeypatch.setenv("LIBRARY_VECTORS_DB", str(path))
    monkeypatch.delenv("LIBRARY_DB", raising=False)

    rc = init_vectors.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "library_vectors: created" in out
    assert "signal_vectors: created" in out

    rc = init_vectors.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "library_vectors: already present" in out
    assert "signal_vectors: already present" in out
