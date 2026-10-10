"""The `bundle_tokens` migration: what it builds, run for real
(issue_01M2VCWQXTF5EGHD1X1J3P4RTJ).

**Driven through the real `alembic` CLI**, for `test_credential_bindings_
migration.py`'s reason: the parts most likely to be wrong are that the revision
chains onto the current head and that its DDL, CHECK included, runs against
SQLite. An in-process `alembic.command` call would skip both.
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

INSERT = ("INSERT INTO bundle_tokens (id, agent, project, variant, scope_kind, "
          "item_id, repo, documents, issued_by, issued_at, expires_at) "
          "VALUES (?,?,?,?,?,?,?,'[]','x','2026-01-01','2026-01-01')")


def alembic(*args, db):
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(REPO), capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(REPO),
             "LIBRARY_DB": str(db)})


@pytest.fixture(scope="module")
def migrated(tmp_path_factory):
    db = tmp_path_factory.mktemp("bundle-migration") / "library.db"
    result = alembic("upgrade", "head", db=db)
    assert result.returncode == 0, result.stderr
    return db


def test_the_migration_creates_the_table_at_head(migrated):
    conn = sqlite3.connect(f"file:{migrated}?mode=ro", uri=True)
    try:
        columns = {row[1]: row for row in conn.execute(
            "PRAGMA table_info(bundle_tokens)")}
    finally:
        conn.close()
    assert set(columns) == {
        "id", "agent", "project", "variant", "scope_kind", "item_id", "repo",
        "product", "documents", "issued_by", "issued_at", "expires_at",
        "redeemed_at", "redeemed_by"}
    # NULL is a real state for these, and only these: a repository-scoped
    # token has no item and no product, and an unspent one has no redemption.
    nullable = {name for name, row in columns.items() if not row[3]}
    assert nullable >= {"item_id", "repo", "product", "redeemed_at",
                        "redeemed_by"}
    assert not nullable & {"id", "agent", "project", "variant", "scope_kind",
                           "documents", "issued_by", "issued_at",
                           "expires_at"}


def test_item_id_declares_a_foreign_key_to_issue_items_as_call_slips_does(
        migrated):
    """Declared and not enforced (`PRAGMA foreign_keys` is off for every
    connection the service opens), which is `call_slips`' convention."""
    conn = sqlite3.connect(f"file:{migrated}?mode=ro", uri=True)
    try:
        fks = {row[3]: (row[2], row[4]) for row in conn.execute(
            "PRAGMA foreign_key_list(bundle_tokens)")}
    finally:
        conn.close()
    assert fks == {"item_id": ("issue_items", "id")}


def test_the_revision_chains_onto_the_previous_head_and_the_tree_does_not_branch(
        tmp_path):
    """**ONE head, never THIS head.** `alembic heads` lists only revisions
    nothing chains onto, so a test naming its own revision there passes until
    the next migration lands and then fails for a reason unrelated to what it
    tests.

    What is worth pinning is that the tree has a single head — a second one
    means two migrations chained onto the same parent and `upgrade head` became
    ambiguous — and that THIS revision's own link is the one intended, which is
    the `history` assertion below and does not go stale.
    """
    db = tmp_path / "library.db"
    heads = alembic("heads", db=db)
    assert heads.returncode == 0, heads.stderr
    assert heads.stdout.count("(head)") == 1, heads.stdout
    history = alembic("history", db=db)
    assert "0652cc6b2cee -> 3e8a5c1d7b40" in history.stdout, history.stdout


@pytest.mark.parametrize("item_id,repo", [
    ("issue_1", None),
    (None, "some-repo"),
])
def test_an_item_or_a_repository_scope_is_accepted(migrated, tmp_path,
                                                   item_id, repo):
    conn = sqlite3.connect(migrated)
    try:
        conn.execute(INSERT, ("bundle_ok", "coder", "p", "daytime",
                              "item" if item_id else "repo", item_id, repo))
        conn.rollback()
    finally:
        conn.close()


@pytest.mark.parametrize("scope_kind,item_id,repo", [
    ("item", "issue_1", "some-repo"),
    ("repo", "issue_1", "some-repo"),
    ("item", None, None),
    ("repo", None, None),
    ("item", None, "some-repo"),
    ("repo", "issue_1", None),
    ("neither", "issue_1", None),
])
def test_a_scope_that_names_two_things_or_none_is_refused_by_the_database(
        migrated, scope_kind, item_id, repo):
    """A bundle scoped to nothing in particular is not a state the table can
    hold, whatever code writes to it."""
    conn = sqlite3.connect(migrated)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(INSERT, ("bundle_bad", "coder", "p", "daytime",
                                  scope_kind, item_id, repo))
    finally:
        conn.rollback()
        conn.close()


def test_the_downgrade_removes_the_table(tmp_path):
    db = tmp_path / "library.db"
    assert alembic("upgrade", "head", db=db).returncode == 0
    assert alembic("downgrade", "0652cc6b2cee", db=db).returncode == 0
    conn = sqlite3.connect(db)
    try:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'bundle_tokens'"
        ).fetchone() is None
    finally:
        conn.close()
