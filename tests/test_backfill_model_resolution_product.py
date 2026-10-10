"""The one-time backfill of `product` on existing `model_resolution` signals
so it matches the crew registry (issue_01M2K33M9NG0S9HCJQJ2EPD7RP). Every
assertion here runs against a temporary database, a temporary registry
file, temporary token files, and a temporary run-directory tree — never the
live `~/.library/library.db`, `agent-platform/config/projects.json`, or
`~/Convivy/agent-runs`."""
from __future__ import annotations

import json
import sqlite3
import types

import pytest

import backfill_common
import backfill_model_resolution_product as backfill
import identity
import projects
import store

SCHEMA = """
CREATE TABLE signals (
    id TEXT PRIMARY KEY, source TEXT, stream TEXT, kind TEXT, actor TEXT,
    ingested_by TEXT, product TEXT, refs TEXT, body TEXT, meta TEXT,
    privacy_class TEXT, produced_at TEXT, ingested_at TEXT,
    embedded_at TEXT, dedup_key TEXT UNIQUE, acting_human TEXT);
"""


def _insert(conn, id_, *, kind="model_resolution", product="platform",
            run="run-aaaaaaaa", meta_extra=None, dedup_key=None):
    meta = {}
    if run is not None:
        meta["run"] = run
    if meta_extra:
        meta.update(meta_extra)
    if dedup_key is None:
        dedup_key = f"model-resolution-{id_}"
    conn.execute(
        "INSERT INTO signals (id, source, refs, meta, body, product, kind, "
        "actor, ingested_by, privacy_class, dedup_key) VALUES (?, "
        "'agent-platform', '{}', ?, 'x', ?, ?, 'agentctl', 'bosun', "
        "'internal', ?)",
        (id_, json.dumps(meta), product, kind, dedup_key))


def _write_run(root, date, run_id, *, repo="agent-platform", omit_repo=False,
                bad_json=False):
    run_dir = root / date / run_id
    run_dir.mkdir(parents=True)
    meta_path = run_dir / "meta.json"
    if bad_json:
        meta_path.write_text("{not valid json")
    elif omit_repo:
        meta_path.write_text(json.dumps({"run_id": run_id}))
    else:
        meta_path.write_text(json.dumps({"run_id": run_id, "repo": repo}))
    return run_dir


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "library.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    monkeypatch.setattr(store, "DB_PATH", path)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)

    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({
        "_meta": {},
        "library": {"repos": ["library"]},
        "agent-platform": {"repos": ["agent-platform"]},
        "vivy": {"repos": ["vivy"]},
    }))
    monkeypatch.setattr(projects, "REGISTRY_PATH", registry)
    projects.reload()

    # NEVER THE LIVE TOKEN FILES. The script reads every identity's scope, so
    # an unpatched test would read this machine's real credentials and pass or
    # fail on whoever happens to hold a token today. Default: every reader `*`.
    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({"tok-a": {"name": "orient", "products": ["*"]}}))
    client = tmp_path / "client-tokens.json"
    client.write_text(json.dumps({}))
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(identity, "CLIENT_TOKENS", client)

    run_root = tmp_path / "agent-runs"
    run_root.mkdir()
    monkeypatch.setattr(backfill, "RUN_ROOT", run_root)

    yield types.SimpleNamespace(db=path, runs=run_root)
    projects.reload()


def _conn(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def test_a_platform_row_whose_runs_repo_resolves_elsewhere_is_corrected(db):
    _write_run(db.runs, "2026-09-16", "run-aaaaaaaa", repo="agent-platform")
    conn = _conn(db.db)
    _insert(conn, "sig_a", product="platform", run="run-aaaaaaaa")
    conn.commit()
    conn.close()

    assert backfill.run(write=True) == 0

    conn = _conn(db.db)
    row = conn.execute("SELECT product, meta FROM signals WHERE id = 'sig_a'").fetchone()
    conn.close()
    assert row["product"] == "agent-platform"
    assert json.loads(row["meta"]) == {"run": "run-aaaaaaaa"}, (
        "meta must be left exactly as it was")


def test_a_row_whose_run_directory_is_missing_is_untouched_reported_and_exits_nonzero(db, capsys):
    conn = _conn(db.db)
    _insert(conn, "sig_b", product="platform", run="run-ffffffff")
    conn.commit()
    conn.close()

    exit_code = backfill.run(write=True)
    assert exit_code != 0

    out = capsys.readouterr()
    assert "run-ffffffff" in out.err

    conn = _conn(db.db)
    row = conn.execute("SELECT product FROM signals WHERE id = 'sig_b'").fetchone()
    conn.close()
    assert row["product"] == "platform", (
        "a row whose run directory can't be found must be left alone")


def test_a_row_with_no_meta_run_is_untouched_and_reported(db, capsys):
    conn = _conn(db.db)
    _insert(conn, "sig_c", product="platform", run=None)
    conn.commit()
    conn.close()

    exit_code = backfill.run(write=True)
    assert exit_code != 0

    out = capsys.readouterr()
    assert "no meta.run" in out.err

    conn = _conn(db.db)
    row = conn.execute("SELECT product FROM signals WHERE id = 'sig_c'").fetchone()
    conn.close()
    assert row["product"] == "platform", (
        "a row with no run to look up must be left alone")


def test_a_run_whose_meta_json_has_no_repo_is_untouched_and_reported(db, capsys):
    _write_run(db.runs, "2026-09-16", "run-bbbbbbbb", omit_repo=True)
    conn = _conn(db.db)
    _insert(conn, "sig_d", product="platform", run="run-bbbbbbbb")
    conn.commit()
    conn.close()

    exit_code = backfill.run(write=True)
    assert exit_code != 0

    out = capsys.readouterr()
    assert "run-bbbbbbbb" in out.err
    assert "no usable repo" in out.err

    conn = _conn(db.db)
    row = conn.execute("SELECT product FROM signals WHERE id = 'sig_d'").fetchone()
    conn.close()
    assert row["product"] == "platform", (
        "a run whose own record names no repo must be left alone")


def test_a_row_already_matching_the_registry_is_untouched(db):
    _write_run(db.runs, "2026-09-16", "run-cccccccc", repo="vivy")
    conn = _conn(db.db)
    _insert(conn, "sig_e", product="vivy", run="run-cccccccc")
    conn.commit()
    conn.close()

    changes, no_run, missing_run_dir, no_repo, unresolved = backfill.plan(store.connect())
    assert changes == []
    assert no_run == []
    assert missing_run_dir == []
    assert no_repo == []
    assert unresolved == []

    assert backfill.run(write=True) == 0
    conn = _conn(db.db)
    row = conn.execute("SELECT product FROM signals WHERE id = 'sig_e'").fetchone()
    conn.close()
    assert row["product"] == "vivy"


def test_dry_run_writes_nothing(db, capsys):
    _write_run(db.runs, "2026-09-16", "run-dddddddd", repo="agent-platform")
    conn = _conn(db.db)
    _insert(conn, "sig_f", product="platform", run="run-dddddddd")
    conn.commit()
    conn.close()

    assert backfill.run(write=False) == 0
    out = capsys.readouterr()
    assert "would update agent-platform platform->agent-platform: 1 row(s)" in out.out

    conn = _conn(db.db)
    row = conn.execute("SELECT product FROM signals WHERE id = 'sig_f'").fetchone()
    conn.close()
    assert row["product"] == "platform", "a dry run must not have written anything"


def test_running_write_twice_is_a_clean_no_op_the_second_time(db):
    _write_run(db.runs, "2026-09-16", "run-eeeeeeee", repo="agent-platform")
    conn = _conn(db.db)
    _insert(conn, "sig_g", product="platform", run="run-eeeeeeee")
    conn.commit()
    conn.close()

    first_changes, *_ = backfill.plan(store.connect())
    assert len(first_changes) == 1
    assert backfill.run(write=True) == 0

    second_changes, *_ = backfill.plan(store.connect())
    assert second_changes == [], (
        "a row already corrected must not be selected again — re-running "
        "with --write must be a clean no-op over it")
    assert backfill.run(write=True) == 0


def test_a_non_model_resolution_row_under_platform_is_untouched(db):
    _write_run(db.runs, "2026-09-16", "run-11111111", repo="agent-platform")
    conn = _conn(db.db)
    _insert(conn, "sig_h", kind="pr_state", product="platform", run="run-11111111",
            meta_extra={"repo": "convivy/agent-platform"})
    conn.commit()
    conn.close()

    changes, no_run, missing_run_dir, no_repo, unresolved = backfill.plan(store.connect())
    assert changes == []
    assert no_run == []
    assert missing_run_dir == []
    assert no_repo == []
    assert unresolved == []

    assert backfill.run(write=True) == 0
    conn = _conn(db.db)
    row = conn.execute("SELECT product FROM signals WHERE id = 'sig_h'").fetchone()
    conn.close()
    assert row["product"] == "platform", (
        "a non-model_resolution row must never be touched, even though it "
        "sits under product='platform' too")


def test_write_is_refused_while_writes_are_disabled(db, monkeypatch):
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    assert backfill.run(write=True) == 2


def test_an_unavailable_registry_changes_nothing_and_exits_nonzero(db, monkeypatch):
    _write_run(db.runs, "2026-09-16", "run-99999999", repo="agent-platform")
    conn = _conn(db.db)
    _insert(conn, "sig_i", product="platform", run="run-99999999")
    conn.commit()
    conn.close()

    monkeypatch.setattr(projects, "REGISTRY_PATH", db.db.parent / "does-not-exist.json")
    projects.reload()

    assert backfill.run(write=True) != 0

    conn = _conn(db.db)
    row = conn.execute("SELECT product FROM signals WHERE id = 'sig_i'").fetchone()
    conn.close()
    assert row["product"] == "platform", (
        "an unreadable registry must never read as nothing to correct")


# --- the visibility precondition, the same set backfill_pr_state_product.py has ---


def _narrow_reader(tmp_path, products):
    identity.TOKENS.write_text(json.dumps({
        "tok-a": {"name": "orient", "products": ["*"]},
        "tok-narrow": {"name": "narrow-reader", "products": products},
    }))


def test_all_wildcard_readers_meet_the_precondition(db, capsys):
    _write_run(db.runs, "2026-09-16", "run-22222222", repo="agent-platform")
    conn = _conn(db.db)
    _insert(conn, "sig_a", product="platform", run="run-22222222")
    conn.commit()
    conn.close()
    assert backfill.run(write=False) == 0
    assert "precondition met" in capsys.readouterr().err


def test_a_narrow_reader_holding_a_moved_product_fails_the_precondition(db, tmp_path, capsys):
    """Moving a row out of `platform` removes it from a reader scoped to
    `platform`, which is the visibility change the correction must not make
    silently."""
    _narrow_reader(tmp_path, ["platform"])
    _write_run(db.runs, "2026-09-16", "run-33333333", repo="agent-platform")
    conn = _conn(db.db)
    _insert(conn, "sig_a", product="platform", run="run-33333333")
    conn.commit()
    conn.close()
    assert backfill.run(write=False) == 1
    err = capsys.readouterr().err
    assert "PRECONDITION NOT MET" in err and "narrow-reader" in err


def test_a_narrow_reader_on_an_untouched_product_meets_the_precondition(db, tmp_path, capsys):
    _narrow_reader(tmp_path, ["vivy"])
    _write_run(db.runs, "2026-09-16", "run-44444444", repo="agent-platform")
    conn = _conn(db.db)
    _insert(conn, "sig_a", product="platform", run="run-44444444")
    conn.commit()
    conn.close()
    assert backfill.run(write=False) == 0
    assert "precondition met" in capsys.readouterr().err


def test_write_refuses_a_failed_precondition_without_the_flag(db, tmp_path):
    _narrow_reader(tmp_path, ["agent-platform"])
    _write_run(db.runs, "2026-09-16", "run-55555555", repo="agent-platform")
    conn = _conn(db.db)
    _insert(conn, "sig_a", product="platform", run="run-55555555")
    conn.commit()
    conn.close()
    assert backfill.run(write=True) == 2
    conn = _conn(db.db)
    assert conn.execute("SELECT product FROM signals WHERE id='sig_a'").fetchone()[0] == "platform"
    conn.close()


def test_write_proceeds_when_the_scope_change_is_accepted(db, tmp_path):
    _narrow_reader(tmp_path, ["agent-platform"])
    _write_run(db.runs, "2026-09-16", "run-66666666", repo="agent-platform")
    conn = _conn(db.db)
    _insert(conn, "sig_a", product="platform", run="run-66666666")
    conn.commit()
    conn.close()
    backfill.run(write=True, accept_scope_change=True)
    conn = _conn(db.db)
    assert conn.execute("SELECT product FROM signals WHERE id='sig_a'").fetchone()[0] == "agent-platform"
    conn.close()


def test_the_scope_report_never_prints_a_token(db, tmp_path, capsys):
    _narrow_reader(tmp_path, ["platform"])
    _write_run(db.runs, "2026-09-16", "run-77777777", repo="agent-platform")
    conn = _conn(db.db)
    _insert(conn, "sig_a", product="platform", run="run-77777777")
    conn.commit()
    conn.close()
    backfill.run(write=False)
    out = capsys.readouterr()
    assert "tok-a" not in out.out + out.err
    assert "tok-narrow" not in out.out + out.err


# --- the paced writer, shared with backfill_pr_state_product.py -----------------


def test_the_paced_writer_sleeps_between_batches_and_not_after_the_last(db, monkeypatch):
    """A regression guard for the live-store incident `backfill_common`'s
    docstring records: this must fail if the pause between commits is ever
    removed."""
    monkeypatch.setattr(backfill_common, "BATCH_SIZE", 1)
    conn = _conn(db.db)
    for id_ in ("sig_p1", "sig_p2", "sig_p3"):
        _insert(conn, id_, product="platform", run="run-paced")
    conn.commit()
    conn.close()

    changes = [{"repo": "agent-platform", "old_product": "platform",
                "new_product": "agent-platform",
                "ids": ["sig_p1", "sig_p2", "sig_p3"]}]

    calls = []
    backfill_common.apply_changes(changes, sleep=calls.append)

    assert calls == [backfill_common.BATCH_SLEEP_SECONDS] * 2, (
        "three batches of one row each (BATCH_SIZE patched to 1) must sleep "
        "between the 1st and 2nd batch and between the 2nd and 3rd, but "
        "never before the first or after the last")

    conn = _conn(db.db)
    rows = conn.execute(
        "SELECT product FROM signals WHERE id IN ('sig_p1','sig_p2','sig_p3') "
        "ORDER BY id").fetchall()
    conn.close()
    assert [r["product"] for r in rows] == ["agent-platform"] * 3
