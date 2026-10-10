"""The `corrections_queues` migration, run for real
(issue_01M12NE0RFPJY12TT8CF1169V1, E18·2 — library/specs/one-home-per-work-
item, PR 1).

Driven through the `alembic` CLI, the same way `test_item_key_migration.py`
is. **Additive only**: this migration creates the table and seeds one row
per product that has a correction today; it writes no `parent_id` and
changes no existing row, which the second test below checks directly.
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PREVIOUS_HEAD = "cb1fe64e0559"
REVISION = "8d88e5026d39"


def alembic(*args, db):
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(REPO), capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(REPO),
             "LIBRARY_DB": str(db)})


def _tables(db):
    conn = sqlite3.connect(db)
    try:
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def _seed(db):
    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO issue_items (id, product, title, body, refs, severity, "
        "state, type, created_by, created_at, parent_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            # vivy: an epic and a correction (no parent) — vivy needs a queue.
            ("e_v1", "vivy", "epic", "b", "{}", "low", "open", "epic", "t",
             "2026-01-01", None),
            ("c_v1", "vivy", "fix", "b", "{}", "low", "open", "bug", "t",
             "2026-01-01", None),
            # constance: an epic and a sub-item THAT HAS A PARENT — not a
            # correction by today's rule, but still a non-epic row, so this
            # product gets a queue too (the migration seeds by product, not
            # by whether a given row is currently parentless).
            ("e_c1", "constance", "epic", "b", "{}", "low", "open", "epic",
             "t", "2026-01-01", None),
            ("t_c1", "constance", "task under e_c1", "b", "{}", "low",
             "open", "task", "t", "2026-01-01", "e_c1"),
            # orient: one untyped correction, as library PR 236's migration
            # leaves an open non-epic item. `NULL != 'epic'` is not true, so
            # this row is what fails if the seed drops `type IS NULL`.
            ("c_o1", "orient", "fix", "b", "{}", "low", "open", None, "t",
             "2026-01-01", None),
            # keel: an epic ONLY, no non-epic row and no ledger correction —
            # keel gets no queue.
            ("e_k1", "keel", "epic", "b", "{}", "low", "open", "epic", "t",
             "2026-01-01", None),
        ])
    conn.execute(
        "INSERT INTO ledger_entries (id, ledger_id, day, usd_micro, at, "
        "ingested_at, source, product, reported_by, epic_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("l1", "ld1", "2026-01-01", 100, "2026-01-01", "2026-01-01",
         "agentctl", "archy", "t", "corrections"))
    conn.commit()
    conn.close()


def test_upgrade_seeds_one_queue_per_product_with_a_correction(tmp_path):
    db = tmp_path / "library.db"
    assert alembic("upgrade", PREVIOUS_HEAD, db=db).returncode == 0
    assert "corrections_queues" not in _tables(db)
    _seed(db)

    # A copy of the populated database is what the upgrade runs against.
    copy = tmp_path / "copy.db"
    copy.write_bytes(db.read_bytes())
    result = alembic("upgrade", REVISION, db=copy)
    assert result.returncode == 0, result.stderr
    assert "corrections_queues" in _tables(copy)

    conn = sqlite3.connect(copy)
    conn.row_factory = sqlite3.Row
    rows = {r["product"]: r["id"]
            for r in conn.execute("SELECT * FROM corrections_queues")}
    conn.close()

    # vivy (a correction), constance (a non-epic row with a parent) and
    # archy (a ledger 'corrections' entry only, no issue_items row at all)
    # each get exactly one queue, and so does orient (an untyped correction).
    # keel, which has only an epic, gets none.
    assert set(rows) == {"vivy", "constance", "archy", "orient"}
    for queue_id in rows.values():
        assert queue_id.startswith("cq_")


def test_upgrade_writes_no_parent_id_and_changes_no_existing_row(tmp_path):
    """Additive means additive: the table and its seed rows are new, and
    nothing already in `issue_items` or `ledger_entries` moves."""
    db = tmp_path / "library.db"
    assert alembic("upgrade", PREVIOUS_HEAD, db=db).returncode == 0
    _seed(db)

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    before_items = {r["id"]: dict(r) for r in conn.execute(
        "SELECT * FROM issue_items ORDER BY id")}
    before_ledger = {r["id"]: dict(r) for r in conn.execute(
        "SELECT * FROM ledger_entries ORDER BY id")}
    conn.close()

    copy = tmp_path / "copy.db"
    copy.write_bytes(db.read_bytes())
    assert alembic("upgrade", REVISION, db=copy).returncode == 0

    conn = sqlite3.connect(copy)
    conn.row_factory = sqlite3.Row
    after_items = {r["id"]: dict(r) for r in conn.execute(
        "SELECT * FROM issue_items ORDER BY id")}
    after_ledger = {r["id"]: dict(r) for r in conn.execute(
        "SELECT * FROM ledger_entries ORDER BY id")}
    conn.close()

    assert after_items == before_items
    assert after_ledger == before_ledger


def test_product_is_unique_so_one_queue_never_duplicates(tmp_path):
    """`vivy` names both a correction in `issue_items` and, separately, could
    also carry a `'corrections'` ledger entry — the schema's own `UNIQUE`
    on `product` is what keeps that from ever minting a second row, not
    just the `SELECT DISTINCT` the migration happens to run."""
    db = tmp_path / "library.db"
    assert alembic("upgrade", PREVIOUS_HEAD, db=db).returncode == 0
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO issue_items (id, product, title, body, refs, severity, "
        "state, type, created_by, created_at, parent_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("c_v1", "vivy", "fix", "b", "{}", "low", "open", "bug", "t",
         "2026-01-01", None))
    conn.execute(
        "INSERT INTO ledger_entries (id, ledger_id, day, usd_micro, at, "
        "ingested_at, source, product, reported_by, epic_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("l1", "ld1", "2026-01-01", 100, "2026-01-01", "2026-01-01",
         "agentctl", "vivy", "t", "corrections"))
    conn.commit()
    conn.close()

    assert alembic("upgrade", REVISION, db=db).returncode == 0
    conn = sqlite3.connect(db)
    count = conn.execute(
        "SELECT COUNT(*) FROM corrections_queues WHERE product = 'vivy'"
    ).fetchone()[0]
    conn.close()
    assert count == 1


def test_downgrade_drops_the_table(tmp_path):
    db = tmp_path / "library.db"
    assert alembic("upgrade", REVISION, db=db).returncode == 0
    assert "corrections_queues" in _tables(db)
    result = alembic("downgrade", PREVIOUS_HEAD, db=db)
    assert result.returncode == 0, result.stderr
    assert "corrections_queues" not in _tables(db)
