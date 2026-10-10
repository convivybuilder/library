"""`enroll_signing_cert.py`: the local admin command that writes a
`signing_certificates` row, the same shape `test_enroll_machine.py` uses
for the sibling registry.
"""
from __future__ import annotations

import sqlite3

import pytest

import enroll_signing_cert
import signing_registry
import store

PERSON = "operator@example.com"
MACHINE = "macbook"
FP = "a" * 64
NOT_AFTER = "2026-12-01T00:00:00"


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "control.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE signing_certificates (
            id INTEGER PRIMARY KEY,
            fingerprint_sha256 TEXT NOT NULL UNIQUE,
            person TEXT NOT NULL,
            machine TEXT NOT NULL,
            not_after TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'revoked')),
            issued_at TEXT NOT NULL,
            revoked_at TEXT,
            note TEXT
        );
        CREATE TABLE machine_certificates (
            id INTEGER PRIMARY KEY,
            machine TEXT NOT NULL,
            fingerprint_sha256 TEXT NOT NULL UNIQUE,
            not_after TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'revoked')),
            revoked_at TEXT,
            note TEXT,
            created_at TEXT NOT NULL,
            custody TEXT NOT NULL CHECK (custody IN ('hardware', 'file'))
        );
    """)
    conn.commit()
    conn.close()
    monkeypatch.setattr(store, "DB_PATH", path)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    return path


def _rows(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in
                conn.execute("SELECT * FROM signing_certificates").fetchall()]
    finally:
        conn.close()


def test_issue_writes_a_row(db, capsys):
    rc = enroll_signing_cert.main(["issue", PERSON, MACHINE, FP, NOT_AFTER])
    assert rc == 0
    rows = _rows(db)
    assert len(rows) == 1
    assert rows[0]["person"] == PERSON
    assert rows[0]["machine"] == MACHINE
    assert rows[0]["fingerprint_sha256"] == FP
    out = capsys.readouterr().out
    assert "issued" in out


def test_issue_records_an_optional_note(db):
    enroll_signing_cert.main(["issue", PERSON, MACHINE, FP, NOT_AFTER,
                              "--note", "root ceremony"])
    assert _rows(db)[0]["note"] == "root ceremony"


def test_an_invalid_fingerprint_is_refused_with_a_clean_error(db, capsys):
    rc = enroll_signing_cert.main(
        ["issue", PERSON, MACHINE, "not-hex", NOT_AFTER])
    assert rc == 2
    assert _rows(db) == []
    assert "fingerprint_sha256" in capsys.readouterr().err


def test_an_unparseable_not_after_is_refused_by_argparse(db):
    with pytest.raises(SystemExit):
        enroll_signing_cert.main(
            ["issue", PERSON, MACHINE, FP, "not-a-date"])


def test_issue_refuses_while_writes_are_disabled(db, monkeypatch):
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    rc = enroll_signing_cert.main(["issue", PERSON, MACHINE, FP, NOT_AFTER])
    assert rc == 1
    assert _rows(db) == []


def test_a_person_can_be_issued_two_certificates(db):
    enroll_signing_cert.main(["issue", PERSON, "macbook", FP, NOT_AFTER])
    enroll_signing_cert.main(
        ["issue", PERSON, "deck", "b" * 64, NOT_AFTER])
    rows = _rows(db)
    assert len(rows) == 2
    assert all(r["status"] == "active" for r in rows)


def test_revoke_revokes_the_named_certificate(db, capsys):
    signing_registry.issue(FP, PERSON, MACHINE, "2026-12-01T00:00:00")
    rc = enroll_signing_cert.main(["revoke", FP, "--note", "lost the key"])
    assert rc == 0
    row = _rows(db)[0]
    assert row["status"] == "revoked"
    assert row["note"] == "lost the key"
    assert "revoked" in capsys.readouterr().out


def test_revoking_an_unknown_fingerprint_is_a_clean_error(db, capsys):
    rc = enroll_signing_cert.main(["revoke", FP])
    assert rc == 2
    assert "no active certificate" in capsys.readouterr().err


def test_revoke_refuses_while_writes_are_disabled(db, monkeypatch):
    signing_registry.issue(FP, PERSON, MACHINE, "2026-12-01T00:00:00")
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    rc = enroll_signing_cert.main(["revoke", FP])
    assert rc == 1
    assert _rows(db)[0]["status"] == "active"


def test_a_command_is_required(db):
    with pytest.raises(SystemExit):
        enroll_signing_cert.main([])
