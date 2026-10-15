"""The `refusal_blocking_taken_for` migration, run for real
(issue_01M0RCF408K7TBE0N9VP224MGA).

Driven through the `alembic` CLI, as `test_short_description_migration.py` is.
The upgrade runs against a database that already holds refusals, one whose
blocking lease is still on file and one whose lease is gone, so the backfill
is checked on both outcomes.
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PREVIOUS_HEAD = "5c10b5ba7f45"
THIS_REVISION = "ce4413f890e4"
COLUMN = "blocking_taken_for"


def alembic(*args, db):
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(REPO), capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(REPO),
             "LIBRARY_DB": str(db)})


def _columns(db):
    conn = sqlite3.connect(db)
    try:
        return [r[1] for r in conn.execute("PRAGMA table_info(lease_refusals)")]
    finally:
        conn.close()


def test_upgrade_backfills_from_the_lease_each_refusal_names(tmp_path):
    db = tmp_path / "library.db"
    assert alembic("upgrade", PREVIOUS_HEAD, db=db).returncode == 0
    before = _columns(db)
    assert COLUMN not in before
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO issue_leases (id, root_id, taken_for, holder, taken_at, "
        "heartbeat_at) VALUES ('lease_1', 'epic', 'leaf', 'crew-a', "
        "'2026-01-01', '2026-01-01')")
    conn.executemany(
        "INSERT INTO lease_refusals (id, at, requested_item_id, product, "
        "requester, actor, blocking_lease_id, blocking_root_id, "
        "blocking_holder, blocks_by) VALUES (?, '2026-01-01', 'leaf', 'vivy', "
        "'crew-b', 't', ?, 'epic', 'crew-a', 'ancestor')",
        [("ref_kept", "lease_1"), ("ref_gone", "lease_purged")])
    conn.commit()
    conn.close()

    result = alembic("upgrade", THIS_REVISION, db=db)
    assert result.returncode == 0, result.stderr
    assert _columns(db) == before + [COLUMN]
    conn = sqlite3.connect(db)
    got = dict(conn.execute(f"SELECT id, {COLUMN} FROM lease_refusals"))
    conn.close()
    assert got == {"ref_kept": "leaf", "ref_gone": None}


def test_downgrade_drops_exactly_the_one_column(tmp_path):
    db = tmp_path / "library.db"
    assert alembic("upgrade", THIS_REVISION, db=db).returncode == 0
    before = _columns(db)
    result = alembic("downgrade", PREVIOUS_HEAD, db=db)
    assert result.returncode == 0, result.stderr
    assert _columns(db) == [c for c in before if c != COLUMN]
