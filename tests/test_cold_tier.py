"""cold_tier.py — the mover that relocates a signal to the cold tier once it
passes its kind's hot window, and `--restore`, its reverse
(library/decisions/0001-what-the-archives-keep-embed-and-move-cold, memory-
retention epic clause 9, issue_01M35R5QKX7QDCM822K3J18SWX).

Mapped to the build plan's own sections:
- §3 ("Windows are data in code") — the policy tests below.
- §3 ("The move is copy-then-delete") — the move/restore round trips.
- §5's "Interrupted move" / "Interrupted restore" — the failure-injection
  tests.
- §5's "Lock budget" — the concurrent-writer test.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import traceback

import pytest

import cold_tier as ct
import store
import vectors

MAIN_SCHEMA = """
CREATE TABLE signals (
    id TEXT PRIMARY KEY, source TEXT, stream TEXT, kind TEXT, actor TEXT,
    ingested_by TEXT, product TEXT, refs TEXT, body TEXT, meta TEXT,
    privacy_class TEXT, produced_at TEXT, ingested_at TEXT,
    embedded_at TEXT, dedup_key TEXT UNIQUE, acting_human TEXT);
CREATE TABLE cold_dedup_keys (
    dedup_key TEXT PRIMARY KEY, signal_id TEXT NOT NULL);
"""

COLD_SCHEMA = """
CREATE TABLE signals (
    id TEXT PRIMARY KEY, source TEXT, stream TEXT, kind TEXT, actor TEXT,
    ingested_by TEXT, product TEXT, refs TEXT, body TEXT, meta TEXT,
    privacy_class TEXT, produced_at TEXT, ingested_at TEXT,
    embedded_at TEXT, dedup_key TEXT UNIQUE, acting_human TEXT);
"""


@pytest.fixture
def db(tmp_path, monkeypatch, vector_index, cold_db):
    path = tmp_path / "library.db"
    conn = sqlite3.connect(path)
    conn.executescript(MAIN_SCHEMA)
    conn.commit()
    conn.close()
    vector_index(path)
    cold_db(COLD_SCHEMA)
    monkeypatch.setattr(store, "DB_PATH", path)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    return path


def _insert(path, sig_id, kind, *, ingested_at, embedded_at="2020-01-01 00:00:00",
            dedup_key=None, body="x", meta=None):
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO signals (id, source, kind, actor, ingested_by, product, "
        "refs, body, meta, privacy_class, ingested_at, embedded_at, dedup_key) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sig_id, "test", kind, "someone", "someone", "vivy", "{}", body,
         json.dumps(meta or {}), "internal", ingested_at, embedded_at,
         dedup_key))
    conn.commit()
    conn.close()


def _hot_exists(path, sig_id) -> bool:
    conn = sqlite3.connect(path)
    row = conn.execute("SELECT 1 FROM signals WHERE id = ?", (sig_id,)).fetchone()
    conn.close()
    return row is not None


def _cold_exists(sig_id) -> bool:
    import os
    conn = sqlite3.connect(os.environ["LIBRARY_COLD_DB"])
    row = conn.execute("SELECT 1 FROM signals WHERE id = ?", (sig_id,)).fetchone()
    conn.close()
    return row is not None


def _registry_signal_for(path, dedup_key) -> str | None:
    conn = sqlite3.connect(path)
    row = conn.execute("SELECT signal_id FROM cold_dedup_keys WHERE dedup_key = ?",
                       (dedup_key,)).fetchone()
    conn.close()
    return row[0] if row else None


OLD = "2020-01-01 00:00:00.000000"
RECENT = "2026-09-30 00:00:00.000000"


# --- policy: HOT_WINDOW_DAYS --------------------------------------------------


def test_deletion_record_never_moves_however_old(db):
    _insert(db, "sig_dr", "deletion-record", ingested_at="2010-01-01 00:00:00")

    reports = ct.move(_conn(db), dry_run=True)

    assert _hot_exists(db, "sig_dr")
    by_kind = {r.kind: r.moved for r in reports}
    assert by_kind.get("deletion-record", 0) == 0


def test_an_unembedded_row_never_moves_even_when_old(db):
    _insert(db, "sig_noemb", "note", ingested_at=OLD, embedded_at=None)

    ct.run(dry_run=False, do_restore=False)

    assert _hot_exists(db, "sig_noemb")


def test_an_unruled_kind_moves_at_the_default_window(db, monkeypatch):
    monkeypatch.setattr(vectors, "DEFAULT_HOT_WINDOW_DAYS", 0)
    _insert(db, "sig_mystery", "mystery-kind", ingested_at=OLD)

    ct.run(dry_run=False, do_restore=False)

    assert not _hot_exists(db, "sig_mystery")
    assert _cold_exists("sig_mystery")


def test_the_window_dicts_keys_equal_the_kept_sets():
    assert (set(vectors.HOT_WINDOW_DAYS)
            == vectors.EMBEDDED_SIGNAL_KINDS | vectors.KEPT_NOT_EMBEDDED_SIGNAL_KINDS)


def test_a_transcript_turn_keeps_its_thirty_day_window():
    """Turns are embedded (180 days from the spread over `EMBEDDED_SIGNAL_KINDS`),
    but their own explicit key must win over that spread. Reordering the dict
    literal would silently give every turn 180 days, so this pins the value."""
    assert vectors.HOT_WINDOW_DAYS["transcript-turn"] == 30
    assert "transcript-turn" in vectors.EMBEDDED_SIGNAL_KINDS


def test_a_recent_row_stays_hot(db):
    _insert(db, "sig_recent", "note", ingested_at=RECENT)

    ct.run(dry_run=False, do_restore=False)

    assert _hot_exists(db, "sig_recent")
    assert not _cold_exists("sig_recent")


# --- the move itself -----------------------------------------------------


def test_a_moved_row_is_gone_from_hot_and_present_in_cold_byte_identical(db):
    _insert(db, "sig_old", "note", ingested_at=OLD, body="exact content")

    assert ct.run(dry_run=False, do_restore=False) == 0

    assert not _hot_exists(db, "sig_old")
    import os
    conn = sqlite3.connect(os.environ["LIBRARY_COLD_DB"])
    row = conn.execute("SELECT body FROM signals WHERE id = ?",
                       ("sig_old",)).fetchone()
    conn.close()
    assert row == ("exact content",)


def test_a_moved_rows_dedup_key_lands_in_the_registry(db):
    _insert(db, "sig_old", "note", ingested_at=OLD, dedup_key="dk1")

    ct.run(dry_run=False, do_restore=False)

    assert _registry_signal_for(db, "dk1") == "sig_old"


def test_a_row_with_no_dedup_key_writes_no_registry_entry(db):
    _insert(db, "sig_old", "note", ingested_at=OLD, dedup_key=None)

    ct.run(dry_run=False, do_restore=False)

    conn = sqlite3.connect(db)
    n = conn.execute("SELECT COUNT(*) FROM cold_dedup_keys").fetchone()[0]
    conn.close()
    assert n == 0


def test_a_second_pass_does_not_re_move_or_duplicate(db):
    _insert(db, "sig_old", "note", ingested_at=OLD, dedup_key="dk1")
    ct.run(dry_run=False, do_restore=False)
    ct.run(dry_run=False, do_restore=False)

    import os
    conn = sqlite3.connect(os.environ["LIBRARY_COLD_DB"])
    n = conn.execute("SELECT COUNT(*) FROM signals WHERE id = ?",
                     ("sig_old",)).fetchone()[0]
    conn.close()
    assert n == 1


def test_a_dry_run_writes_nothing(db):
    _insert(db, "sig_old", "note", ingested_at=OLD, dedup_key="dk1")

    ct.run(dry_run=True, do_restore=False)

    assert _hot_exists(db, "sig_old")
    assert not _cold_exists("sig_old")
    assert _registry_signal_for(db, "dk1") is None


# --- --restore -----------------------------------------------------------


def test_restore_returns_every_row_to_hot_and_empties_the_registry(db):
    _insert(db, "sig_old", "note", ingested_at=OLD, dedup_key="dk1")
    ct.run(dry_run=False, do_restore=False)
    assert not _hot_exists(db, "sig_old")

    assert ct.run(dry_run=False, do_restore=True) == 0

    assert _hot_exists(db, "sig_old")
    assert not _cold_exists("sig_old")
    assert _registry_signal_for(db, "dk1") is None


def test_a_dry_run_restore_writes_nothing(db):
    _insert(db, "sig_old", "note", ingested_at=OLD, dedup_key="dk1")
    ct.run(dry_run=False, do_restore=False)

    ct.run(dry_run=True, do_restore=True)

    assert not _hot_exists(db, "sig_old")
    assert _cold_exists("sig_old")
    assert _registry_signal_for(db, "dk1") == "sig_old"


# --- byte budget: a row is never split across batches -------------------


def test_the_byte_budget_never_splits_a_row_and_resumes_correctly(
        db, monkeypatch):
    """A batch whose budget is crossed mid-page must still move every
    candidate eventually, never skipping the rows the budget deferred."""
    monkeypatch.setattr(ct, "MOVE_BATCH_BYTE_BUDGET", 10)
    for i in range(5):
        _insert(db, f"sig_{i}", "note",
                ingested_at=f"2020-01-01 00:00:0{i}.000000", body="x" * 8)

    assert ct.run(dry_run=False, do_restore=False) == 0

    for i in range(5):
        assert not _hot_exists(db, f"sig_{i}")
        assert _cold_exists(f"sig_{i}")


def test_a_single_row_over_the_budget_still_moves_whole(db, monkeypatch):
    monkeypatch.setattr(ct, "MOVE_BATCH_BYTE_BUDGET", 1)
    _insert(db, "sig_big", "note", ingested_at=OLD, body="x" * 1000)

    ct.run(dry_run=False, do_restore=False)

    assert _cold_exists("sig_big")


# Known SQLITE_BUSY / SQLITE_LOCKED extended codes, for naming the code in a
# lock error's evidence — Python's sqlite3 module gives the raw int only.
_EXTENDED_ERROR_NAMES = {
    5: "SQLITE_BUSY",
    5 | (1 << 8): "SQLITE_BUSY_RECOVERY",
    5 | (2 << 8): "SQLITE_BUSY_SNAPSHOT",
    5 | (3 << 8): "SQLITE_BUSY_TIMEOUT",
    6: "SQLITE_LOCKED",
    6 | (1 << 8): "SQLITE_LOCKED_SHAREDCACHE",
    6 | (2 << 8): "SQLITE_LOCKED_VTAB",
}


def _errorname(code) -> str:
    return _EXTENDED_ERROR_NAMES.get(code, f"UNKNOWN({code})")


def _journal_modes(main_path, cold_path) -> dict:
    """PRAGMA journal_mode of both files, read on fresh connections — never
    on a connection that just raised, since that connection may already be
    unusable. Part of a lock error's evidence (issue_01M504BG7VGE2AFXSYAHH9PDFX):
    a failure naming only "database is locked" cannot say whether `cold` had
    already been switched to WAL by the move pass's own `attach_cold` when
    the race started."""
    modes = {}
    for label, path in (("main", main_path), ("cold", cold_path)):
        try:
            c = sqlite3.connect(path, timeout=1)
            modes[label] = c.execute("PRAGMA journal_mode").fetchone()[0]
            c.close()
        except Exception as exc:  # noqa: BLE001 - best effort
            modes[label] = f"<unreadable: {exc}>"
    return modes


# The retry helpers sit between a call site and the `conn.execute` that
# raised, so `_labelled` skips them and names the call site instead — the
# same split `tests/test_discard_sweep.py`'s own `_labelled` draws for
# `discard_sweep`'s identically-shaped helpers.
_RETRY_HELPERS = frozenset({"_begin_immediate", "_execute_retrying_busy"})


def _labelled(side: str, e: sqlite3.OperationalError, main_path, cold_path) -> str:
    """A lock error's full evidence: which side raised, the call site that
    issued the refused statement (skipping the retry helpers), the extended
    `sqlite_errorcode`/name, both schemas' journal mode, and the traceback —
    everything issue_01M504BG7VGE2AFXSYAHH9PDFX's own CI failures lacked,
    because the test only kept the exception, not which statement raised it.
    """
    frames = [f for f in traceback.extract_tb(e.__traceback__)
              if f.name not in _RETRY_HELPERS]
    frame = frames[-1]
    code = getattr(e, "sqlite_errorcode", None)
    name = _errorname(code)
    modes = _journal_modes(main_path, cold_path)
    tb = "".join(traceback.format_exception(type(e), e, e.__traceback__))
    return (f"{side} {frame.name}:{frame.lineno}: {e} "
            f"(sqlite_errorcode={code} sqlite_errorname={name} "
            f"journal_modes={modes})\n{tb}")


def _watch_refused(monkeypatch, on_refused, prefix="BEGIN IMMEDIATE"):
    """Route every `sqlite3.connect` in the test through a Connection subclass
    that calls `on_refused()` each time a statement starting with `prefix` on
    it is refused. The refusal is SQLite's own, so the handler sees a real
    `SQLITE_BUSY` — the same helper `tests/test_discard_sweep.py` uses for
    `discard_sweep`'s identically-shaped `_begin_immediate`."""
    class _Watching(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            try:
                return super().execute(sql, *args, **kwargs)
            except sqlite3.OperationalError:
                if sql.startswith(prefix):
                    on_refused()
                raise

    real_connect = sqlite3.connect

    def connect(*args, **kwargs):
        kwargs.setdefault("factory", _Watching)
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)


# --- lock budget: a move pass beside a concurrent writer -----------------


def test_a_move_pass_and_a_concurrent_writer_neither_get_a_lock_error(db):
    """The race runs in WAL, the mode the live vault and cold file are in.

    The fixture creates its file in rollback-journal mode, and
    `store.connect_rw` swallows a contested switch to WAL, so when the
    writer thread and the move pass opened at the same instant the race
    could run in rollback mode. There SQLite returns "database is locked"
    without invoking the busy handler when a reader upgrading to a writer
    meets a writer waiting to commit, which is deadlock avoidance rather than
    a timeout. That failed library main twice on 2026-10-15 on a loaded
    runner, and forcing rollback mode under eight-way parallel load
    reproduced the same error locally, 1 run in 420, against 0 in 420 in
    WAL. Production never runs in rollback mode, so the test switches the
    file before the race starts.

    **The later, WAL-mode failures (library#290's head, and main's merge of
    #289, both 2026-10-15) were a second, different mechanism**
    (issue_01M504BG7VGE2AFXSYAHH9PDFX): `_move_batch`'s step 2, `BEGIN
    IMMEDIATE` on `main`, losing the race for the write lock against this
    same writer thread, and raising a plain `SQLITE_BUSY` once
    `store.BUSY_TIMEOUT_MS` ran out — confirmed by amplified reproduction
    (the PR body's evidence table), never `SQLITE_BUSY_SNAPSHOT` or
    `SQLITE_BUSY_RECOVERY`, and never step 1's `INSERT ... SELECT`. That is
    ordinary writer-vs-writer contention the busy handler already tried and
    failed to clear in one attempt — `cold_tier._begin_immediate` now
    retries it, the fix `discard_sweep.py` already carries for the
    identical shape. `_labelled` below keeps the statement, the extended
    error code and both schemas' journal mode in the failure message, so a
    future failure here names its own cause rather than repeating the
    2026-10-15 failures' bare "database is locked".
    """
    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    conn.close()
    for i in range(40):
        _insert(db, f"sig_old_{i:03d}", "note",
                ingested_at=f"2020-01-01 00:00:{i:02d}.000000")

    cold_path = os.environ["LIBRARY_COLD_DB"]
    errors = []
    writes = []
    stop = threading.Event()

    def writer():
        i = 0
        while not stop.is_set():
            try:
                conn = store.connect_rw()
                try:
                    conn.execute(
                        "INSERT INTO signals (id, source, kind, actor, "
                        "ingested_by, product, refs, body, meta, "
                        "privacy_class, ingested_at) VALUES "
                        "(?,?,?,?,?,?,?,?,?,?,?)",
                        (f"sig_live_{i:04d}", "test", "note", "someone",
                         "someone", "vivy", "{}", "b", "{}", "internal",
                         RECENT))
                    conn.commit()
                finally:
                    conn.close()
                writes.append(i)
            except sqlite3.OperationalError as e:
                errors.append(_labelled("writer", e, db, cold_path))
            i += 1

    t = threading.Thread(target=writer)
    t.start()
    try:
        assert ct.run(dry_run=False, do_restore=False) == 0
    except sqlite3.OperationalError as e:
        errors.append(_labelled("move", e, db, cold_path))
    finally:
        stop.set()
        t.join(timeout=5)

    assert not errors, f"a concurrent write hit a lock error:\n{errors}"
    assert writes, "the concurrent writer never got a turn — this test proves nothing"
    for i in range(40):
        assert not _hot_exists(db, f"sig_old_{i:03d}")
    for i in writes:
        assert _hot_exists(db, f"sig_live_{i:04d}")


# --- cold_tier._begin_immediate: retried on SQLITE_BUSY -------------------
#
# issue_01M504BG7VGE2AFXSYAHH9PDFX's fix. Mirrors `tests/test_discard_sweep.
# py`'s own tests for `discard_sweep._begin_immediate`, the identical helper
# for the identical shape (a `BEGIN IMMEDIATE` on `main` that can lose a
# race to a live writer).


def test_a_refused_move_begin_immediate_is_retried_and_the_pass_completes(
        db, monkeypatch):
    """`_begin_immediate` retries a refused lock. A holder keeps the write
    lock on `main` until `_move_batch`'s step 2 is refused once, and
    releases it from inside the refusal handler, so the second attempt is
    the one that takes the lock and the pass completes."""
    monkeypatch.setattr(store, "BUSY_TIMEOUT_MS", 50)
    _insert(db, "sig_old", "note", ingested_at=OLD, dedup_key="dk1")
    holder = sqlite3.connect(db, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    refusals = []

    def release_on_first_refusal():
        refusals.append(1)
        if len(refusals) == 1:
            holder.execute("COMMIT")

    _watch_refused(monkeypatch, release_on_first_refusal)
    try:
        assert ct.run(dry_run=False, do_restore=False) == 0
    finally:
        holder.close()

    assert len(refusals) == 1
    assert not _hot_exists(db, "sig_old")
    assert _cold_exists("sig_old")
    assert _registry_signal_for(db, "dk1") == "sig_old"


def test_a_move_begin_immediate_refused_on_every_attempt_fails_the_pass_and_writes_nothing(
        db, monkeypatch):
    """A lock that never frees fails the pass after three tries, and step 2
    has written nothing by then: the row stays hot, and step 1's cold copy
    (already committed before step 2 ever runs) is the only trace of the
    attempt — exactly what lets a resumed pass skip re-copying it rather
    than find the row moved twice."""
    monkeypatch.setattr(store, "BUSY_TIMEOUT_MS", 50)
    _insert(db, "sig_old", "note", ingested_at=OLD, dedup_key="dk1")
    holder = sqlite3.connect(db, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    refusals = []
    _watch_refused(monkeypatch, lambda: refusals.append(1))
    try:
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            ct.run(dry_run=False, do_restore=False)
    finally:
        holder.execute("COMMIT")
        holder.close()

    # Pinned as a literal, not `ct.BEGIN_ATTEMPTS`: the pass's worst-case wait
    # is this count times `BUSY_TIMEOUT_MS`, so lowering the constant must
    # fail here rather than pass by agreeing with itself.
    assert len(refusals) == 3
    assert _hot_exists(db, "sig_old")
    assert _cold_exists("sig_old")
    assert _registry_signal_for(db, "dk1") is None


def test_a_refused_restore_begin_immediate_is_retried_and_the_pass_completes(
        db, monkeypatch):
    """The reverse of the first test above: `_restore_batch`'s own `BEGIN
    IMMEDIATE` (step 1, on `main`) is retried the same way."""
    _insert(db, "sig_old", "note", ingested_at=OLD, dedup_key="dk1")
    ct.run(dry_run=False, do_restore=False)
    assert not _hot_exists(db, "sig_old")

    monkeypatch.setattr(store, "BUSY_TIMEOUT_MS", 50)
    holder = sqlite3.connect(db, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    refusals = []

    def release_on_first_refusal():
        refusals.append(1)
        if len(refusals) == 1:
            holder.execute("COMMIT")

    _watch_refused(monkeypatch, release_on_first_refusal)
    try:
        assert ct.run(dry_run=False, do_restore=True) == 0
    finally:
        holder.close()

    assert len(refusals) == 1
    assert _hot_exists(db, "sig_old")
    assert not _cold_exists("sig_old")
    assert _registry_signal_for(db, "dk1") is None


def _conn(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    import os
    conn.execute("ATTACH DATABASE ? AS cold", (os.environ["LIBRARY_COLD_DB"],))
    return conn


# --- store.cold_db_available, the service's own startup line -----------------


def test_cold_db_available_is_true_against_a_real_file(db):
    ok, why = store.cold_db_available()
    assert ok is True
    assert why == "ready"


def test_cold_db_available_is_false_when_the_file_is_missing(db, monkeypatch):
    monkeypatch.setenv("LIBRARY_COLD_DB", str(db.parent / "no-such-cold.db"))
    ok, why = store.cold_db_available()
    assert ok is False
    assert why == "the cold tier's database is missing or could not be opened"


def test_cold_db_available_is_false_when_the_variable_is_unset(db, monkeypatch):
    monkeypatch.delenv("LIBRARY_COLD_DB")
    ok, why = store.cold_db_available()
    assert ok is False
    assert why == "the cold tier could not be opened"
