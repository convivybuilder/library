"""The corrections backfill migration (`e18a2c4f9b06`), run for real
(issue_01M4J613MK4QAKT4CZ54NY0BR8, CQ·200 — library/specs/one-home-per-work-
item, PR 2).

Driven through the `alembic` CLI, the same pattern `test_item_key_migration.py`
and `test_corrections_queues_migration.py` (PR 1) use. `PREVIOUS_HEAD` is PR
1's own revision, which creates `corrections_queues` but writes no
`parent_id` — the state every live row is in until this migration runs.
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PREVIOUS_HEAD = "8d88e5026d39"
REVISION = "e18a2c4f9b06"


def alembic(*args, db):
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(REPO), capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(REPO),
             "LIBRARY_DB": str(db)})


def _items(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return {r["id"]: dict(r) for r in conn.execute(
            "SELECT * FROM issue_items ORDER BY id")}
    finally:
        conn.close()


def _queues(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return {r["product"]: r["id"] for r in conn.execute(
            "SELECT * FROM corrections_queues")}
    finally:
        conn.close()


def _ledger(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return {r["id"]: dict(r) for r in conn.execute(
            "SELECT * FROM ledger_entries ORDER BY id")}
    finally:
        conn.close()


def _seed(db):
    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO issue_items (id, product, title, body, refs, severity, "
        "state, type, created_by, created_at, parent_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            # vivy: an open correction and a CLOSED one — both get the
            # queue (the operator's ruling, 2026-10-09: closed
            # corrections get it too, "for cleanliness").
            ("c_v1", "vivy", "open correction", "b", "{}", "low", "open",
             "bug", "t", "2026-01-01", None),
            ("c_v2", "vivy", "closed correction", "b", "{}", "low", "fixed",
             "bug", "t", "2026-01-01", None),
            # vivy: an untyped open correction, matching what library PR
            # 236's migration leaves an open non-epic item as.
            ("c_v3", "vivy", "untyped correction", "b", "{}", "low", "open",
             None, "t", "2026-01-01", None),
            # vivy: an epic with a NULL parent — stays top-level, untouched.
            ("e_v1", "vivy", "epic", "b", "{}", "low", "open", "epic", "t",
             "2026-01-01", None),
            # vivy: a work item already under the epic — untouched; its
            # parent is not NULL, so the migration's WHERE clause never
            # reaches it.
            ("t_v1", "vivy", "under the epic", "b", "{}", "low", "open",
             None, "t", "2026-01-01", "e_v1"),
            # vivy: a dangling parent — left exactly as it is. Its
            # `parent_id` is not NULL, so this migration does not touch it;
            # `GET /items?home=invalid` is what surfaces it afterward.
            ("d_v1", "vivy", "dangling parent", "b", "{}", "low", "open",
             "bug", "t", "2026-01-01", "issue_does_not_exist"),
            # archy: a correction in a product PR 1's own migration never
            # saw — seeded here, after that migration ran, the way a
            # product's first correction filed in the gap between PR 1's
            # deploy and this one's would look. No queue exists for it yet.
            ("c_a1", "archy", "archy's first correction", "b", "{}", "low",
             "open", "bug", "t", "2026-01-01", None),
        ])
    conn.execute(
        "INSERT INTO ledger_entries (id, ledger_id, day, usd_micro, at, "
        "ingested_at, source, product, reported_by, epic_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("lde_v1", "ld_v", "2026-01-01", 100, "2026-01-01", "2026-01-01",
         "agentctl", "vivy", "t", "corrections"))
    # A ledger entry naming a product with no `issue_items` correction at
    # all — the other way `8d88e5026d39` seeded a queue, and the other way
    # this migration has to resolve one.
    conn.execute(
        "INSERT INTO ledger_entries (id, ledger_id, day, usd_micro, at, "
        "ingested_at, source, product, reported_by, epic_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("lde_a1", "ld_a", "2026-01-01", 100, "2026-01-01", "2026-01-01",
         "agentctl", "archy", "t", "corrections"))
    conn.commit()
    # PR 1's own migration, simulated: a queue already exists for `vivy`
    # (the state the live vault was in before this migration ran), but not
    # for `archy`, which filed its first correction afterward.
    conn.execute(
        "INSERT INTO corrections_queues (id, product, created_at) "
        "VALUES ('cq_vivy', 'vivy', '2026-01-01 00:00:00')")
    conn.commit()
    conn.close()


def _upgraded_copy(tmp_path, seed=True):
    db = tmp_path / "library.db"
    assert alembic("upgrade", PREVIOUS_HEAD, db=db).returncode == 0
    if seed:
        _seed(db)
    copy = tmp_path / "copy.db"
    copy.write_bytes(db.read_bytes())
    result = alembic("upgrade", REVISION, db=copy)
    assert result.returncode == 0, result.stderr
    return copy


def test_upgrade_backfills_open_and_closed_corrections_to_the_queue(tmp_path):
    copy = _upgraded_copy(tmp_path)
    items = _items(copy)
    queue_id = _queues(copy)["vivy"]
    assert queue_id == "cq_vivy"
    assert items["c_v1"]["parent_id"] == queue_id
    assert items["c_v2"]["parent_id"] == queue_id          # closed, too
    assert items["c_v3"]["parent_id"] == queue_id          # untyped, too


def test_upgrade_leaves_epics_and_properly_parented_items_untouched(tmp_path):
    copy = _upgraded_copy(tmp_path)
    items = _items(copy)
    assert items["e_v1"]["parent_id"] is None
    assert items["t_v1"]["parent_id"] == "e_v1"


def test_upgrade_leaves_a_dangling_parent_as_it_is(tmp_path):
    copy = _upgraded_copy(tmp_path)
    assert _items(copy)["d_v1"]["parent_id"] == "issue_does_not_exist"


def test_upgrade_reseeds_a_queue_for_a_product_pr1_never_saw(tmp_path):
    """`archy` had no `corrections_queues` row before this migration ran —
    PR 1's own migration seeded only what existed at ITS deploy time, and
    `archy`'s first correction was filed after that. The backfill still
    resolves it, by minting the missed queue first."""
    copy = _upgraded_copy(tmp_path)
    queues = _queues(copy)
    assert "archy" in queues
    assert queues["archy"].startswith("cq_")
    assert _items(copy)["c_a1"]["parent_id"] == queues["archy"]


def test_upgrade_backfills_the_ledger_literal_to_the_queue_id(tmp_path):
    copy = _upgraded_copy(tmp_path)
    queues = _queues(copy)
    ledger = _ledger(copy)
    assert ledger["lde_v1"]["epic_id"] == queues["vivy"]
    assert ledger["lde_a1"]["epic_id"] == queues["archy"]


def test_upgrade_changes_no_other_column(tmp_path):
    """Additive to every row it touches: `parent_id` moves, nothing else
    does — not `item_key`, not `state`, not any other column — and no row
    this migration does not target changes at all."""
    db = tmp_path / "library.db"
    assert alembic("upgrade", PREVIOUS_HEAD, db=db).returncode == 0
    _seed(db)
    before = _items(db)

    copy = tmp_path / "copy.db"
    copy.write_bytes(db.read_bytes())
    assert alembic("upgrade", REVISION, db=copy).returncode == 0
    after = _items(copy)

    assert set(before) == set(after)
    for item_id, before_row in before.items():
        after_row = after[item_id]
        changed = {c for c in before_row if before_row[c] != after_row[c]}
        if item_id in ("c_v1", "c_v2", "c_v3", "c_a1"):
            assert changed == {"parent_id"}, (item_id, changed)
        else:
            assert changed == set(), (item_id, changed)


def test_upgrade_writes_no_comment_or_event(tmp_path):
    """`issue_comments` and `issue_events` are baseline tables — already
    there before this migration runs — so this reads their counts rather
    than creating them."""
    db = tmp_path / "library.db"
    assert alembic("upgrade", PREVIOUS_HEAD, db=db).returncode == 0
    _seed(db)

    copy = tmp_path / "copy.db"
    copy.write_bytes(db.read_bytes())
    assert alembic("upgrade", REVISION, db=copy).returncode == 0

    conn = sqlite3.connect(copy)
    try:
        comments = conn.execute("SELECT COUNT(*) FROM issue_comments").fetchone()[0]
        events = conn.execute("SELECT COUNT(*) FROM issue_events").fetchone()[0]
    finally:
        conn.close()
    assert comments == 0
    assert events == 0


def test_the_refusal_backstop_is_present_and_shaped_like_the_key_migrations(tmp_path):
    """**Why this is a structural check, not a behavioural one.** The
    re-seed and the backfill read the identical `WHERE` clause (same
    `product`, same NULL-parent, same non-epic test), so any `product`
    string that qualifies for one always qualifies for the other — and
    `issue_items.product` is `NOT NULL`, which closes the one gap in that
    argument (`NULL = NULL` is never true in SQL, the shape that would
    otherwise slip past the re-seed). There is no data this migration can
    be handed that reaches the refusal, which is this test's own positive
    control: it is what the re-seed step above buys. What is still checked
    is that the backstop EXISTS and is shaped like `d70d450ef8c7`'s own —
    a `COUNT` read against a real connection feeding a `RuntimeError` naming
    the row count — not merely a comment promising one."""
    source = (REPO / "alembic" / "versions" /
              "e18a2c4f9b06_backfill_corrections_into_their_queues.py").read_text()
    assert "raise RuntimeError(" in source
    assert "SELECT COUNT(*) FROM issue_items" in source
    assert "refusing to " in source and "continue the migration" in source


def test_downgrade_restores_null_and_the_ledger_literal(tmp_path):
    db = tmp_path / "library.db"
    assert alembic("upgrade", PREVIOUS_HEAD, db=db).returncode == 0
    _seed(db)
    assert alembic("upgrade", REVISION, db=db).returncode == 0

    result = alembic("downgrade", PREVIOUS_HEAD, db=db)
    assert result.returncode == 0, result.stderr

    items = _items(db)
    ledger = _ledger(db)
    assert items["c_v1"]["parent_id"] is None
    assert items["c_v2"]["parent_id"] is None
    assert items["c_v3"]["parent_id"] is None
    assert items["c_a1"]["parent_id"] is None
    assert items["t_v1"]["parent_id"] == "e_v1"             # untouched
    assert items["d_v1"]["parent_id"] == "issue_does_not_exist"  # untouched
    assert ledger["lde_v1"]["epic_id"] == "corrections"
    assert ledger["lde_a1"]["epic_id"] == "corrections"
    # `corrections_queues` itself is PR 1's own table; this downgrade does
    # not drop it.
    assert "corrections_queues" in {r[0] for r in sqlite3.connect(db).execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
