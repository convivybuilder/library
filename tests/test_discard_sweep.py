"""discard_sweep: a discarded kind's signal row, vector rows and unreferenced
blob go together; a referenced signal (or a shared blob) is kept whatever its
own kind's rule says; a dry run writes nothing; an unruled kind is reported,
never touched."""
from __future__ import annotations

import json
import os
import sqlite3
import struct
import subprocess
import sys
import threading
import traceback
from pathlib import Path

import pytest

import blobs
import discard_sweep as ds
import store
import vectors

REPO = Path(__file__).resolve().parent.parent

SCHEMA = """
CREATE TABLE signals (
    id TEXT PRIMARY KEY, source TEXT, stream TEXT, kind TEXT, actor TEXT,
    ingested_by TEXT, product TEXT, refs TEXT, body TEXT, meta TEXT,
    privacy_class TEXT, produced_at TEXT, ingested_at TEXT,
    embedded_at TEXT, dedup_key TEXT UNIQUE, acting_human TEXT);
"""

# `cold_dedup_keys` lives in `main` only — `init_cold.py` never replays it
# into the cold file, since it replays `main.signals`' own DDL alone — so
# this is appended for the main database's own schema and never passed to
# `cold_db`.
MAIN_SCHEMA = SCHEMA + """
CREATE TABLE cold_dedup_keys (
    dedup_key TEXT PRIMARY KEY, signal_id TEXT NOT NULL);
"""


@pytest.fixture
def db(tmp_path, monkeypatch, vector_index, cold_db):
    path = tmp_path / "library.db"
    conn = sqlite3.connect(path)
    conn.executescript(MAIN_SCHEMA)
    conn.commit()
    conn.close()
    vector_index(path)
    # Same shape as `main.signals` — the real split replays `main`'s own
    # DDL verbatim (`init_cold.py`); this suite's hand-written schema is
    # already one definition to keep in step with `SCHEMA` above, and a
    # second, independent copy would be a second one.
    cold_db(SCHEMA)
    monkeypatch.setattr(store, "DB_PATH", path)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    monkeypatch.setattr(blobs, "BLOB_DIR", tmp_path / "blobs")
    return path


def _cold_signal_exists(sig_id: str) -> bool:
    conn = sqlite3.connect(os.environ["LIBRARY_COLD_DB"])
    row = conn.execute("SELECT 1 FROM signals WHERE id = ?", (sig_id,)).fetchone()
    conn.close()
    return row is not None


def _insert_cold_signal(sig_id, kind, *, refs=None, meta=None,
                        ingested_at="2026-09-01 00:00:00.000000",
                        dedup_key=None):
    """A row that moved cold BEFORE its kind was ever marked discarded —
    the case row 10 of the build plan's read-path table exists for."""
    conn = sqlite3.connect(os.environ["LIBRARY_COLD_DB"])
    conn.execute(
        "INSERT INTO signals (id, source, kind, actor, ingested_by, product, "
        "refs, body, meta, privacy_class, ingested_at, embedded_at, dedup_key) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sig_id, "test", kind, "someone", "someone", "vivy",
         json.dumps(refs or {}), "body", json.dumps(meta or {}), "internal",
         ingested_at, ingested_at, dedup_key))
    conn.commit()
    conn.close()


def _insert_signal(path, sig_id, kind, *, refs=None, meta=None,
                    ingested_at="2026-09-01 00:00:00.000000"):
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO signals (id, source, kind, actor, ingested_by, product, "
        "refs, body, meta, privacy_class, ingested_at) VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?)",
        (sig_id, "test", kind, "someone", "someone", "vivy",
         json.dumps(refs or {}), "body", json.dumps(meta or {}), "internal",
         ingested_at))
    conn.commit()
    conn.close()


def _insert_vector(sig_id):
    sqlite_vec = pytest.importorskip("sqlite_vec")
    vconn = sqlite3.connect(vectors.VECTORS_DB_PATH)
    vconn.enable_load_extension(True)
    sqlite_vec.load(vconn)
    vconn.enable_load_extension(False)
    vconn.execute(
        f"INSERT INTO {vectors.VEC_TABLE}(embedding, vault, item_id) "
        "VALUES (?,?,?)",
        (struct.pack(f"{vectors.EMBED_DIM}f", *([0.0] * vectors.EMBED_DIM)),
         vectors.VAULT_ARCHIVES, sig_id))
    vconn.commit()
    vconn.close()


def _vector_count(sig_id):
    sqlite_vec = pytest.importorskip("sqlite_vec")
    vconn = sqlite3.connect(vectors.VECTORS_DB_PATH)
    vconn.enable_load_extension(True)
    sqlite_vec.load(vconn)
    vconn.enable_load_extension(False)
    n = vconn.execute(
        f"SELECT COUNT(*) FROM {vectors.VEC_TABLE} WHERE item_id = ?",
        (sig_id,)).fetchone()[0]
    vconn.close()
    return n


def _signal_exists(path, sig_id):
    conn = sqlite3.connect(path)
    row = conn.execute("SELECT 1 FROM signals WHERE id = ?", (sig_id,)).fetchone()
    conn.close()
    return row is not None


# The sweep's retry helpers sit between a call site and the `conn.execute` that
# raised, so `_labelled` skips them and names the call site instead.
_RETRY_HELPERS = frozenset({"_begin_immediate", "_execute_retrying_busy"})


def _labelled(side, e):
    """`side` names which thread raised. The frame is the innermost one outside
    the retry helpers, so a lock error names the call site that issued the
    refused statement (for example `_sweep_kind:315` or `_prune...:478`), not
    the helper that retried it."""
    frames = [f for f in traceback.extract_tb(e.__traceback__)
              if f.name not in _RETRY_HELPERS]
    frame = frames[-1]
    return f"{side} {frame.name}:{frame.lineno}: {e}"


def _signal_count(path):
    conn = sqlite3.connect(path)
    n = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    conn.close()
    return n


def _reinsert(dedup_key, kind="junk"):
    """A fresh capture of the same `dedup_key`, through the real write path
    — the build plan's §5 assertion that a freed key mints a new hot signal
    rather than `insert_signal` finding a stale registry entry and refusing
    or, worse, returning a row that no longer exists."""
    row = {
        "id": f"sig_reinsert_{dedup_key}", "source": "test", "kind": kind,
        "actor": "someone", "ingested_by": "someone", "product": "vivy",
        "stream": None, "body": "a fresh capture", "refs": "{}", "meta": "{}",
        "privacy_class": "internal", "dedup_key": dedup_key,
        "produced_at": None, "acting_human": None,
    }
    return store.insert_signal(row, lambda _existing: True)


# --- the three kind lists ----------------------------------------------------


def test_the_three_kind_sets_are_disjoint():
    assert not (vectors.EMBEDDED_SIGNAL_KINDS & vectors.KEPT_NOT_EMBEDDED_SIGNAL_KINDS)
    assert not (vectors.EMBEDDED_SIGNAL_KINDS & vectors.DISCARDED_SIGNAL_KINDS)
    assert not (vectors.KEPT_NOT_EMBEDDED_SIGNAL_KINDS & vectors.DISCARDED_SIGNAL_KINDS)


def test_todays_discarded_set_is_empty():
    """The decision names no kind as discarded today (its own Discarded
    section); this is the fact the rest of this file's "does nothing today"
    behaviour rests on."""
    assert vectors.DISCARDED_SIGNAL_KINDS == frozenset()


# --- a discarded-kind signal: row, vectors and blob removed together ----------


def test_a_discarded_kind_signal_is_removed_with_its_vectors_and_blob(
        db, monkeypatch):
    monkeypatch.setattr(vectors, "DISCARDED_SIGNAL_KINDS", frozenset({"junk"}))
    ref = blobs.write_blob(b"disposable content")
    _insert_signal(db, "sig_a", "junk", meta={"blob_ref": ref})
    _insert_vector("sig_a")

    assert ds.run(dry_run=False) == 0

    assert not _signal_exists(db, "sig_a")
    assert _vector_count("sig_a") == 0
    with pytest.raises(FileNotFoundError):
        blobs.read_blob(ref)


def test_a_referenced_signal_is_kept_with_its_vectors(db, monkeypatch):
    monkeypatch.setattr(vectors, "DISCARDED_SIGNAL_KINDS", frozenset({"junk"}))
    _insert_signal(db, "sig_a", "junk")
    _insert_vector("sig_a")
    _insert_signal(db, "sig_b", "note", refs={"target": "sig_a"})

    assert ds.run(dry_run=False) == 0

    assert _signal_exists(db, "sig_a")
    assert _vector_count("sig_a") == 1


def test_a_blob_another_remaining_record_still_references_is_kept(
        db, monkeypatch):
    monkeypatch.setattr(vectors, "DISCARDED_SIGNAL_KINDS", frozenset({"junk"}))
    ref = blobs.write_blob(b"shared content")
    _insert_signal(db, "sig_a", "junk", meta={"blob_ref": ref})
    _insert_signal(db, "sig_c", "note", meta={"blob_ref": ref})

    assert ds.run(dry_run=False) == 0

    assert not _signal_exists(db, "sig_a")
    assert _signal_exists(db, "sig_c")
    assert blobs.read_blob(ref) == b"shared content"


# --- dry run writes nothing ---------------------------------------------------


def test_dry_run_writes_nothing(db, monkeypatch, capsys):
    monkeypatch.setattr(vectors, "DISCARDED_SIGNAL_KINDS", frozenset({"junk"}))
    ref = blobs.write_blob(b"disposable content")
    _insert_signal(db, "sig_a", "junk", meta={"blob_ref": ref})
    _insert_vector("sig_a")
    before_signals = _signal_count(db)
    before_vectors = _vector_count("sig_a")

    assert ds.run(dry_run=True) == 0

    assert _signal_count(db) == before_signals
    assert _vector_count("sig_a") == before_vectors
    assert blobs.read_blob(ref) == b"disposable content"
    out = capsys.readouterr().out
    assert "would remove 1 signal(s)" in out
    assert "would remove 1 blob(s)" in out


# --- the empty set removes nothing, and unruled kinds are reported -----------


def test_the_real_empty_set_removes_nothing_and_reports_unruled_kinds(
        db, capsys):
    _insert_signal(db, "sig_mystery", "mystery-kind")

    assert ds.run(dry_run=False) == 0

    assert _signal_exists(db, "sig_mystery")
    err = capsys.readouterr().err
    assert "mystery-kind: 1 signal(s)" in err


def test_an_unruled_kind_is_reported_on_a_dry_run_too(db, capsys):
    _insert_signal(db, "sig_mystery", "mystery-kind")

    assert ds.run(dry_run=True) == 0

    assert _signal_exists(db, "sig_mystery")
    err = capsys.readouterr().err
    assert "mystery-kind: 1 signal(s)" in err


def test_a_ruled_kind_is_never_reported_as_unruled(db, capsys):
    _insert_signal(db, "sig_a", "pr_comment")  # embedded
    _insert_signal(db, "sig_b", "pr_state")    # kept, not embedded

    ds.run(dry_run=False)

    err = capsys.readouterr().err
    assert "pr_comment" not in err
    assert "pr_state" not in err


# --- concurrency: a real sweep pass beside a concurrent writer ---------------


def test_a_sweep_pass_and_a_concurrent_writer_neither_get_a_lock_error(
        db, monkeypatch):
    monkeypatch.setattr(vectors, "DISCARDED_SIGNAL_KINDS", frozenset({"junk"}))
    monkeypatch.setattr(ds, "SWEEP_BATCH_SIZE", 5)
    for i in range(40):
        _insert_signal(db, f"sig_junk_{i:03d}", "junk",
                        ingested_at=f"2026-09-01 00:00:{i:02d}.000000")

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
                         "2026-09-02 00:00:00.000000"))
                    conn.commit()
                finally:
                    conn.close()
                writes.append(i)
            except sqlite3.OperationalError as e:
                errors.append(_labelled("writer", e))
            i += 1

    t = threading.Thread(target=writer)
    t.start()
    try:
        assert ds.run(dry_run=False) == 0
    except sqlite3.OperationalError as e:
        errors.append(_labelled("sweep", e))
    finally:
        stop.set()
        t.join(timeout=5)

    assert not errors, f"a concurrent write hit a lock error: {errors}"
    assert writes, "the concurrent writer never got a turn — this test proves nothing"
    for i in range(40):
        assert not _signal_exists(db, f"sig_junk_{i:03d}")
    for i in writes:
        assert _signal_exists(db, f"sig_live_{i:04d}")


def _watch_refused(monkeypatch, on_refused, prefix="BEGIN IMMEDIATE"):
    """Route every `sqlite3.connect` in the test through a Connection subclass
    that calls `on_refused()` each time a statement starting with `prefix` on it
    is refused. The refusal is SQLite's own, so the handler sees a real
    `SQLITE_BUSY`."""
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


def test_a_refused_begin_immediate_is_retried_and_the_pass_completes(
        db, monkeypatch):
    """`_begin_immediate` retries a refused lock. A holder keeps the write lock
    until the sweep's first attempt is refused, and releases it from inside the
    refusal handler, so the second attempt is the one that takes the lock."""
    monkeypatch.setattr(vectors, "DISCARDED_SIGNAL_KINDS", frozenset({"junk"}))
    monkeypatch.setattr(store, "BUSY_TIMEOUT_MS", 50)
    _insert_signal(db, "sig_a", "junk")
    holder = sqlite3.connect(db, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    refusals = []

    def release_on_first_refusal():
        refusals.append(1)
        if len(refusals) == 1:
            holder.execute("COMMIT")

    _watch_refused(monkeypatch, release_on_first_refusal)
    try:
        assert ds.run(dry_run=False) == 0
    finally:
        holder.close()

    assert len(refusals) == 1
    assert not _signal_exists(db, "sig_a")


def test_a_begin_immediate_refused_on_every_attempt_fails_the_pass_and_writes_nothing(
        db, monkeypatch):
    """A lock that never frees fails the pass after three tries, and the batch
    has written nothing by then: the row and its vectors remain."""
    monkeypatch.setattr(vectors, "DISCARDED_SIGNAL_KINDS", frozenset({"junk"}))
    monkeypatch.setattr(store, "BUSY_TIMEOUT_MS", 50)
    _insert_signal(db, "sig_a", "junk")
    _insert_vector("sig_a")
    holder = sqlite3.connect(db, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    refusals = []
    _watch_refused(monkeypatch, lambda: refusals.append(1))
    try:
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            ds.run(dry_run=False)
    finally:
        holder.execute("COMMIT")
        holder.close()

    # Pinned as a literal, not `ds.BEGIN_ATTEMPTS`: the pass's worst-case wait
    # is this count times `BUSY_TIMEOUT_MS` per `BEGIN`, so lowering the
    # constant must fail here rather than pass by agreeing with itself.
    assert len(refusals) == 3
    assert _signal_exists(db, "sig_a")
    assert _vector_count("sig_a") == 1


def test_a_refused_hot_delete_begin_immediate_is_retried_and_the_pass_completes(
        db, monkeypatch):
    """The second `BEGIN IMMEDIATE`, the one that opens the hot delete, is
    retried too. A holder takes the write lock only after transaction A has
    committed, so the sweep's first `BEGIN` is never refused and the first
    refusal is the hot-delete `BEGIN`. The refusal handler releases the holder,
    and the retry takes the lock and removes the row."""
    monkeypatch.setattr(vectors, "DISCARDED_SIGNAL_KINDS", frozenset({"junk"}))
    monkeypatch.setattr(store, "BUSY_TIMEOUT_MS", 50)
    _insert_signal(db, "sig_a", "junk")
    holder = sqlite3.connect(db, isolation_level=None)
    commits = 0
    refused_after_commits = []

    class _Holding(sqlite3.Connection):
        def commit(self):
            nonlocal commits
            super().commit()
            commits += 1
            if commits == 1:
                holder.execute("BEGIN IMMEDIATE")

        def execute(self, sql, *args, **kwargs):
            try:
                return super().execute(sql, *args, **kwargs)
            except sqlite3.OperationalError:
                if sql == "BEGIN IMMEDIATE":
                    refused_after_commits.append(commits)
                    if len(refused_after_commits) == 1:
                        holder.execute("COMMIT")
                raise

    real_sqlite_connect = sqlite3.connect

    def holding_connect(*args, **kwargs):
        kwargs.setdefault("factory", _Holding)
        return real_sqlite_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", holding_connect)
    try:
        assert ds.run(dry_run=False) == 0
    finally:
        holder.close()

    assert refused_after_commits == [1], (
        "the first refusal must come after transaction A's commit, at the "
        f"hot-delete BEGIN; it came after commits={refused_after_commits}")
    assert not _signal_exists(db, "sig_a")


def test_a_refused_prune_delete_is_retried_and_the_prune_completes(
        db, monkeypatch, capsys):
    """The end-of-pass registry prune retries its `DELETE` on a refused write
    lock. No kind is discarded here, so the prune's `DELETE` is the only
    write-lock attempt in the pass. A holder keeps the `main` lock until that
    first attempt is refused, then releases it from inside the refusal handler,
    and the retry removes the dangling entry."""
    monkeypatch.setattr(store, "BUSY_TIMEOUT_MS", 50)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO cold_dedup_keys (dedup_key, signal_id) "
                "VALUES (?, ?)", ("dk-orphan", "sig_long_gone"))
    conn.commit()
    conn.close()
    holder = sqlite3.connect(db, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    refusals = []

    def release_on_first_refusal():
        refusals.append(1)
        if len(refusals) == 1:
            holder.execute("COMMIT")

    _watch_refused(monkeypatch, release_on_first_refusal,
                   prefix="DELETE FROM cold_dedup_keys")
    try:
        assert ds.run(dry_run=False) == 0
    finally:
        holder.close()

    assert len(refusals) == 1
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT 1 FROM cold_dedup_keys WHERE dedup_key = ?",
                       ("dk-orphan",)).fetchone()
    conn.close()
    assert row is None
    assert "registry: pruned 1 stale cold_dedup_keys entry(ies)" in capsys.readouterr().out


def test_the_prune_completes_while_another_connection_holds_only_the_cold_write_lock(
        db, monkeypatch, capsys):
    """The prune writes `main` alone, so a writer holding `cold`'s write lock
    must not stall it. A `BEGIN IMMEDIATE` there would take `cold`'s lock too,
    since the prune's subquery reads `cold` and `cold` is attached, and a lock
    held past the retries would fail the pass after its batches had committed.
    The holder's own write to `cold` is refused first, as a control that the
    lock is really held."""
    monkeypatch.setattr(store, "BUSY_TIMEOUT_MS", 50)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO cold_dedup_keys (dedup_key, signal_id) "
                "VALUES (?, ?)", ("dk-orphan", "sig_long_gone"))
    conn.commit()
    conn.close()
    cold_path = os.environ["LIBRARY_COLD_DB"]
    holder = sqlite3.connect(cold_path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        control = sqlite3.connect(cold_path, timeout=0.05)
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            control.execute("INSERT INTO signals (id) VALUES ('sig_control')")
        control.close()
        assert ds.run(dry_run=False) == 0
    finally:
        holder.close()

    conn = sqlite3.connect(db)
    row = conn.execute("SELECT 1 FROM cold_dedup_keys WHERE dedup_key = ?",
                       ("dk-orphan",)).fetchone()
    conn.close()
    assert row is None
    assert "registry: pruned 1 stale cold_dedup_keys entry(ies)" in capsys.readouterr().out


def test_each_batch_commits_separately_with_no_transaction_left_open(
        db, monkeypatch):
    """A direct assertion on transaction boundaries, not merely an absence
    of lock errors: `store.BUSY_TIMEOUT_MS` (5s) absorbs enough contention
    that a regression collapsing every batch into one long transaction would
    still pass the lock-error test above. Verified by hand (reviewer, PR
    #180): temporarily moving `_sweep_kind`'s `BEGIN IMMEDIATE`/`commit()`
    pair outside its `while` loop, so the whole kind swept in one
    transaction, makes `commit_count` read 1 instead of 5 and fails this
    test — reverted before committing, not shipped."""
    monkeypatch.setattr(vectors, "DISCARDED_SIGNAL_KINDS", frozenset({"junk"}))
    monkeypatch.setattr(ds, "SWEEP_BATCH_SIZE", 5)
    for i in range(23):
        _insert_signal(db, f"sig_{i:03d}", "junk",
                        ingested_at=f"2026-09-01 00:00:{i:02d}.000000")

    # `sqlite3.Connection` is an immutable C type — an instance's `commit`
    # cannot be monkeypatched directly — so a subclass installed as the
    # `factory` of the one `sqlite3.connect` call `store.connect_rw` makes is
    # what lets this record `in_transaction` right after every real commit.
    commits = []

    class _TrackingConnection(sqlite3.Connection):
        def commit(self):
            super().commit()
            commits.append(self.in_transaction)

    real_sqlite_connect = sqlite3.connect

    def tracking_connect(*args, **kwargs):
        kwargs.setdefault("factory", _TrackingConnection)
        return real_sqlite_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", tracking_connect)

    assert ds.run(dry_run=False) == 0

    # 23 signals in batches of 5: five batches (5, 5, 5, 5, 3), each now
    # ending TWO commits — transaction A (the cold delete, vectors and blob
    # quarantine) and transaction B (the hot delete and registry cleanup),
    # never one combined commit spanning both files (archives-cold-tier.md
    # §1 row 10) — so 10, plus one more for `sweep`'s own unscoped
    # end-of-pass `cold_dedup_keys` prune, run once per pass rather than
    # per batch: 11.
    assert len(commits) == 11, (
        f"expected 11 separate commits (2 per batch x 5 batches, plus the "
        f"pass's own end-of-pass prune), got {len(commits)}")
    assert not any(commits), (
        "a transaction was still open immediately after commit() returned — "
        "the sweep is no longer batching into separate transactions")


# --- the two-phase blob delete: quarantine, then resolve after a grace
# period (security-reviewer, PR #180) --------------------------------------


def test_an_unreferenced_quarantined_blob_is_finalized_past_its_grace(
        db, monkeypatch):
    """With the grace period at zero, one `ds.run` both quarantines the
    candidate and immediately resolves it, since nothing re-referenced it."""
    monkeypatch.setattr(vectors, "DISCARDED_SIGNAL_KINDS", frozenset({"junk"}))
    monkeypatch.setattr(blobs, "QUARANTINE_GRACE_SECONDS", 0)
    ref = blobs.write_blob(b"disposable content")
    _insert_signal(db, "sig_a", "junk", meta={"blob_ref": ref})

    assert ds.run(dry_run=False) == 0

    with pytest.raises(FileNotFoundError):
        blobs.read_blob(ref)
    assert not blobs._quarantine_path(ref).is_file()


def test_a_blob_still_in_its_grace_period_is_neither_removed_nor_finalized(
        db, monkeypatch):
    """The default grace period (not injected to zero here) leaves a freshly
    quarantined blob exactly where it is — recoverable, not yet unlinked."""
    monkeypatch.setattr(vectors, "DISCARDED_SIGNAL_KINDS", frozenset({"junk"}))
    ref = blobs.write_blob(b"disposable content")
    _insert_signal(db, "sig_a", "junk", meta={"blob_ref": ref})

    assert ds.run(dry_run=False) == 0

    # moved out of its content-addressed path (so `read_blob` sees it as
    # gone from there)...
    with pytest.raises(FileNotFoundError):
        blobs.read_blob(ref)
    # ...but still recoverable: the quarantine file itself still holds the
    # exact bytes, untouched because its grace period has not elapsed.
    assert blobs._quarantine_path(ref).read_bytes() == b"disposable content"


def test_a_writer_that_re_references_a_quarantined_blob_before_the_grace_recheck_keeps_it(
        db, monkeypatch):
    """The race the two-phase delete exists to close: a submission that
    calls `write_blob` for the SAME content this sweep just decided is
    unreferenced, and commits its own row naming that ref, before the grace
    re-check runs. The blob must still be readable afterward."""
    monkeypatch.setattr(vectors, "DISCARDED_SIGNAL_KINDS", frozenset({"junk"}))
    ref = blobs.write_blob(b"raced content")
    _insert_signal(db, "sig_a", "junk", meta={"blob_ref": ref})

    # First pass: quarantines the blob (grace not yet elapsed — the default
    # is well above zero), and its own resolve step leaves it pending.
    assert ds.run(dry_run=False) == 0
    assert not blobs._path(ref).is_file()
    assert blobs._quarantine_path(ref).is_file()

    # The race: a concurrent submission resends the identical bytes —
    # `write_blob`'s content-addressed rewrite finds nothing at `ref` (this
    # sweep moved it away) and writes a fresh copy — then its row lands,
    # naming the same ref, before this sweep's grace re-check runs.
    fresh_ref = blobs.write_blob(b"raced content")
    assert fresh_ref == ref
    _insert_signal(db, "sig_b", "note", meta={"blob_ref": ref})

    # Second pass, with the grace period injected to zero so the re-check
    # runs without a real sleep: the quarantined duplicate is dropped (a
    # fresh, byte-identical copy already sits at `ref`), and the blob stays
    # readable the whole time.
    monkeypatch.setattr(blobs, "QUARANTINE_GRACE_SECONDS", 0)
    assert ds.run(dry_run=False) == 0

    assert blobs.read_blob(ref) == b"raced content"
    assert not blobs._quarantine_path(ref).is_file()


def test_a_leftover_quarantine_file_from_an_interrupted_pass_is_resolved_by_the_next_pass(
        db, monkeypatch):
    """A quarantine file this run never created itself — standing in for
    one a prior pass left behind when it crashed between quarantining a
    blob and re-checking it — is found and finished by scanning the blob
    store's own shard directories, not by any record this sweep keeps
    across runs."""
    ref = blobs.write_blob(b"orphaned by a crashed pass")
    assert blobs.quarantine_blob(ref)
    assert blobs._quarantine_path(ref).is_file()

    monkeypatch.setattr(blobs, "QUARANTINE_GRACE_SECONDS", 0)
    assert ds.run(dry_run=False) == 0

    assert not blobs._quarantine_path(ref).is_file()
    with pytest.raises(FileNotFoundError):
        blobs.read_blob(ref)


def test_dry_run_does_not_touch_quarantine_state(db, monkeypatch, capsys):
    """`--dry-run` must still write nothing — including leaving an
    already-quarantined, grace-elapsed blob exactly where it is."""
    ref = blobs.write_blob(b"leftover")
    assert blobs.quarantine_blob(ref)
    monkeypatch.setattr(blobs, "QUARANTINE_GRACE_SECONDS", 0)

    assert ds.run(dry_run=True) == 0

    assert blobs._quarantine_path(ref).is_file()
    out = capsys.readouterr().out
    assert "would finalize 1 blob(s)" in out


# --- the quarantine rename runs inside the batch's own transaction, not
# after it (security-reviewer, PR #180 round 2) -------------------------------


def test_a_quarantine_failure_mid_batch_rolls_back_the_batch_and_the_next_pass_restores_the_already_quarantined_blob(
        db, monkeypatch):
    """The window security-reviewer flagged in round 2: an `OSError` from
    `blobs.quarantine_blob` on the SECOND ref of a batch must not leave the
    FIRST ref's already-renamed file orphaned. `_sweep_kind` rolls the whole
    batch's SQL transaction back on any such error and re-raises — so both
    signals and both signals' vectors are still present afterward, exactly
    as if the batch had never run — while the blob the batch DID manage to
    rename before the failure stays renamed (`os.rename` is a filesystem
    operation, not part of the SQL transaction, so the rollback cannot undo
    it). The next pass's `_resolve_quarantine` finds that leftover
    quarantine file, sees the signal that names it is back (its delete was
    rolled back), and restores it rather than finalizing it."""
    monkeypatch.setattr(vectors, "DISCARDED_SIGNAL_KINDS", frozenset({"junk"}))
    ref_a = blobs.write_blob(b"first blob")
    ref_b = blobs.write_blob(b"second blob")
    content_by_ref = {ref_a: b"first blob", ref_b: b"second blob"}
    _insert_signal(db, "sig_a", "junk", meta={"blob_ref": ref_a},
                    ingested_at="2026-09-01 00:00:01.000000")
    _insert_signal(db, "sig_b", "junk", meta={"blob_ref": ref_b},
                    ingested_at="2026-09-01 00:00:02.000000")
    _insert_vector("sig_a")
    _insert_vector("sig_b")

    real_quarantine = blobs.quarantine_blob
    calls = []

    def flaky_quarantine(ref):
        calls.append(ref)
        if len(calls) == 2:
            raise OSError("simulated disk hiccup")
        return real_quarantine(ref)

    monkeypatch.setattr(blobs, "quarantine_blob", flaky_quarantine)

    with pytest.raises(OSError):
        ds.run(dry_run=False)

    assert len(calls) == 2
    succeeded_ref, failed_ref = calls

    # rolled back: both rows and both vectors are still present
    assert _signal_exists(db, "sig_a")
    assert _signal_exists(db, "sig_b")
    assert _vector_count("sig_a") == 1
    assert _vector_count("sig_b") == 1
    # the first ref's rename already happened and is not undone by rollback
    assert not blobs._path(succeeded_ref).is_file()
    assert blobs._quarantine_path(succeeded_ref).is_file()
    # the second ref's rename never ran
    assert blobs._path(failed_ref).is_file()
    assert not blobs._quarantine_path(failed_ref).is_file()

    # the next pass: turn off the fault and the discarded-kind rule, so
    # nothing tries to remove sig_a or sig_b again, and only
    # `_resolve_quarantine` acts on the leftover quarantine file.
    monkeypatch.setattr(blobs, "quarantine_blob", real_quarantine)
    monkeypatch.setattr(vectors, "DISCARDED_SIGNAL_KINDS", frozenset())
    monkeypatch.setattr(blobs, "QUARANTINE_GRACE_SECONDS", 0)

    assert ds.run(dry_run=False) == 0

    assert blobs.read_blob(succeeded_ref) == content_by_ref[succeeded_ref]
    assert not blobs._quarantine_path(succeeded_ref).is_file()
    assert _signal_exists(db, "sig_a")
    assert _signal_exists(db, "sig_b")


def test_a_crash_after_a_batchs_commit_leaves_its_quarantined_blob_for_the_next_pass_to_resolve(
        db, monkeypatch):
    """Quarantining now happens inside the same transaction as the row
    delete, so a crash AFTER that transaction commits is ordinary: the row
    delete and the blob rename are durable together, and only
    `_resolve_quarantine` — run once at the end of `sweep()`, not inside
    `_sweep_kind` — is still pending. This exercises the real `_sweep_kind`
    commit path directly (unlike `test_a_leftover_quarantine_file_from_an_
    interrupted_pass_is_resolved_by_the_next_pass` above, which stands in
    for a crash by calling `blobs.quarantine_blob` directly rather than
    going through a real batch commit) and stops before `_resolve_quarantine`
    runs, to stand in for the crash, then confirms the next full pass
    resolves it."""
    monkeypatch.setattr(vectors, "DISCARDED_SIGNAL_KINDS", frozenset({"junk"}))
    ref = blobs.write_blob(b"disposable content")
    _insert_signal(db, "sig_a", "junk", meta={"blob_ref": ref})

    conn = vectors.connect_rw()
    try:
        report = ds._sweep_kind(conn, "junk", dry_run=False, removed_ids=set())
    finally:
        conn.close()

    assert report.removed == 1
    assert report.blobs_removed == 1
    assert not _signal_exists(db, "sig_a")
    # committed and quarantined together, durably — but not yet resolved,
    # standing in for a crash between this commit and `_resolve_quarantine`
    assert blobs._quarantine_path(ref).is_file()
    assert not blobs._path(ref).is_file()

    # the next pass resolves it: nothing references it, so it is finalized
    monkeypatch.setattr(blobs, "QUARANTINE_GRACE_SECONDS", 0)
    assert ds.run(dry_run=False) == 0

    assert not blobs._quarantine_path(ref).is_file()
    with pytest.raises(FileNotFoundError):
        blobs.read_blob(ref)


# --- paged by (ingested_at, id), not id alone (reviewer, PR #180 round 2) ----


def test_paging_resumes_correctly_across_a_batch_boundary_with_tied_ingested_at(
        db, monkeypatch):
    """The keyset cursor is `(ingested_at, id)`, not `id` alone — several
    signals sharing one `ingested_at` instant, split across a batch
    boundary, must each be swept exactly once, and a kept (referenced) row
    among them must not make the cursor loop forever on the same page."""
    monkeypatch.setattr(vectors, "DISCARDED_SIGNAL_KINDS", frozenset({"junk"}))
    monkeypatch.setattr(ds, "SWEEP_BATCH_SIZE", 3)
    same_instant = "2026-09-01 00:00:00.000000"
    for i in range(7):
        _insert_signal(db, f"sig_{i:03d}", "junk", ingested_at=same_instant)
    # referenced, and must be kept rather than removed, though it sits in
    # the middle of a batch of otherwise-removed, identically-timed rows
    _insert_signal(db, "sig_ref", "note", refs={"target": "sig_003"})

    assert ds.run(dry_run=False) == 0

    for i in range(7):
        if i == 3:
            assert _signal_exists(db, f"sig_{i:03d}")
        else:
            assert not _signal_exists(db, f"sig_{i:03d}")


def test_the_per_batch_query_does_not_sort_the_whole_kind(tmp_path):
    """`EXPLAIN QUERY PLAN` against the real, alembic-migrated schema, with
    a real cold file attached — the exact two-tier query `_sweep_kind` runs
    today, not the single-table form it ran before archives-cold-tier.md.
    `ix_signals_kind_ingested_at` (`kind`, `ingested_at`) is the index every
    other `kind`-filtered list route uses, and the HOT ARM must seek on it
    rather than pulling every row of the kind into a temp B-tree to sort by
    `id` first. A temp B-tree for only `id` — the tiebreak within one
    `ingested_at` instant — is fine; one for the whole `ORDER BY` is the
    regression this guards against."""
    db_path = tmp_path / "library.db"
    cold_path = tmp_path / "cold.db"
    env = {"PATH": "/usr/bin:/bin", "HOME": str(REPO),
           "LIBRARY_DB": str(db_path), "LIBRARY_COLD_DB": str(cold_path)}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(REPO), capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr
    result = subprocess.run(
        [sys.executable, "init_cold.py"], cwd=str(REPO),
        capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr

    conn = sqlite3.connect(db_path)
    conn.execute("ATTACH DATABASE ? AS cold", (str(cold_path),))
    source = store.signals_source("kind = ?")
    plan = conn.execute(
        f"EXPLAIN QUERY PLAN SELECT id, meta, ingested_at FROM {source} "
        "AS signals WHERE "
        "(ingested_at > ? OR (ingested_at = ? AND id > ?)) "
        "ORDER BY ingested_at, id LIMIT ?",
        ("junk", "junk", "", "", "", ds.SWEEP_BATCH_SIZE)).fetchall()
    conn.close()

    plan_text = " | ".join(row[3] for row in plan)
    assert "ix_signals_kind_ingested_at" in plan_text
    assert "SCAN signals" not in plan_text
    assert "TEMP B-TREE FOR ORDER BY" not in plan_text


# --- the cold tier: a discarded kind's row may already be cold
# (archives-cold-tier.md §1 row 10) ------------------------------------------


def test_a_discarded_kind_cold_resident_row_is_removed_with_its_registry_entry(
        db, monkeypatch):
    """A signal moved cold BEFORE its kind was ever discarded still gets
    swept: `_sweep_kind`'s candidate query reads both tiers, so the cold
    row is found, removed from `cold.signals`, and its `cold_dedup_keys`
    entry removed alongside it — never left stranded for the unscoped
    end-of-pass step to find."""
    monkeypatch.setattr(vectors, "DISCARDED_SIGNAL_KINDS", frozenset({"junk"}))
    _insert_cold_signal("sig_cold_junk", "junk", dedup_key="dk-cold-junk")
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO cold_dedup_keys (dedup_key, signal_id) "
                "VALUES (?, ?)", ("dk-cold-junk", "sig_cold_junk"))
    conn.commit()
    conn.close()

    assert ds.run(dry_run=False) == 0

    assert not _cold_signal_exists("sig_cold_junk")
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT 1 FROM cold_dedup_keys WHERE dedup_key = ?",
                       ("dk-cold-junk",)).fetchone()
    conn.close()
    assert row is None

    # archives-cold-tier.md §5: the freed key mints a new hot signal rather
    # than `insert_signal` finding a registry entry that no longer resolves.
    status, out = _reinsert("dk-cold-junk")
    assert status == 201, out
    assert _signal_exists(db, out["id"])


def test_a_cold_row_referenced_by_a_hot_signal_is_kept(db, monkeypatch):
    """A referenced signal is kept whatever its own rule says, cross-tier:
    a hot `note` referencing a cold `junk` row must keep that cold row,
    exactly as a hot reference keeps a hot row."""
    monkeypatch.setattr(vectors, "DISCARDED_SIGNAL_KINDS", frozenset({"junk"}))
    _insert_cold_signal("sig_cold_junk", "junk")
    _insert_signal(db, "sig_b", "note", refs={"target": "sig_cold_junk"})

    assert ds.run(dry_run=False) == 0

    assert _cold_signal_exists("sig_cold_junk")


def test_a_blob_a_cold_row_still_references_is_kept(db, monkeypatch):
    """One blob can back a hot row and a cold row at once
    (archives-cold-tier.md §3); discarding the hot row's kind must not
    quarantine a blob a surviving cold row still names."""
    monkeypatch.setattr(vectors, "DISCARDED_SIGNAL_KINDS", frozenset({"junk"}))
    ref = blobs.write_blob(b"shared across tiers")
    _insert_signal(db, "sig_a", "junk", meta={"blob_ref": ref})
    _insert_cold_signal("sig_cold_keep", "note", meta={"blob_ref": ref})

    assert ds.run(dry_run=False) == 0

    assert not _signal_exists(db, "sig_a")
    assert blobs.read_blob(ref) == b"shared across tiers"


def test_the_unscoped_end_of_pass_step_prunes_a_dangling_registry_entry(
        db, capsys):
    """The window a crash between `_sweep_kind`'s two transactions leaves:
    a `cold_dedup_keys` entry whose cold row is already gone. No kind is
    discarded here, so the per-kind loop never runs — only `sweep`'s own
    unscoped step can find this."""
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO cold_dedup_keys (dedup_key, signal_id) "
                "VALUES (?, ?)", ("dk-orphan", "sig_long_gone"))
    conn.commit()
    conn.close()

    assert ds.run(dry_run=False) == 0

    conn = sqlite3.connect(db)
    row = conn.execute("SELECT 1 FROM cold_dedup_keys WHERE dedup_key = ?",
                       ("dk-orphan",)).fetchone()
    conn.close()
    assert row is None
    out = capsys.readouterr().out
    assert "registry: pruned 1 stale cold_dedup_keys entry(ies)" in out

    # archives-cold-tier.md §5: the same assertion after the sweep's own
    # unscoped step — not only the per-kind loop's clean removal above —
    # clears the dangling entry the crash window between the cold delete and
    # the registry delete can leave.
    status, reinserted = _reinsert("dk-orphan")
    assert status == 201, reinserted
    assert _signal_exists(db, reinserted["id"])


def test_the_unscoped_end_of_pass_step_leaves_a_live_registry_entry_alone(
        db):
    """The other half of the same check: an entry whose cold row genuinely
    still exists must survive the prune."""
    _insert_cold_signal("sig_live_cold", "note", dedup_key="dk-live")
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO cold_dedup_keys (dedup_key, signal_id) "
                "VALUES (?, ?)", ("dk-live", "sig_live_cold"))
    conn.commit()
    conn.close()

    assert ds.run(dry_run=False) == 0

    conn = sqlite3.connect(db)
    row = conn.execute("SELECT 1 FROM cold_dedup_keys WHERE dedup_key = ?",
                       ("dk-live",)).fetchone()
    conn.close()
    assert row is not None


def test_a_dry_run_reports_the_prune_without_writing(db, capsys):
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO cold_dedup_keys (dedup_key, signal_id) "
                "VALUES (?, ?)", ("dk-orphan", "sig_long_gone"))
    conn.commit()
    conn.close()

    assert ds.run(dry_run=True) == 0

    conn = sqlite3.connect(db)
    row = conn.execute("SELECT 1 FROM cold_dedup_keys WHERE dedup_key = ?",
                       ("dk-orphan",)).fetchone()
    conn.close()
    assert row is not None, "a dry run must write nothing"
    out = capsys.readouterr().out
    assert "registry: would prune 1 stale cold_dedup_keys entry(ies)" in out
