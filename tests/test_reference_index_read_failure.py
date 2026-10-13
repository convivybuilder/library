"""A knowledge index that opens but cannot be read answers 503, never 500.

issue_01M4RD3919HN309R1S2KHR0JDQ. The knowledge watcher rebuilds the index
in place, so `reference.connect()` can hand back a connection whose first
statement fails. Every route that reads the index does so through
`reference.reading()`, which raises that failure as
`ReferenceIndexUnavailable`, and the registered handler answers its
sanitized 503. A file that is not a database stands in for an index the
watcher replaced mid-request, as library#259's test for `GET /profiles` does.
"""
from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

import app as libapp
import identity
import protocol
import reference
import store

AGENT = "agent-token-wide"
DOC_ID = "platform/decisions/0022-library-repo"


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = tmp_path / "control.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE reference_checkouts (
            id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, holder TEXT NOT NULL,
            run_id TEXT, taken_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL,
            released_at TEXT, release_reason TEXT, released_by TEXT);
    """)
    conn.commit()
    conn.close()

    broken = tmp_path / "broken-index.db"
    broken.write_bytes(b"not a database, " * 256)

    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({AGENT: {"name": "wide-agent", "products": ["*"]}}))

    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(reference, "INDEX_DB", broken)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    c = TestClient(libapp.app)
    c.broken = broken
    return c


def _h():
    return {"Authorization": f"Bearer {AGENT}",
            protocol.HEADER: str(protocol.CURRENT)}


@pytest.mark.parametrize("method, path", [
    ("get", "/reference/search?q=library"),
    ("get", "/search?q=library&vault=reference"),
    ("get", "/reference/topics"),
    ("get", "/reference/documents"),
    ("get", f"/reference/documents/{DOC_ID}/checkout"),
    ("get", f"/reference/documents/{DOC_ID}"),
    ("post", f"/reference/documents/{DOC_ID}/checkout"),
])
def test_a_read_on_a_broken_index_is_a_sanitized_503(client, capsys, method, path):
    kwargs = {"json": {}} if method == "post" else {}
    r = getattr(client, method)(path, headers=_h(), **kwargs)
    assert r.status_code == 503, r.text
    assert r.json()["error"] == "the knowledge index is unavailable"
    assert str(client.broken) not in r.text
    assert "reference index unavailable" in capsys.readouterr().err


def test_reading_raises_a_failed_read_as_the_index_unavailable(tmp_path, monkeypatch):
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"not a database, " * 256)
    monkeypatch.setattr(reference, "INDEX_DB", broken)
    with pytest.raises(reference.ReferenceIndexUnavailable, match="could not be read"):
        with reference.reading() as conn:
            conn.execute("SELECT 1 FROM docs").fetchone()


def test_reading_leaves_a_busy_index_to_the_busy_handler(tmp_path, monkeypatch):
    """`database_busy` answers `SQLITE_BUSY` with `Retry-After`; relabelling
    it as an unavailable index would drop that hint."""
    index = tmp_path / "index.db"
    sqlite3.connect(index).close()
    monkeypatch.setattr(reference, "INDEX_DB", index)
    busy = sqlite3.OperationalError("database is locked")
    busy.sqlite_errorcode = sqlite3.SQLITE_BUSY
    with pytest.raises(sqlite3.OperationalError) as raised:
        with reference.reading():
            raise busy
    assert raised.value is busy


def test_reading_closes_the_connection(tmp_path, monkeypatch):
    index = tmp_path / "index.db"
    sqlite3.connect(index).close()
    monkeypatch.setattr(reference, "INDEX_DB", index)
    with reference.reading() as conn:
        pass
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")
