"""platform/decisions/0059: the commit `/health` reports must survive the move
from a development checkout to a deploy area.

`bin/install-library` builds the deploy area with `git archive`, which
exports no `.git` at all — so `git rev-parse HEAD`, `_running_commit`'s only
resolution path before this change, silently returns None in a deployed
tree, and `/health` would drop the `commit` field entirely the moment the
epic's own fix shipped. These tests hold the two properties that keep that
from happening: a DEPLOYED_COMMIT file in this process's own directory takes
priority over `git rev-parse HEAD`, and a malformed one resolves to absent
rather than to a wrong value — never to an exception, on either path.

`test_health_readiness.py` already covers `RUNNING_COMMIT` being stamped once
at import and never re-read per request; this file is about what feeds that
stamp, and about `staged_commit` / `restart`, the two `/health` fields
platform/decisions/0059 adds.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import app as libapp
import identity
import store

GOOD_SHA = "a" * 40
OTHER_SHA = "b" * 40


def _write_deployed_commit(path, sha, ref="main"):
    path.write_text(
        f"ref: {ref}\n"
        f"commit: {sha}\n"
        f"installed_at: 2026-08-30T00:00:00Z\n"
        f"installed_from: /tmp/wherever\n")


# --- _read_deployed_commit ---------------------------------------------------


def test_read_deployed_commit_returns_the_commit_line(tmp_path):
    f = tmp_path / "DEPLOYED_COMMIT"
    _write_deployed_commit(f, GOOD_SHA)
    assert libapp._read_deployed_commit(str(f)) == GOOD_SHA


def test_read_deployed_commit_missing_file_is_none(tmp_path):
    assert libapp._read_deployed_commit(str(tmp_path / "nope")) is None


def test_read_deployed_commit_with_no_commit_line_is_none(tmp_path):
    f = tmp_path / "DEPLOYED_COMMIT"
    f.write_text("ref: main\ninstalled_at: 2026-08-30T00:00:00Z\n")
    assert libapp._read_deployed_commit(str(f)) is None


@pytest.mark.parametrize("bad", [
    "too-short",
    "g" * 40,          # not hex
    "A" * 40,           # uppercase, the shape check requires lowercase
    "a" * 39,           # one short
    "a" * 41,           # one long
    "",
])
def test_read_deployed_commit_malformed_value_is_none(tmp_path, bad):
    f = tmp_path / "DEPLOYED_COMMIT"
    f.write_text(f"commit: {bad}\n")
    assert libapp._read_deployed_commit(str(f)) is None


def test_read_deployed_commit_invalid_utf8_is_none(tmp_path):
    """Invalid UTF-8 bytes raise `UnicodeDecodeError`, a `ValueError` rather
    than an `OSError` — the gap a reviewer caught (PR #70 round 1): the
    existing malformed-value cases above all use valid UTF-8 of the wrong
    shape, so none of them exercised this path. Planting actual invalid
    bytes is what proves the fix rather than the docstring's promise."""
    f = tmp_path / "DEPLOYED_COMMIT"
    f.write_bytes(b"commit: " + b"\xff\xfe" + b"a" * 38)
    assert libapp._read_deployed_commit(str(f)) is None


def test_read_deployed_commit_unreadable_directory_is_none(tmp_path):
    """A directory where a file was expected — the same "cannot determine,
    so absent" contract as every other failure mode here, never an
    exception reaching the caller."""
    assert libapp._read_deployed_commit(str(tmp_path)) is None


# --- _running_commit: DEPLOYED_COMMIT takes priority over git ---------------


def test_running_commit_prefers_deployed_commit_file(tmp_path):
    """A deploy-area install (git archive, no .git) resolves from the file
    it wrote — the whole fix. Confirmed against a directory that ALSO has a
    git repository, so this proves priority, not merely fallback."""
    import subprocess
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    (tmp_path / "f").write_text("x")
    subprocess.run(["git", "-C", str(tmp_path), "add", "f"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "x"], check=True)

    _write_deployed_commit(tmp_path / "DEPLOYED_COMMIT", GOOD_SHA)

    assert libapp._running_commit(dir_=str(tmp_path)) == GOOD_SHA


def test_running_commit_falls_back_to_git_with_no_deployed_commit_file(tmp_path):
    """A working checkout: no DEPLOYED_COMMIT file, so this falls through to
    `git rev-parse HEAD`, which is the pre-existing behaviour."""
    import subprocess
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    (tmp_path / "f").write_text("x")
    subprocess.run(["git", "-C", str(tmp_path), "add", "f"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "x"], check=True)
    head = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True).stdout.strip()

    assert libapp._running_commit(dir_=str(tmp_path)) == head


def test_running_commit_malformed_deployed_commit_falls_back_to_git(tmp_path):
    """A malformed DEPLOYED_COMMIT is not a wrong answer — it is treated as
    absent, and the resolver keeps looking rather than surfacing garbage."""
    import subprocess
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    (tmp_path / "f").write_text("x")
    subprocess.run(["git", "-C", str(tmp_path), "add", "f"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "x"], check=True)
    head = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True).stdout.strip()

    (tmp_path / "DEPLOYED_COMMIT").write_text("commit: not-a-sha\n")

    assert libapp._running_commit(dir_=str(tmp_path)) == head


def test_running_commit_neither_source_available_is_none(tmp_path):
    assert libapp._running_commit(dir_=str(tmp_path)) is None


# --- _staged_commit -----------------------------------------------------------


def test_staged_commit_reads_the_deployed_commit_file(tmp_path):
    _write_deployed_commit(tmp_path / "DEPLOYED_COMMIT", GOOD_SHA)
    assert libapp._staged_commit(dir_=str(tmp_path)) == GOOD_SHA


def test_staged_commit_is_none_for_a_working_checkout(tmp_path):
    """No DEPLOYED_COMMIT file, and — unlike `_running_commit` — no git
    fallback either: there is no separate staged state to report for a
    working checkout."""
    import subprocess
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    (tmp_path / "f").write_text("x")
    subprocess.run(["git", "-C", str(tmp_path), "add", "f"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "x"], check=True)

    assert libapp._staged_commit(dir_=str(tmp_path)) is None


def test_staged_commit_is_read_fresh_every_call(tmp_path):
    """The property that makes drift visible: change the file on disk with
    no reimport, and the next call sees the new value — the opposite of
    RUNNING_COMMIT's contract, deliberately."""
    f = tmp_path / "DEPLOYED_COMMIT"
    _write_deployed_commit(f, GOOD_SHA)
    assert libapp._staged_commit(dir_=str(tmp_path)) == GOOD_SHA

    _write_deployed_commit(f, OTHER_SHA)
    assert libapp._staged_commit(dir_=str(tmp_path)) == OTHER_SHA


# --- /health: commit, staged_commit and restart round-trip -------------------

AGENT = "agent-token"


@pytest.fixture
def client(tmp_path, monkeypatch, vector_index):
    db = tmp_path / "control.db"
    vector_index(db)
    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({AGENT: {"name": "an-agent", "products": ["*"]}}))
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    yield TestClient(libapp.app)
    libapp._QUIESCING = False
    libapp._IN_FLIGHT = 0


def test_health_reports_staged_commit_when_resolvable(client, monkeypatch):
    monkeypatch.setattr(libapp, "_staged_commit", lambda: OTHER_SHA)
    body = client.get("/health").json()
    assert body["staged_commit"] == OTHER_SHA


def test_health_omits_staged_commit_when_unresolvable(client, monkeypatch):
    monkeypatch.setattr(libapp, "_staged_commit", lambda: None)
    body = client.get("/health").json()
    assert "staged_commit" not in body


def test_health_staged_commit_can_differ_from_running_commit(client, monkeypatch):
    """The whole point: a deploy area updated on merge, this process has not
    restarted onto it yet, and both facts are visible in the same response."""
    monkeypatch.setattr(libapp, "RUNNING_COMMIT", GOOD_SHA)
    monkeypatch.setattr(libapp, "_staged_commit", lambda: OTHER_SHA)
    body = client.get("/health").json()
    assert body["commit"] == GOOD_SHA
    assert body["staged_commit"] == OTHER_SHA
    assert body["commit"] != body["staged_commit"]


def test_health_restart_reports_not_quiescing_by_default(client):
    body = client.get("/health").json()
    assert body["restart"] == {"quiescing": False, "in_flight": 0}


def test_health_restart_reports_quiescing_state(client, monkeypatch):
    monkeypatch.setattr(libapp, "_QUIESCING", True)
    monkeypatch.setattr(libapp, "_IN_FLIGHT", 3)
    body = client.get("/health").json()
    assert body["restart"] == {"quiescing": True, "in_flight": 3}


def test_health_stays_reachable_while_quiescing(client, monkeypatch):
    """`/health` is exempt from the refusing middleware — it must stay
    answerable through a quiesce, since it is the only place the refusing
    state is observable from outside the process."""
    monkeypatch.setattr(libapp, "_QUIESCING", True)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["restart"]["quiescing"] is True
