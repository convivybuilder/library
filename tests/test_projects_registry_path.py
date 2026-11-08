"""projects.py's module-level `REGISTRY_PATH`: `PROJECTS_JSON` when set and
non-empty, else `None` — CQ·243, platform/decisions/0098's 2026-10-25
amendment that makes the Chart House's `project_registry` table the registry
and drops every file fallback. `REGISTRY_PATH` is computed once, at import,
so each test here loads its own fresh copy of the module rather than
monkeypatching the shared one — the shared `projects` module other test
files import is never touched.
"""
from __future__ import annotations

import importlib.util
import itertools
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_counter = itertools.count()


def _load_projects(monkeypatch):
    """A fresh `projects` module instance, re-executed from source so its
    module-level `REGISTRY_PATH` is recomputed against whatever env the
    caller has already patched."""
    name = f"projects_under_test_{next(_counter)}"
    spec = importlib.util.spec_from_file_location(name, ROOT / "projects.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_registry(path, product):
    path.write_text(json.dumps({product: {"aliases": [], "repos": []}}))


def _make_table_db(tmp_path, document=None):
    """A scratch database holding only `project_registry`, seeded with
    `document` (default: empty, so the table reads as unreadable). Never
    touches the real vault — this is its own file under `tmp_path`."""
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
    if document:
        conn.executemany(
            "INSERT INTO project_registry (slug, entry, updated_at, "
            "updated_by) VALUES (?, ?, '2026-10-27 00:00:00', 'test')",
            [(slug, json.dumps(entry)) for slug, entry in document.items()])
    conn.commit()
    conn.close()
    return db


def test_projects_json_wins_when_set(tmp_path, monkeypatch):
    """Set alongside a populated Chart House table, `PROJECTS_JSON` is what
    `resolve()` reads — the table is never even opened."""
    registry = tmp_path / "registry.json"
    _write_registry(registry, "file-product")
    db = _make_table_db(tmp_path, {"table-product": {"aliases": [], "repos": []}})
    monkeypatch.setenv("PROJECTS_JSON", str(registry))
    mod = _load_projects(monkeypatch)
    import store
    monkeypatch.setattr(store, "DB_PATH", db)
    assert mod.REGISTRY_PATH == registry
    assert mod.resolve("file-product") == "file-product"
    assert mod.resolve("table-product") is None


def test_the_table_is_read_when_projects_json_is_unset(tmp_path, monkeypatch):
    db = _make_table_db(tmp_path, {"table-product": {"aliases": [], "repos": []}})
    monkeypatch.delenv("PROJECTS_JSON", raising=False)
    mod = _load_projects(monkeypatch)
    import store
    monkeypatch.setattr(store, "DB_PATH", db)
    assert mod.REGISTRY_PATH is None
    assert mod.resolve("table-product") == "table-product"
    assert mod.resolve("file-product") is None


def test_an_empty_table_reads_as_none_and_no_old_file_path_is_read(
        tmp_path, monkeypatch):
    """With the table empty, a registry file sitting at either of the two
    paths this module used to fall back to (the pre-0098 agent-platform
    path, and CQ·240's install-repository path) is not read: this module no
    longer computes either path at all."""
    old_agent_platform_path = tmp_path / "agent-platform" / "config" / "projects.json"
    old_agent_platform_path.parent.mkdir(parents=True)
    _write_registry(old_agent_platform_path, "agent-platform-path-product")

    old_install_path = tmp_path / "install" / "projects.json"
    old_install_path.parent.mkdir(parents=True)
    _write_registry(old_install_path, "install-path-product")

    db = _make_table_db(tmp_path)  # empty
    monkeypatch.delenv("PROJECTS_JSON", raising=False)
    mod = _load_projects(monkeypatch)
    import store
    monkeypatch.setattr(store, "DB_PATH", db)
    assert mod.REGISTRY_PATH is None
    assert not mod.available()
    assert mod.resolve("agent-platform-path-product") == "agent-platform-path-product"
    assert mod.resolve("install-path-product") == "install-path-product"
    assert mod.resolve("anything") == "anything"


# --- CQ-243 round 1: a failure opening or reading the table must not raise ---
# (reviewer and security-reviewer on PR #339: `store.connect()` can raise
# `sqlite3.Error` directly, not only `OSError`, and `project_registry.
# registry_document` can raise `json.JSONDecodeError` on a corrupt row —
# neither was caught, so either escaped `_table_signature`/
# `_read_table_registry` as a raw exception instead of reading as "registry
# unusable" the way the module's own docstring promises.)


def _raises(exc):
    """A zero-arg callable that raises `exc`, standing in for
    `store.connect` on a failed open."""
    def _connect():
        raise exc
    return _connect


def test_a_store_connect_sqlite_error_fails_open_and_closed_before_any_snapshot(
        tmp_path, monkeypatch):
    """`store.connect()` raising `sqlite3.OperationalError` — a permission
    problem, or a TOCTOU race after `DB_PATH.is_file()` — reads exactly as a
    missing database does on the very first read: `resolve()` fails open,
    the privacy readers fail closed to `None`, and nothing raises."""
    db = _make_table_db(tmp_path)
    monkeypatch.delenv("PROJECTS_JSON", raising=False)
    mod = _load_projects(monkeypatch)
    import store
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(
        store, "connect",
        _raises(sqlite3.OperationalError("unable to open database file")))

    assert mod.resolve("alpha") == "alpha"
    assert not mod.available()
    assert mod.private_repos() is None
    assert mod.private_products() is None
    assert mod.private_crew_names() is None


def test_a_store_connect_sqlite_error_keeps_the_last_good_snapshot(
        tmp_path, monkeypatch):
    """The same failure, hitting a process that already served a good
    snapshot: `resolve()` and the privacy readers keep serving it rather
    than raising or falling back to fail-open."""
    db = _make_table_db(tmp_path, {
        "alpha": {"aliases": [], "repos": ["alpha-repo"], "private": True},
    })
    monkeypatch.delenv("PROJECTS_JSON", raising=False)
    mod = _load_projects(monkeypatch)
    import store
    monkeypatch.setattr(store, "DB_PATH", db)
    clock = {"now": 0.0}
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock["now"])

    assert mod.resolve("alpha") == "alpha"
    assert mod.private_repos() == frozenset({"alpha", "alpha-repo"})

    clock["now"] += mod.FRESHNESS_SECONDS
    monkeypatch.setattr(
        store, "connect",
        _raises(sqlite3.OperationalError("unable to open database file")))

    assert mod.resolve("alpha") == "alpha"
    assert mod.private_repos() == frozenset({"alpha", "alpha-repo"})
    assert mod.available()


def test_a_misconfigured_database_fails_open_and_closed_before_any_snapshot(
        tmp_path, monkeypatch):
    """`store.connect()` raising `store.MisconfiguredDatabase` behaves the
    same as `sqlite3.Error` above: fail open on `resolve()`, fail closed to
    `None` on the privacy readers, nothing raised."""
    db = _make_table_db(tmp_path)
    monkeypatch.delenv("PROJECTS_JSON", raising=False)
    mod = _load_projects(monkeypatch)
    import store
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(
        store, "connect",
        _raises(store.MisconfiguredDatabase("LIBRARY_DB is Orient's database")))

    assert mod.resolve("alpha") == "alpha"
    assert not mod.available()
    assert mod.private_repos() is None
    assert mod.private_products() is None
    assert mod.private_crew_names() is None


def test_a_misconfigured_database_keeps_the_last_good_snapshot(tmp_path, monkeypatch):
    """`store.MisconfiguredDatabase` hitting a process that already served a
    good snapshot keeps serving it, the same as `sqlite3.Error` above."""
    db = _make_table_db(tmp_path, {
        "alpha": {"aliases": [], "repos": ["alpha-repo"], "private": True},
    })
    monkeypatch.delenv("PROJECTS_JSON", raising=False)
    mod = _load_projects(monkeypatch)
    import store
    monkeypatch.setattr(store, "DB_PATH", db)
    clock = {"now": 0.0}
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock["now"])

    assert mod.resolve("alpha") == "alpha"
    assert mod.private_repos() == frozenset({"alpha", "alpha-repo"})

    clock["now"] += mod.FRESHNESS_SECONDS
    monkeypatch.setattr(
        store, "connect",
        _raises(store.MisconfiguredDatabase("LIBRARY_DB is Orient's database")))

    assert mod.resolve("alpha") == "alpha"
    assert mod.private_repos() == frozenset({"alpha", "alpha-repo"})
    assert mod.available()


def _insert_raw_row(db, slug, entry_text, *, updated_at="2026-10-27 00:00:00"):
    """A `project_registry` row written with `entry` as the LITERAL text
    given, bypassing `json.dumps` — the one way a corrupt row gets into the
    table (a hand edit, or damage to the file), since every writer in
    `project_registry.py` serialises through `json.dumps` itself."""
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO project_registry (slug, entry, updated_at, "
            "updated_by) VALUES (?, ?, ?, 'test')",
            (slug, entry_text, updated_at))
        conn.commit()
    finally:
        conn.close()


def test_a_malformed_row_fails_open_and_closed_before_any_snapshot(
        tmp_path, monkeypatch):
    """A `project_registry.entry` column holding text that is not valid
    JSON — `project_registry.registry_document`'s `json.loads` raises
    `json.JSONDecodeError` on it — must not escape `_read_table_registry`
    as a raw exception on the very first read; it reads as an unreadable
    registry, exactly like a missing database."""
    db = _make_table_db(tmp_path)
    _insert_raw_row(db, "alpha", "{not json")
    monkeypatch.delenv("PROJECTS_JSON", raising=False)
    mod = _load_projects(monkeypatch)
    import store
    monkeypatch.setattr(store, "DB_PATH", db)

    assert mod.resolve("anything") == "anything"
    assert not mod.available()
    assert mod.private_repos() is None


def test_a_malformed_row_after_a_good_read_keeps_the_last_good_snapshot(
        tmp_path, monkeypatch):
    """The same corrupt row, landing after a good snapshot has already
    served: the last good snapshot keeps serving rather than the corrupt
    re-read raising or wiping it out."""
    db = _make_table_db(tmp_path, {"alpha": {"aliases": [], "repos": []}})
    monkeypatch.delenv("PROJECTS_JSON", raising=False)
    mod = _load_projects(monkeypatch)
    import store
    monkeypatch.setattr(store, "DB_PATH", db)
    clock = {"now": 0.0}
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock["now"])

    assert mod.resolve("alpha") == "alpha"

    clock["now"] += mod.FRESHNESS_SECONDS
    conn = sqlite3.connect(db)
    try:
        conn.execute("DELETE FROM project_registry")
        conn.commit()
    finally:
        conn.close()
    _insert_raw_row(db, "alpha", "{not json", updated_at="2026-10-27 00:00:01")

    assert mod.resolve("alpha") == "alpha"
    assert mod.available()
