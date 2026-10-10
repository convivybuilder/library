"""`projects.py` picks up a registry edit without a restart (agent-platform#757:
the register-a-crew command writes `config/projects.json` atomically — a temp
file in the same directory, then a rename — and Shipwright's rehearsal found
the Library still refusing the new crew's product because the old module
cached the registry for the life of the process).

Every assertion here points `projects.REGISTRY_PATH` at a file under `tmp_path`;
none of this reads or writes the real
`~/Convivy/agent-platform/config/projects.json`.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

import projects


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
