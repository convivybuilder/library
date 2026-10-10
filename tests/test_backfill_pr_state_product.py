"""The one-time backfill of `product` on existing `pr_state` signals so it
matches the crew registry (issue_01M2K33M9NG0S9HCJQJ2EPD7RP). Every
assertion here runs against a temporary database and a temporary registry
file, never the live `~/.library/library.db` or the real
`agent-platform/config/projects.json`."""
from __future__ import annotations

import json
import sqlite3

import pytest

import backfill_pr_state_product as backfill
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


def _insert(conn, id_, *, kind="pr_state", product="platform", repo="convivy/library",
           meta_extra=None, refs=None, dedup_key=None):
    meta = {"repo": repo}
    if meta_extra:
        meta.update(meta_extra)
    if dedup_key is None:
        dedup_key = f"pr-state:{repo}#1:{id_}"
    conn.execute(
        "INSERT INTO signals (id, source, refs, meta, body, product, kind, "
        "actor, ingested_by, privacy_class, dedup_key) VALUES (?, 'github', "
        "?, ?, 'x', ?, ?, 'pr-state-capture', 'bosun', 'internal', ?)",
        (id_, json.dumps(refs or {}), json.dumps(meta), product, kind, dedup_key))


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
        "convivy-deck": {"repos": ["convivy-deck"]},
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
    yield path
    projects.reload()


def _conn(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def test_a_platform_row_whose_repo_resolves_elsewhere_is_corrected(db):
    conn = _conn(db)
    _insert(conn, "sig_a", product="platform", repo="convivy/library")
    conn.commit()
    conn.close()

    assert backfill.run(write=True) == 0

    conn = _conn(db)
    row = conn.execute("SELECT product, meta FROM signals WHERE id = 'sig_a'").fetchone()
    conn.close()
    assert row["product"] == "library"
    assert json.loads(row["meta"]) == {"repo": "convivy/library"}, (
        "meta must be left exactly as it was")


def test_a_row_already_matching_the_registry_is_untouched(db):
    conn = _conn(db)
    _insert(conn, "sig_b", product="vivy", repo="convivy/vivy")
    conn.commit()
    conn.close()

    changes, unresolved = backfill.plan(store.connect())
    assert changes == []
    assert unresolved == []

    assert backfill.run(write=True) == 0
    conn = _conn(db)
    row = conn.execute("SELECT product FROM signals WHERE id = 'sig_b'").fetchone()
    conn.close()
    assert row["product"] == "vivy"


def test_an_unresolvable_repo_is_untouched_reported_and_exits_nonzero(db, capsys):
    conn = _conn(db)
    _insert(conn, "sig_c", product="platform", repo="convivy/no-such-repo")
    conn.commit()
    conn.close()

    exit_code = backfill.run(write=True)
    assert exit_code != 0

    out = capsys.readouterr()
    assert "convivy/no-such-repo" in out.err

    conn = _conn(db)
    row = conn.execute("SELECT product FROM signals WHERE id = 'sig_c'").fetchone()
    conn.close()
    assert row["product"] == "platform", (
        "a row whose repo resolves to no product must be left alone")


def test_an_unavailable_registry_changes_nothing_and_exits_nonzero(db, monkeypatch):
    conn = _conn(db)
    _insert(conn, "sig_d", product="platform", repo="convivy/library")
    conn.commit()
    conn.close()

    monkeypatch.setattr(projects, "REGISTRY_PATH", db.parent / "does-not-exist.json")
    projects.reload()

    assert backfill.run(write=True) != 0

    conn = _conn(db)
    row = conn.execute("SELECT product FROM signals WHERE id = 'sig_d'").fetchone()
    conn.close()
    assert row["product"] == "platform", (
        "an unreadable registry must never read as nothing to correct")


def test_dry_run_writes_nothing(db, capsys):
    conn = _conn(db)
    _insert(conn, "sig_e", product="platform", repo="convivy/library")
    conn.commit()
    conn.close()

    assert backfill.run(write=False) == 0
    out = capsys.readouterr()
    assert "would update convivy/library platform->library: 1 row(s)" in out.out

    conn = _conn(db)
    row = conn.execute("SELECT product FROM signals WHERE id = 'sig_e'").fetchone()
    conn.close()
    assert row["product"] == "platform", "a dry run must not have written anything"


def test_running_write_twice_is_a_clean_no_op_the_second_time(db):
    conn = _conn(db)
    _insert(conn, "sig_f", product="platform", repo="convivy/library")
    conn.commit()
    conn.close()

    first_changes, _ = backfill.plan(store.connect())
    assert len(first_changes) == 1
    assert backfill.run(write=True) == 0

    second_changes, _ = backfill.plan(store.connect())
    assert second_changes == [], (
        "a row already corrected must not be selected again — re-running "
        "with --write must be a clean no-op over it")
    assert backfill.run(write=True) == 0


def test_a_non_pr_state_row_under_platform_is_untouched(db):
    conn = _conn(db)
    _insert(conn, "sig_g", kind="model_resolution", product="platform",
            repo="convivy/library")
    conn.commit()
    conn.close()

    changes, unresolved = backfill.plan(store.connect())
    assert changes == []
    assert unresolved == []

    assert backfill.run(write=True) == 0
    conn = _conn(db)
    row = conn.execute("SELECT product FROM signals WHERE id = 'sig_g'").fetchone()
    conn.close()
    assert row["product"] == "platform", (
        "a non-pr_state row carries no repository to resolve from and must "
        "never be touched, even though it sits under product='platform' too")


def test_bare_repo_derivation_matches_review_record_products(db):
    assert backfill._bare_repo("convivy/convivy-deck") == "convivy-deck"
    assert backfill._bare_repo("convivy/library") == "library"


def test_write_is_refused_while_writes_are_disabled(db, monkeypatch):
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    assert backfill.run(write=True) == 2


# --- the visibility precondition ----------------------------------------------


def _narrow_reader(tmp_path, products):
    identity.TOKENS.write_text(json.dumps({
        "tok-a": {"name": "orient", "products": ["*"]},
        "tok-narrow": {"name": "narrow-reader", "products": products},
    }))


def test_all_wildcard_readers_meet_the_precondition(db, capsys):
    conn = _conn(db)
    _insert(conn, "sig_a", product="platform", repo="convivy/library")
    conn.commit()
    conn.close()
    assert backfill.run(write=False) == 0
    assert "precondition met" in capsys.readouterr().err


def test_a_narrow_reader_holding_a_moved_product_fails_the_precondition(db, tmp_path, capsys):
    """Moving a row out of `platform` removes it from a reader scoped to
    `platform`, which is the visibility change the correction must not make
    silently."""
    _narrow_reader(tmp_path, ["platform"])
    conn = _conn(db)
    _insert(conn, "sig_a", product="platform", repo="convivy/library")
    conn.commit()
    conn.close()
    assert backfill.run(write=False) == 1
    err = capsys.readouterr().err
    assert "PRECONDITION NOT MET" in err and "narrow-reader" in err


def test_a_narrow_reader_on_an_untouched_product_meets_the_precondition(db, tmp_path, capsys):
    """The live case on 2026-09-16: two `vivy`-scoped readers, and no `vivy`
    row moving."""
    _narrow_reader(tmp_path, ["vivy"])
    conn = _conn(db)
    _insert(conn, "sig_a", product="platform", repo="convivy/library")
    conn.commit()
    conn.close()
    assert backfill.run(write=False) == 0
    assert "precondition met" in capsys.readouterr().err


def test_write_refuses_a_failed_precondition_without_the_flag(db, tmp_path):
    _narrow_reader(tmp_path, ["library"])
    conn = _conn(db)
    _insert(conn, "sig_a", product="platform", repo="convivy/library")
    conn.commit()
    conn.close()
    assert backfill.run(write=True) == 2
    conn = _conn(db)
    assert conn.execute("SELECT product FROM signals WHERE id='sig_a'").fetchone()[0] == "platform"
    conn.close()


def test_write_proceeds_when_the_scope_change_is_accepted(db, tmp_path):
    _narrow_reader(tmp_path, ["library"])
    conn = _conn(db)
    _insert(conn, "sig_a", product="platform", repo="convivy/library")
    conn.commit()
    conn.close()
    backfill.run(write=True, accept_scope_change=True)
    conn = _conn(db)
    assert conn.execute("SELECT product FROM signals WHERE id='sig_a'").fetchone()[0] == "library"
    conn.close()


def test_the_scope_report_never_prints_a_token(db, tmp_path, capsys):
    _narrow_reader(tmp_path, ["platform"])
    conn = _conn(db)
    _insert(conn, "sig_a", product="platform", repo="convivy/library")
    conn.commit()
    conn.close()
    backfill.run(write=False)
    out = capsys.readouterr()
    assert "tok-a" not in out.out + out.err
    assert "tok-narrow" not in out.out + out.err
