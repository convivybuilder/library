"""The one-off ledger history correction (issue_01M5H05DW8E50K02YB1GFNP4M4,
CQ-233). Built on a database from the real migration, for the reason
`test_review_record_backfill.py` gives: the behaviour under test includes
the uniqueness key and total-accumulation mechanics `store.record_spend`
and `store.void_spend_entry` already own, and a hand-written fixture
schema is a second copy of that contract that can drift from it.

Every assertion here runs against a temporary database and a temporary
run-records file, never `~/.library/library.db` or a copy of it — the
brief for this script is explicit that a rehearsal against a copy of the
live database is a separate, later step this test suite does not
perform. Every product and crew name here is made up
(0.7.0 name scrub): `fennick`, `mossbright` and `driftwood` name nothing
in this codebase.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import ledger_history_correction as backfill
import projects
import store

REPO = Path(__file__).resolve().parent.parent


def make_db(tmp_path):
    db = tmp_path / "library.db"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
             "LIBRARY_DB": str(db)})
    assert result.returncode == 0, result.stderr
    return db


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = make_db(tmp_path)
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({
        "_meta": {},
        "fennick": {"repos": ["fennick"]},
        "mossbright": {"repos": ["mossbright"]},
        "driftwood": {"repos": ["driftwood"], "private": True},
    }))
    monkeypatch.setattr(projects, "REGISTRY_PATH", registry)
    monkeypatch.setattr(store, "DB_PATH", path)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    projects.reload()
    yield path
    projects.reload()


def _conn(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _entries(path, **where):
    conn = _conn(path)
    try:
        clauses = " AND ".join(f"{k} = ?" for k in where)
        sql = "SELECT * FROM ledger_entries"
        if clauses:
            sql += f" WHERE {clauses}"
        sql += " ORDER BY ingested_at, id"
        return [dict(r) for r in conn.execute(sql, tuple(where.values())).fetchall()]
    finally:
        conn.close()


def _seed_posted(product, run_id, *, day, lane, usd, agent="reviewer",
                 model="claude-sonnet-5", item_id=None, dedup_key=None):
    """One entry the Bosun's old batch writer would have produced — posted
    directly through `store.record_spend`, the same function this
    script's own backfill path uses."""
    ledger = store.ensure_live_ledger("project", product)
    _idempotent, entry, _crossed = store.record_spend({
        "ledger_id": ledger["id"], "day": day,
        "usd_micro": backfill._usd_micro(usd), "source": "agentctl",
        "lane": lane, "product": product, "reported_by": "bosun-batch",
        "run_id": run_id, "agent": agent, "model": model, "item_id": item_id,
        "dedup_key": dedup_key or run_id})
    return entry


def _record(run_id, *, day, lane, product="fennick", agent="reviewer",
           model="claude-sonnet-5", item_id=None, usd=1.0,
           batch_eligible=False):
    return {"dedup_key": run_id, "run_id": run_id, "day": day, "agent": agent,
            "model": model, "lane": lane, "product": product,
            "item_id": item_id, "usd": usd, "batch_eligible": batch_eligible}


def _write_run_files(tmp_path, records, *, name="runs"):
    runs_path = tmp_path / f"{name}.jsonl"
    gateway_path = tmp_path / f"{name}-gateway.jsonl"
    with open(runs_path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    with open(gateway_path, "w") as f:
        for r in records:
            if r["lane"] == "gateway":
                f.write(json.dumps(r) + "\n")
    return runs_path, gateway_path


def _apply(db, records, tmp_path, *, name="runs", batch_size=200, write=True):
    runs_path, gateway_path = _write_run_files(tmp_path, records, name=name)
    rc = backfill.main([str(db), str(runs_path), str(gateway_path)]
                       + (["--apply", "--batch-size", str(batch_size)]
                          if write else []))
    assert rc == 0
    return runs_path, gateway_path


def test_a_redated_entry_is_voided_and_reposted_on_the_corrected_day(db, tmp_path):
    _seed_posted("fennick", "run-redate", day="2026-10-08", lane="subscription",
                usd=1.50)
    _apply(db, [_record("run-redate", day="2026-10-10", lane="subscription",
                        usd=1.50)], tmp_path)

    rows = _entries(db, run_id="run-redate")
    assert len(rows) == 2
    original = next(r for r in rows if r["dedup_key"] == "run-redate")
    corrected = next(r for r in rows if r["dedup_key"] == "run-redate:redated")
    assert original["voided_at"] is not None
    assert original["day"] == "2026-10-08"
    assert "ledger_history_correction" in original["void_reason"]
    assert corrected["voided_at"] is None
    assert corrected["day"] == "2026-10-10"
    assert corrected["lane"] == "subscription"
    assert corrected["usd_micro"] == 1_500_000


def test_a_lane_only_correction_leaves_the_day_unchanged(db, tmp_path):
    _seed_posted("fennick", "run-lane", day="2026-10-09", lane=None, usd=2.00)
    _apply(db, [_record("run-lane", day="2026-10-09", lane="subscription",
                        usd=2.00)], tmp_path)

    rows = _entries(db, run_id="run-lane")
    original = next(r for r in rows if r["dedup_key"] == "run-lane")
    corrected = next(r for r in rows if r["dedup_key"] == "run-lane:redated")
    assert original["voided_at"] is not None
    assert original["lane"] is None
    assert corrected["day"] == "2026-10-09"
    assert corrected["lane"] == "subscription"

    ledger = store.ensure_live_ledger("project", "fennick")
    conn = _conn(db)
    try:
        gateway_total = store.ledger_total(conn, ledger["id"], "2026-10-09")
        subscription_total = store.ledger_lane_total(
            conn, ledger["id"], "2026-10-09", "subscription")
    finally:
        conn.close()
    assert gateway_total == 0
    assert subscription_total == 2_000_000


def test_a_backfill_posts_under_the_bare_run_id(db, tmp_path):
    _apply(db, [_record("run-new", day="2026-10-15", lane="subscription",
                        usd=3.25, agent="coder", model="zai/glm-5.2",
                        item_id="item_01ABC")], tmp_path)

    rows = _entries(db, run_id="run-new")
    assert len(rows) == 1
    assert rows[0]["dedup_key"] == "run-new"
    assert rows[0]["day"] == "2026-10-15"
    assert rows[0]["lane"] == "subscription"
    assert rows[0]["usd_micro"] == 3_250_000
    assert rows[0]["agent"] == "coder"
    assert rows[0]["voided_at"] is None


def test_an_already_correct_entry_is_untouched(db, tmp_path):
    seeded = _seed_posted("fennick", "run-fine", day="2026-10-11",
                          lane="subscription", usd=0.75)
    _apply(db, [_record("run-fine", day="2026-10-11", lane="subscription",
                        usd=0.75)], tmp_path)

    rows = _entries(db, run_id="run-fine")
    assert len(rows) == 1
    assert rows[0]["id"] == seeded["id"]
    assert rows[0]["voided_at"] is None


def test_a_left_alone_no_lane_entry_with_no_run_record_is_reported_and_untouched(
        db, tmp_path, capsys):
    orphan = _seed_posted("mossbright", "run-unattributed-orphan",
                          day=backfill.UNATTRIBUTED_DAY, lane=None, usd=4.0)

    runs_path, gateway_path = _write_run_files(tmp_path, [])
    rc = backfill.main([str(db), str(runs_path), str(gateway_path)])
    assert rc == 0
    err = capsys.readouterr().err
    assert "left alone 1" in err

    rows = _entries(db, run_id="run-unattributed-orphan")
    assert len(rows) == 1
    assert rows[0]["id"] == orphan["id"]
    assert rows[0]["lane"] is None
    assert rows[0]["voided_at"] is None

    # Still untouched after --apply, since nothing in the run file names it.
    _apply(db, [], tmp_path, name="second")
    rows = _entries(db, run_id="run-unattributed-orphan")
    assert len(rows) == 1
    assert rows[0]["id"] == orphan["id"]
    assert rows[0]["voided_at"] is None


def test_a_refused_product_is_reported_and_not_booked(db, tmp_path, capsys):
    runs_path, gateway_path = _write_run_files(
        tmp_path, [_record("run-private", day="2026-10-12", lane="subscription",
                           product="driftwood", usd=9.99)])
    rc = backfill.main([str(db), str(runs_path), str(gateway_path)])
    assert rc == 0
    err = capsys.readouterr().err
    assert "refused 1" in err

    assert _entries(db, run_id="run-private") == []

    rc = backfill.main([str(db), str(runs_path), str(gateway_path), "--apply",
                        "--batch-size", "200"])
    assert rc == 0
    assert _entries(db, run_id="run-private") == []


def test_a_second_apply_pass_changes_nothing(db, tmp_path):
    _seed_posted("fennick", "run-redate-2x", day="2026-10-08", lane="subscription",
                usd=1.11)
    records = [_record("run-redate-2x", day="2026-10-13", lane="subscription",
                       usd=1.11),
              _record("run-new-2x", day="2026-10-13", lane="subscription",
                      usd=2.22)]
    _apply(db, records, tmp_path, name="first")
    first = _entries(db)

    _apply(db, records, tmp_path, name="second")
    second = _entries(db)
    assert second == first


def test_a_voided_row_is_never_deleted(db, tmp_path):
    seeded = _seed_posted("fennick", "run-keep-void", day="2026-10-08",
                          lane="subscription", usd=5.0)
    _apply(db, [_record("run-keep-void", day="2026-10-14", lane="subscription",
                        usd=5.0)], tmp_path)

    rows = _entries(db, run_id="run-keep-void")
    assert len(rows) == 2
    original = next(r for r in rows if r["id"] == seeded["id"])
    assert original["voided_at"] is not None
    assert original["day"] == "2026-10-08"


def test_a_later_bare_run_id_post_replays_the_voided_original_without_moving_a_total(
        db, tmp_path):
    _seed_posted("fennick", "run-race-replay", day="2026-10-08",
                lane="subscription", usd=6.0)
    _apply(db, [_record("run-race-replay", day="2026-10-16", lane="subscription",
                        usd=6.0)], tmp_path)

    ledger = store.ensure_live_ledger("project", "fennick")
    conn = _conn(db)
    try:
        before = store.ledger_lane_total(conn, ledger["id"], "2026-10-08",
                                         "subscription")
    finally:
        conn.close()

    # The Bosun's own sweeper, or anything else, posting again under the
    # bare run_id after this script's correction has landed.
    idempotent, replay, crossed = store.record_spend({
        "ledger_id": ledger["id"], "day": "2026-10-08",
        "usd_micro": backfill._usd_micro(6.0), "source": "agentctl",
        "lane": "subscription", "product": "fennick",
        "reported_by": "bosun-sweeper", "run_id": "run-race-replay",
        "dedup_key": "run-race-replay"})

    assert idempotent is True
    assert replay["day"] == "2026-10-08"
    assert replay["voided_at"] is not None
    assert crossed is None

    conn = _conn(db)
    try:
        after = store.ledger_lane_total(conn, ledger["id"], "2026-10-08",
                                        "subscription")
    finally:
        conn.close()
    assert after == before


def test_dry_run_writes_nothing(db, tmp_path):
    _seed_posted("fennick", "run-dry", day="2026-10-08", lane="subscription",
                usd=1.0)
    before = _entries(db)

    _apply(db, [_record("run-dry", day="2026-10-17", lane="subscription",
                        usd=1.0),
               _record("run-dry-new", day="2026-10-17", lane="gateway",
                       usd=0.05)],
          tmp_path, write=False)

    assert _entries(db) == before
