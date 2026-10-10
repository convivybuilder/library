"""The one-time backfill of `meta.status`/`meta.run_url` onto the CI-poller
rows written before `bin/ci_result_fetch.py` populated them
(issue_01M1F431KC6DPYY2D185GD036J). Every assertion here runs against a
temporary database, never `~/.library/library.db` — the coder brief is
explicit that this script must not touch the real one."""
from __future__ import annotations

import json
import sqlite3

import pytest

import backfill_ci_meta_status as backfill
import store

SCHEMA = """
CREATE TABLE signals (
    id TEXT PRIMARY KEY, source TEXT, stream TEXT, kind TEXT, actor TEXT,
    ingested_by TEXT, product TEXT, refs TEXT, body TEXT, meta TEXT,
    privacy_class TEXT, produced_at TEXT, ingested_at TEXT,
    embedded_at TEXT, dedup_key TEXT UNIQUE, acting_human TEXT);
"""


def _insert(conn, id_, *, source="ci", refs=None, meta=None, dedup_key=None):
    """One signal row.

    **`dedup_key` defaults to the fetcher's own shape**, `ci:<product>:<workflow
    -slug>:<run-id>`, because the backfill selects on it as well as on `source`
    — a row without it is not a row this script would ever touch in production,
    so a fixture lacking one tests a population that does not exist. Pass an
    explicit key to build a row that deliberately does NOT look like the
    fetcher's."""
    if dedup_key is None:
        dedup_key = f"ci:vivy:e2e-stage-smoke:{id_}"
    conn.execute(
        "INSERT INTO signals (id, source, refs, meta, body, product, kind, "
        "actor, ingested_by, privacy_class, dedup_key) VALUES (?, ?, ?, ?, 'x', "
        "'vivy', 'test-result', 'github-actions', 'ci-fetch', 'internal', ?)",
        (id_, source, json.dumps(refs or {}), json.dumps(meta or {}), dedup_key))


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "library.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    monkeypatch.setattr(store, "DB_PATH", path)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    return path


def _conn(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def test_a_poller_row_with_a_success_conclusion_gets_status_pass_and_run_url(db):
    conn = _conn(db)
    _insert(conn, "sig_a",
            refs={"conclusion": "success", "workflow": "e2e-stage-smoke",
                  "commit": "deadbeef",
                  "run_url": "https://github.com/convivy/vivy/actions/runs/1"},
            meta={"run_number": 1, "event": "schedule"})
    conn.commit()
    conn.close()

    assert backfill.run(write=True) == 0

    conn = _conn(db)
    row = conn.execute("SELECT refs, meta FROM signals WHERE id = 'sig_a'").fetchone()
    conn.close()
    meta = json.loads(row["meta"])
    assert meta["status"] == "pass"
    assert meta["run_url"] == "https://github.com/convivy/vivy/actions/runs/1"
    assert meta["run_number"] == 1 and meta["event"] == "schedule"
    refs = json.loads(row["refs"])
    assert refs == {"conclusion": "success", "workflow": "e2e-stage-smoke",
                    "commit": "deadbeef",
                    "run_url": "https://github.com/convivy/vivy/actions/runs/1"}, (
        "refs must be left exactly as it was — the raw conclusion is its "
        "only remaining home once meta.status maps it away")


def test_a_cancelled_conclusion_gets_run_url_but_no_meta_status(db):
    conn = _conn(db)
    _insert(conn, "sig_b",
            refs={"conclusion": "cancelled", "workflow": "e2e-stage-smoke",
                  "commit": "deadbeef",
                  "run_url": "https://github.com/convivy/vivy/actions/runs/2"},
            meta={"run_number": 2, "event": "schedule"})
    conn.commit()
    conn.close()

    assert backfill.run(write=True) == 0

    conn = _conn(db)
    meta = json.loads(conn.execute(
        "SELECT meta FROM signals WHERE id = 'sig_b'").fetchone()["meta"])
    conn.close()
    assert "status" not in meta, (
        "an unrecognised conclusion must not be forced into a guessed "
        "pass/fail — Orient renders the honest 'unknown' from its absence")
    assert meta["run_url"] == "https://github.com/convivy/vivy/actions/runs/2"


def test_dry_run_reports_but_writes_nothing(db, capsys):
    conn = _conn(db)
    _insert(conn, "sig_c",
            refs={"conclusion": "success",
                  "run_url": "https://github.com/convivy/vivy/actions/runs/3"},
            meta={"run_number": 3})
    conn.commit()
    conn.close()

    assert backfill.run(write=False) == 0
    out = capsys.readouterr()
    assert "would update sig_c" in out.out

    conn = _conn(db)
    meta = json.loads(conn.execute(
        "SELECT meta FROM signals WHERE id = 'sig_c'").fetchone()["meta"])
    conn.close()
    assert "status" not in meta, "a dry run must not have written anything"


def test_a_row_that_already_has_meta_status_is_left_alone(db):
    """The retired push path's own rows carry `meta.status` already — this
    script must not touch them, proving the selection is scoped to the
    poller's pre-fix shape rather than every `source = 'ci'` row."""
    conn = _conn(db)
    _insert(conn, "sig_pushed",
            refs={},
            meta={"status": "pass", "run_url": "https://example.com/already"})
    conn.commit()
    conn.close()

    changes = backfill.plan(store.connect())
    assert changes == [], (
        "a row that already has meta.status must not be selected for backfill")


def test_running_twice_with_write_is_a_clean_no_op_the_second_time(db):
    conn = _conn(db)
    _insert(conn, "sig_d",
            refs={"conclusion": "failure",
                  "run_url": "https://github.com/convivy/vivy/actions/runs/4"},
            meta={"run_number": 4})
    conn.commit()
    conn.close()

    first = backfill.plan(store.connect())
    assert len(first) == 1
    backfill.run(write=True)

    second = backfill.plan(store.connect())
    assert second == [], (
        "a row already backfilled must not be selected again — re-running "
        "with --write must be a clean no-op over it")


def test_a_row_with_no_conclusion_in_refs_is_never_selected(db):
    """A non-poller `source = 'ci'` row with nothing in `refs` at all (or one
    already partially hand-edited) must not be treated as backfillable —
    only rows carrying `refs.conclusion`, the poller's own shape, qualify."""
    conn = _conn(db)
    _insert(conn, "sig_e", refs={}, meta={})
    conn.commit()
    conn.close()

    assert backfill.plan(store.connect()) == []


def test_a_row_not_carrying_the_fetchers_dedup_key_is_never_selected(db):
    """A row with the poller's SHAPE but not its provenance is left alone.

    `POST /signals` lets any caller holding `signal.create` choose `source` and
    `refs`, so "source='ci' with a refs.conclusion" is a shape anyone can mint
    (security review, 2026-09-01). Selection therefore also requires the
    fetcher's own `ci:<product>:<workflow-slug>:<run-id>` key. This is not
    unforgeable — a caller can choose that key too — and it does stop a re-run
    relabelling a row that never came through this path on shape alone.
    """
    conn = _conn(db)
    _insert(conn, "sig_mimic", refs={"conclusion": "failure",
                                     "run_url": "https://example.invalid/1"},
            meta={}, dedup_key="something-else:1")
    conn.commit()
    conn.close()

    assert backfill.plan(store.connect()) == []


def test_write_is_refused_while_writes_are_disabled(db, monkeypatch):
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    with pytest.raises(store.WritesDisabled):
        backfill.run(write=True)


def test_main_defaults_to_dry_run(db, capsys):
    conn = _conn(db)
    _insert(conn, "sig_f",
            refs={"conclusion": "success",
                  "run_url": "https://github.com/convivy/vivy/actions/runs/5"},
            meta={"run_number": 5})
    conn.commit()
    conn.close()

    assert backfill.main([]) == 0
    assert "would update sig_f" in capsys.readouterr().out

    conn = _conn(db)
    meta = json.loads(conn.execute(
        "SELECT meta FROM signals WHERE id = 'sig_f'").fetchone()["meta"])
    conn.close()
    assert "status" not in meta, "main() with no flags must not write"


def test_main_write_flag_actually_writes(db):
    conn = _conn(db)
    _insert(conn, "sig_g",
            refs={"conclusion": "success",
                  "run_url": "https://github.com/convivy/vivy/actions/runs/6"},
            meta={"run_number": 6})
    conn.commit()
    conn.close()

    assert backfill.main(["--write"]) == 0

    conn = _conn(db)
    meta = json.loads(conn.execute(
        "SELECT meta FROM signals WHERE id = 'sig_g'").fetchone()["meta"])
    conn.close()
    assert meta["status"] == "pass"
