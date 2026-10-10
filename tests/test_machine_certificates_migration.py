"""The `machine_certificates` migration: what it builds, run for real.

**Driven through the real `alembic` CLI, the same way `test_alembic_baseline.
py` drives every other revision** — the parts most likely to be wrong are
that the revision chains onto head and that its DDL actually runs against
SQLite, and an in-process `alembic.command` call would skip both.
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
            "PRAGMA table_info(machine_certificates)")}
    finally:
        conn.close()
    assert columns == {"id", "machine", "fingerprint_sha256", "not_after",
                       "status", "revoked_at", "note", "created_at",
                       "custody"}


def test_the_fingerprint_column_is_unique(tmp_path):
    db = tmp_path / "library.db"
    assert alembic("upgrade", "head",
                   env_extra={"LIBRARY_DB": str(db)}).returncode == 0
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO machine_certificates "
        "(machine, fingerprint_sha256, not_after, status, created_at, custody) "
        "VALUES ('a', 'fp', '2099-01-01', 'active', '2026-01-01', 'hardware')")
    conn.commit()
    try:
        conn.execute(
            "INSERT INTO machine_certificates "
            "(machine, fingerprint_sha256, not_after, status, created_at, custody) "
            "VALUES ('b', 'fp', '2099-01-01', 'active', '2026-01-01', 'hardware')")
        conn.commit()
        raised = False
    except sqlite3.IntegrityError:
        raised = True
    finally:
        conn.close()
    assert raised, "two rows shared a fingerprint — the unique constraint is gone"


def test_custody_rejects_a_value_outside_hardware_or_file(tmp_path):
    db = tmp_path / "library.db"
    assert alembic("upgrade", "head",
                   env_extra={"LIBRARY_DB": str(db)}).returncode == 0
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO machine_certificates "
            "(machine, fingerprint_sha256, not_after, status, created_at, custody) "
            "VALUES ('a', 'fp', '2099-01-01', 'active', '2026-01-01', 'usb-stick')")
        conn.commit()
        raised = False
    except sqlite3.IntegrityError:
        raised = True
    finally:
        conn.close()
    assert raised, "a custody value outside hardware/file was accepted"


def test_status_rejects_a_value_outside_active_or_revoked(tmp_path):
    db = tmp_path / "library.db"
    assert alembic("upgrade", "head",
                   env_extra={"LIBRARY_DB": str(db)}).returncode == 0
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO machine_certificates "
            "(machine, fingerprint_sha256, not_after, status, created_at, custody) "
            "VALUES ('a', 'fp', '2099-01-01', 'decommissioned', '2026-01-01', 'hardware')")
        conn.commit()
        raised = False
    except sqlite3.IntegrityError:
        raised = True
    finally:
        conn.close()
    assert raised, "a status value outside active/revoked was accepted"


def test_custody_is_required(tmp_path):
    db = tmp_path / "library.db"
    assert alembic("upgrade", "head",
                   env_extra={"LIBRARY_DB": str(db)}).returncode == 0
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO machine_certificates "
            "(machine, fingerprint_sha256, not_after, status, created_at) "
            "VALUES ('a', 'fp', '2099-01-01', 'active', '2026-01-01')")
        conn.commit()
        raised = False
    except sqlite3.IntegrityError:
        raised = True
    finally:
        conn.close()
    assert raised, "a row with no custody value was accepted"


def test_the_lookup_index_is_there(tmp_path):
    db = tmp_path / "library.db"
    assert alembic("upgrade", "head",
                   env_extra={"LIBRARY_DB": str(db)}).returncode == 0
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        indexes = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='machine_certificates'")}
    finally:
        conn.close()
    assert "ix_machine_certificates_lookup" in indexes


def test_the_downgrade_drops_the_table(tmp_path):
    db = tmp_path / "library.db"
    assert alembic("upgrade", "head",
                   env_extra={"LIBRARY_DB": str(db)}).returncode == 0
    result = alembic("downgrade", "d3f81a6c07be",
                     env_extra={"LIBRARY_DB": str(db)})
    assert result.returncode == 0, result.stderr
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        present = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='machine_certificates'").fetchone()
    finally:
        conn.close()
    assert present is None
