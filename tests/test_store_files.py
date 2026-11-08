"""Every path that creates a store file makes it 0600 under the common
umask of 022, so `serve.sh`'s mode check (library#317) passes on a fresh
install. Shipwright's Debian 12 rehearsal of the 0.7.0 install found
`library.db`, `cold.db` and `vectors.db` at 0644, and the Library refused to
start on them.
"""
from __future__ import annotations

import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import init_cold
import init_vectors
import migrate
import split_vectors
import store
import store_files

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def umask_022():
    old = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(old)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _migrate(env: dict) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(REPO), capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(REPO), **env})
    assert result.returncode == 0, result.stderr


def test_create_private_makes_a_missing_file_0600(tmp_path, umask_022):
    path = tmp_path / "library.db"
    assert store_files.create_private(path) is True
    assert _mode(path) == 0o600
    assert path.read_bytes() == b""


def test_create_private_leaves_an_existing_file_alone(tmp_path, umask_022):
    path = tmp_path / "library.db"
    path.write_bytes(b"live")
    os.chmod(path, 0o644)
    assert store_files.create_private(path) is False
    assert _mode(path) == 0o644
    assert path.read_bytes() == b"live"


def test_a_fresh_migration_makes_library_and_cold_0600(tmp_path, umask_022):
    db_path = tmp_path / "library.db"
    cold_path = tmp_path / "cold.db"
    _migrate({"LIBRARY_DB": str(db_path), "LIBRARY_COLD_DB": str(cold_path)})
    assert _mode(db_path) == 0o600
    assert _mode(cold_path) == 0o600


def test_init_cold_makes_a_fresh_cold_file_0600(
        tmp_path, monkeypatch, umask_022):
    db_path = tmp_path / "library.db"
    cold_path = tmp_path / "cold.db"
    _migrate({"LIBRARY_DB": str(db_path)})
    monkeypatch.setattr(store, "DB_PATH", db_path)
    assert init_cold.init(cold_path) is True
    assert _mode(cold_path) == 0o600


def test_init_vectors_makes_a_fresh_vectors_file_0600(tmp_path, umask_022):
    vectors_path = tmp_path / "vectors.db"
    init_vectors.init(vectors_path)
    assert _mode(vectors_path) == 0o600


@pytest.mark.parametrize("module", [migrate, split_vectors])
def test_a_cutover_tools_writable_connect_makes_its_output_0600(
        tmp_path, umask_022, module):
    """`migrate.split` and `split_vectors.export_vectors` write the files
    that become the live `LIBRARY_DB` and `LIBRARY_VECTORS_DB`."""
    dest = tmp_path / "out.db"
    module.connect(dest, readonly=False).close()
    assert _mode(dest) == 0o600


def test_sqlites_wal_and_shm_files_take_the_database_files_mode(
        tmp_path, umask_022):
    """The module docstring's claim that SQLite's side files follow the
    database file's mode, which is why only the database file is created
    here."""
    path = tmp_path / "library.db"
    store_files.create_private(path)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE t (x)")
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
        assert _mode(Path(f"{path}-wal")) == 0o600
        assert _mode(Path(f"{path}-shm")) == 0o600
    finally:
        conn.close()
