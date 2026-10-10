"""The `stable_short_keys` migration, run for real
(issue_01M2P5CBY5JY8SK5W344CV82AE).

Driven through the `alembic` CLI, as `test_item_model_migration.py` is. The
upgrade runs against a COPY of a database that already holds a history of
epics, sub-items and corrections across two products, so the backfill is
checked on data with an order to get right, not on an empty schema — and
against a tree four levels deep (an epic nested under another epic, with
its own further-nested child), the shape a prior version of this migration
left silently keyless: its sub-item pass resolved a row's key through a
parent lookup that read the parent's own not-yet-written value when the
parent was itself only being keyed in that same statement.
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PREVIOUS_HEAD = "ba532e49e577"
# The revision under test, named rather than read as `head`: later migrations
# chain after it, and each would otherwise show up in its before/after diff.
ITEM_KEY_REVISION = "d70d450ef8c7"


def alembic(*args, db):
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(REPO), capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(REPO),
             "LIBRARY_DB": str(db)})


def _columns(db):
    conn = sqlite3.connect(db)
    try:
        return [r[1] for r in conn.execute("PRAGMA table_info(issue_items)")]
    finally:
        conn.close()


def _seed(db):
    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO issue_items (id, product, title, body, refs, severity, "
        "state, type, created_by, created_at, parent_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            # vivy: two epics, filed out of id order so a naive MAX()/id sort
            # would get the numbering wrong — only created_at should decide it.
            ("e_v2", "vivy", "epic two", "b", "{}", "low", "open", "epic", "t",
             "2026-01-02", None),
            ("e_v1", "vivy", "epic one", "b", "{}", "low", "open", "epic", "t",
             "2026-01-01", None),
            # vivy: two corrections, a sequence independent of the epics'.
            ("c_v1", "vivy", "fix one", "b", "{}", "low", "open", "bug", "t",
             "2026-01-01", None),
            ("c_v2", "vivy", "fix two", "b", "{}", "low", "open", "nit", "t",
             "2026-01-02", None),
            # vivy: sub-items under both epics, also out of id order.
            ("t_v1", "vivy", "task under e1, later", "b", "{}", "low", "open",
             "task", "t", "2026-01-03", "e_v1"),
            ("t_v2", "vivy", "task under e1, earlier", "b", "{}", "low",
             "open", "task", "t", "2026-01-02", "e_v1"),
            ("t_v3", "vivy", "task under e2", "b", "{}", "low", "open",
             "task", "t", "2026-01-01", "e_v2"),
            # constance: its own product-scoped sequence, starting over at 1.
            ("e_c1", "constance", "a constance epic", "b", "{}", "low",
             "open", "epic", "t", "2026-01-01", None),
            # vivy: a four-level tree — a sub-epic under e_v1, a sub-sub-epic
            # under THAT, and a leaf item under the sub-sub-epic. Every one of
            # these three rows is itself only keyed by the same backfill pass
            # that must also key its own parent — the exact shape the old,
            # un-fixed backfill left `item_key = NULL` for below the second
            # level.
            ("se_v1", "vivy", "sub-epic under e1", "b", "{}", "low", "open",
             "epic", "t", "2026-01-04", "e_v1"),
            ("sse_v1", "vivy", "sub-sub-epic under se_v1", "b", "{}", "low",
             "open", "epic", "t", "2026-01-05", "se_v1"),
            ("leaf_v1", "vivy", "leaf under sse_v1", "b", "{}", "low", "open",
             "task", "t", "2026-01-06", "sse_v1"),
        ])
    conn.commit()
    conn.close()


def test_upgrade_backfills_keys_in_created_at_order_per_product(tmp_path):
    db = tmp_path / "library.db"
    assert alembic("upgrade", PREVIOUS_HEAD, db=db).returncode == 0
    before = _columns(db)
    assert "item_key" not in before
    _seed(db)

    # A copy of the populated database is what the upgrade runs against.
    copy = tmp_path / "copy.db"
    copy.write_bytes(db.read_bytes())
    result = alembic("upgrade", ITEM_KEY_REVISION, db=copy)
    assert result.returncode == 0, result.stderr

    after = _columns(copy)
    assert after == before + ["item_key"]

    conn = sqlite3.connect(copy)
    conn.row_factory = sqlite3.Row
    keys = {r["id"]: r["item_key"]
            for r in conn.execute("SELECT id, item_key FROM issue_items")}
    counters = {(r["scope_kind"], r["scope_id"]): r["next_value"]
                for r in conn.execute("SELECT * FROM issue_item_key_counters")}
    conn.close()

    assert keys == {
        "e_v1": "E1", "e_v2": "E2",
        "c_v1": "CQ·1", "c_v2": "CQ·2",
        "t_v2": "E1·1", "t_v1": "E1·2", "t_v3": "E2·1",
        "e_c1": "E1",
        # The four-level tree: se_v1 and sse_v1 are epics, so each gets its
        # own per-product E<n> — fixed by created_at order across every epic
        # in the product, regardless of depth — rather than a key built from
        # its parent's. leaf_v1 is the one non-epic here, and extends its
        # immediate parent sse_v1's key.
        "se_v1": "E3", "sse_v1": "E4", "leaf_v1": "E4·1",
    }
    assert counters == {
        ("epic", "vivy"): 5, ("epic", "constance"): 2,
        ("correction", "vivy"): 3,
        ("item", "e_v1"): 3, ("item", "e_v2"): 2, ("item", "sse_v1"): 2,
    }


def test_upgrade_resolves_a_legacy_item_nested_under_an_item(tmp_path):
    """`_validate_parent` refuses a new item's parent unless it is an epic, so
    the live write path can never create an item nested under another item —
    but a row from before that rule existed is not guaranteed to honor it,
    and the backfill has to resolve one anyway rather than leave it keyless.
    This seeds exactly that: `mid` is a plain item (not an epic) under an
    epic, and `deep` is a plain item under `mid` — two levels the sub-item
    pass cannot both resolve in one trip, which is why it runs in a loop."""
    db = tmp_path / "library.db"
    assert alembic("upgrade", PREVIOUS_HEAD, db=db).returncode == 0
    _seed(db)
    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO issue_items (id, product, title, body, refs, severity, "
        "state, type, created_by, created_at, parent_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("e_v9", "vivy", "epic for the legacy chain", "b", "{}", "low",
             "open", "epic", "t", "2026-02-01", None),
            ("mid", "vivy", "a plain item with its own child", "b", "{}",
             "low", "open", "task", "t", "2026-02-02", "e_v9"),
            ("deep", "vivy", "nested under a non-epic item", "b", "{}",
             "low", "open", "task", "t", "2026-02-03", "mid"),
        ])
    conn.commit()
    conn.close()

    result = alembic("upgrade", ITEM_KEY_REVISION, db=db)
    assert result.returncode == 0, result.stderr

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    keys = {r["id"]: r["item_key"]
            for r in conn.execute(
                "SELECT id, item_key FROM issue_items "
                "WHERE id IN ('e_v9','mid','deep')")}
    conn.close()
    assert keys["e_v9"] == "E5"
    assert keys["mid"] == "E5·1"
    assert keys["deep"] == f"{keys['mid']}·1"


def test_upgrade_refuses_rather_than_leave_a_row_keyless(tmp_path):
    """The backstop: two items whose `parent_id`s point at each other can
    never satisfy the sub-item pass's "my parent already has a key"
    condition, so the loop makes zero progress and the final assertion has
    to fire — the upgrade fails loudly rather than commit a row with
    `item_key IS NULL`."""
    db = tmp_path / "library.db"
    assert alembic("upgrade", PREVIOUS_HEAD, db=db).returncode == 0
    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO issue_items (id, product, title, body, refs, severity, "
        "state, type, created_by, created_at, parent_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("cyc_a", "vivy", "cycle a", "b", "{}", "low", "open", "task",
             "t", "2026-01-01", "cyc_b"),
            ("cyc_b", "vivy", "cycle b", "b", "{}", "low", "open", "task",
             "t", "2026-01-01", "cyc_a"),
        ])
    conn.commit()
    conn.close()

    result = alembic("upgrade", ITEM_KEY_REVISION, db=db)
    assert result.returncode != 0
    assert "item_key" in result.stderr


def test_downgrade_drops_the_column_and_the_counters_table(tmp_path):
    db = tmp_path / "library.db"
    assert alembic("upgrade", ITEM_KEY_REVISION, db=db).returncode == 0
    assert "item_key" in _columns(db)
    result = alembic("downgrade", PREVIOUS_HEAD, db=db)
    assert result.returncode == 0, result.stderr
    assert "item_key" not in _columns(db)
    conn = sqlite3.connect(db)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert "issue_item_key_counters" not in tables
