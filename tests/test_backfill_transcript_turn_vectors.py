"""`backfill_transcript_turn_vectors.py`: the one-time repair for `transcript-turn`
signals that were stamped while the kind was not embedded and so hold no
`library_vectors` rows.

**Hot rows** get their `embedded_at` cleared, and the ordinary sweep then embeds
them. **Cold rows** get vectors written directly, since the sweep cannot reach the
cold tier, and their stamp is left alone.

The fixture seeds one row for each case the selection has to tell apart, in both
tiers. Each test below pins one of those cases.
"""
from __future__ import annotations

import sqlite3
import struct

import pytest

import backfill_transcript_turn_vectors as bf
import cold_tier as ct
import conftest
import store
import vectors

sqlite_vec = pytest.importorskip("sqlite_vec")

STAMP = "2026-09-30 10:00:01.000000"
COLD_STAMP = "2026-09-01 10:00:01.000000"

SCHEMA = """
    CREATE TABLE signals (
        id TEXT PRIMARY KEY, source TEXT, stream TEXT, kind TEXT, actor TEXT,
        ingested_by TEXT, product TEXT, refs TEXT, body TEXT, meta TEXT,
        privacy_class TEXT, produced_at TEXT, ingested_at TEXT,
        embedded_at TEXT, dedup_key TEXT UNIQUE, acting_human TEXT);
    CREATE TABLE cold_dedup_keys (
        dedup_key TEXT PRIMARY KEY, signal_id TEXT NOT NULL);
"""


@pytest.fixture
def db(tmp_path, monkeypatch, vector_index, cold_db):
    path = tmp_path / "control.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    vector_index(path)
    cold_db(SCHEMA)
    monkeypatch.setattr(store, "DB_PATH", path)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    return path


def _insert(path, signal_id, kind, *, ingested_at, embedded_at, body=None):
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO signals (id, source, kind, body, meta, privacy_class, "
        "ingested_at, embedded_at) VALUES (?,?,?,?,'{}','internal',?,?)",
        (signal_id, "test", kind, body or f"{kind} body", ingested_at,
         embedded_at))
    conn.commit()
    conn.close()


def _add_vector(item_id):
    """One `library_vectors` row for an Archives item, written directly: the
    backfill only reads this table, so the row's shape is all that matters."""
    conn = sqlite3.connect(vectors.VECTORS_DB_PATH)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    blob = struct.pack(f"{vectors.EMBED_DIM}f",
                       *([1.0] + [0.0] * (vectors.EMBED_DIM - 1)))
    conn.execute(
        f"INSERT INTO {vectors.VEC_TABLE}(embedding, vault, item_id) "
        f"VALUES (?, ?, ?)", (blob, vectors.VAULT_ARCHIVES, item_id))
    conn.commit()
    conn.close()


def _stamp(path, signal_id):
    conn = sqlite3.connect(path)
    row = conn.execute("SELECT embedded_at FROM signals WHERE id = ?",
                       (signal_id,)).fetchone()
    conn.close()
    return row[0]


def _cold_path():
    return store.resolve_cold_db_path()


def _candidates():
    """Both tiers' candidate lists, read the way the script reads them."""
    conn = bf._open(dry_run=True)
    try:
        vectored = bf._vectored_ids(conn)
        hot = bf.find_hot_candidates(conn, vectored)
        cold = [r["id"] for r in bf.find_cold_candidates(conn, vectored)]
        return hot, cold
    finally:
        conn.close()


@pytest.fixture
def seeded(db):
    # Hot A: stamped turn, no vectors. Selected.
    _insert(db, "turn_old", "transcript-turn",
            ingested_at="2026-09-30 10:00:00", embedded_at=STAMP)
    # Hot B: stamped turn that already has vectors. Not selected.
    _insert(db, "turn_vectored", "transcript-turn",
            ingested_at="2026-09-30 11:00:00", embedded_at=STAMP)
    _add_vector("turn_vectored")
    # Hot C: turn never stamped, so the ordinary sweep already owns it. Not selected.
    _insert(db, "turn_unstamped", "transcript-turn",
            ingested_at="2026-09-30 12:00:00", embedded_at=None)
    # Hot D: stamped alert with no vectors, a kind that stays not embedded. Not
    # selected.
    _insert(db, "alert_stamped", "alert",
            ingested_at="2026-09-30 13:00:00", embedded_at=STAMP)
    # Hot E: a second stamped turn with no vectors, ingested later. Selected.
    _insert(db, "turn_new", "transcript-turn",
            ingested_at="2026-10-01 10:00:00", embedded_at=STAMP)
    # Hot F: stamped turn with no vectors, whose cold copy is also present (a mover
    # pass caught between its two transactions). The hot leg takes it.
    _insert(db, "turn_both", "transcript-turn",
            ingested_at="2026-09-29 10:00:00", embedded_at=STAMP)

    # Cold G: stamped turn with no vectors, and no hot copy. Selected for the cold leg.
    _insert(_cold_path(), "cold_old", "transcript-turn",
            ingested_at="2026-08-01 10:00:00", embedded_at=COLD_STAMP)
    # Cold H: stamped turn that already has vectors. Not selected.
    _insert(_cold_path(), "cold_vectored", "transcript-turn",
            ingested_at="2026-08-02 10:00:00", embedded_at=COLD_STAMP)
    _add_vector("cold_vectored")
    # Cold I: stamped alert with no vectors. Not selected.
    _insert(_cold_path(), "cold_alert", "alert",
            ingested_at="2026-08-03 10:00:00", embedded_at=COLD_STAMP)
    # Cold J: turn with a NULL stamp and no vectors, the shape a clear that races
    # the mover leaves behind. Selected, and stamped by the cold leg.
    _insert(_cold_path(), "cold_unstamped", "transcript-turn",
            ingested_at="2026-08-04 10:00:00", embedded_at=None)
    # Cold copy of the hot F row. Not selected: the hot copy owns it.
    _insert(_cold_path(), "turn_both", "transcript-turn",
            ingested_at="2026-09-29 10:00:00", embedded_at=STAMP)
    return db


def test_selects_the_stamped_hot_turns_that_hold_no_vectors(seeded):
    hot, _ = _candidates()
    assert hot == ["turn_both", "turn_old", "turn_new"]


def test_a_stamped_hot_row_of_another_kind_is_not_selected(seeded):
    hot, _ = _candidates()
    assert "alert_stamped" not in hot


def test_a_hot_turn_that_already_has_vectors_is_not_selected(seeded):
    hot, _ = _candidates()
    assert "turn_vectored" not in hot


def test_a_hot_turn_with_a_null_stamp_is_not_selected(seeded):
    hot, _ = _candidates()
    assert "turn_unstamped" not in hot


def test_selects_every_cold_turn_that_holds_no_vectors_stamped_or_not(seeded):
    _, cold = _candidates()
    assert cold == ["cold_old", "cold_unstamped"]


def test_a_cold_turn_with_a_hot_copy_is_left_to_the_hot_leg(seeded):
    _, cold = _candidates()
    assert "turn_both" not in cold


def test_a_dry_run_reports_both_counts_and_writes_nothing(seeded, capsys):
    assert bf.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "3 hot and 2 cold transcript-turn signal(s) hold no vectors" in out
    assert "DRY RUN" in out
    assert _stamp(seeded, "turn_old") == STAMP
    assert _stamp(_cold_path(), "cold_old") == COLD_STAMP
    assert conftest.vectors_for(seeded, "cold_old") == []


def test_a_dry_run_counts_a_cleared_hot_turn_the_sweep_has_not_embedded(seeded, capsys):
    # The clear runs and the sweep does not, so the cleared turns sit at NULL with no
    # vectors. The stamped-only counts read 0 here, which is the gap the drain missed.
    assert bf.main([]) == 0
    capsys.readouterr()
    assert bf.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "0 hot and 0 cold" in out
    assert "4 unstamped hot transcript-turn signal(s) hold no vectors" in out


def test_the_dry_run_reads_zero_unstamped_once_the_sweep_has_drained(seeded, capsys):
    bf.main([])
    vectors.embed_missing(vault=vectors.VAULT_ARCHIVES)
    capsys.readouterr()
    assert bf.main(["--dry-run"]) == 0
    assert "0 unstamped hot transcript-turn signal(s) hold no vectors" in capsys.readouterr().out


def test_a_real_run_clears_the_hot_candidates_and_nothing_else(seeded):
    assert bf.main([]) == 0
    assert _stamp(seeded, "turn_old") is None
    assert _stamp(seeded, "turn_new") is None
    assert _stamp(seeded, "turn_both") is None
    assert _stamp(seeded, "turn_vectored") == STAMP
    assert _stamp(seeded, "turn_unstamped") is None
    assert _stamp(seeded, "alert_stamped") == STAMP


def test_a_real_run_writes_cold_vectors_and_keeps_the_cold_stamp(seeded):
    assert bf.main([]) == 0
    assert len(conftest.vectors_for(seeded, "cold_old")) == 1
    assert _stamp(_cold_path(), "cold_old") == COLD_STAMP
    assert conftest.vectors_for(seeded, "cold_alert") == []


def test_the_sweep_embeds_the_cleared_hot_turns(seeded):
    bf.main([])
    vectors.embed_missing(vault=vectors.VAULT_ARCHIVES)
    assert len(conftest.vectors_for(seeded, "turn_old")) == 1
    assert len(conftest.vectors_for(seeded, "turn_new")) == 1
    assert conftest.vectors_for(seeded, "alert_stamped") == []


def test_a_rerun_is_a_no_op(seeded, capsys):
    bf.main([])
    capsys.readouterr()
    assert bf.main([]) == 0
    assert "0 hot and 0 cold" in capsys.readouterr().out
    assert _stamp(seeded, "turn_old") is None
    assert len(conftest.vectors_for(seeded, "cold_old")) == 1


def test_a_rerun_after_the_sweep_drains_is_a_no_op(seeded):
    bf.main([])
    vectors.embed_missing(vault=vectors.VAULT_ARCHIVES)
    assert bf.main([]) == 0
    assert len(conftest.vectors_for(seeded, "turn_old")) == 1
    assert len(conftest.vectors_for(seeded, "cold_old")) == 1


def test_a_run_spanning_several_hot_batches_clears_every_candidate(seeded, monkeypatch):
    monkeypatch.setattr(bf, "BATCH_SIZE", 1)
    monkeypatch.setattr(bf, "BATCH_SLEEP_SECONDS", 0)
    assert bf.main([]) == 0
    assert _stamp(seeded, "turn_old") is None
    assert _stamp(seeded, "turn_new") is None
    assert _stamp(seeded, "turn_both") is None


def test_a_cold_run_spanning_several_groups_writes_every_candidate(seeded, monkeypatch):
    _insert(_cold_path(), "cold_second", "transcript-turn",
            ingested_at="2026-08-05 10:00:00", embedded_at=COLD_STAMP)
    monkeypatch.setattr(vectors, "MAX_CHUNKS_PER_TXN", 1)
    assert bf.main([]) == 0
    assert len(conftest.vectors_for(seeded, "cold_old")) == 1
    assert len(conftest.vectors_for(seeded, "cold_second")) == 1


def test_writes_disabled_refuses_with_exit_2_and_writes_nothing(seeded, monkeypatch):
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    assert bf.main([]) == 2
    assert _stamp(seeded, "turn_old") == STAMP
    assert conftest.vectors_for(seeded, "cold_old") == []


def test_a_missing_hot_database_refuses_with_exit_2_on_a_dry_run(tmp_path, monkeypatch, db):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "absent.db")
    assert bf.main(["--dry-run"]) == 2


def test_a_missing_cold_database_refuses_with_exit_2(db, monkeypatch, tmp_path):
    monkeypatch.setenv("LIBRARY_COLD_DB", str(tmp_path / "absent-cold.db"))
    assert bf.main(["--dry-run"]) == 2
    assert bf.main([]) == 2


def test_a_cold_turn_with_no_stamp_gets_its_vectors_and_a_stamp(seeded):
    bf.main([])
    assert len(conftest.vectors_for(seeded, "cold_unstamped")) == 1
    assert _stamp(_cold_path(), "cold_unstamped") is not None
    assert _stamp(_cold_path(), "cold_old") == COLD_STAMP


def test_a_clear_between_the_movers_select_and_insert_leaves_a_turn_the_backfill_embeds(
        seeded, monkeypatch):
    _insert(seeded, "turn_moved", "transcript-turn",
            ingested_at="2020-01-01 10:00:00", embedded_at=STAMP)
    real_move_batch = ct._move_batch

    def clear_then_move(conn, ids):
        # The mover has selected turn_moved. The backfill's clear commits before the
        # mover's INSERT copies the row, so the cold copy takes a NULL stamp.
        bf.clear_stamps(conn, ["turn_moved"])
        real_move_batch(conn, ids)

    monkeypatch.setattr(ct, "_move_batch", clear_then_move)
    conn = bf._open(dry_run=False)
    try:
        ct.move(conn, dry_run=False)
    finally:
        conn.close()
    assert _stamp(_cold_path(), "turn_moved") is None

    _, cold = _candidates()
    assert "turn_moved" in cold
    assert bf.main([]) == 0
    assert len(conftest.vectors_for(seeded, "turn_moved")) == 1


def test_a_turn_the_mover_takes_before_its_clear_is_not_counted_and_is_embedded_by_the_next_run(
        seeded, monkeypatch, capsys):
    real_clear = bf.clear_stamps

    def move_then_clear(conn, ids):
        # The mover takes turn_old out of hot after the backfill chose it and before
        # the clear reaches it, so the clear's UPDATE matches no row for it.
        ct._move_batch(conn, ["turn_old"])
        return real_clear(conn, ids)

    monkeypatch.setattr(bf, "clear_stamps", move_then_clear)
    assert bf.main([]) == 0
    assert ("cleared 2 hot stamp(s) and wrote vectors for 2 cold signal(s)"
            in capsys.readouterr().out)
    assert _stamp(_cold_path(), "turn_old") == STAMP
    assert conftest.vectors_for(seeded, "turn_old") == []

    hot, cold = _candidates()
    assert hot == []
    assert cold == ["turn_old"]

    assert bf.main([]) == 0
    assert len(conftest.vectors_for(seeded, "turn_old")) == 1
    assert _candidates() == ([], [])


def test_a_cold_group_skips_an_item_another_run_embedded_after_the_snapshot(seeded):
    conn = bf._open(dry_run=False)
    try:
        cold = bf.find_cold_candidates(conn, bf._vectored_ids(conn))
        _add_vector("cold_old")  # another run commits cold_old's vectors meanwhile
        assert bf.embed_cold(conn, cold) == 1
    finally:
        conn.close()
    assert len(conftest.vectors_for(seeded, "cold_old")) == 1
    assert len(conftest.vectors_for(seeded, "cold_unstamped")) == 1


def test_cold_groups_are_paced_as_hot_batches_are(seeded, monkeypatch):
    _insert(_cold_path(), "cold_second", "transcript-turn",
            ingested_at="2026-08-05 10:00:00", embedded_at=COLD_STAMP)
    monkeypatch.setattr(vectors, "MAX_CHUNKS_PER_TXN", 1)
    monkeypatch.setattr(bf, "BATCH_SLEEP_SECONDS", 0.5)
    pauses = []
    monkeypatch.setattr(bf.time, "sleep", pauses.append)
    assert bf.main([]) == 0
    # Three cold groups of one item each, so two pauses between them. The three hot
    # candidates fit one clear batch, which pauses not at all.
    assert pauses == [0.5, 0.5]


def test_a_cold_group_reads_bodies_as_it_is_built_not_all_at_once(seeded, monkeypatch):
    _insert(_cold_path(), "cold_second", "transcript-turn",
            ingested_at="2026-08-05 10:00:00", embedded_at=COLD_STAMP)
    monkeypatch.setattr(vectors, "MAX_CHUNKS_PER_TXN", 1)
    body_reads = []
    model_calls = []
    real_embed = vectors.embed_texts

    def counting_embed(texts):
        model_calls.append(len(body_reads))
        return real_embed(texts)

    monkeypatch.setattr(vectors, "embed_texts", counting_embed)
    conn = bf._open(dry_run=False)
    try:
        conn.set_trace_callback(
            lambda sql: body_reads.append(sql)
            if sql.startswith("SELECT id, kind, stream, body FROM cold.signals") else None)
        cold = bf.find_cold_candidates(conn, bf._vectored_ids(conn))
        assert bf.embed_cold(conn, cold) == 3
    finally:
        conn.close()
    # The first model call happens before the last body is read. Reading every body
    # up front would make every call see all three.
    assert model_calls[0] < 3


def test_a_multi_chunk_cold_turn_gets_one_vector_per_chunk_and_its_neighbours_keep_theirs(
        seeded):
    long_body = "alpha beta " * (vectors.CHUNK_CHARS // 4)
    expected = len(vectors.chunks(long_body))
    assert expected > 1
    _insert(_cold_path(), "cold_long", "transcript-turn",
            ingested_at="2026-08-05 10:00:00", embedded_at=COLD_STAMP, body=long_body)
    assert bf.main([]) == 0
    assert len(conftest.vectors_for(seeded, "cold_long")) == expected
    assert len(conftest.vectors_for(seeded, "cold_old")) == 1
    assert len(conftest.vectors_for(seeded, "cold_unstamped")) == 1


def test_an_unset_cold_database_refuses_with_exit_2_and_names_the_variable(
        seeded, monkeypatch, capsys):
    monkeypatch.delenv("LIBRARY_COLD_DB")
    assert bf.main(["--dry-run"]) == 2
    assert "LIBRARY_COLD_DB is not set" in capsys.readouterr().err
    assert bf.main([]) == 2
    assert _stamp(seeded, "turn_old") == STAMP


def test_a_missing_hot_database_refuses_with_exit_2_on_a_real_run(
        db, monkeypatch, tmp_path):
    absent = tmp_path / "absent.db"
    monkeypatch.setattr(store, "DB_PATH", absent)
    assert bf.main([]) == 2
    assert not absent.exists()
