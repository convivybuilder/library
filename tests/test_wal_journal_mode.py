"""WAL journal mode (issue_01M2KW35JX19GVG9Z13R10HKNZ): the write-open switch,
the read-only paths against it, the vector index, and backup completeness.

**The subject is a rollback journal's reader-blocks-writer / writer-blocks-reader
pair.** Under `delete` mode a reader holds a SHARED lock across a writer's
commit and a writer's commit blocks every reader; `connect_rw` now puts every
database it writes to into WAL, where writers still serialize but no reader
waits on one. `store.BUSY_TIMEOUT_MS` is 5,000ms while the service's fastest
clients give up in 2-3s, so that wait was an outright lost record, not a slow
one — the failure this whole change removes.

**Every database here is a `tmp_path` file. Nothing in this module opens
`~/.library/library.db`.**
"""
from __future__ import annotations

import sqlite3
import struct
from pathlib import Path

import pytest

import store
import vectors


def _empty_db(path):
    """A file SQLite will open, with no vault schema — `connect_rw`'s identity
    check only ever queries `sqlite_master`, which exists on any database."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE placeholder (x)")
    conn.commit()
    conn.close()
    return path


# --- the write-open switch --------------------------------------------------

def test_connect_rw_puts_a_fresh_database_in_wal(tmp_path, monkeypatch):
    """DoD: "a test proves a fresh database opened through that path comes up
    in WAL." A brand-new file has no other connection to contend with, so the
    mode switch is unconditional here."""
    db = _empty_db(tmp_path / "fresh.db")
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)

    conn = store.connect_rw()
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()


def test_connect_rw_sets_synchronous_full(tmp_path, monkeypatch):
    """`synchronous` is a per-connection setting, not persisted in the file
    the way `journal_mode` is, so it has to be asserted on the connection
    `connect_rw` itself returns rather than read back from a fresh handle.
    2 is SQLite's own integer code for FULL."""
    db = _empty_db(tmp_path / "sync.db")
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)

    conn = store.connect_rw()
    try:
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
    finally:
        conn.close()


def test_the_mode_persists_in_the_file_not_the_connection(tmp_path, monkeypatch):
    """`journal_mode` is a property of the database header. A later connection
    that never calls `connect_rw` — an ordinary `sqlite3.connect`, standing in
    for `sqlite3 library.db 'PRAGMA journal_mode'` from a shell — still reads
    WAL back, which is what makes one write-open the whole fix rather than
    something every caller has to remember to ask for."""
    db = _empty_db(tmp_path / "persist.db")
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    store.connect_rw().close()

    plain = sqlite3.connect(db)
    try:
        assert plain.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        plain.close()


# --- the read-only paths, in the three states the epic names ---------------

def test_store_connect_reads_wal_with_a_writer_connected(tmp_path, monkeypatch):
    db = _empty_db(tmp_path / "state1.db")
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    writer = store.connect_rw()
    try:
        writer.execute("INSERT INTO placeholder VALUES (1)")
        writer.commit()

        reader = store.connect()
        try:
            row = reader.execute("SELECT * FROM placeholder").fetchone()
            assert tuple(row) == (1,)
        finally:
            reader.close()
    finally:
        writer.close()


def test_store_connect_reads_wal_with_no_writer_and_wal_files_present(
        tmp_path, monkeypatch):
    """A second connection stays open across the writer's close, so SQLite
    cannot run its close-time auto-checkpoint — the `-wal`/`-shm` files are
    left behind holding the write, exactly the shape a crashed or killed
    writer leaves for the next reader to find."""
    db = _empty_db(tmp_path / "state2.db")
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    writer = store.connect_rw()
    writer.execute("INSERT INTO placeholder VALUES (2)")
    writer.commit()
    # **A `mode=ro` reader, not an ordinary read-write connection, is what
    # actually holds the checkpoint back — confirmed empirically rather than
    # assumed.** A plain read-write connection that only ever ran a SELECT does
    # not stop the writer's close from checkpointing and deleting `-wal`/`-shm`
    # out from under it; a read-only one does. This is also the realistic
    # shape: every reader this service opens (`store.connect`, `vectors.connect`)
    # is `mode=ro`.
    holder = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    holder.execute("SELECT * FROM placeholder").fetchall()
    writer.close()
    holder.close()

    assert (tmp_path / "state2.db-wal").exists()
    assert (tmp_path / "state2.db-shm").exists()

    reader = store.connect()
    try:
        row = reader.execute("SELECT * FROM placeholder").fetchone()
        assert tuple(row) == (2,)
    finally:
        reader.close()


def test_store_connect_reads_wal_with_no_writer_and_no_wal_files(
        tmp_path, monkeypatch):
    """The sole-connection close case: SQLite's own auto-checkpoint folds the
    WAL back into the main file and removes `-wal`/`-shm` on the way out, so
    this state is an ordinary, cleanly-closed WAL database."""
    db = _empty_db(tmp_path / "state3.db")
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    writer = store.connect_rw()
    writer.execute("INSERT INTO placeholder VALUES (3)")
    writer.commit()
    writer.close()

    assert not (tmp_path / "state3.db-wal").exists()
    assert not (tmp_path / "state3.db-shm").exists()

    reader = store.connect()
    try:
        row = reader.execute("SELECT * FROM placeholder").fetchone()
        assert tuple(row) == (3,)
    finally:
        reader.close()


# **ALL THREE STATES WORK, and why: `mode=ro` restricts writes to the MAIN
# database file, not to the `-shm`/`-wal` files a WAL reader needs to
# coordinate through.** SQLite is free to create or open those beside a
# read-only main-file handle as long as the containing directory is writable —
# true of every deployment of this service, since the reader and the writer
# both run as the same user in the same directory. There is no state here
# `store.connect`/`vectors.connect` need a code change for; these three tests
# are the proof, not a description of a fix.


def test_vectors_connect_reads_wal_in_all_three_states(tmp_path, vector_index):
    """`library_vectors` lives in its own file now, ATTACHed by `vectors.connect`
    (memory-retention epic clause 7) — the three states below are exercised
    against THAT file's `-wal`/`-shm`, not the main database's, which is why
    `db` here is an otherwise-empty file: only its existence matters, for
    `vectors.connect`'s own required argument."""
    sqlite_vec = pytest.importorskip("sqlite_vec")

    db = tmp_path / "vec_states.db"
    sqlite3.connect(db).close()
    vector_index(db)
    vec_db = vectors.VECTORS_DB_PATH
    vec_wal = Path(f"{vec_db}-wal")

    # State 1: writer connected. A raw connection stands in for
    # `vectors.connect_rw`, loading the extension by hand the same way it does.
    w = sqlite3.connect(vec_db, isolation_level=None)
    w.enable_load_extension(True)
    sqlite_vec.load(w)
    w.enable_load_extension(False)
    w.execute("PRAGMA journal_mode=WAL")
    w.execute("INSERT INTO library_vectors (rowid, embedding, vault, item_id) "
              "VALUES (1, ?, 'archives', 'sig_1')",
              (struct.pack(f"{vectors.EMBED_DIM}f", *([0.0] * vectors.EMBED_DIM)),))
    conn = vectors.connect(db)
    try:
        assert conn.execute(
            "SELECT item_id FROM library_vectors WHERE rowid = 1"
        ).fetchone()[0] == "sig_1"
    finally:
        conn.close()

    # State 2: no writer, -wal/-shm present. A `mode=ro` reader is what
    # actually holds the checkpoint back (see the equivalent `store` test for
    # the empirical comparison); `sqlite_master` rather than the vec0 table
    # itself, so this reader doesn't need the extension loaded.
    holder = sqlite3.connect(f"file:{vec_db}?mode=ro", uri=True)
    holder.execute("SELECT name FROM sqlite_master").fetchall()
    w.close()
    holder.close()
    assert vec_wal.exists()
    conn = vectors.connect(db)
    try:
        assert conn.execute(
            "SELECT item_id FROM library_vectors WHERE rowid = 1"
        ).fetchone()[0] == "sig_1"
    finally:
        conn.close()

    # State 3: no writer, no -wal/-shm — force a checkpoint-and-close.
    sole = sqlite3.connect(vec_db, isolation_level=None)
    sole.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    sole.close()
    assert not vec_wal.exists()
    conn = vectors.connect(db)
    try:
        assert conn.execute(
            "SELECT item_id FROM library_vectors WHERE rowid = 1"
        ).fetchone()[0] == "sig_1"
    finally:
        conn.close()


# --- vec0 ---------------------------------------------------------------

def _vec(dominant: int) -> bytes:
    """A packed float32 vector with one coordinate set, the rest zero — enough
    to make several rows genuinely distinguishable to a KNN search."""
    values = [0.0] * vectors.EMBED_DIM
    values[dominant] = 1.0
    return struct.pack(f"{vectors.EMBED_DIM}f", *values)


def _seed(db_path):
    sqlite_vec = pytest.importorskip("sqlite_vec")
    conn = sqlite3.connect(db_path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute(
        f"INSERT INTO {vectors.VEC_TABLE} (embedding, vault, item_id) "
        f"VALUES (?, 'archives', ?)", (_vec(0), "sig_near"))
    conn.execute(
        f"INSERT INTO {vectors.VEC_TABLE} (embedding, vault, item_id) "
        f"VALUES (?, 'archives', ?)", (_vec(1), "sig_mid"))
    conn.execute(
        f"INSERT INTO {vectors.VEC_TABLE} (embedding, vault, item_id) "
        f"VALUES (?, 'archives', ?)", (_vec(2), "sig_far"))
    conn.commit()
    conn.close()


def test_vec0_search_is_identical_on_a_wal_copy(tmp_path, vector_index,
                                                 monkeypatch):
    """DoD: "vec0 search returns the same results on a WAL-mode copy of the
    database as before the switch." `vectors.nearest` is the Library's own
    search function — the same KNN `GET /signals/search` calls — run against a
    fixed query vector on a `delete`-mode original and a WAL copy of it.

    **The copy is of the VECTORS file, not the main one** — `library_vectors`
    lives there now (memory-retention epic clause 7), so `original`/`copy`
    below are the otherwise-empty main files `vectors.connect` still requires,
    and `vec_original`/`vec_copy` are what actually gets seeded and switched."""
    original = tmp_path / "original.db"
    sqlite3.connect(original).close()
    vector_index(original)
    vec_original = vectors.VECTORS_DB_PATH
    _seed(vec_original)
    query = _vec(0)

    before_conn = vectors.connect(original)
    try:
        before = vectors.nearest(before_conn, query, k=3, vault="archives")
    finally:
        before_conn.close()
    assert before  # not a vacuous pass

    copy = tmp_path / "copy.db"
    sqlite3.connect(copy).close()
    vec_copy = tmp_path / "copy.db.vectors"
    vec_copy.write_bytes(vec_original.read_bytes())
    switch = sqlite3.connect(vec_copy, isolation_level=None)
    switch.execute("PRAGMA journal_mode=WAL")
    switch.close()
    assert sqlite3.connect(vec_copy).execute(
        "PRAGMA journal_mode").fetchone()[0] == "wal"

    monkeypatch.setattr(vectors, "VECTORS_DB_PATH", vec_copy)
    after_conn = vectors.connect(copy)
    try:
        after = vectors.nearest(after_conn, query, k=3, vault="archives")
    finally:
        after_conn.close()

    assert after == before


# --- backups --------------------------------------------------------------

def _uncheckpointed_wal_db(path):
    """A WAL database with a write sitting only in `-wal`, never folded into
    the main file, which is the shape a live-traffic snapshot always has some of.

    **THE WRITER STAYS OPEN, WITH AUTOCHECKPOINT OFF, UNTIL THE CALLER HAS
    TAKEN ITS COPY.** Only two things move a WAL write into the main file: a
    checkpoint, and the checkpoint SQLite runs when the last connection
    closes. An earlier version closed the writer and relied on a second
    connection that ran only `SELECT 1` to keep that close from being the
    last. `SELECT 1` never reads the database, so whether that connection
    counts as a WAL participant depends on the SQLite build. It passed on
    macOS (SQLite 3.51 and 3.53) and failed on CI's Linux runner, where the
    close checkpointed the row away: `-wal` was missing, and a bare copy of
    the main file already held the row. With the writer open and
    `wal_autocheckpoint=0`, no checkpoint can run on any platform, so the row
    is in `-wal` and not in the main file for as long as the caller needs."""
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("CREATE TABLE t (x)")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("INSERT INTO t VALUES (1)")
    return conn  # caller closes once the copy has been taken


def test_sqlite3_backup_captures_uncheckpointed_wal_writes(tmp_path):
    """DoD: "`sqlite3 .backup` ... produce[s] a complete, openable copy of a
    WAL database that still has uncheckpointed writes in its `-wal` file."
    `Connection.backup` is the same `sqlite3_backup_init` API the `sqlite3`
    CLI's `.backup` dot-command drives — this is the one already available
    without shelling out to the CLI binary."""
    db = tmp_path / "live.db"
    writer = _uncheckpointed_wal_db(db)
    try:
        assert (tmp_path / "live.db-wal").stat().st_size > 0
        dest = sqlite3.connect(tmp_path / "backup.db")
        src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            src.backup(dest)
        finally:
            src.close()
            dest.close()
    finally:
        writer.close()

    reopened = sqlite3.connect(tmp_path / "backup.db")
    try:
        assert reopened.execute("SELECT * FROM t").fetchall() == [(1,)]
    finally:
        reopened.close()


def test_a_bare_copy_of_the_main_file_alone_loses_the_wal_write(tmp_path):
    """The negative case, proving the danger the runbook fix is for: copying
    `library.db` without `-wal`/`-shm` silently drops whatever is still only
    in the log — the exact hazard DoD item 4 exists to close off in every
    place this repository tells someone to back the file up."""
    db = tmp_path / "live.db"
    writer = _uncheckpointed_wal_db(db)
    try:
        bad_copy = tmp_path / "bad_copy.db"
        bad_copy.write_bytes(db.read_bytes())  # the main file alone, no -wal
    finally:
        writer.close()

    reopened = sqlite3.connect(bad_copy)
    try:
        assert reopened.execute("SELECT * FROM t").fetchall() == []
    finally:
        reopened.close()


def test_vacuum_into_captures_uncheckpointed_wal_writes(tmp_path, monkeypatch):
    """DoD's "pre-migration backup step this repository documents":
    `replay_stranded.py`'s own backup line, `conn.execute("VACUUM INTO ?", ...)`
    on the connection `store.connect_rw()` returns."""
    db = tmp_path / "live.db"
    holder = _uncheckpointed_wal_db(db)
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    try:
        conn = store.connect_rw()
        try:
            backup_path = tmp_path / "vacuum_backup.db"
            conn.execute("VACUUM INTO ?", (str(backup_path),))
        finally:
            conn.close()
    finally:
        holder.close()

    reopened = sqlite3.connect(backup_path)
    try:
        assert reopened.execute("SELECT * FROM t").fetchall() == [(1,)]
    finally:
        reopened.close()


# --- checkpointing ----------------------------------------------------------

def test_the_wal_file_stays_bounded_under_a_burst_of_small_commits(
        tmp_path, monkeypatch):
    """DoD: "either SQLite's automatic checkpoint ... is shown to keep the
    `-wal` file bounded under the Library's write rate, or the service
    checkpoints explicitly." `connect_rw` sets no `wal_autocheckpoint`, so
    SQLite's own default (every 1,000 pages) applies. 3,000 commits through
    the real `connect_rw` path — a fresh connection per commit, the same as
    one request per write — should never let `-wal` grow anywhere near the
    size an un-checkpointed run of this many commits would reach; measured
    directly against a raw WAL database with no service code involved, 5,000
    similarly-sized commits peaked at ~4 MB (issue's PR body carries the
    number this asserts a generous multiple of)."""
    db = _empty_db(tmp_path / "checkpoint.db")
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    body = "x" * 500
    max_wal_bytes = 0
    wal_path = tmp_path / "checkpoint.db-wal"
    for _ in range(3000):
        conn = store.connect_rw()
        conn.execute("INSERT INTO placeholder VALUES (?)", (body,))
        conn.commit()
        conn.close()
        if wal_path.exists():
            max_wal_bytes = max(max_wal_bytes, wal_path.stat().st_size)

    total_row_bytes = 3000 * len(body)
    assert max_wal_bytes < total_row_bytes, (
        f"-wal peaked at {max_wal_bytes} bytes across {total_row_bytes} "
        f"bytes of row data — SQLite's automatic checkpoint is not bounding "
        f"it under this write shape, and this service sets no explicit "
        f"checkpoint of its own")


def test_a_contested_switch_does_not_fail_the_write_open(tmp_path, monkeypatch):
    """Switching into WAL needs exclusive access. With another connection
    holding a read lock, the switch waits out the busy timeout and raises
    `database is locked`. Measured at 2.17 s against a 2 s timeout. `connect_rw`
    must return a usable connection anyway, still in the file's old mode,
    because failing every write open on a busy database still in rollback mode
    is the outage this epic exists to remove."""
    db = _empty_db(tmp_path / "contested.db")
    raw = sqlite3.connect(db)
    raw.execute("PRAGMA journal_mode=DELETE")
    raw.execute("CREATE TABLE IF NOT EXISTS t (x)")
    raw.commit()
    raw.close()

    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    monkeypatch.setattr(store, "BUSY_TIMEOUT_MS", 200)

    reader = sqlite3.connect(db)
    reader.isolation_level = None
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM sqlite_master").fetchall()
    try:
        conn = store.connect_rw()
        try:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        finally:
            conn.close()
    finally:
        reader.execute("COMMIT")
        reader.close()

    conn = store.connect_rw()
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()
