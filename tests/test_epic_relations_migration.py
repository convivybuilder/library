"""The `issue_epic_relations` migration's downgrade, run through the real
`alembic` CLI as `test_black_box_registrations_migration.py` runs its own
(security-reviewer, PR #193 round 1)."""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def alembic(*args, env_extra=None):
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(REPO), capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(REPO), **(env_extra or {})})


def _tables(db):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')")}
    finally:
        conn.close()


def test_the_downgrade_drops_the_table_and_its_index(tmp_path):
    db = tmp_path / "library.db"
    env = {"LIBRARY_DB": str(db)}
    assert alembic("upgrade", "2c89868910a8", env_extra=env).returncode == 0
    assert {"issue_epic_relations",
            "ix_issue_epic_relations_epic_id"} <= _tables(db)
    result = alembic("downgrade", "ee33c883acc4", env_extra=env)
    assert result.returncode == 0, result.stderr
    assert not {"issue_epic_relations",
                "ix_issue_epic_relations_epic_id"} & _tables(db)
