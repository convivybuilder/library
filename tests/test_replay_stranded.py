"""The one-shot replay of Orient's stranded copy into the Library.

**The two guards are the load-bearing parts, for the reason `migrate.py`'s own
tests give: they are the ones that have already failed in the real world once
this class of script exists.** `guard_source` is what stood between an
unidentified file and being trusted as Orient's copy; `guard_dest` is what
stood between an unresolved environment and writing into whatever `resolve_db`
happened to guess. Both are asserted from both directions.

**A `once_key`/`dedup_key` collision is not the same failure as an id
collision, and the tests here hold that distinction** — the real run this
script was written for hit exactly one, on a genuinely different event that
happened to share a day-scoped key, and silently overwriting it or crashing
the whole run on it would each have been wrong in a different way.
"""
from __future__ import annotations

import sqlite3

import pytest

import replay_stranded as rs
import store

SCHEMA = """
CREATE TABLE issue_items (
    id TEXT PRIMARY KEY, product TEXT, title TEXT, once_key TEXT UNIQUE,
    embedded_at TEXT, created_at TEXT);
CREATE TABLE signals (
    id TEXT PRIMARY KEY, product TEXT, body TEXT,
    dedup_key TEXT UNIQUE, embedded_at TEXT, ingested_at TEXT);
CREATE TABLE decision_items (id TEXT PRIMARY KEY, product TEXT, body TEXT);
CREATE TABLE signal_acks (id TEXT PRIMARY KEY, signal_id TEXT, identity TEXT);
CREATE TABLE courses (id TEXT PRIMARY KEY, product TEXT, title TEXT);
CREATE TABLE issue_leases (
    id TEXT PRIMARY KEY, root_id TEXT, holder TEXT, released_at TEXT);
CREATE UNIQUE INDEX ix_issue_leases_live_root ON issue_leases (root_id)
    WHERE released_at IS NULL;
CREATE TABLE lease_refusals (id TEXT PRIMARY KEY, at TEXT);
CREATE TABLE course_events (id TEXT PRIMARY KEY, course_id TEXT, at TEXT);
CREATE TABLE issue_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT, actor TEXT,
    kind TEXT, body TEXT, at TEXT);
CREATE TABLE issue_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT, actor TEXT,
    from_state TEXT, to_state TEXT, note TEXT, at TEXT);
CREATE TABLE decision_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT, actor TEXT,
    from_state TEXT, to_state TEXT, directive TEXT, at TEXT);
"""

# The fingerprint `store.py` and `guard_source` both key on.
ORIENT_MARKER = "CREATE TABLE reading_rooms (id TEXT PRIMARY KEY)"


def _build(path, *, orient=False):
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    if orient:
        conn.execute(ORIENT_MARKER)
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def source_db(tmp_path):
    return _build(tmp_path / "control.db", orient=True)


@pytest.fixture
def dest_db(tmp_path):
    return _build(tmp_path / "library.db", orient=False)


def _conn(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


# --- guard_source: refuses anything that is not Orient's copy, by content ------


def test_guard_source_refuses_a_file_with_no_orient_fingerprint(dest_db):
    with pytest.raises(rs.RefusedError, match="none of"):
        rs.guard_source(dest_db)


def test_guard_source_refuses_a_missing_file(tmp_path):
    with pytest.raises(rs.RefusedError, match="no database"):
        rs.guard_source(tmp_path / "nope.db")


def test_guard_source_accepts_a_file_with_the_fingerprint(source_db):
    rs.guard_source(source_db)  # does not raise


# --- guard_dest: refuses anything resolve_db did not source from LIBRARY_DB ----


def test_guard_dest_refuses_when_resolve_db_is_not_library_db(monkeypatch, dest_db):
    monkeypatch.setattr(store, "resolve_db", lambda: (dest_db, "ORIENT_CONTROL_DB"))
    with pytest.raises(rs.RefusedError, match="not LIBRARY_DB"):
        rs.guard_dest()


def test_guard_dest_returns_the_path_when_it_is_library_db(monkeypatch, dest_db):
    monkeypatch.setattr(store, "resolve_db", lambda: (dest_db, "LIBRARY_DB"))
    assert rs.guard_dest() == dest_db


def test_guard_distinct_refuses_the_same_path(dest_db):
    with pytest.raises(rs.RefusedError, match="same path"):
        rs._guard_distinct(dest_db, dest_db)


def test_guard_distinct_accepts_two_different_files(source_db, dest_db):
    rs._guard_distinct(source_db, dest_db)  # does not raise


# --- stranded(): the measurement, matched by id or by content ------------------


def test_stranded_counts_by_id_and_by_content(source_db, dest_db):
    src, dst = _conn(source_db), _conn(dest_db)
    src.execute("INSERT INTO issue_items (id, product, title) VALUES "
               "('a', 'vivy', 'only in source')")
    src.execute("INSERT INTO issue_comments (item_id, actor, kind, body, at) "
               "VALUES ('a', 'x', 'note', 'hi', '2026-01-01')")
    dst.execute("INSERT INTO issue_comments (item_id, actor, kind, body, at) "
               "VALUES ('a', 'x', 'note', 'hi', '2026-01-01')")  # already there
    src.commit()
    dst.commit()
    src.close()
    dst.close()

    conn = sqlite3.connect(f"file:{dest_db}?mode=ro", uri=True)
    conn.execute("ATTACH DATABASE ? AS o", (f"file:{source_db}?mode=ro",))
    counts = rs.stranded(conn)
    assert counts["issue_items"] == 1
    assert counts["issue_comments"] == 0  # content already present, not stranded


# --- replay(): the write, and what it refuses to do silently -------------------


def test_replay_inserts_by_id_tables_preserving_the_id(source_db, dest_db):
    src = _conn(source_db)
    src.execute("INSERT INTO decision_items (id, product, body) VALUES "
               "('dec_1', 'vivy', 'a ruling')")
    src.commit()
    src.close()

    conn = sqlite3.connect(dest_db)
    conn.execute("ATTACH DATABASE ? AS o", (f"file:{source_db}?mode=ro",))
    moved, skipped = rs.replay(conn)
    conn.commit()

    assert moved["decision_items"] == 1
    assert not skipped
    row = conn.execute("SELECT id, body FROM decision_items").fetchone()
    assert row == ("dec_1", "a ruling")


def test_replay_renulls_embedded_at_on_arrival(source_db, dest_db):
    """Orient's copy carries a stamp from an index the Library never built —
    copying it verbatim would make the row invisible to the embed sweep."""
    src = _conn(source_db)
    src.execute("INSERT INTO issue_items (id, product, title, embedded_at) "
               "VALUES ('a', 'vivy', 't', '2026-01-01 00:00:00')")
    src.commit()
    src.close()

    conn = sqlite3.connect(dest_db)
    conn.execute("ATTACH DATABASE ? AS o", (f"file:{source_db}?mode=ro",))
    rs.replay(conn)
    conn.commit()

    assert conn.execute(
        "SELECT embedded_at FROM issue_items WHERE id='a'").fetchone()[0] is None


def test_replay_inserts_by_content_tables_with_a_fresh_id(source_db, dest_db):
    """The source's own integer id is meaningless here — the destination
    assigns a new one, which is the whole point of matching on content."""
    src = _conn(source_db)
    src.execute("INSERT INTO issue_comments (id, item_id, actor, kind, body, at) "
               "VALUES (9999, 'a', 'x', 'note', 'hi', '2026-01-01')")
    src.commit()
    src.close()

    conn = sqlite3.connect(dest_db)
    conn.execute("ATTACH DATABASE ? AS o", (f"file:{source_db}?mode=ro",))
    moved, skipped = rs.replay(conn)
    conn.commit()

    assert moved["issue_comments"] == 1
    row = conn.execute(
        "SELECT id, body FROM issue_comments WHERE item_id='a'").fetchone()
    assert row[1] == "hi"
    assert row[0] != 9999  # the destination's own autoincrement, not carried over


def test_replay_does_not_move_the_same_content_twice(source_db, dest_db):
    src, dst = _conn(source_db), _conn(dest_db)
    for c in (src, dst):
        c.execute("INSERT INTO issue_comments (item_id, actor, kind, body, at) "
                 "VALUES ('a', 'x', 'note', 'hi', '2026-01-01')")
        c.commit()
    src.close()
    dst.close()

    conn = sqlite3.connect(dest_db)
    conn.execute("ATTACH DATABASE ? AS o", (f"file:{source_db}?mode=ro",))
    moved, skipped = rs.replay(conn)
    conn.commit()

    assert moved["issue_comments"] == 0
    assert not skipped
    n = conn.execute("SELECT COUNT(*) FROM issue_comments").fetchone()[0]
    assert n == 1  # not duplicated


def test_replay_skips_a_unique_collision_rather_than_failing_the_whole_run(
        source_db, dest_db):
    """The real incident this defends against: two DIFFERENT events sharing
    one day-scoped `once_key` because each side's own dedup check could only
    see the database it was configured to read. Neither a silent overwrite
    nor a crash is correct; a report is."""
    src, dst = _conn(source_db), _conn(dest_db)
    src.execute("INSERT INTO issue_items (id, product, title, once_key) "
               "VALUES ('orient-side', 'vivy', 'destroyed files', 'k')")
    src.execute("INSERT INTO issue_items (id, product, title, once_key) "
               "VALUES ('also-clean', 'vivy', 'unrelated new item', 'k2')")
    dst.execute("INSERT INTO issue_items (id, product, title, once_key) "
               "VALUES ('library-side', 'vivy', 'appeared path', 'k')")
    src.commit()
    dst.commit()
    src.close()
    dst.close()

    conn = sqlite3.connect(dest_db)
    conn.execute("ATTACH DATABASE ? AS o", (f"file:{source_db}?mode=ro",))
    moved, skipped = rs.replay(conn)
    conn.commit()

    assert moved["issue_items"] == 1  # only 'also-clean' landed
    assert conn.execute(
        "SELECT id FROM issue_items WHERE id='also-clean'").fetchone()
    assert not conn.execute(
        "SELECT id FROM issue_items WHERE id='orient-side'").fetchone()
    assert skipped["issue_items"] == [("orient-side", skipped["issue_items"][0][1])]
    assert "once_key" in skipped["issue_items"][0][1]
    # The row already in the destination under that once_key is untouched.
    assert conn.execute(
        "SELECT title FROM issue_items WHERE id='library-side'"
    ).fetchone()[0] == "appeared path"


def test_replay_refuses_the_whole_run_on_an_unrecognized_collision(
        source_db, dest_db):
    """A live-lease collision (`ix_issue_leases_live_root`) reports as
    `UNIQUE constraint failed: issue_leases.root_id` — not one of the four
    signatures this script has verified safe to skip, and not obviously
    benign the way a duplicate dedup-guarded filing is: it means Orient's
    stale copy and the Library each hold a DIFFERENT lease claiming to be
    live on the SAME root. Skipping it under the same reassuring label as
    the known case would be the wrong call, so this fails the whole run
    instead — nothing commits."""
    src, dst = _conn(source_db), _conn(dest_db)
    src.execute("INSERT INTO issue_leases (id, root_id, holder, released_at) "
               "VALUES ('orient-lease', 'root-1', 'stale-holder', NULL)")
    dst.execute("INSERT INTO issue_leases (id, root_id, holder, released_at) "
               "VALUES ('library-lease', 'root-1', 'live-holder', NULL)")
    src.commit()
    dst.commit()
    src.close()
    dst.close()

    conn = sqlite3.connect(dest_db)
    conn.execute("ATTACH DATABASE ? AS o", (f"file:{source_db}?mode=ro",))
    with pytest.raises(rs.RefusedError, match="does not match any"):
        rs.replay(conn)
    conn.rollback()

    # Nothing landed — the whole call refused rather than skipping one row.
    assert conn.execute(
        "SELECT COUNT(*) FROM issue_leases").fetchone()[0] == 1


# --- main(): the guards this script's write path adds on top of store.py's ----


def test_main_refuses_cleanly_when_writes_are_disabled(
        monkeypatch, capsys, source_db, dest_db):
    monkeypatch.setattr(store, "resolve_db", lambda: (dest_db, "LIBRARY_DB"))
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    src = _conn(source_db)
    src.execute("INSERT INTO decision_items (id, product, body) VALUES "
               "('dec_1', 'vivy', 'a ruling')")
    src.commit()
    src.close()

    rc = rs.main(["--source", str(source_db), "--i-mean-it"])
    assert rc == 2
    assert "LIBRARY_WRITES_ENABLED" in capsys.readouterr().err
    assert not _conn(dest_db).execute("SELECT * FROM decision_items").fetchall()


def test_main_refuses_a_missing_destination_file(monkeypatch, capsys, source_db,
                                                  tmp_path):
    missing = tmp_path / "does-not-exist.db"
    monkeypatch.setattr(store, "resolve_db", lambda: (missing, "LIBRARY_DB"))
    rc = rs.main(["--source", str(source_db)])
    assert rc == 2
    assert "does not exist" in capsys.readouterr().err


# --- main(): the CLI shape, dry run vs write ------------------------------------


def test_main_dry_run_reports_and_writes_nothing(
        monkeypatch, capsys, source_db, dest_db):
    monkeypatch.setattr(store, "resolve_db", lambda: (dest_db, "LIBRARY_DB"))
    src = _conn(source_db)
    src.execute("INSERT INTO decision_items (id, product, body) VALUES "
               "('dec_1', 'vivy', 'a ruling')")
    src.commit()
    src.close()

    rc = rs.main(["--source", str(source_db)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "dry run" in out
    assert not _conn(dest_db).execute(
        "SELECT * FROM decision_items").fetchall()


def test_main_with_nothing_stranded_is_a_clean_no_op(monkeypatch, source_db, dest_db):
    monkeypatch.setattr(store, "resolve_db", lambda: (dest_db, "LIBRARY_DB"))
    assert rs.main(["--source", str(source_db)]) == 0


def test_main_refuses_a_non_orient_source(monkeypatch, capsys, dest_db, tmp_path):
    monkeypatch.setattr(store, "resolve_db", lambda: (dest_db, "LIBRARY_DB"))
    not_orient = _build(tmp_path / "not-orient.db")
    rc = rs.main(["--source", str(not_orient)])
    assert rc == 2
    assert "none of" in capsys.readouterr().err


def test_main_i_mean_it_writes_backs_up_and_embeds_nothing_when_none_exists(
        monkeypatch, capsys, source_db, dest_db, tmp_path):
    """No vault items/signals in this fixture's stranded set, so the embed
    sweep runs and finds nothing — proving it runs without needing a real
    embedding model wired up for tables that never reach it in this test."""
    monkeypatch.setattr(store, "resolve_db", lambda: (dest_db, "LIBRARY_DB"))
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    monkeypatch.setattr(store, "DB_PATH", dest_db)
    backup_dir = tmp_path / "backups"
    monkeypatch.setattr(rs, "BACKUP_DIR", backup_dir)

    src = _conn(source_db)
    src.execute("INSERT INTO decision_items (id, product, body) VALUES "
               "('dec_1', 'vivy', 'a ruling')")
    src.commit()
    src.close()

    rc = rs.main(["--source", str(source_db), "--i-mean-it"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "PASS" in out
    assert list(backup_dir.glob("library-pre-replay-*.db"))
    row = _conn(dest_db).execute(
        "SELECT body FROM decision_items WHERE id='dec_1'").fetchone()
    assert row["body"] == "a ruling"


def test_main_reports_cleanly_when_the_embed_sweep_fails(
        monkeypatch, capsys, source_db, dest_db, tmp_path):
    """The write already committed and verified by the time the embed sweep
    runs, so a failure there must not read like the write itself went wrong —
    and must not crash raw, which is exactly what this path did before the
    fix (reviewer, PR #39)."""
    monkeypatch.setattr(store, "resolve_db", lambda: (dest_db, "LIBRARY_DB"))
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    monkeypatch.setattr(store, "DB_PATH", dest_db)
    monkeypatch.setattr(rs, "BACKUP_DIR", tmp_path / "backups")

    def _boom():
        raise RuntimeError("the embedding model is unavailable")
    monkeypatch.setattr(rs.vectors, "embed_missing", _boom)

    src = _conn(source_db)
    src.execute("INSERT INTO decision_items (id, product, body) VALUES "
               "('dec_1', 'vivy', 'a ruling')")
    src.commit()
    src.close()

    rc = rs.main(["--source", str(source_db), "--i-mean-it"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "embed sweep failed" in err
    assert "the write itself is unaffected" in err
    # The write landed regardless of the sweep dying afterward.
    row = _conn(dest_db).execute(
        "SELECT body FROM decision_items WHERE id='dec_1'").fetchone()
    assert row["body"] == "a ruling"
