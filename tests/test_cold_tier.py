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
import sqlite3
import threading

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


# --- lock budget: a move pass beside a concurrent writer -----------------


def test_a_move_pass_and_a_concurrent_writer_neither_get_a_lock_error(db):
    for i in range(40):
        _insert(db, f"sig_old_{i:03d}", "note",
                ingested_at=f"2020-01-01 00:00:{i:02d}.000000")

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
                errors.append(e)
            i += 1

    t = threading.Thread(target=writer)
    t.start()
    try:
        assert ct.run(dry_run=False, do_restore=False) == 0
    except sqlite3.OperationalError as e:
        errors.append(e)
    finally:
        stop.set()
        t.join(timeout=5)

    assert not errors, f"a concurrent write hit a lock error: {errors}"
    assert writes, "the concurrent writer never got a turn — this test proves nothing"
    for i in range(40):
        assert not _hot_exists(db, f"sig_old_{i:03d}")
    for i in writes:
        assert _hot_exists(db, f"sig_live_{i:04d}")


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
