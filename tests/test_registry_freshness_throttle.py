"""CQ·243 follow-up: `projects.py`'s table-mode freshness check
(`_throttled_table_signature`) bounds the vault-connection cost that moved
the project registry into the Chart House table introduced.

`store._pr_repo_pattern` calls `projects.projects()` and
`projects.repo_owner()` for every row `store.item_dict` serves — before this
throttle, a 200-row page opened about 400 vault connections for the
registry alone, where file mode's cost was a cached `stat()`. These tests
pin the throttle itself (`projects.py`, unit-level) and the route that made
it necessary (`GET /items`, integration-level).

The `GET /items` fixture is built on a database from the real migration,
matching `test_item_pr_fields.py`'s reason: the scratch db needs the real
`project_registry` table CQ·241 added, not a hand-built stand-in.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as libapp
import identity
import project_registry
import projects
import store

REPO = Path(__file__).resolve().parent.parent


# --- the throttle itself, with no HTTP route involved ------------------------


def _make_table_db(tmp_path) -> Path:
    db = tmp_path / "scratch.db"
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE project_registry (
            id INTEGER PRIMARY KEY,
            slug TEXT NOT NULL UNIQUE,
            entry TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            updated_by TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()
    return db


@pytest.fixture
def table_db(tmp_path, monkeypatch):
    db = _make_table_db(tmp_path)
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    monkeypatch.setattr(projects, "REGISTRY_PATH", None)
    projects.reload()
    yield db
    projects.reload()


@pytest.fixture
def fake_clock(monkeypatch):
    """A controllable `time.monotonic()` for the throttle, so a test can
    step past `FRESHNESS_SECONDS` without a real sleep."""
    state = {"now": 0.0}
    monkeypatch.setattr(projects.time, "monotonic", lambda: state["now"])
    return state


def test_many_reads_within_the_interval_share_one_freshness_query(
        table_db, fake_clock, monkeypatch):
    project_registry.replace({"alpha": {"aliases": ["a"]}}, actor="test")

    calls = []
    real_table_signature = projects._table_signature

    def counting():
        calls.append(1)
        return real_table_signature()

    monkeypatch.setattr(projects, "_table_signature", counting)

    for _ in range(5):
        assert projects.resolve("alpha") == "alpha"
    assert len(calls) == 1, (
        "every read inside the throttle window must share one freshness "
        f"query, not {len(calls)}")

    fake_clock["now"] += projects.FRESHNESS_SECONDS
    assert projects.resolve("alpha") == "alpha"
    assert len(calls) == 2, "a read past the throttle window issues one more"


def test_a_direct_table_write_is_not_seen_before_the_interval_and_is_after(
        table_db, fake_clock):
    """Standing in for `load_project_registry.py`, which runs as its own
    process and writes through its own connection, never through this
    module's lazy `project_registry` import: a plain `sqlite3.connect`
    insert, rather than `project_registry.replace`."""
    project_registry.replace({"alpha": {"aliases": ["a"]}}, actor="test",
                             now="2026-10-27 00:00:00")
    assert projects.resolve("alpha") == "alpha"

    conn = sqlite3.connect(table_db)
    try:
        conn.execute(
            "INSERT INTO project_registry (slug, entry, updated_at, "
            "updated_by) VALUES (?, ?, ?, ?)",
            ("beta", json.dumps({"aliases": ["b"]}),
             "2026-10-27 00:00:01", "loader"))
        conn.commit()
    finally:
        conn.close()

    assert projects.resolve("beta") is None, (
        "inside the throttle window, a write already in the table is not "
        "yet seen")

    fake_clock["now"] += projects.FRESHNESS_SECONDS
    assert projects.resolve("beta") == "beta"


# --- the route the throttle exists for ---------------------------------------

TOKEN = "t-freshness"


def _ts(**delta):
    return (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
            - dt.timedelta(**delta)).isoformat(" ")


def _insert_item(db_path, item_id, **fields):
    defaults = {
        "id": item_id, "product": "keel", "title": item_id,
        "body": "b", "refs": json.dumps({"url": LINKED_URL}),
        "severity": "medium", "state": "open", "created_by": "seed",
        "created_at": _ts(days=1), "needs": "agent",
        "created_by_kind": "agent",
    }
    defaults.update(fields)
    conn = sqlite3.connect(db_path)
    try:
        cols = ", ".join(defaults)
        marks = ", ".join("?" * len(defaults))
        conn.execute(f"INSERT INTO issue_items ({cols}) VALUES ({marks})",
                    list(defaults.values()))
        conn.commit()
    finally:
        conn.close()


# A `refs.url` naming the `library` project's own repo, so every row's
# `pr_repo` resolution reaches `_pr_repo_pattern()` — `_pr_repo` returns
# early for a row with no `url` at all, which would make this file's own
# count prove nothing about the throttle.
LINKED_URL = "https://github.com/convivy/library/pull/1"


@pytest.fixture
def items_db_path(tmp_path):
    db = tmp_path / "control.db"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO, capture_output=True, text=True, check=False,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LIBRARY_DB": str(db)})
    assert result.returncode == 0, result.stderr
    return db


@pytest.fixture
def items_client(items_db_path, tmp_path, monkeypatch):
    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({TOKEN: {"name": "drain", "products": ["*"]}}))
    monkeypatch.setattr(store, "DB_PATH", items_db_path)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(identity, "CLIENT_TOKENS", tmp_path / "client-tokens.json")
    monkeypatch.setattr(projects, "REGISTRY_PATH", None)
    projects.reload()
    yield TestClient(libapp.app)
    projects.reload()


def _h():
    return {"Authorization": f"Bearer {TOKEN}", "Library-Protocol": "1"}


def test_get_items_for_fifty_items_issues_at_most_one_freshness_query(
        items_client, items_db_path, monkeypatch):
    project_registry.replace(
        {"library": {"owner": "convivy", "repos": ["library"]}}, actor="test")
    for i in range(50):
        _insert_item(items_db_path, f"item-{i}")

    calls = []
    real_table_signature = projects._table_signature

    def counting():
        calls.append(1)
        return real_table_signature()

    monkeypatch.setattr(projects, "_table_signature", counting)

    r = items_client.get("/items?product=keel", headers=_h())
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 50
    assert all(item["pr_repo"] == "convivy/library" for item in r.json()["items"])
    assert len(calls) <= 1, (
        f"serving 50 items issued {len(calls)} registry freshness queries")
