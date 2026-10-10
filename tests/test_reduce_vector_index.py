"""`reduce_vector_index.py`: the one-time removal of `library_vectors` rows
for an Archives signal whose kind `vectors.EMBEDDED_SIGNAL_KINDS`
(convivy/library#177) does not name.

**The fixture inserts vector rows directly, bypassing `vectors.insert_vectors`
— that is the point.** #177 already stops a not-embedded kind from ever
reaching the model, so the only way a `library_vectors` row exists for one is
the shape this script targets: written before that gate did, by the old,
ungated embed path. `_insert_vector_row` reproduces exactly that row, the same
way `test_split_vectors.py` seeds its source tables directly rather than
through the write routes.
"""
from __future__ import annotations

import sqlite3
import struct

import pytest

import conftest
import reduce_vector_index
import store
import vectors

sqlite_vec = pytest.importorskip("sqlite_vec")


@pytest.fixture
def db(tmp_path, monkeypatch, vector_index):
    path = tmp_path / "control.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE signals (
            id TEXT PRIMARY KEY, source TEXT, stream TEXT, kind TEXT, actor TEXT,
            ingested_by TEXT, product TEXT, refs TEXT, body TEXT, meta TEXT,
            privacy_class TEXT, produced_at TEXT, ingested_at TEXT,
            embedded_at TEXT, dedup_key TEXT UNIQUE, acting_human TEXT);
    """)
    conn.commit()
    conn.close()
    vector_index(path)
    monkeypatch.setattr(store, "DB_PATH", path)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    return path


def _signal(db_path, signal_id, kind):
    """A signal row, already embedded — `embedded_at` set, the same as every
    row this script's target vectors were written for, since they predate
    #177's gate."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO signals (id, product, kind, body, privacy_class, "
        "ingested_at, embedded_at) VALUES (?,?,?,?,'internal',?,?)",
        (signal_id, "vivy", kind, f"{kind} body",
         "2026-08-01 11:00:00.000000", "2026-08-01 11:00:01.000000"))
    conn.commit()
    conn.close()


def _vec(dominant: int) -> bytes:
    values = [0.0] * vectors.EMBED_DIM
    values[dominant % vectors.EMBED_DIM] = 1.0
    return struct.pack(f"{vectors.EMBED_DIM}f", *values)


def _insert_vector_row(signal_id: str, embedding: bytes | int) -> None:
    if isinstance(embedding, int):
        embedding = _vec(embedding)
    conn = sqlite3.connect(vectors.VECTORS_DB_PATH)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute(
        f"INSERT INTO {vectors.VEC_TABLE}(embedding, vault, item_id) "
        f"VALUES (?, 'archives', ?)", (embedding, signal_id))
    conn.commit()
    conn.close()


def _connect_rw():
    conn = vectors.connect_rw()
    store.attach_cold(conn, readonly=False)
    return conn


def _cold_signal(signal_id, kind):
    """A signal the cold-tier mover has already moved: its row is in the
    cold file only, and its vectors stay in `library_vectors`."""
    import os
    conn = sqlite3.connect(os.environ["LIBRARY_COLD_DB"])
    conn.execute(
        "INSERT INTO signals (id, product, kind, body, privacy_class, "
        "ingested_at, embedded_at) VALUES (?,?,?,?,'internal',?,?)",
        (signal_id, "vivy", kind, f"{kind} body",
         "2026-08-01 11:00:00.000000", "2026-08-01 11:00:01.000000"))
    conn.commit()
    conn.close()


def _vector_rows(signal_id):
    return conftest.vectors_for(None, signal_id)


def _embedded_at(db_path, signal_id):
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT embedded_at FROM signals WHERE id = ?",
                       (signal_id,)).fetchone()
    conn.close()
    return row[0] if row else None


# --- the reduction itself ----------------------------------------------------------


def test_removes_not_embedded_and_unnamed_kinds_only(db):
    """`finding` is a named embedded kind, `review_verdict` a named
    not-embedded one, and `some-new-kind` unnamed by the decision at all —
    the Defaults section's own "unnamed defaults to not embedded"."""
    _signal(db, "sig_kept", "finding")
    _signal(db, "sig_dropped", "review_verdict")
    _signal(db, "sig_unnamed", "some-new-kind")
    for sid in ("sig_kept", "sig_dropped", "sig_unnamed"):
        _insert_vector_row(sid, hash(sid) % 997)

    conn = _connect_rw()
    try:
        deleted = reduce_vector_index.reduce_index(conn)
    finally:
        conn.close()

    assert deleted == 2
    assert len(_vector_rows("sig_kept")) == 1
    assert _vector_rows("sig_dropped") == []
    assert _vector_rows("sig_unnamed") == []


def test_a_cold_signals_vectors_are_reduced_too(db):
    """The cold-tier mover leaves vectors in place, so a not-embedded kind
    whose signal is already cold still has rows here to remove, and an
    embedded cold signal keeps its rows."""
    _cold_signal("sig_cold_dropped", "review_verdict")
    _cold_signal("sig_cold_kept", "finding")
    _insert_vector_row("sig_cold_dropped", 1)
    _insert_vector_row("sig_cold_kept", 2)

    conn = _connect_rw()
    try:
        before = reduce_vector_index._kind_report(conn)
        deleted = reduce_vector_index.reduce_index(conn)
    finally:
        conn.close()

    assert before["review_verdict"] == {"signals": 1, "vectors": 1}
    assert deleted == 1
    assert _vector_rows("sig_cold_dropped") == []
    assert len(_vector_rows("sig_cold_kept")) == 1


def test_a_signal_in_both_tiers_is_counted_and_reduced_once(db):
    """The transient two-copy state a crash between the cold mover's two
    transactions leaves: the same id in hot and cold. `signals_source()`'s
    de-dup arm keeps it from doubling the report or the deleted count."""
    _signal(db, "sig_both", "review_verdict")
    _cold_signal("sig_both", "review_verdict")
    _insert_vector_row("sig_both", 1)

    conn = _connect_rw()
    try:
        before = reduce_vector_index._kind_report(conn)
        deleted = reduce_vector_index.reduce_index(conn)
    finally:
        conn.close()

    assert before["review_verdict"] == {"signals": 1, "vectors": 1}
    assert deleted == 1
    assert _vector_rows("sig_both") == []


def test_an_unset_cold_db_exits_2_naming_it(db, monkeypatch, capsys):
    monkeypatch.delenv("LIBRARY_COLD_DB", raising=False)

    assert reduce_vector_index.main(["--dry-run"]) == 2
    assert "LIBRARY_COLD_DB" in capsys.readouterr().err


def test_every_signal_row_and_its_stamp_survive_untouched(db):
    _signal(db, "sig_kept", "finding")
    _signal(db, "sig_dropped", "review_verdict")
    _insert_vector_row("sig_kept", 1)
    _insert_vector_row("sig_dropped", 2)

    conn = _connect_rw()
    try:
        reduce_vector_index.reduce_index(conn)
    finally:
        conn.close()

    assert _embedded_at(db, "sig_kept") == "2026-08-01 11:00:01.000000"
    assert _embedded_at(db, "sig_dropped") == "2026-08-01 11:00:01.000000"
    signal_conn = sqlite3.connect(db)
    ids = {r[0] for r in signal_conn.execute("SELECT id FROM signals")}
    signal_conn.close()
    assert ids == {"sig_kept", "sig_dropped"}


def test_the_sweep_does_not_re_embed_what_this_removed(db, monkeypatch):
    """`embed_missing` selects `WHERE embedded_at IS NULL` — this script
    never touches that column, so a row it reduces must stay invisible to
    the sweep."""
    _signal(db, "sig_dropped", "review_verdict")
    _insert_vector_row("sig_dropped", 1)
    calls = []
    monkeypatch.setattr(
        vectors, "_embedder",
        lambda texts: calls.append(list(texts)) or [conftest.fake_vector(t) for t in texts])

    conn = _connect_rw()
    try:
        reduce_vector_index.reduce_index(conn)
    finally:
        conn.close()

    assert vectors.embed_missing(vault=vectors.VAULT_ARCHIVES) == 0
    assert calls == [], "a row already stamped must never reach the model again"


def test_semantic_search_still_finds_the_embedded_signal(db):
    _signal(db, "sig_kept", "finding")
    _signal(db, "sig_dropped", "review_verdict")
    query = _vec(42)
    _insert_vector_row("sig_kept", query)
    _insert_vector_row("sig_dropped", 1)

    conn = _connect_rw()
    try:
        reduce_vector_index.reduce_index(conn)
        hits = vectors.nearest(conn, query, 5, vectors.VAULT_ARCHIVES)
    finally:
        conn.close()

    assert hits
    assert hits[0][0] == "sig_kept"
    assert hits[0][1] == pytest.approx(0.0, abs=1e-4)


# --- dry run and resumability -------------------------------------------------------


def test_dry_run_writes_nothing(db, capsys):
    _signal(db, "sig_dropped", "review_verdict")
    _insert_vector_row("sig_dropped", 1)

    rc = reduce_vector_index.main(["--dry-run"])

    assert rc == 0
    assert _vector_rows("sig_dropped") != []
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "review_verdict" in out


def test_a_rerun_after_completion_deletes_nothing(db, capsys):
    _signal(db, "sig_dropped", "review_verdict")
    _insert_vector_row("sig_dropped", 1)

    assert reduce_vector_index.main([]) == 0
    assert "deleted 1 row" in capsys.readouterr().out

    assert reduce_vector_index.main([]) == 0
    assert "deleted 0 row" in capsys.readouterr().out


def test_an_interrupted_run_is_finished_by_a_rerun(db):
    for i in range(5):
        _signal(db, f"sig_{i}", "review_verdict")
        _insert_vector_row(f"sig_{i}", i)

    conn = _connect_rw()
    try:
        first = reduce_vector_index.reduce_index(conn, batch_size=1, max_batches=2)
    finally:
        conn.close()
    assert first == 2
    assert sum(1 for i in range(5) if _vector_rows(f"sig_{i}")) == 3

    conn = _connect_rw()
    try:
        rest = reduce_vector_index.reduce_index(conn, batch_size=1)
    finally:
        conn.close()
    assert rest == 3
    assert all(_vector_rows(f"sig_{i}") == [] for i in range(5))


def test_a_concurrent_writer_to_signals_raises_no_lock_error(db):
    """The reduction never writes to `main` — its only writes land in the
    attached vectors file — so a writer holding an open transaction on
    `signals` must not be able to make it raise `database is locked`."""
    _signal(db, "sig_dropped", "review_verdict")
    _insert_vector_row("sig_dropped", 1)

    setup = sqlite3.connect(db)
    setup.execute("PRAGMA journal_mode=WAL")
    setup.close()

    writer = sqlite3.connect(db, timeout=1)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute(
        "INSERT INTO signals (id, product, kind, body, privacy_class, "
        "ingested_at, embedded_at) VALUES ('sig_concurrent','vivy','finding',"
        "'x','internal','2026-08-01 12:00:00.000000',NULL)")
    try:
        conn = _connect_rw()
        try:
            deleted = reduce_vector_index.reduce_index(conn)
        finally:
            conn.close()
    finally:
        writer.commit()
        writer.close()

    assert deleted == 1
