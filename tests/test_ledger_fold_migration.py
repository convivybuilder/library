"""The `1e5e5078a1c6` migration: fold every repository-keyed ledger into its
registry project, run for real through the `alembic` CLI (the same way
`test_corrections_queues_migration.py` is).
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PREVIOUS_HEAD = "ad6a0a903c09"
REVISION = "1e5e5078a1c6"


def alembic(*args, db):
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(REPO), capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(db.parent), "LIBRARY_DB": str(db)})


def _row(conn, scope_id):
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT * FROM ledgers WHERE scope_kind = 'project' AND scope_id = ?",
        (scope_id,)).fetchone()


def _seed(db, *, now="2026-10-13 00:00:00"):
    """The 12 live keys, each with one ledger, one day of spend and one
    entry — small enough to hand-check, shaped like the real vault."""
    conn = sqlite3.connect(db)
    keys = ["agent-platform", "convivy-deck", "convivy-orient", "knowledge",
            "library", "selector", "selector-backend", "shipwright",
            "sous-chef", "vivy", "vivy-landing",
            "vivy-ownerrez-sandbox-tools"]
    for i, key in enumerate(keys):
        ledger_id = f"ldg_{i:02d}"
        conn.execute(
            "INSERT INTO ledgers (id, scope_kind, scope_id, state, "
            " granted_by, created_at, updated_at) "
            "VALUES (?, 'project', ?, 'live', 'operator', ?, ?)",
            (ledger_id, key, now, now))
        conn.execute(
            "INSERT INTO ledger_days (ledger_id, day, spent_usd_micro, "
            " updated_at) VALUES (?, '2026-10-13', ?, ?)",
            (ledger_id, (i + 1) * 1_000_000, now))
        conn.execute(
            "INSERT INTO ledger_entries (id, ledger_id, day, usd_micro, at, "
            " ingested_at, source, product, reported_by) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (f"lde_{i:02d}", ledger_id, "2026-10-13", (i + 1) * 1_000_000,
             now, now, "agentctl", key, "tester"))
    conn.commit()
    conn.close()


@pytest.fixture
def seeded(tmp_path):
    db = tmp_path / "library.db"
    assert alembic("upgrade", PREVIOUS_HEAD, db=db).returncode == 0
    _seed(db)
    return db


def test_upgrade_folds_12_live_keys_into_8_projects(seeded):
    result = alembic("upgrade", REVISION, db=seeded)
    assert result.returncode == 0, result.stderr

    conn = sqlite3.connect(seeded)
    conn.row_factory = sqlite3.Row
    live = {r["scope_id"] for r in conn.execute(
        "SELECT scope_id FROM ledgers WHERE state = 'live'")}
    closed = {r["scope_id"] for r in conn.execute(
        "SELECT scope_id FROM ledgers WHERE state = 'closed'")}
    conn.close()

    # bosun has no pre-existing row, so agent-platform is RENAMED rather
    # than folded into a second row — it is live, not closed.
    assert live == {"bosun", "convivy-deck", "convivy-orient", "library",
                    "selector", "shipwright", "sous-chef", "vivy"}
    assert closed == {"knowledge", "selector-backend", "vivy-landing",
                      "vivy-ownerrez-sandbox-tools"}


def test_upgrade_keeps_every_entry_and_sums_every_day(seeded):
    result = alembic("upgrade", REVISION, db=seeded)
    assert result.returncode == 0, result.stderr

    conn = sqlite3.connect(seeded)
    conn.row_factory = sqlite3.Row
    total_entries = conn.execute(
        "SELECT COUNT(*) c FROM ledger_entries").fetchone()["c"]

    # library = knowledge (4) + library (5); selector = selector (6) +
    # selector-backend (7); vivy = vivy (10) + vivy-landing (11) +
    # vivy-ownerrez-sandbox-tools (12); bosun = agent-platform (1) alone.
    library = _row(conn, "library")
    selector = _row(conn, "selector")
    vivy = _row(conn, "vivy")
    bosun = _row(conn, "bosun")

    def day_total(ledger_id):
        row = conn.execute(
            "SELECT spent_usd_micro FROM ledger_days WHERE ledger_id = ? "
            "AND day = '2026-10-13'", (ledger_id,)).fetchone()
        return row[0]

    library_total = day_total(library["id"])
    selector_total = day_total(selector["id"])
    vivy_total = day_total(vivy["id"])
    bosun_total = day_total(bosun["id"])
    conn.close()

    assert total_entries == 12  # nothing deleted — every entry survives
    assert library_total == (4 + 5) * 1_000_000
    assert selector_total == (6 + 7) * 1_000_000
    assert vivy_total == (10 + 11 + 12) * 1_000_000
    assert bosun_total == 1 * 1_000_000


def test_upgrade_repoints_every_folded_entry_to_the_survivor_and_project(seeded):
    result = alembic("upgrade", REVISION, db=seeded)
    assert result.returncode == 0, result.stderr

    conn = sqlite3.connect(seeded)
    conn.row_factory = sqlite3.Row
    library = _row(conn, "library")
    rows = conn.execute(
        "SELECT ledger_id, product FROM ledger_entries "
        "WHERE id IN ('lde_03', 'lde_04')").fetchall()
    conn.close()

    # lde_03 was knowledge's own entry, lde_04 was library's own — both now
    # point at library's (the survivor's) ledger id and say "library".
    for row in rows:
        assert row["ledger_id"] == library["id"]
        assert row["product"] == "library"


def test_upgrade_renames_the_lone_source_with_no_pre_existing_target(seeded):
    """bosun has no row of its own before this migration — agent-platform's
    own pre-existing entry has to be rewritten too, not only a folded-in
    one, since the rename moves the ledger's scope_id without a merge."""
    result = alembic("upgrade", REVISION, db=seeded)
    assert result.returncode == 0, result.stderr

    conn = sqlite3.connect(seeded)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT ledger_id, product FROM ledger_entries WHERE id = 'lde_00'"
    ).fetchone()
    bosun = _row(conn, "bosun")
    conn.close()

    assert row["ledger_id"] == bosun["id"]
    assert row["product"] == "bosun"


def test_upgrade_leaves_an_already_correct_ledger_untouched(seeded):
    """convivy-deck is already its own project — same id, same state, same
    `updated_at`, confirming the no-op branch never touches a row it has
    nothing to fold."""
    conn = sqlite3.connect(seeded)
    conn.row_factory = sqlite3.Row
    before = dict(_row(conn, "convivy-deck"))
    conn.close()

    result = alembic("upgrade", REVISION, db=seeded)
    assert result.returncode == 0, result.stderr

    conn = sqlite3.connect(seeded)
    conn.row_factory = sqlite3.Row
    after = dict(_row(conn, "convivy-deck"))
    conn.close()
    assert after == before


def test_upgrade_refuses_an_unresolvable_scope_and_writes_nothing(tmp_path):
    db = tmp_path / "library.db"
    assert alembic("upgrade", PREVIOUS_HEAD, db=db).returncode == 0
    conn = sqlite3.connect(db)
    now = "2026-10-13 00:00:00"
    conn.execute(
        "INSERT INTO ledgers (id, scope_kind, scope_id, state, granted_by, "
        " created_at, updated_at) "
        "VALUES ('ldg_x', 'project', 'mystery-repo', 'live', 'operator', "
        " ?, ?)", (now, now))
    conn.commit()
    conn.close()

    result = alembic("upgrade", REVISION, db=db)
    assert result.returncode != 0
    assert "mystery-repo" in result.stderr

    conn = sqlite3.connect(db)
    version = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    row = conn.execute(
        "SELECT state FROM ledgers WHERE scope_id = 'mystery-repo'").fetchone()
    conn.close()
    assert version[0] == PREVIOUS_HEAD
    assert row[0] == "live"


def test_upgrade_refuses_disagreeing_caps_and_writes_nothing(tmp_path):
    db = tmp_path / "library.db"
    assert alembic("upgrade", PREVIOUS_HEAD, db=db).returncode == 0
    conn = sqlite3.connect(db)
    now = "2026-10-13 00:00:00"
    conn.execute(
        "INSERT INTO ledgers (id, scope_kind, scope_id, state, granted_by, "
        " created_at, updated_at, soft_cap_usd_micro) "
        "VALUES ('ldg_know', 'project', 'knowledge', 'live', 'operator', "
        " ?, ?, 50000000)", (now, now))
    conn.execute(
        "INSERT INTO ledgers (id, scope_kind, scope_id, state, granted_by, "
        " created_at, updated_at, soft_cap_usd_micro) "
        "VALUES ('ldg_lib', 'project', 'library', 'live', 'operator', "
        " ?, ?, 90000000)", (now, now))
    conn.commit()
    conn.close()

    result = alembic("upgrade", REVISION, db=db)
    assert result.returncode != 0
    assert "soft_cap_usd_micro" in result.stderr

    conn = sqlite3.connect(db)
    version = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    rows = {r[0]: r[1] for r in conn.execute(
        "SELECT scope_id, soft_cap_usd_micro FROM ledgers")}
    conn.close()
    assert version[0] == PREVIOUS_HEAD
    assert rows == {"knowledge": 50_000_000, "library": 90_000_000}


def test_upgrade_moves_a_disagreeing_free_cap_onto_the_survivor(tmp_path):
    """Not every column has to disagree for the refusal to fire — a column
    only ONE of the two rows sets is not a disagreement, and the merged
    ledger carries it forward."""
    db = tmp_path / "library.db"
    assert alembic("upgrade", PREVIOUS_HEAD, db=db).returncode == 0
    conn = sqlite3.connect(db)
    now = "2026-10-13 00:00:00"
    conn.execute(
        "INSERT INTO ledgers (id, scope_kind, scope_id, state, granted_by, "
        " created_at, updated_at, soft_cap_usd_micro) "
        "VALUES ('ldg_know', 'project', 'knowledge', 'live', 'operator', "
        " ?, ?, 50000000)", (now, now))
    conn.execute(
        "INSERT INTO ledgers (id, scope_kind, scope_id, state, granted_by, "
        " created_at, updated_at) "
        "VALUES ('ldg_lib', 'project', 'library', 'live', 'operator', "
        " ?, ?)", (now, now))
    conn.commit()
    conn.close()

    result = alembic("upgrade", REVISION, db=db)
    assert result.returncode == 0, result.stderr

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    library = _row(conn, "library")
    conn.close()
    assert library["soft_cap_usd_micro"] == 50_000_000
    assert library["state"] == "live"


def test_downgrade_raises_when_anything_actually_folded(seeded):
    assert alembic("upgrade", REVISION, db=seeded).returncode == 0
    result = alembic("downgrade", PREVIOUS_HEAD, db=seeded)
    assert result.returncode != 0
    assert "no downgrade" in result.stderr


def test_downgrade_is_a_clean_no_op_when_nothing_needed_folding(tmp_path):
    """A database with none of the 12 repository keys — every other
    migration test in this tree upgrades to head and downgrades an
    unrelated earlier revision this way, and this migration must not break
    every one of them just by existing."""
    db = tmp_path / "library.db"
    assert alembic("upgrade", "head", db=db).returncode == 0
    result = alembic("downgrade", PREVIOUS_HEAD, db=db)
    assert result.returncode == 0, result.stderr
