"""`enroll_machine.py`: the local admin command that writes a
`machine_certificates` row, and the lockout recovery path
platform/decisions/0066 specifies for it.
"""
from __future__ import annotations

import sqlite3

import pytest

import enroll_machine
import machine_registry
import store

MACHINE = "macbook"
FP = "a" * 64
NOT_AFTER = "2026-12-01T00:00:00"


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "control.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
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
            custody TEXT NOT NULL CHECK (custody IN ('hardware', 'file', 'root-file'))
        );
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
                conn.execute("SELECT * FROM machine_certificates").fetchall()]
    finally:
        conn.close()


def test_issue_writes_a_row(db, capsys):
    rc = enroll_machine.main(
        ["issue", MACHINE, FP, NOT_AFTER, "--custody", "hardware"])
    assert rc == 0
    rows = _rows(db)
    assert len(rows) == 1
    assert rows[0]["machine"] == MACHINE
    assert rows[0]["fingerprint_sha256"] == FP
    assert rows[0]["custody"] == "hardware"
    out = capsys.readouterr().out
    assert "issued" in out


def test_issue_records_an_optional_note(db):
    enroll_machine.main(["issue", MACHINE, FP, NOT_AFTER,
                         "--custody", "file", "--note", "renewal"])
    assert _rows(db)[0]["note"] == "renewal"


def test_custody_is_required(db):
    with pytest.raises(SystemExit):
        enroll_machine.main(["issue", MACHINE, FP, NOT_AFTER])


def test_custody_must_be_hardware_or_file(db):
    with pytest.raises(SystemExit):
        enroll_machine.main(
            ["issue", MACHINE, FP, NOT_AFTER, "--custody", "usb-stick"])


def test_an_invalid_fingerprint_is_refused_with_a_clean_error(db, capsys):
    rc = enroll_machine.main(
        ["issue", MACHINE, "not-hex", NOT_AFTER, "--custody", "hardware"])
    assert rc == 2
    assert _rows(db) == []
    assert "fingerprint_sha256" in capsys.readouterr().err


def test_an_unparseable_not_after_is_refused_by_argparse(db):
    with pytest.raises(SystemExit):
        enroll_machine.main(
            ["issue", MACHINE, FP, "not-a-date", "--custody", "hardware"])


def test_issue_refuses_while_writes_are_disabled(db, monkeypatch, capsys):
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    rc = enroll_machine.main(
        ["issue", MACHINE, FP, NOT_AFTER, "--custody", "hardware"])
    assert rc == 1
    assert _rows(db) == []


def test_re_issuing_revokes_the_previous_row(db):
    enroll_machine.main(
        ["issue", MACHINE, FP, NOT_AFTER, "--custody", "hardware"])
    enroll_machine.main(
        ["issue", MACHINE, "b" * 64, NOT_AFTER, "--custody", "hardware"])
    rows = {r["fingerprint_sha256"]: r for r in _rows(db)}
    assert rows[FP]["status"] == "revoked"
    assert rows["b" * 64]["status"] == "active"


def test_revoke_revokes_the_active_certificate(db, capsys):
    machine_registry.issue(MACHINE, FP, "2026-12-01T00:00:00", "hardware")
    rc = enroll_machine.main(["revoke", MACHINE, "--note", "lost the key"])
    assert rc == 0
    row = _rows(db)[0]
    assert row["status"] == "revoked"
    assert row["note"] == "lost the key"
    assert "revoked" in capsys.readouterr().out


def test_revoking_a_machine_with_nothing_active_is_a_clean_error(db, capsys):
    rc = enroll_machine.main(["revoke", MACHINE])
    assert rc == 2
    assert "no active certificate" in capsys.readouterr().err


def test_revoke_refuses_while_writes_are_disabled(db, monkeypatch):
    machine_registry.issue(MACHINE, FP, "2026-12-01T00:00:00", "hardware")
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    rc = enroll_machine.main(["revoke", MACHINE])
    assert rc == 1
    assert _rows(db)[0]["status"] == "active"


def test_a_command_is_required(db):
    with pytest.raises(SystemExit):
        enroll_machine.main([])


def test_issue_accepts_root_file_custody(db):
    """The Individuator's daemon key: software, readable only by root
    (platform/decisions/0092)."""
    assert enroll_machine.main(
        ["issue", MACHINE, FP, NOT_AFTER, "--custody", "root-file"]) == 0
    assert _rows(db)[0]["custody"] == "root-file"
