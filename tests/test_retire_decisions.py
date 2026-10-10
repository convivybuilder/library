"""The one-time migration of the decision plane's rows into the Archives."""
from __future__ import annotations

import json
import sqlite3

import pytest

import retire_decisions as rd
import store

SCHEMA = """
CREATE TABLE decision_items (
    id TEXT PRIMARY KEY, product TEXT, kind TEXT, title TEXT, body TEXT,
    refs TEXT, state TEXT, decided_by TEXT, decided_at TEXT, directive TEXT,
    snooze_until TEXT, once_key TEXT, created_by TEXT, created_at TEXT,
    run_id TEXT, outcome_note TEXT, blocked INTEGER);
CREATE TABLE decision_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT, actor TEXT,
    body TEXT, at TEXT);
CREATE TABLE decision_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT, actor TEXT,
    from_state TEXT, to_state TEXT, directive TEXT, at TEXT);
CREATE TABLE signals (
    id TEXT PRIMARY KEY, source TEXT, stream TEXT, kind TEXT, actor TEXT,
    ingested_by TEXT, product TEXT, refs TEXT, body TEXT, meta TEXT,
    privacy_class TEXT, produced_at TEXT, ingested_at TEXT,
    embedded_at TEXT, dedup_key TEXT UNIQUE, acting_human TEXT);
"""


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "library.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO decision_items (id, product, kind, title, body, refs, "
        "state, decided_by, decided_at, directive, snooze_until, once_key, "
        "created_by, created_at, run_id, outcome_note, blocked) VALUES "
        "('dec_a', 'vivy', 'needs-decision', 'Ship it?', 'the body', '{}', "
        "'approved', 'operator@example.com', '2026-08-01 10:00:00.000000', "
        "'go ahead', NULL, NULL, 'ops-mcp', '2026-07-30 09:00:00.000000', "
        "NULL, NULL, 0)")
    conn.execute(
        "INSERT INTO decision_comments (item_id, actor, body, at) VALUES "
        "('dec_a', 'ops-mcp', 'a comment', '2026-07-30 09:05:00.000000')")
    conn.execute(
        "INSERT INTO decision_events (item_id, actor, from_state, to_state, "
        "directive, at) VALUES "
        "('dec_a', 'ops-mcp', NULL, 'pending', NULL, "
        "'2026-07-30 09:00:00.000000'), "
        "('dec_a', 'operator@example.com', 'pending', 'approved', 'go ahead', "
        "'2026-08-01 10:00:00.000000')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(store, "DB_PATH", path)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    return path


def _conn(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def test_a_decision_migrates_to_a_signal_preserving_its_id(db):
    assert rd.run(dry_run=False) == 0
    conn = _conn(db)
    row = conn.execute("SELECT * FROM signals WHERE id = 'dec_a'").fetchone()
    conn.close()
    assert row is not None
    assert row["source"] == rd.SOURCE
    assert row["kind"] == rd.KIND
    assert row["actor"] == "ops-mcp"
    assert row["ingested_by"] == rd.MIGRATED_BY
    assert row["product"] == "vivy"
    assert row["privacy_class"] == "internal"
    assert "Ship it?" in row["body"] and "the body" in row["body"]


def test_the_comment_thread_and_events_are_folded_into_meta(db):
    rd.run(dry_run=False)
    conn = _conn(db)
    meta = json.loads(conn.execute(
        "SELECT meta FROM signals WHERE id = 'dec_a'").fetchone()["meta"])
    conn.close()
    assert meta["decision_kind"] == "needs-decision"
    assert meta["state"] == "approved"
    assert meta["decided_by"] == "operator@example.com"
    assert meta["directive"] == "go ahead"
    assert len(meta["comments"]) == 1
    assert meta["comments"][0]["body"] == "a comment"
    assert len(meta["events"]) == 2
    assert meta["events"][1]["to_state"] == "approved"


def test_a_decision_with_no_comments_or_events_migrates_with_empty_lists(
        tmp_path, monkeypatch):
    path = tmp_path / "library.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO decision_items (id, product, kind, title, body, refs, "
        "state, decided_by, decided_at, directive, snooze_until, once_key, "
        "created_by, created_at, run_id, outcome_note, blocked) VALUES "
        "('dec_bare', 'vivy', 'proposal', 'A bare one', '', '{}', "
        "'pending', NULL, NULL, NULL, NULL, NULL, "
        "'ops-mcp', '2026-07-30 09:00:00.000000', NULL, NULL, 0)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(store, "DB_PATH", path)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)

    assert rd.run(dry_run=False) == 0
    conn = _conn(path)
    row = conn.execute("SELECT * FROM signals WHERE id = 'dec_bare'").fetchone()
    conn.close()
    meta = json.loads(row["meta"])
    assert meta["comments"] == [] and meta["events"] == []
    assert row["body"] == "A bare one"


def test_refs_are_preserved(db):
    conn = _conn(db)
    conn.execute("UPDATE decision_items SET refs = ? WHERE id = 'dec_a'",
                 (json.dumps({"item_id": "issue_x"}),))
    conn.commit()
    conn.close()
    rd.run(dry_run=False)
    conn = _conn(db)
    refs = json.loads(conn.execute(
        "SELECT refs FROM signals WHERE id = 'dec_a'").fetchone()["refs"])
    conn.close()
    assert refs == {"item_id": "issue_x"}


def test_dry_run_writes_nothing(db):
    assert rd.run(dry_run=True) == 0
    conn = _conn(db)
    n = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    conn.close()
    assert n == 0


def test_running_twice_is_a_clean_no_op_the_second_time(db):
    rd.run(dry_run=False)
    rd.run(dry_run=False)
    conn = _conn(db)
    n = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    conn.close()
    assert n == 1


def test_writes_disabled_refuses_the_same_way_every_write_does(db, monkeypatch):
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    with pytest.raises(store.WritesDisabled):
        rd.run(dry_run=False)


def test_verify_passes_once_every_decision_has_a_signal(db, capsys):
    rd.run(dry_run=False)
    assert rd.verify() == 0
    assert "verified" in capsys.readouterr().err


def test_verify_fails_loudly_when_a_decision_was_never_migrated(db, capsys):
    assert rd.verify() == 1
    err = capsys.readouterr().err
    assert "VERIFY FAILED" in err and "dec_a" in err


def test_main_dry_run_flag(db, capsys):
    assert rd.main(["--dry-run"]) == 0
    assert "would migrate dec_a" in capsys.readouterr().out
    conn = _conn(db)
    n = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    conn.close()
    assert n == 0


def test_main_verify_flag(db, capsys):
    rd.main([])
    assert rd.main(["--verify"]) == 0
