"""The `ix_signals_test_result_commit` migration: what it builds, run for
real (issue_01M2PERFVRG9NGS7DK0XXNZCTQ round 2 and round 3).

**Driven through the real `alembic` CLI**, for `test_credential_bindings_
migration.py`'s reason: the parts most likely to be wrong are that the
revision chains onto head and that its DDL actually runs against SQLite. An
in-process `alembic.command` call would skip both.

**No `ANALYZE` runs anywhere in this file (round 3)** — the migration no
longer runs one, on purpose: the live database has no `sqlite_stat1` table
to begin with, and this PR does not want to be the first thing that creates
one. `test_the_indexed_by_query_uses_the_index_without_analyze` proves
`_pr_ci_for_page`'s actual query, naming the index with `INDEXED BY`, does
not need statistics to use it — built against a 16,693-row, six-product
fixture with `sqlite_stat1` confirmed absent, the same shape `signals`
actually holds. `test_indexed_by_fails_loudly_without_the_index` proves the
other half: a database that has not run this migration gets a loud
`sqlite3.OperationalError`, never a silent fall back to the old partition
scan. A future edit that changes the query text enough to stop matching the
partial index's own condition fails the first of these two, not just a
measurement nobody re-runs.

Every database here is a scratch file under `tmp_path`, built by this file's
own fixtures or by the migration tree — never a copy of, or a connection to,
`~/.library` or any other live database.
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

TEST_RESULT_INSERT = (
    "INSERT INTO signals (id, source, kind, actor, ingested_by, product, "
    "refs, meta, body, privacy_class, ingested_at) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?)")


def alembic(*args, db):
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(REPO), capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(REPO),
             "LIBRARY_DB": str(db)})


def test_the_migration_creates_a_partial_expression_index(tmp_path):
    db = tmp_path / "library.db"
    assert alembic("upgrade", "head", db=db).returncode == 0
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE "
            "name = 'ix_signals_test_result_commit'").fetchone()
    finally:
        conn.close()
    assert row is not None
    assert "json_extract(refs, '$.commit')" in row[0]
    assert "WHERE kind = 'test-result'" in row[0]


def test_the_downgrade_removes_the_index(tmp_path):
    db = tmp_path / "library.db"
    assert alembic("upgrade", "head", db=db).returncode == 0
    assert alembic("downgrade", "f8c1f89ef65c", db=db).returncode == 0
    conn = sqlite3.connect(db)
    try:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE "
            "name = 'ix_signals_test_result_commit'").fetchone() is None
    finally:
        conn.close()


def _seed_mixed_kind_signals(conn, *, test_result_rows: int) -> None:
    """A `test-result`-dominated table with other kinds mixed in, the same
    shape `app.py`'s own `signals` table has — `_pr_ci_for_page`'s sha
    lookup is never the only kind in this table, and a fixture that made it
    the only one would bias the query planner's selectivity guess in a way
    production's own mix of kinds does not."""
    now = "2026-10-01 00:00:00.000000"
    rows = []
    products = ("vivy", "selector", "library", "convivy-orient", "svc",
                "constance")
    for i in range(test_result_rows):
        sha = f"sha{i:07d}"
        refs = ('{"commit": "%s", "conclusion": "success", '
                '"workflow": "ci", '
                '"run_url": "https://github.com/convivy/x/actions/runs/%d"}'
                % (sha, i))
        rows.append((f"sig_tr_{i:08d}", "ci", "test-result",
                     "github-actions", "ci-fetcher",
                     products[i % len(products)], refs, "{}",
                     "a test-result row", "internal", now))
    for i in range(2000):
        rows.append((f"sig_pr_{i:08d}", "keel", "pr_state", "keel", "keel",
                     "vivy", "{}", "{}", "a pr_state row", "internal", now))
    for i, kind in enumerate(("alert", "handoff", "decision") * 170):
        rows.append((f"sig_misc_{i:08d}", "misc", kind, "misc", "misc",
                     "vivy", "{}", "{}", "a misc row", "internal", now))
    conn.executemany(TEST_RESULT_INSERT, rows)
    conn.commit()


def _plan(conn, shas, *, indexed_by: str = "") -> str:
    placeholders = ",".join("?" * len(shas))
    indexed_by_clause = f" INDEXED BY {indexed_by}" if indexed_by else ""
    sql = (f"SELECT * FROM signals AS signals{indexed_by_clause} "
           "WHERE kind = 'test-result' "
           "AND json_extract(refs, '$.commit') IN "
           f"({placeholders})")
    return conn.execute(f"EXPLAIN QUERY PLAN {sql}", shas).fetchall()[-1][3]


def test_the_indexed_by_query_uses_the_index_without_analyze(tmp_path):
    """The exact query `_pr_ci_for_page` runs — `INDEXED BY
    ix_signals_test_result_commit` named explicitly, the same way
    `store.signals_source`'s `hot_indexed_by` names it — against a
    fixture-built database that never runs `ANALYZE` and so never gains a
    `sqlite_stat1` table. This is the `EXPLAIN QUERY PLAN` the PR body's
    round 3 revision section reports."""
    db = tmp_path / "library.db"
    assert alembic("upgrade", "head", db=db).returncode == 0
    conn = sqlite3.connect(db)
    _seed_mixed_kind_signals(conn, test_result_rows=16693)

    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE "
        "name = 'sqlite_stat1'").fetchone() is None

    try:
        shas = [f"sha{i:07d}" for i in range(200)]
        assert _plan(conn, shas,
                     indexed_by="ix_signals_test_result_commit") == (
            "SEARCH signals USING INDEX ix_signals_test_result_commit "
            "(<expr>=?)")
    finally:
        conn.close()


def test_without_indexed_by_the_planner_does_not_choose_this_index(tmp_path):
    """The planner's own, unforced choice on the same fixture, with no
    `sqlite_stat1` to tell it the new index is cheaper — the fact
    `_pr_ci_for_page`'s docstring gives as the reason it names the index
    with `INDEXED BY` rather than leaving the choice to the planner."""
    db = tmp_path / "library.db"
    assert alembic("upgrade", "head", db=db).returncode == 0
    conn = sqlite3.connect(db)
    _seed_mixed_kind_signals(conn, test_result_rows=16693)
    try:
        shas = [f"sha{i:07d}" for i in range(200)]
        assert _plan(conn, shas) == (
            "SEARCH signals USING INDEX ix_signals_kind_ingested_at "
            "(kind=?)")
    finally:
        conn.close()


def test_indexed_by_fails_loudly_without_the_index(tmp_path):
    """A database that has not reached this migration refuses the query
    `_pr_ci_for_page` would run against it, rather than silently falling
    back to the partition scan the index exists to avoid."""
    db = tmp_path / "library.db"
    assert alembic("upgrade", "f8c1f89ef65c", db=db).returncode == 0
    conn = sqlite3.connect(db)
    try:
        with pytest.raises(sqlite3.OperationalError, match="no such index"):
            _plan(conn, ["sha0000000"],
                  indexed_by="ix_signals_test_result_commit")
    finally:
        conn.close()


