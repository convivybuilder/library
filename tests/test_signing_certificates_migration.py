"""The `signing_certificates` migration: what it builds, run for real.

**Driven through the real `alembic` CLI**, the same shape
`test_machine_certificates_migration.py` uses for the sibling registry: the
parts most likely to be wrong are that the revision chains onto head and
that its DDL actually runs against SQLite, and an in-process
`alembic.command` call would skip both.
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def alembic(*args, env_extra=None):
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(REPO), capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(REPO), **(env_extra or {})})


def test_the_migration_creates_the_table_at_head(tmp_path):
    db = tmp_path / "library.db"
    result = alembic("upgrade", "head", env_extra={"LIBRARY_DB": str(db)})
    assert result.returncode == 0, result.stderr

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        columns = {row[1] for row in conn.execute(
            "PRAGMA table_info(signing_certificates)")}
    finally:
        conn.close()
    assert columns == {"id", "fingerprint_sha256", "person", "machine",
                       "not_after", "status", "issued_at", "revoked_at",
                       "note"}


def test_the_fingerprint_column_is_unique(tmp_path):
    db = tmp_path / "library.db"
    assert alembic("upgrade", "head",
                   env_extra={"LIBRARY_DB": str(db)}).returncode == 0
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO signing_certificates "
        "(fingerprint_sha256, person, machine, not_after, status, "
        " issued_at) VALUES ('fp', 'operator@example.com', 'a', "
        "'2099-01-01', 'active', '2026-01-01')")
    conn.commit()
    try:
        conn.execute(
            "INSERT INTO signing_certificates "
            "(fingerprint_sha256, person, machine, not_after, status, "
            " issued_at) VALUES ('fp', 'operator@example.com', 'b', "
            "'2099-01-01', 'active', '2026-01-01')")
        conn.commit()
        raised = False
    except sqlite3.IntegrityError:
        raised = True
    finally:
        conn.close()
    assert raised, "two rows shared a fingerprint — the unique constraint is gone"


def test_status_rejects_a_value_outside_active_or_revoked(tmp_path):
    db = tmp_path / "library.db"
    assert alembic("upgrade", "head",
                   env_extra={"LIBRARY_DB": str(db)}).returncode == 0
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO signing_certificates "
            "(fingerprint_sha256, person, machine, not_after, status, "
            " issued_at) VALUES ('fp', 'operator@example.com', 'a', "
            "'2099-01-01', 'decommissioned', '2026-01-01')")
        conn.commit()
        raised = False
    except sqlite3.IntegrityError:
        raised = True
    finally:
        conn.close()
    assert raised, "a status value outside active/revoked was accepted"


def test_a_person_can_hold_two_active_rows(tmp_path):
    """The invariant this schema deliberately does NOT enforce: unlike
    `machine_certificates`, there is no uniqueness on `person` — a person
    may hold several active signing certificates, one per Mac."""
    db = tmp_path / "library.db"
    assert alembic("upgrade", "head",
                   env_extra={"LIBRARY_DB": str(db)}).returncode == 0
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO signing_certificates "
            "(fingerprint_sha256, person, machine, not_after, status, "
            " issued_at) VALUES (?, 'operator@example.com', 'macbook', "
            "'2099-01-01', 'active', '2026-01-01')", ("a" * 64,))
        conn.execute(
            "INSERT INTO signing_certificates "
            "(fingerprint_sha256, person, machine, not_after, status, "
            " issued_at) VALUES (?, 'operator@example.com', 'deck', "
            "'2099-01-01', 'active', '2026-01-01')", ("b" * 64,))
        conn.commit()
        rows = conn.execute(
            "SELECT COUNT(*) FROM signing_certificates "
            "WHERE person = 'operator@example.com' AND status = 'active'"
        ).fetchone()
    finally:
        conn.close()
    assert rows[0] == 2


def test_the_downgrade_drops_the_table(tmp_path):
    db = tmp_path / "library.db"
    assert alembic("upgrade", "head",
                   env_extra={"LIBRARY_DB": str(db)}).returncode == 0
    result = alembic("downgrade", "a8d2c5e91f37",
                     env_extra={"LIBRARY_DB": str(db)})
    assert result.returncode == 0, result.stderr
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        present = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='signing_certificates'").fetchone()
    finally:
        conn.close()
    assert present is None
