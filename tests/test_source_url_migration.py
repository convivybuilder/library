"""The `a63c8a7f1b85` migration, run for real (CQ·248,
issue_01M5XTC6P9W92YFJP9WJ6NNADY).

Driven through the `alembic` CLI, matching `test_item_key_migration.py`'s
shape: the upgrade runs against real `issue_items` rows rather than a
hand-rolled schema, so the JSON predicate is checked against SQLite's own
`json1` functions.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PREVIOUS_HEAD = "da38780cfabd"
REVISION = "a63c8a7f1b85"


def alembic(*args, db):
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(REPO), capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(REPO),
             "LIBRARY_DB": str(db)})


def _refs_by_id(db):
    conn = sqlite3.connect(db)
    try:
        return {r[0]: r[1] for r in
                conn.execute("SELECT id, refs FROM issue_items")}
    finally:
        conn.close()


def _seed(db):
    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO issue_items (id, product, title, body, refs, severity, "
        "state, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        [
            # url only, no pr — the shape only create_item could have
            # written. Moves.
            ("url_only", "fennick", "a", "b",
             json.dumps({"url": "https://example.com/src"}), "low", "open",
             "t", "2026-01-01"),
            # url and pr both set, by /link-pr. Untouched.
            ("url_and_pr", "fennick", "a", "b",
             json.dumps({"url": "https://example.com/pr/9", "pr": 9}), "low",
             "open", "t", "2026-01-01"),
            # a non-object refs. Untouched.
            ("list_refs", "fennick", "a", "b", json.dumps(["x"]), "low",
             "open", "t", "2026-01-01"),
            # invalid JSON. Untouched.
            ("bad_refs", "fennick", "a", "b", "not json", "low", "open", "t",
             "2026-01-01"),
            # no url at all, another key present. Untouched.
            ("other_key", "fennick", "a", "b", json.dumps({"note": "x"}),
             "low", "open", "t", "2026-01-01"),
            # the empty object every create without refs writes. Untouched.
            ("empty_refs", "fennick", "a", "b", "{}", "low", "open", "t",
             "2026-01-01"),
        ])
    conn.commit()
    conn.close()


def test_upgrade_moves_a_url_only_row_and_leaves_the_rest(tmp_path):
    db = tmp_path / "library.db"
    assert alembic("upgrade", PREVIOUS_HEAD, db=db).returncode == 0
    _seed(db)

    # A copy of the populated database is what the upgrade runs against.
    copy = tmp_path / "copy.db"
    copy.write_bytes(db.read_bytes())
    result = alembic("upgrade", REVISION, db=copy)
    assert result.returncode == 0, result.stderr

    refs = _refs_by_id(copy)
    assert json.loads(refs["url_only"]) == {
        "source_url": "https://example.com/src"}
    assert json.loads(refs["url_and_pr"]) == {
        "url": "https://example.com/pr/9", "pr": 9}
    assert json.loads(refs["list_refs"]) == ["x"]
    assert refs["bad_refs"] == "not json"
    assert json.loads(refs["other_key"]) == {"note": "x"}
    assert refs["empty_refs"] == "{}"


def test_downgrade_restores_the_url_only_row(tmp_path):
    db = tmp_path / "library.db"
    assert alembic("upgrade", PREVIOUS_HEAD, db=db).returncode == 0
    _seed(db)
    assert alembic("upgrade", REVISION, db=db).returncode == 0

    result = alembic("downgrade", PREVIOUS_HEAD, db=db)
    assert result.returncode == 0, result.stderr

    refs = _refs_by_id(db)
    assert json.loads(refs["url_only"]) == {
        "url": "https://example.com/src"}
    assert json.loads(refs["url_and_pr"]) == {
        "url": "https://example.com/pr/9", "pr": 9}
    assert json.loads(refs["list_refs"]) == ["x"]
    assert refs["bad_refs"] == "not json"
    assert json.loads(refs["other_key"]) == {"note": "x"}
    assert refs["empty_refs"] == "{}"
