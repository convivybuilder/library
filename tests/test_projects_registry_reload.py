"""`projects.py` picks up a registry edit without a restart (agent-platform#757:
the register-a-crew command writes `config/projects.json` atomically — a temp
file in the same directory, then a rename — and Shipwright's rehearsal found
the Library still refusing the new crew's product because the old module
cached the registry for the life of the process).

Every file-mode assertion here points `projects.REGISTRY_PATH` at a file
under `tmp_path`; the table-mode tests at the bottom point `store.DB_PATH`
at a scratch database under `tmp_path` instead. None of this reads or
writes the real vault.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

import pytest

import project_registry
import projects
import store


def _atomic_write(path: Path, data: dict, *, tag: str = "") -> None:
    """The write agent-platform#757 makes: a temp file in the same directory,
    then a rename over the target."""
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{tag}")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


@pytest.fixture
def registry_path(tmp_path, monkeypatch):
    """A fresh `REGISTRY_PATH` under `tmp_path`, with the module's memory of
    any previous test's registry cleared on the way in and the way out."""
    path = tmp_path / "projects.json"
    monkeypatch.setattr(projects, "REGISTRY_PATH", path)
    projects.reload()
    yield path
    projects.reload()


def test_an_edit_is_seen_by_resolve_without_reload(registry_path):
    _atomic_write(registry_path, {"alpha": {"aliases": ["a"]}})
    assert projects.resolve("alpha") == "alpha"
    assert projects.resolve("beta") is None

    _atomic_write(registry_path, {"beta": {"aliases": ["b"]}}, tag="2")
    assert projects.resolve("beta") == "beta"
    assert projects.resolve("alpha") is None


def test_an_edit_is_seen_by_every_private_view_without_reload(registry_path):
    _atomic_write(registry_path, {
        "alpha": {"private": True, "repos": ["alpha-repo"]},
    })
    assert projects.private_repos() == frozenset({"alpha", "alpha-repo"})
    assert projects.private_products() == frozenset({"alpha"})
    assert projects.private_crew_names() == frozenset({"alpha", "alpha-repo"})

    _atomic_write(registry_path, {
        "beta": {"private": True, "repos": ["beta-repo"]},
    }, tag="2")
    assert projects.private_repos() == frozenset({"beta", "beta-repo"})
    assert projects.private_products() == frozenset({"beta"})
    assert projects.private_crew_names() == frozenset({"beta", "beta-repo"})


def test_an_atomic_rename_with_identical_size_and_mtime_is_seen(registry_path):
    """The inode is the part of the signature that has to do the work here:
    forcing matching sizes and a matching mtime on both versions still must
    not hide the second write, because a rename over the target is exactly
    what can leave size and, within timestamp resolution, mtime unchanged."""
    first = {"alpha": {"repos": ["r"]}}
    second = {"beta_": {"repos": ["r"]}}
    assert len(json.dumps(first)) == len(json.dumps(second))

    _atomic_write(registry_path, first)
    fixed_ns = registry_path.stat().st_mtime_ns
    assert projects.resolve("alpha") == "alpha"

    _atomic_write(registry_path, second, tag="2")
    os.utime(registry_path, ns=(fixed_ns, fixed_ns))
    second_stat = registry_path.stat()
    assert second_stat.st_mtime_ns == fixed_ns
    assert second_stat.st_size == len(json.dumps(first))

    assert projects.resolve("beta_") == "beta_"
    assert projects.resolve("alpha") is None


def test_a_malformed_rewrite_after_a_good_read_keeps_the_last_good_snapshot(
        registry_path, caplog):
    _atomic_write(registry_path, {"alpha": {"aliases": ["a"]}})
    assert projects.resolve("alpha") == "alpha"

    caplog.set_level("WARNING", logger="projects")
    registry_path.write_text("{not json")

    assert projects.resolve("alpha") == "alpha", (
        "a half-written edit must not turn off validation on a service "
        "that was already serving a good registry")
    assert projects.available()

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1, (
        "the failed re-read logs once, not on every subsequent call "
        f"against the same broken file: {warnings}")

    # Calling in some more does not add a second warning for the same break.
    projects.resolve("alpha")
    projects.available()
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1


def test_an_edit_is_seen_by_every_kb_view_without_reload(registry_path):
    """`kb_slugs()`, `scopes()` and `kb_legacy()` — platform/decisions/0097
    point 1's three pieces — each pick up a registry edit the same way
    every other view in this module does."""
    _atomic_write(registry_path, {
        "alpha": {"kb": "central", "kb_project": "alpha-kb"},
        "beta": {"kb": "split"},
        "gamma": {"kb": "local"},
        "_scopes": {"company": {}, "platform": {}},
        "_kb_legacy": ["substrate", "knowledge"],
    })
    assert projects.kb_slugs() == frozenset({"alpha-kb", "beta"})
    assert projects.scopes() == frozenset({"company", "platform"})
    assert projects.kb_legacy() == frozenset({"substrate", "knowledge"})

    _atomic_write(registry_path, {
        "delta": {"kb": "central"},
        "_scopes": {"commercial": {}},
        "_kb_legacy": [],
    }, tag="2")
    assert projects.kb_slugs() == frozenset({"delta"})
    assert projects.scopes() == frozenset({"commercial"})
    assert projects.kb_legacy() == frozenset()


def test_kb_views_return_none_when_the_registry_is_unreadable(registry_path):
    """Distinguishable from "readable and admits nothing" the same way
    `private_crew_names()` returns `None` rather than an empty set — no
    `_atomic_write` has run, so `registry_path` does not exist yet."""
    assert projects.kb_slugs() is None
    assert projects.scopes() is None
    assert projects.kb_legacy() is None


def test_a_missing_file_on_first_read_fails_open_as_before(registry_path, capsys):
    assert projects.resolve("anything") == "anything"
    assert not projects.available()
    err = capsys.readouterr().err
    assert "not found" in err
    assert "product validation" in err


def test_private_products_never_mixes_two_registry_versions(registry_path):
    """`private_products()` is the view most likely to show a mix, because it
    reads `raw` (to find the private entries) and `resolution_map` (to
    canonicalise their slugs) together — exactly the pairing that split,
    separately-invalidated caches could serve from two different reads.
    Mixing shows up as an impossible answer: neither version's full set,
    typically empty, because a slug from one version's `raw` finds nothing
    under that key in the other version's `resolution_map`.
    """
    version_a = {"proj-a": {"private": True}}
    version_b = {"proj-b": {"private": True}}
    expected = {frozenset({"proj-a"}), frozenset({"proj-b"})}

    _atomic_write(registry_path, version_a)

    stop = threading.Event()

    def writer():
        tag = 0
        while not stop.is_set():
            tag += 1
            _atomic_write(registry_path, version_a if tag % 2 else version_b,
                          tag=str(tag))

    results: list[frozenset] = []
    errors: list[Exception] = []

    def reader():
        try:
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline:
                results.append(projects.private_products())
        except Exception as e:  # noqa: BLE001 - surfaced via `errors` below
            errors.append(e)

    writer_thread = threading.Thread(target=writer)
    reader_threads = [threading.Thread(target=reader) for _ in range(4)]
    writer_thread.start()
    for t in reader_threads:
        t.start()
    for t in reader_threads:
        t.join()
    stop.set()
    writer_thread.join()

    assert not errors
    assert results, "the reader threads should have observed something"
    seen = set(results)
    assert seen <= expected, (
        f"saw a result that matches neither registry version whole: "
        f"{seen - expected}")


# --- table mode (CQ·243) -----------------------------------------------------


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
    """`REGISTRY_PATH` cleared to `None` (table mode) and `store.DB_PATH`
    pointed at a scratch database holding only `project_registry`, with the
    module's memory of any previous test's registry cleared on the way in
    and the way out — the table-mode counterpart of `registry_path` above."""
    db = _make_table_db(tmp_path)
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    monkeypatch.setattr(projects, "REGISTRY_PATH", None)
    projects.reload()
    yield db
    projects.reload()


@pytest.fixture
def fake_clock(monkeypatch):
    """A controllable `time.monotonic()` for `projects.py`'s table-mode
    freshness throttle (CQ·243, `FRESHNESS_SECONDS`), so a test can step
    past the throttle window without a real sleep. Starts at `0.0`, which
    `table_db`'s `projects.reload()` already made irrelevant to the very
    first check (`_last_table_check` is `_UNATTEMPTED`, not a time)."""
    state = {"now": 0.0}
    monkeypatch.setattr(projects.time, "monotonic", lambda: state["now"])
    return state


def test_a_registration_is_seen_by_resolve_without_reload(table_db, fake_clock):
    project_registry.replace({"alpha": {"aliases": ["a"]}}, actor="test")
    assert projects.resolve("alpha") == "alpha"
    assert projects.resolve("beta") is None

    # Past the throttle window, standing in for `load_project_registry.py`'s
    # write landing in a different process: nothing here calls
    # `projects.reload()`, so this is the table changing and the clock
    # moving, not an explicit cache-bust.
    fake_clock["now"] += projects.FRESHNESS_SECONDS
    project_registry.replace({"beta": {"aliases": ["b"]}}, actor="test",
                             allow_populated=True, remove=["alpha"])
    assert projects.resolve("beta") == "beta"
    assert projects.resolve("alpha") is None


def test_an_unchanged_updated_at_does_not_reread_the_table(table_db, monkeypatch):
    project_registry.replace({"alpha": {"aliases": ["a"]}}, actor="test")

    calls = []
    real_registry_document = project_registry.registry_document

    def counting(conn):
        calls.append(1)
        return real_registry_document(conn)

    monkeypatch.setattr(project_registry, "registry_document", counting)

    assert projects.resolve("alpha") == "alpha"
    assert projects.resolve("alpha") == "alpha"
    assert projects.resolve("alpha") == "alpha"
    assert len(calls) == 1, (
        "three reads with no table change must share one document read")


def test_an_updated_at_change_reloads(table_db, monkeypatch, fake_clock):
    project_registry.replace({"alpha": {"aliases": ["a"]}}, actor="test",
                             now="2026-10-27 00:00:00")
    assert projects.resolve("alpha") == "alpha"

    calls = []
    real_registry_document = project_registry.registry_document

    def counting(conn):
        calls.append(1)
        return real_registry_document(conn)

    monkeypatch.setattr(project_registry, "registry_document", counting)

    # Past the throttle window: the freshness query this test counts is the
    # one `_throttled_table_signature` runs, not merely `registry_document`,
    # so it is gated on the clock exactly as a reader in a live process is.
    fake_clock["now"] += projects.FRESHNESS_SECONDS
    project_registry.replace({"beta": {"aliases": ["b"]}}, actor="test",
                             allow_populated=True, remove=["alpha"],
                             now="2026-10-27 00:00:01")
    assert projects.resolve("beta") == "beta"
    assert projects.resolve("alpha") is None
    assert len(calls) == 1, "the changed updated_at must trigger exactly one reread"


def test_table_views_return_none_when_the_table_is_empty(table_db):
    assert projects.kb_slugs() is None
    assert projects.scopes() is None
    assert projects.kb_legacy() is None
    assert projects.private_repos() is None
    assert projects.resolve("anything") == "anything"
    assert not projects.available()
