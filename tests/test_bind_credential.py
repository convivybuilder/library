"""`bind_credential.py`: the local admin command that writes a
`credential_bindings` row, and the lockout recovery path
platform/decisions/0066 specifies for it.
"""
from __future__ import annotations

import io
import sqlite3

import pytest

import bind_credential
import credential_binding
import store

MACHINE = "macbook"
TOKEN = "s3cr3t-bearer-token-do-not-log-me"


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "control.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE credential_bindings (
            id INTEGER PRIMARY KEY,
            credential_sha256 TEXT NOT NULL UNIQUE,
            machine TEXT NOT NULL,
            note TEXT,
            created_at TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()
    monkeypatch.setattr(store, "DB_PATH", path)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    return path


def test_binds_the_token_read_from_stdin(db, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(TOKEN + "\n"))
    rc = bind_credential.main([MACHINE])
    assert rc == 0
    assert (credential_binding._bound_machine(
        credential_binding.sha256_token(TOKEN)) == MACHINE)
    out = capsys.readouterr().out
    assert TOKEN not in out


def test_refuses_an_empty_token(db, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("   \n"))
    rc = bind_credential.main([MACHINE])
    assert rc == 2
    conn = sqlite3.connect(db)
    try:
        n = conn.execute("SELECT COUNT(*) FROM credential_bindings").fetchone()[0]
    finally:
        conn.close()
    assert n == 0


def test_records_an_optional_note(db, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(TOKEN))
    rc = bind_credential.main([MACHINE, "--note", "recovering a lockout"])
    assert rc == 0
    conn = sqlite3.connect(db)
    try:
        note = conn.execute(
            "SELECT note FROM credential_bindings").fetchone()[0]
    finally:
        conn.close()
    assert note == "recovering a lockout"


def test_refuses_while_writes_are_disabled(db, monkeypatch, capsys):
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    monkeypatch.setattr("sys.stdin", io.StringIO(TOKEN))
    rc = bind_credential.main([MACHINE])
    assert rc == 1


def test_the_raw_token_never_reaches_the_database(db, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(TOKEN))
    bind_credential.main([MACHINE])
    assert TOKEN.encode() not in db.read_bytes()
