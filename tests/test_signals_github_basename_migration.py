"""The `5b667713b87c` migration, run for real (CQ·252,
issue_01M67TF7Y14VS7JZ98Y08EKDY4).

Driven through the `alembic` CLI, matching `test_source_url_migration.py`'s
shape: the revision has to chain onto head and its SQL has to run against
real `signals` rows on alembic's own connection, which loads no `vec0`.
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PREVIOUS_HEAD = "42d8d9e44390"
REVISION = "5b667713b87c"

CAPTURE = "github-activity-capture"


def alembic(*args, db):
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(REPO), capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(REPO),
             "LIBRARY_DB": str(db)})


def _rows(db):
    conn = sqlite3.connect(db)
    try:
        return {r[0]: (r[1], r[2], r[3]) for r in conn.execute(
            "SELECT id, product, stream, dedup_key FROM signals")}
    finally:
        conn.close()


SEEDED = {
    # A capture row with a bare stream. Moves.
    "bare_stream": (CAPTURE, "convivy/knowledge", "convivy/knowledge",
                    "gh:convivy/knowledge:push:abc"),
    # A capture row with a `#n` stream. Moves.
    "pr_stream": (CAPTURE, "convivy/widget", "convivy/widget#13",
                  "gh:convivy/widget:pr13:merged"),
    # Another writer's owner/repo row. Untouched.
    "other_writer": ("agent-platform", "convivy/agent-platform",
                     "convivy/agent-platform", "ap:1"),
    # A capture row already bare, as the fixed writer stores it. Untouched.
    "already_bare": (CAPTURE, "library", "library#7",
                     "gh:convivy/library:pr7:opened"),
}


def _seed(db):
    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO signals (id, source, stream, kind, actor, ingested_by, "
        "product, refs, body, meta, privacy_class, ingested_at, dedup_key) "
        "VALUES (?, 'github', ?, 'changelog', 'a', ?, ?, '{}', 'b', '{}', "
        "'internal', '2026-10-30', ?)",
        [(sid, stream, writer, product, dedup)
         for sid, (writer, product, stream, dedup) in SEEDED.items()])
    conn.commit()
    conn.close()


def _migrated(tmp_path):
    db = tmp_path / "library.db"
    assert alembic("upgrade", PREVIOUS_HEAD, db=db).returncode == 0
    _seed(db)
    result = alembic("upgrade", REVISION, db=db)
    assert result.returncode == 0, result.stderr
    return db


def test_upgrade_rewrites_capture_rows_and_leaves_the_rest(tmp_path):
    rows = _rows(_migrated(tmp_path))

    assert rows["bare_stream"] == (
        "knowledge", "knowledge", "gh:convivy/knowledge:push:abc")
    assert rows["pr_stream"] == (
        "widget", "widget#13", "gh:convivy/widget:pr13:merged")
    assert rows["other_writer"] == SEEDED["other_writer"][1:]
    assert rows["already_bare"] == SEEDED["already_bare"][1:]


def test_downgrade_restores_owner_repo_from_the_dedup_key(tmp_path):
    db = _migrated(tmp_path)
    result = alembic("downgrade", PREVIOUS_HEAD, db=db)
    assert result.returncode == 0, result.stderr
    rows = _rows(db)

    assert rows["bare_stream"] == SEEDED["bare_stream"][1:]
    assert rows["pr_stream"] == SEEDED["pr_stream"][1:]
    assert rows["other_writer"] == SEEDED["other_writer"][1:]
    # A bare row the fixed writer stored goes back to owner/repo too, which
    # is what a rollback to the owner/repo writer needs.
    assert rows["already_bare"] == (
        "convivy/library", "convivy/library#7", "gh:convivy/library:pr7:opened")
