"""The vault split: the guard, the seam, and the proof the index survived.

Three properties carry the risk here and the rest of the file is them.

**The guard is the one that has already failed in the real world.** A wrong
environment variable name once migrated the live control DB while its operator
believed a dry run was running, so "it refuses the live database" is asserted
from both directions rather than assumed from reading the code.

**The seam has to account for every table.** A migration that skips a table it
does not recognise leaves it behind at the cutover, silently, and the source is a
live schema that grows. So an unknown table is an error, not a shrug.

**A vector index that did not survive the move still counts correctly.** Copying
vec0's shadow tables as ordinary tables produces a database that opens, reports
every expected row, and answers a search with nonsense. Every count-based check
passes. That is why `verify` compares every stored vector by bytes, keyed by
rowid rather than by an item id several chunks share, and why the tests for it
plant the broken migration rather than trusting that the good one works.
"""
from __future__ import annotations

import sqlite3
import struct

import pytest

import migrate

sqlite_vec = pytest.importorskip("sqlite_vec")

DIM = 8


def _vec(seed: float) -> bytes:
    return struct.pack(f"{DIM}f", *[seed + i / 100 for i in range(DIM)])


def _conn(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


@pytest.fixture
def source(tmp_path, monkeypatch):
    """A miniature control DB: one table from each side of the seam, plus both
    vec0 indexes in their real, DIFFERENT shapes."""
    path = tmp_path / "control.db"
    conn = _conn(path)
    for t in migrate.MOVES:
        conn.execute(f"CREATE TABLE {t} (id TEXT PRIMARY KEY, note TEXT, "
                     f"root_id TEXT, released_at TEXT)")
        conn.executemany(f"INSERT INTO {t} (id, note, root_id) VALUES (?,?,?)",
                         [(f"{t}-{i}", f"row {i}", f"root-{i}") for i in range(3)])
    # **THE INDEXES ARE PART OF THE FIXTURE BECAUSE THE COPY LOOP HAD NO TEST**
    # (reviewer, PR #22). Without one, an index-matching regression passed the
    # whole suite — and the index it dropped was the partial unique one below,
    # which is not a performance hint but the one-live-lease-per-root rule.
    conn.execute("CREATE UNIQUE INDEX ix_issue_leases_live_root "
                 "ON issue_leases (root_id) WHERE released_at IS NULL")
    conn.execute("CREATE INDEX ix_issue_items_note ON issue_items (note)")
    # Bracket-quoted, which is what the old substring match silently missed.
    conn.execute("CREATE INDEX ix_signals_note ON [signals] (note)")
    for t in migrate.STAYS:
        conn.execute(f"CREATE TABLE {t} (id TEXT PRIMARY KEY, note TEXT)")
        conn.execute(f"INSERT INTO {t} VALUES ('stays-1', 'n')")
    conn.execute("CREATE INDEX ix_reading_rooms_note ON reading_rooms (note)")
    conn.execute(f"CREATE VIRTUAL TABLE library_vectors USING "
                 f"vec0(embedding float[{DIM}], vault text, +item_id text)")
    conn.execute(f"CREATE VIRTUAL TABLE signal_vectors USING "
                 f"vec0(embedding float[{DIM}], +signal_id text)")
    # **ONE ITEM, SEVERAL CHUNKS, SHARING AN `item_id`** — the real shape, and the
    # one a fixture of unique ids cannot test. `insert_vectors` writes a row per
    # chunk and a full-size item chunks to 78 of them. A comparison keyed on
    # `item_id` collapses them all onto the last, so corrupting an earlier chunk
    # is invisible (reviewer, PR #22).
    conn.executemany(
        "INSERT INTO library_vectors(embedding, vault, item_id) VALUES (?,?,?)",
        [(_vec(i), "chart_house", f"item-{i}") for i in range(12)]
        + [(_vec(100 + c), "chart_house", "item-chunked") for c in range(3)])
    conn.executemany(
        "INSERT INTO signal_vectors(embedding, signal_id) VALUES (?,?)",
        [(_vec(i + 0.5), f"sig-{i}") for i in range(9)])
    conn.commit()
    conn.close()
    # The live path points somewhere else entirely, so the guard is exercised
    # against a real difference rather than against a stub that always passes.
    monkeypatch.setenv("ORIENT_CONTROL_DB", str(tmp_path / "not-this-one.db"))
    return path


# --- the guard ------------------------------------------------------------------


def test_the_live_database_is_refused_as_a_source(tmp_path, monkeypatch):
    """**THE FAILURE THIS PREVENTS HAS HAPPENED.** A dry run pointed at the live
    control DB by a wrong variable name migrated it for real."""
    live = tmp_path / "control.db"
    live.write_bytes(b"")
    monkeypatch.setenv("ORIENT_CONTROL_DB", str(live))
    with pytest.raises(migrate.RefusedError, match="IS the live control DB"):
        migrate.guard(live)


def test_a_hard_link_to_the_live_database_is_refused_too(tmp_path, monkeypatch):
    """The path comparison alone is not enough. A second name for the same inode
    passes it and writes to the same file, which is the whole point of the
    check."""
    live = tmp_path / "control.db"
    live.write_bytes(b"x")
    alias = tmp_path / "innocent-looking-copy.db"
    alias.hardlink_to(live)
    monkeypatch.setenv("ORIENT_CONTROL_DB", str(live))
    assert alias.resolve() != live.resolve(), "the paths must differ for this to bite"
    with pytest.raises(migrate.RefusedError, match="same inode"):
        migrate.guard(alias)


def test_a_real_cutover_can_say_so(tmp_path, monkeypatch):
    """The guard is a safety catch, not a wall: the cutover itself has to be able
    to run against the live database, and does so by saying it explicitly."""
    live = tmp_path / "control.db"
    live.write_bytes(b"")
    monkeypatch.setenv("ORIENT_CONTROL_DB", str(live))
    migrate.guard(live, allow_live=True)


def test_not_knowing_which_database_is_live_refuses(tmp_path, monkeypatch):
    """**A GUARD THAT GUESSES IS NOT A GUARD** (security-reviewer, PR #22). The
    first version fell back to `~/.orient-control/control.db` when
    `ORIENT_CONTROL_DB` was unset, so it compared the source against a path
    nobody had confirmed — and an operator who had simply not exported the
    variable could read the real live database with no refusal and no
    `--i-mean-it`. An inconclusive safety check fails closed."""
    monkeypatch.delenv("ORIENT_CONTROL_DB", raising=False)
    with pytest.raises(migrate.RefusedError, match="unset"):
        migrate.guard(tmp_path / "anything.db")
    # And a sanctioned cutover still gets through, since it named its intent.
    migrate.guard(tmp_path / "anything.db", allow_live=True)


def test_verify_guards_its_source_too(tmp_path, monkeypatch):
    """The guard lived in `split` alone, so anything calling `verify` directly
    ran full scans against whatever it was handed, live database included
    (security-reviewer, PR #22). A check covering half a module is not one."""
    live = tmp_path / "control.db"
    live.write_bytes(b"")
    monkeypatch.setenv("ORIENT_CONTROL_DB", str(live))
    with pytest.raises(migrate.RefusedError):
        migrate.verify(live, tmp_path / "dest.db")


def test_a_path_holding_a_question_mark_still_opens_read_only(tmp_path):
    """**A RAW PATH IN A SQLITE URI DEFEATS `mode=ro`** (security-reviewer,
    PR #22). SQLite splits the URI on the FIRST `?`, so a path containing one has
    the rest of itself swallowed as query string — `mode=ro` included — and the
    handle opens a DIFFERENT file, read-write, creating it if absent. The file
    `guard` approved and the file opened diverge, which is the one thing this
    module exists to prevent."""
    odd = tmp_path / "weird?dir"
    odd.mkdir()
    db = odd / "control.db"
    conn = _conn(db)
    conn.execute("CREATE TABLE t (id TEXT)")
    conn.commit()
    conn.close()

    ro = migrate.connect(db, readonly=True)
    try:
        # The right file, or this opened something else entirely.
        assert [r[0] for r in ro.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")] == ["t"]
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            ro.execute("CREATE TABLE proof (x)")
    finally:
        ro.close()
    assert not (tmp_path / "weird").exists(), "a stray file was created"


def test_a_path_holding_a_hash_is_handled_too(tmp_path):
    """`#` truncates a URI as a fragment, the same failure by another character."""
    odd = tmp_path / "with#hash"
    odd.mkdir()
    db = odd / "control.db"
    conn = _conn(db)
    conn.execute("CREATE TABLE t (id TEXT)")
    conn.commit()
    conn.close()
    ro = migrate.connect(db, readonly=True)
    try:
        assert [r[0] for r in ro.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")] == ["t"]
    finally:
        ro.close()


# --- the seam -------------------------------------------------------------------


def test_a_table_on_neither_side_of_the_seam_is_an_error(source, tmp_path):
    """**A table the split has never heard of is one that gets left behind.** The
    source is a live schema that grows, so silence here means the next table
    somebody adds vanishes at the cutover with nothing said."""
    conn = _conn(source)
    conn.execute("CREATE TABLE something_new (id TEXT)")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="something_new"):
        migrate.split(source, tmp_path / "library.db")


def test_vec0_shadow_tables_are_not_counted_as_unaccounted_for(source):
    """vec0 keeps its private layout in tables named after the virtual one.
    Treating those as strays would make every run fail on a healthy database."""
    conn = migrate.connect(source, readonly=True)
    try:
        assert migrate.unaccounted(conn) == []
    finally:
        conn.close()


def test_the_split_refuses_to_overwrite_an_existing_destination(source, tmp_path):
    dest = tmp_path / "library.db"
    dest.write_bytes(b"")
    with pytest.raises(FileExistsError):
        migrate.split(source, dest)


# --- the move itself ------------------------------------------------------------


def test_everything_that_moves_arrives_and_nothing_else_does(source, tmp_path):
    dest = tmp_path / "library.db"
    moved = migrate.split(source, dest)
    for t in migrate.MOVES:
        assert moved[t] == 3, t
    assert moved["library_vectors"] == 15   # 12 single-chunk items + 3 chunks of one
    assert moved["signal_vectors"] == 9
    assert migrate.verify(source, dest) == []

    present = {r[0] for r in sqlite3.connect(dest).execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for t in migrate.STAYS:
        assert t not in present, f"{t} should not have moved"


def test_the_two_indexes_are_migrated_on_their_own_shapes(source, tmp_path):
    """**They do not share a column list**, and a hardcoded one migrates
    `library_vectors` then raises `no such column: vault` on `signal_vectors`.
    That is how the difference was found, so it is pinned here."""
    dest = tmp_path / "library.db"
    migrate.split(source, dest)
    conn = migrate.connect(dest, readonly=True)
    try:
        lib = [r[1] for r in conn.execute("PRAGMA table_info(library_vectors)")]
        sig = [r[1] for r in conn.execute("PRAGMA table_info(signal_vectors)")]
        assert "vault" in lib and "item_id" in lib
        assert "vault" not in sig and "signal_id" in sig
    finally:
        conn.close()


def test_the_undeclared_signal_acks_reference_moves_with_the_signals(source, tmp_path):
    """`signal_acks.signal_id` points at `signals` with NO foreign key clause, so
    `PRAGMA foreign_key_list` reports a clean seam that is not one. Leaving it
    behind splits a relationship the schema never declared."""
    assert "signal_acks" in migrate.MOVES and "signals" in migrate.MOVES
    dest = tmp_path / "library.db"
    migrate.split(source, dest)
    n = sqlite3.connect(dest).execute("SELECT COUNT(*) FROM signal_acks").fetchone()[0]
    assert n == 3


def test_the_decision_plane_moves_because_adr_0033_supersedes_0022(source, tmp_path):
    """ADR-0022 left the decision plane with Orient; ADR-0033 overrides that and
    ADR-0033 records the ruling as (the operator, 2026-08-09) and the operator confirmed it again
    in session while this was being written. Pinned because the superseded text
    still reads plausibly."""
    for t in ("decision_items", "decision_comments", "decision_events"):
        assert t in migrate.MOVES
    dest = tmp_path / "library.db"
    migrate.split(source, dest)
    present = {r[0] for r in sqlite3.connect(dest).execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"decision_items", "decision_comments", "decision_events"} <= present


# --- the proof the index survived -----------------------------------------------


def test_verify_catches_an_index_that_counts_right_and_searches_wrong(source, tmp_path):
    """**This is the test that justifies `verify` existing.** A destination whose vectors
    are all present but wrong passes every count and fails every search. Built by
    migrating properly and then overwriting the embeddings, which is what a
    shadow-table copy amounts to from the caller's side.
    """
    dest = tmp_path / "library.db"
    migrate.split(source, dest)
    assert migrate.verify(source, dest) == [], "the clean migration must pass first"

    conn = _conn(dest)
    rows = conn.execute("SELECT rowid FROM library_vectors").fetchall()
    conn.executemany("UPDATE library_vectors SET embedding = ? WHERE rowid = ?",
                     [(_vec(900 + i), r["rowid"]) for i, r in enumerate(rows)])
    conn.commit()
    conn.close()

    problems = migrate.verify(source, dest)
    assert problems, "corrupted vectors must be caught"
    assert any("vectors differ from the source" in p for p in problems)
    # And the counts still agree, which is the whole point.
    assert not any("count mismatch" in p for p in problems)


def test_every_index_on_a_moved_table_arrives(source, tmp_path):
    """**A DROPPED INDEX HERE IS A LOST INVARIANT, not a slower query.**
    `ix_issue_leases_live_root` is the partial unique index that enforces one
    live lease per root. The first version matched `" ON <table>"` in the index
    SQL, so a bracket-quoted name was silently skipped and `verify` still
    returned a clean pass (reviewer, PR #22). `tbl_name` answers exactly."""
    dest = tmp_path / "library.db"
    migrate.split(source, dest)
    got = {r[0] for r in sqlite3.connect(dest).execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL")}
    assert "ix_issue_leases_live_root" in got
    assert "ix_issue_items_note" in got
    assert "ix_signals_note" in got, "a bracket-quoted table name must still match"
    assert "ix_reading_rooms_note" not in got, "an index on a STAYS table must not move"
    assert migrate.verify(source, dest) == []


def test_verify_catches_a_missing_index(source, tmp_path):
    """The clean pass the old bug produced is the thing to prevent, so `verify`
    checks the indexes rather than trusting `split` to have copied them."""
    dest = tmp_path / "library.db"
    migrate.split(source, dest)
    conn = sqlite3.connect(dest)
    conn.execute("DROP INDEX ix_issue_leases_live_root")
    conn.commit()
    conn.close()
    problems = migrate.verify(source, dest)
    assert any("ix_issue_leases_live_root did not arrive" in p for p in problems)


def test_verify_catches_content_that_changed_without_the_count_changing(
        source, tmp_path):
    """The right-count-wrong-data gap, on the ordinary tables this time. The
    vector tables get KNN probes for it; these get a whole-table hash."""
    dest = tmp_path / "library.db"
    migrate.split(source, dest)
    assert migrate.verify(source, dest) == []
    conn = sqlite3.connect(dest)
    conn.execute("UPDATE decision_items SET note = 'tampered' WHERE id = ?",
                 ("decision_items-0",))
    conn.commit()
    conn.close()
    problems = migrate.verify(source, dest)
    assert any("content differs in decision_items" in p for p in problems)
    assert not any("count mismatch" in p for p in problems)


def test_corrupting_a_non_last_chunk_of_one_item_is_caught(source, tmp_path):
    """**THE TEST THE EXHAUSTIVENESS CLAIM ACTUALLY RESTS ON** (reviewer,
    PR #22). An item is stored as one row per chunk, all sharing its `item_id`,
    so a comparison keyed on that id keeps only the last chunk from each side and
    silently agrees about the rest. Corrupting chunk 0 of 3 passed the check that
    called itself exhaustive; most of the corruption surface was still riding on
    the KNN draw it was meant to have replaced. Keyed by `rowid` it is caught.

    The corrupted chunk is deliberately NOT the last one, because the last one is
    the only chunk the broken version compared.
    """
    dest = tmp_path / "library.db"
    migrate.split(source, dest)
    assert migrate.verify(source, dest, probes=0) == []

    conn = _conn(dest)
    rowids = [r["rowid"] for r in conn.execute(
        "SELECT rowid FROM library_vectors WHERE item_id = 'item-chunked' "
        "ORDER BY rowid")]
    assert len(rowids) == 3, "the fixture must hold several chunks under one id"
    conn.execute("UPDATE library_vectors SET embedding = ? WHERE rowid = ?",
                 (_vec(777), rowids[0]))
    conn.commit()
    conn.close()

    # probes=0 so this cannot pass by accident through the KNN spot check.
    problems = migrate.verify(source, dest, probes=0)
    assert any("vectors differ from the source" in p for p in problems), problems


def test_two_chunks_of_one_item_swapped_is_caught(source, tmp_path):
    """**THIS IS THE TEST THAT PINS THE MAPPING RATHER THAN THE DETECTION**
    (reviewer, PR #22). The corruption test above introduces a value that was
    never in the table, which a set comparison catches as readily as a
    rowid-keyed one — so it does not justify the choice actually made. Swapping
    two chunks under one `item_id` changes no value and no count, only which row
    holds which vector, and a set comparison sees nothing. That is a plausible
    index-writeback bug, and positional identity is the only thing that catches
    it.
    """
    dest = tmp_path / "library.db"
    migrate.split(source, dest)
    assert migrate.verify(source, dest, probes=0) == []

    conn = _conn(dest)
    rows = conn.execute(
        "SELECT rowid, embedding FROM library_vectors WHERE item_id = "
        "'item-chunked' ORDER BY rowid").fetchall()
    assert len(rows) == 3
    first, second = rows[0], rows[1]
    conn.execute("UPDATE library_vectors SET embedding = ? WHERE rowid = ?",
                 (second["embedding"], first["rowid"]))
    conn.execute("UPDATE library_vectors SET embedding = ? WHERE rowid = ?",
                 (first["embedding"], second["rowid"]))
    conn.commit()
    conn.close()

    problems = migrate.verify(source, dest, probes=0)
    assert any("vectors differ from the source" in p for p in problems), problems


def test_the_digest_covers_a_table_with_no_rowid(tmp_path, monkeypatch):
    """`_digest` falls back to ordering by every column when a table has no
    `rowid`, and that branch had no coverage at all — a broken return value in it
    left the whole suite green (reviewer, PR #22). It is the content check for
    any moved table declared `WITHOUT ROWID`, so an untested branch there is an
    acceptance test that quietly does nothing."""
    db = tmp_path / "wr.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (id TEXT PRIMARY KEY, note TEXT) WITHOUT ROWID")
    conn.executemany("INSERT INTO t VALUES (?,?)", [("a", "one"), ("b", "two")])
    conn.commit()

    same = tmp_path / "wr2.db"
    other = sqlite3.connect(same)
    other.execute("CREATE TABLE t (id TEXT PRIMARY KEY, note TEXT) WITHOUT ROWID")
    other.executemany("INSERT INTO t VALUES (?,?)", [("b", "two"), ("a", "one")])
    other.commit()

    # Inserted in a different order, so only a deterministic ordering agrees.
    assert migrate._digest(conn, "t") == migrate._digest(other, "t")

    other.execute("UPDATE t SET note = 'changed' WHERE id = 'a'")
    other.commit()
    assert migrate._digest(conn, "t") != migrate._digest(other, "t")
    conn.close()
    other.close()


def test_verify_reports_a_table_that_leaked_across_the_seam(source, tmp_path):
    dest = tmp_path / "library.db"
    migrate.split(source, dest)
    conn = sqlite3.connect(dest)
    conn.execute("CREATE TABLE push_subscriptions (id TEXT)")
    conn.commit()
    conn.close()
    assert any("push_subscriptions leaked" in p for p in migrate.verify(source, dest))
