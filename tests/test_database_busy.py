"""A request that waits out SQLite's busy timeout is a 503 with `Retry-After`.

**The lock is held for real, from a second connection, rather than faked with a
raised exception.** A test that raised `OperationalError("database is locked")`
by hand would pass against a handler matching the message text and prove
nothing about what SQLite produces. Holding `BEGIN IMMEDIATE` makes the route's
own INSERT wait out the busy timeout and fail the way production does. The
timeout is patched down to keep that wait short.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

import app as libapp
import identity
import projects
import protocol
import store

AGENT = "agent-token-vivy"


@pytest.fixture
def db_path(tmp_path, monkeypatch, vector_index):
    db = tmp_path / "control.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE signals (
            id TEXT PRIMARY KEY, source TEXT, stream TEXT, kind TEXT, actor TEXT,
            ingested_by TEXT, product TEXT, refs TEXT, body TEXT, meta TEXT,
            privacy_class TEXT, produced_at TEXT, ingested_at TEXT,
            embedded_at TEXT, dedup_key TEXT UNIQUE, acting_human TEXT);
        CREATE TABLE black_box_registrations (
            id INTEGER PRIMARY KEY, crew TEXT NOT NULL UNIQUE,
            model TEXT NOT NULL, gateway TEXT NOT NULL,
            created_by TEXT NOT NULL, created_at TEXT NOT NULL,
            updated_by TEXT NOT NULL, updated_at TEXT NOT NULL);
    """)
    conn.commit()
    conn.close()
    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps(
        {AGENT: {"name": "vivy-agent", "products": ["vivy"]}}))
    vector_index(db)
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    monkeypatch.setattr(store, "BUSY_TIMEOUT_MS", 50)
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({"_meta": {}}))
    monkeypatch.setattr(projects, "REGISTRY_PATH", registry)
    projects.reload()
    yield db
    projects.reload()


@pytest.fixture
def client(db_path):
    return TestClient(libapp.app)


def _h():
    return {"Authorization": f"Bearer {AGENT}",
            protocol.HEADER: str(protocol.CURRENT)}


def _body():
    return {"source": "github", "kind": "note", "actor": "someone",
            "product": "vivy", "body": "a thing happened"}


def _busy_error(tmp_path) -> sqlite3.OperationalError:
    """A genuine SQLITE_BUSY, raised by SQLite rather than constructed."""
    path = tmp_path / "busy.db"
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("CREATE TABLE t (x)")
    holder.execute("BEGIN IMMEDIATE")
    waiter = sqlite3.connect(path, timeout=0.01)
    try:
        waiter.execute("INSERT INTO t VALUES (1)")
    except sqlite3.OperationalError as exc:
        return exc
    finally:
        waiter.close()
        holder.execute("ROLLBACK")
        holder.close()
    raise AssertionError("the second writer was not refused")


def test_a_write_past_the_busy_timeout_is_503_with_retry_after(
        client, db_path, capsys):
    holder = sqlite3.connect(db_path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        r = client.post("/signals", json=_body(), headers=_h())
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    assert r.status_code == 503, r.text
    assert r.json()["error"] == "the Library's database is busy"
    assert r.headers["Retry-After"] == "1"
    assert "library: database busy — POST /signals\n" in capsys.readouterr().err

    # The same request succeeds once the lock is gone, so the 503 was the lock.
    assert client.post("/signals", json=_body(), headers=_h()).status_code == 201


def test_a_lock_during_the_identity_check_is_busy_not_misconfigured(
        client, db_path):
    """`connect_rw` reads `sqlite_master` to confirm which file it opened before
    it hands the connection back. An EXCLUSIVE lock blocks that read too, and
    the answer is contention with `Retry-After`, never the refusal for a
    database the Library cannot identify (reviewer, PR #112)."""
    holder = sqlite3.connect(db_path, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        r = client.post("/signals", json=_body(), headers=_h())
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    assert r.status_code == 503, r.text
    assert r.json()["error"] == "the Library's database is busy"
    assert r.headers["Retry-After"] == "1"


def test_the_log_line_names_the_route_template_not_the_callers_path(
        tmp_path, capsys):
    """A percent-decoded `\\r\\n` in a path parameter must not reach the log,
    where it would forge a line (security-reviewer, PR #112)."""
    route = next(r for r in libapp.app.routes
                 if getattr(r, "path", None) == "/items/{item_id}")
    request = Request({
        "type": "http", "method": "GET", "headers": [], "query_string": b"",
        "path": "/items/x\r\nlibrary: FORGED LINE", "route": route})

    response = asyncio.run(libapp.database_busy(request, _busy_error(tmp_path)))

    assert response.status_code == 503
    err = capsys.readouterr().err
    assert err == "library: database busy — GET /items/{item_id}\n"
    assert "FORGED" not in err


def test_any_other_operational_error_still_raises(client, monkeypatch):
    """`no such table` is a fault, and must stay the 500 it was."""
    monkeypatch.setattr(store, "connect_rw", lambda: sqlite3.connect(":memory:"))
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        client.post("/signals", json=_body(), headers=_h())
