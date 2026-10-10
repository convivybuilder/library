"""`profile_sync.refresh()` reloads the `profiles` table from the served knowledge
checkout when a profile file changes, with no manual load
(issue_01M45AGVTEH7EZMS45M6PGJ0Y7; the mechanism follows library#206).

**Every assertion reads a served answer or the stored row.** The HTTP tests
issue `POST /bundles` against a real control database built by `alembic upgrade
head`, and the unit tests read the `profiles` table directly. Each knowledge
checkout is a `tmp_path` tree, and `LIBRARY_KNOWLEDGE_ROOT` is never read.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as libapp
import crew_registry
import identity
import profile_sync
import protocol
import reference
import store

REPO = Path(__file__).resolve().parent.parent

AGENT_TOKEN = "agent-token-bosun"
ORCHESTRATOR_TOKEN = "orchestrator-token-co"
ORCHESTRATOR = identity.Identity(actor="co-library-test", kind="orchestrator")

# id, kind, project, status, audience. One `coder` agent definition and one
# `coder` skill in each of two projects, so a profile that changes which
# project its skill rule names changes the bundle's skill.
DOCS = [
    ("p/agents/coder", "agent", "p", "current", ["coder"]),
    ("p/skills/s-one", "skill", "p", "current", ["coder"]),
    ("q/skills/s-two", "skill", "q", "current", ["coder"]),
]


# --- fixtures ----------------------------------------------------------------


@pytest.fixture(scope="module")
def migrated_db(tmp_path_factory):
    db = tmp_path_factory.mktemp("migrated") / "library.db"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=str(REPO),
        capture_output=True, text=True, check=False,
        env={"PATH": "/usr/bin:/bin", "HOME": str(REPO),
             "LIBRARY_DB": str(db)})
    assert result.returncode == 0, result.stderr
    return db


@pytest.fixture
def served(tmp_path, monkeypatch):
    """An empty knowledge checkout served by `profile_sync`, with the module's
    memory of earlier tests cleared on the way in and out."""
    root = tmp_path / "knowledge"
    (root / "projects").mkdir(parents=True)
    monkeypatch.setattr(profile_sync, "KNOWLEDGE_ROOT", str(root))
    profile_sync.reset()
    yield root
    profile_sync.reset()


@pytest.fixture
def db(tmp_path, monkeypatch):
    """The `profiles` and `roster_agents` tables, from the migration's own
    columns, for the unit tests that never issue a bundle."""
    path = tmp_path / "control.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE roster_agents (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            created_by TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            updated_by TEXT NOT NULL
        );
        CREATE TABLE profiles (
            id INTEGER PRIMARY KEY,
            project TEXT NOT NULL,
            variant TEXT NOT NULL,
            include TEXT NOT NULL,
            claude_md_extras TEXT NOT NULL,
            created_at TEXT NOT NULL,
            created_by TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            updated_by TEXT NOT NULL,
            UNIQUE (project, variant)
        );
    """)
    conn.commit()
    conn.close()
    monkeypatch.setattr(store, "DB_PATH", path)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    return path


def _build_index(path):
    iconn = sqlite3.connect(path)
    iconn.executescript("""
        CREATE TABLE docs (
            id TEXT PRIMARY KEY, source TEXT NOT NULL DEFAULT 'kb',
            source_ref TEXT, kind TEXT, project TEXT, title TEXT, status TEXT,
            topics TEXT DEFAULT '[]', audience TEXT DEFAULT '[]',
            last_reviewed TEXT, updated_at TEXT, body TEXT, body_hash TEXT,
            frontmatter_extra TEXT NOT NULL DEFAULT '{}');
    """)
    for doc_id, kind, project, status, audience in DOCS:
        body = f"body of {doc_id}"
        iconn.execute(
            "INSERT INTO docs (id, source_ref, kind, project, title, status, "
            "audience, body, body_hash, last_reviewed, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (doc_id, f"{doc_id}.md", kind, project, f"title of {doc_id}",
             status, json.dumps(audience), body, body,
             "2026-08-01", "2026-08-02"))
    iconn.commit()
    iconn.close()


@pytest.fixture
def client(migrated_db, served, tmp_path, monkeypatch):
    db = tmp_path / "library.db"
    shutil.copy(migrated_db, db)
    index = tmp_path / "index.db"
    _build_index(index)
    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        AGENT_TOKEN: {"name": "bosun", "products": ["*"]},
        ORCHESTRATOR_TOKEN: {"name": "co", "kind": "orchestrator",
                             "products": ["*"]},
    }))
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(reference, "INDEX_DB", index)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(identity, "CLIENT_TOKENS", tmp_path / "none.json")
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    crew_registry.roster_add(ORCHESTRATOR, "coder")
    return TestClient(libapp.app)


# --- helpers -----------------------------------------------------------------


def _profile(skill_project: str, *, name: str = "p/daytime") -> str:
    """A profile selecting `p`'s agent and the skills of `skill_project`."""
    return textwrap.dedent(f"""\
        profile: {name}
        include:
          - kind: agent
            project: [p]
            status: current
            audience_contains: [coder]
          - kind: skill
            project: [{skill_project}]
            status: current
            audience_contains: [coder]
        """)


def _write_profile(root: Path, variant: str, body, project="p") -> None:
    """Write a profile the way a checkout does: a new file renamed over the
    old one, so the inode moves as well as the contents. `body` is text, or
    bytes for a file that is not valid UTF-8."""
    d = root / "projects" / project / "profiles"
    d.mkdir(parents=True, exist_ok=True)
    target = d / f"{variant}.yaml"
    tmp = target.with_name(target.name + ".tmp")
    if isinstance(body, bytes):
        tmp.write_bytes(body)
    else:
        tmp.write_text(body)
    os.replace(tmp, target)


def _h(token=AGENT_TOKEN):
    return {"Authorization": f"Bearer {token}",
            protocol.HEADER: str(protocol.CURRENT)}


def _issue(client, variant="daytime"):
    return client.post("/bundles", json={"agent": "coder", "repo": "p",
                                         "variant": variant}, headers=_h())


def _ids(response):
    return [d["id"] for d in response.json()["documents"]]


def _stored():
    conn = sqlite3.connect(store.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return {(r["project"], r["variant"]): dict(r) for r in
                conn.execute("SELECT * FROM profiles")}
    finally:
        conn.close()


def _include(project, variant):
    return json.loads(_stored()[(project, variant)]["include"])


# --- the served answer changes with no manual load ---------------------------


def test_an_edited_profile_file_changes_the_bundle_on_the_next_issue(
        client, served):
    _write_profile(served, "daytime", _profile("p"))
    first = _issue(client)
    assert first.status_code == 201, first.text
    assert _ids(first) == ["p/agents/coder", "p/skills/s-one"]

    _write_profile(served, "daytime", _profile("q"))
    second = _issue(client)
    assert second.status_code == 201, second.text
    assert _ids(second) == ["p/agents/coder", "q/skills/s-two"]


def test_a_profile_file_added_to_the_checkout_is_served_on_the_next_issue(
        client, served):
    _write_profile(served, "daytime", _profile("p"))
    before = _issue(client, variant="overnight")
    assert before.status_code == 422, before.text
    assert "no profile p/overnight" in before.text

    _write_profile(served, "overnight", _profile("q", name="p/overnight"))
    after = _issue(client, variant="overnight")
    assert after.status_code == 201, after.text
    assert _ids(after) == ["p/agents/coder", "q/skills/s-two"]


def test_get_profiles_lists_a_profile_added_to_the_checkout(client, served):
    _write_profile(served, "daytime", _profile("p"))
    listed = client.get("/profiles", headers=_h(ORCHESTRATOR_TOKEN))
    assert listed.status_code == 200, listed.text
    assert [(p["project"], p["variant"]) for p in listed.json()["profiles"]] \
        == [("p", "daytime")]


def test_get_profile_resolves_the_edited_rule(client, served):
    _write_profile(served, "daytime", _profile("p"))
    first = client.get("/profiles/p/daytime", headers=_h(ORCHESTRATOR_TOKEN))
    assert first.status_code == 200, first.text
    assert "p/skills/s-one" in [d["id"] for d in first.json()["documents"]]

    _write_profile(served, "daytime", _profile("q"))
    second = client.get("/profiles/p/daytime", headers=_h(ORCHESTRATOR_TOKEN))
    assert second.status_code == 200, second.text
    ids = [d["id"] for d in second.json()["documents"]]
    assert "q/skills/s-two" in ids and "p/skills/s-one" not in ids


def test_a_removed_profile_file_is_reconciled_away_once_the_removal_is_confirmed(
        client, served, clock):
    _write_profile(served, "daytime", _profile("p"))
    _write_profile(served, "overnight", _profile("p", name="p/overnight"))
    assert _issue(client, variant="overnight").status_code == 201

    (served / "projects" / "p" / "profiles" / "overnight.yaml").unlink()
    held = _issue(client, variant="overnight")
    assert held.status_code == 201, held.text
    assert set(_stored()) == {("p", "daytime"), ("p", "overnight")}

    clock[0] += profile_sync.CONFIRM_ABSENT_SECONDS
    gone = _issue(client, variant="overnight")
    assert gone.status_code == 422, gone.text
    assert "no profile p/overnight" in gone.text

    stored = _stored()
    assert set(stored) == {("p", "daytime")}
    assert stored[("p", "daytime")]["updated_by"] == \
        profile_sync.AUTOMATIC_ACTOR


# --- the reconcile itself ----------------------------------------------------


def test_an_added_file_is_written_as_the_automatic_reload(db, served):
    _write_profile(served, "daytime", _profile("p"))
    profile_sync.refresh()
    row = _stored()[("p", "daytime")]
    assert row["created_by"] == profile_sync.AUTOMATIC_ACTOR
    assert row["updated_by"] == profile_sync.AUTOMATIC_ACTOR
    assert json.loads(row["include"])[1]["project"] == ["p"]


@pytest.mark.parametrize("broken", [
    # Fails validation: `gateway` belongs to an agent's definition (ARCH-134).
    "profile: p/daytime\ninclude:\n  - kind: agent\n    gateway: nope\n",
    # Does not parse.
    "profile: p/daytime\ninclude: [unclosed\n",
    # Declares a profile other than its own path.
    "profile: p/elsewhere\ninclude: []\n",
    # Not UTF-8: `open` raises UnicodeDecodeError, a ValueError.
    b"profile: p/daytime\ninclude: []\n# \xff\n",
    # A non-string key beside an unknown string key: `sorted(unknown)` in
    # crew_registry raises TypeError on the mixed set.
    "profile: p/daytime\ninclude:\n  - kind: agent\n    1: x\n    foo: y\n",
], ids=["fails-validation", "does-not-parse", "key-mismatch",
        "not-utf8", "mixed-key-types"])
def test_a_malformed_file_keeps_its_stored_row_and_the_others_still_load(
        db, served, caplog, broken):
    _write_profile(served, "daytime", _profile("p"))
    profile_sync.refresh()
    stored = _include("p", "daytime")

    _write_profile(served, "daytime", broken)
    _write_profile(served, "overnight", _profile("q", name="p/overnight"))
    caplog.set_level("WARNING", logger="profile_sync")
    profile_sync.refresh()

    assert _include("p", "daytime") == stored, (
        "a file that does not load must leave its stored row exactly as it was")
    assert ("p", "overnight") in _stored(), (
        "one bad file must not stop the others from loading")

    def not_loaded():
        return [r for r in caplog.records if "not loaded" in r.getMessage()]
    assert len(not_loaded()) == 1
    profile_sync.refresh()
    assert len(not_loaded()) == 1, (
        "the same broken state is logged once, not on every read")


def test_a_single_absent_scan_keeps_the_row(db, served, clock):
    """`git merge --ff-only` unlinks and rewrites a changed file, so a scan
    inside that window sees the file absent. The next scan sees it again. A
    second profile keeps the checkout trusted, so the absence is the file's
    and not the root's."""
    _write_profile(served, "daytime", _profile("p"))
    _write_profile(served, "overnight", _profile("q", name="p/overnight"))
    profile_sync.refresh()
    (served / "projects" / "p" / "profiles" / "daytime.yaml").unlink()
    profile_sync.refresh()
    assert set(_stored()) == {("p", "daytime"), ("p", "overnight")}, (
        "one absent scan is not a removal")

    _write_profile(served, "daytime", _profile("q"))
    clock[0] += profile_sync.CONFIRM_ABSENT_SECONDS
    profile_sync.refresh()
    assert set(_stored()) == {("p", "daytime"), ("p", "overnight")}
    assert _include("p", "daytime")[1]["project"] == ["q"]


def test_a_confirmed_removal_deletes_the_row(db, served, clock):
    _write_profile(served, "daytime", _profile("p"))
    _write_profile(served, "overnight", _profile("q", name="p/overnight"))
    profile_sync.refresh()
    (served / "projects" / "p" / "profiles" / "daytime.yaml").unlink()
    profile_sync.refresh()

    clock[0] += profile_sync.CONFIRM_ABSENT_SECONDS - 1
    profile_sync.refresh()
    assert ("p", "daytime") in _stored(), (
        "a read before the confirming scan is due does not scan again")

    clock[0] += 1
    profile_sync.refresh()
    assert set(_stored()) == {("p", "overnight")}


@pytest.mark.parametrize("failure", [
    sqlite3.OperationalError("database is locked"),
    store.WritesDisabled("writes are off"),
    profile_sync.ProfileRootError("not a checkout"),
    # LIBRARY_DB repointed at Orient's file: `connect_rw` raises here.
    store.MisconfiguredDatabase("LIBRARY_DB is Orient's database"),
], ids=["sqlite", "writes-off", "root-refused", "misconfigured"])
def test_a_failed_reconcile_does_not_let_an_earlier_absence_confirm_a_removal(
        db, served, clock, monkeypatch, failure):
    """A hold from a scan before a failed reconcile must not carry into the next
    absent scan. The failure is injected at the write, where the sqlite and
    write-gate errors occur; `ProfileRootError` is raised there too, which the
    branch handles the same way. Every case returns normally from `refresh()`,
    `MisconfiguredDatabase` included."""
    daytime = served / "projects" / "p" / "profiles" / "daytime.yaml"
    _write_profile(served, "daytime", _profile("p"))
    _write_profile(served, "overnight", _profile("q", name="p/overnight"))
    profile_sync.refresh()

    daytime.unlink()
    profile_sync.refresh()
    assert set(_stored()) == {("p", "daytime"), ("p", "overnight")}

    _write_profile(served, "daytime", _profile("p"))
    real_apply = profile_sync.apply

    def fails(ident, loaded, gone):
        raise failure
    monkeypatch.setattr(profile_sync, "apply", fails)
    clock[0] += 1
    profile_sync.refresh()

    monkeypatch.setattr(profile_sync, "apply", real_apply)
    clock[0] += profile_sync.RETRY_BACKOFF_SECONDS
    daytime.unlink()
    _write_profile(served, "overnight", _profile("q", name="p/overnight"))
    profile_sync.refresh()
    assert set(_stored()) == {("p", "daytime"), ("p", "overnight")}, (
        "the first absent scan after the failure is a first sighting, so it "
        "holds the row")

    clock[0] += profile_sync.CONFIRM_ABSENT_SECONDS
    profile_sync.refresh()
    assert set(_stored()) == {("p", "overnight")}, (
        "a second absent scan a confirmation window later still deletes it")


def test_a_failed_confirming_scan_is_retried_after_the_backoff(
        db, served, clock, monkeypatch):
    """The scan that would confirm a removal hits a transient SQLite error, and
    the checkout does not change afterwards. The retry must come from the
    recheck still being armed, not from a file moving. A failed reconcile
    forgets its holds, so the retry is a first sighting, and the removal is
    confirmed a window after that."""
    daytime = served / "projects" / "p" / "profiles" / "daytime.yaml"
    _write_profile(served, "daytime", _profile("p"))
    _write_profile(served, "overnight", _profile("q", name="p/overnight"))
    profile_sync.refresh()

    daytime.unlink()
    profile_sync.refresh()
    assert set(_stored()) == {("p", "daytime"), ("p", "overnight")}

    real_apply = profile_sync.apply

    def locked_on_delete(ident, loaded, gone):
        if gone:
            raise sqlite3.OperationalError("database is locked")
        return real_apply(ident, loaded, gone)
    monkeypatch.setattr(profile_sync, "apply", locked_on_delete)
    clock[0] += profile_sync.CONFIRM_ABSENT_SECONDS
    profile_sync.refresh()
    assert set(_stored()) == {("p", "daytime"), ("p", "overnight")}

    monkeypatch.setattr(profile_sync, "apply", real_apply)
    clock[0] += profile_sync.RETRY_BACKOFF_SECONDS
    profile_sync.refresh()
    assert set(_stored()) == {("p", "daytime"), ("p", "overnight")}, (
        "the retry after the backoff is a first sighting, so it holds the row")

    clock[0] += profile_sync.CONFIRM_ABSENT_SECONDS
    profile_sync.refresh()
    assert set(_stored()) == {("p", "overnight")}, (
        "the confirming scan retried after the backoff deletes the row once "
        "the absence is confirmed, with no checkout change")


def test_a_read_inside_the_backoff_after_a_misconfigured_database_does_not_reconcile(
        db, served, clock, monkeypatch):
    """The checkout stays changed after the failure, so its signature still
    differs from the last attempt. Only the backoff keeps the read inside the
    window from reconciling again."""
    _write_profile(served, "daytime", _profile("p"))
    profile_sync.refresh()
    stored = _include("p", "daytime")

    _write_profile(served, "daytime", _profile("q"))
    calls = []

    def misconfigured(ident, loaded, gone):
        calls.append(ident)
        raise store.MisconfiguredDatabase("LIBRARY_DB is Orient's database")
    monkeypatch.setattr(profile_sync, "apply", misconfigured)

    profile_sync.refresh()
    assert len(calls) == 1
    clock[0] += profile_sync.RETRY_BACKOFF_SECONDS - 1
    profile_sync.refresh()
    assert len(calls) == 1, "a read inside the backoff does not reconcile"
    assert _include("p", "daytime") == stored, "the stored rows are served"


def test_a_held_removal_is_confirmed_after_a_misconfigured_database_with_no_checkout_change(
        db, served, clock, monkeypatch):
    """The confirming scan's deletion hits the misconfigured raise, and the
    checkout does not change afterwards. The retry must come from the recheck
    still being armed, as in the sqlite case, and the removal is confirmed a
    window after the first scan that runs once the backoff has passed."""
    daytime = served / "projects" / "p" / "profiles" / "daytime.yaml"
    _write_profile(served, "daytime", _profile("p"))
    _write_profile(served, "overnight", _profile("q", name="p/overnight"))
    profile_sync.refresh()

    daytime.unlink()
    profile_sync.refresh()
    assert set(_stored()) == {("p", "daytime"), ("p", "overnight")}

    real_apply = profile_sync.apply

    def misconfigured_on_delete(ident, loaded, gone):
        if gone:
            raise store.MisconfiguredDatabase("LIBRARY_DB is Orient's database")
        return real_apply(ident, loaded, gone)
    monkeypatch.setattr(profile_sync, "apply", misconfigured_on_delete)
    clock[0] += profile_sync.CONFIRM_ABSENT_SECONDS
    profile_sync.refresh()
    assert set(_stored()) == {("p", "daytime"), ("p", "overnight")}

    monkeypatch.setattr(profile_sync, "apply", real_apply)
    clock[0] += profile_sync.RETRY_BACKOFF_SECONDS
    profile_sync.refresh()
    assert set(_stored()) == {("p", "daytime"), ("p", "overnight")}, (
        "the retry after the backoff is a first sighting, so it holds the row")

    clock[0] += profile_sync.CONFIRM_ABSENT_SECONDS
    profile_sync.refresh()
    assert set(_stored()) == {("p", "overnight")}, (
        "the confirming scan runs after the backoff with no checkout change, "
        "and deletes the row")


def test_an_edit_to_one_profile_leaves_the_others_row_untouched(db, served):
    """A reload writes only the rows whose content changed, so an edit to one
    profile does not move `updated_at` or `updated_by` on another."""
    _write_profile(served, "daytime", _profile("p"))
    _write_profile(served, "overnight", _profile("q", name="p/overnight"))
    profile_sync.refresh()
    before = _stored()

    _write_profile(served, "daytime", _profile("r"))
    profile_sync.refresh()
    after = _stored()
    assert after[("p", "overnight")] == before[("p", "overnight")]
    assert after[("p", "daytime")]["include"] != before[("p", "daytime")]["include"]


def test_a_checkout_with_no_profile_files_deletes_nothing(db, served, caplog):
    _write_profile(served, "daytime", _profile("p"))
    profile_sync.refresh()
    (served / "projects" / "p" / "profiles" / "daytime.yaml").unlink()

    caplog.set_level("WARNING", logger="profile_sync")
    profile_sync.refresh()
    assert set(_stored()) == {("p", "daytime")}
    assert any("found no profiles" in r.getMessage() for r in caplog.records)


def test_a_missing_checkout_deletes_nothing(db, served, tmp_path, monkeypatch):
    _write_profile(served, "daytime", _profile("p"))
    profile_sync.refresh()
    monkeypatch.setattr(profile_sync, "KNOWLEDGE_ROOT",
                        str(tmp_path / "not-checked-out"))
    profile_sync.refresh()
    assert set(_stored()) == {("p", "daytime")}


def test_the_stored_rows_are_served_while_writes_are_disabled(
        db, served, monkeypatch, caplog):
    _write_profile(served, "daytime", _profile("p"))
    profile_sync.refresh()
    stored = _include("p", "daytime")

    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    _write_profile(served, "daytime", _profile("q"))
    caplog.set_level("WARNING", logger="profile_sync")
    profile_sync.refresh()

    assert _include("p", "daytime") == stored
    assert any("writes are off" in r.getMessage() for r in caplog.records)


def test_an_unchanged_checkout_is_not_reconciled_again(db, served,
                                                       monkeypatch):
    _write_profile(served, "daytime", _profile("p"))
    profile_sync.refresh()

    def no_reconcile(*args, **kwargs):
        raise AssertionError("reconciled a checkout that did not change")
    monkeypatch.setattr(profile_sync, "apply", no_reconcile)
    profile_sync.refresh()
    profile_sync.refresh()


@pytest.fixture
def clock(monkeypatch):
    """`profile_sync`'s clock, held still until a test advances it."""
    now = [1000.0]
    monkeypatch.setattr(profile_sync, "_clock", lambda: now[0])
    return now


def test_a_transient_write_failure_is_retried_once_the_backoff_passes(
        db, served, monkeypatch, clock):
    _write_profile(served, "daytime", _profile("p"))
    real_apply = profile_sync.apply
    calls = []

    def locked_once(ident, loaded, gone):
        calls.append(ident)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_apply(ident, loaded, gone)
    monkeypatch.setattr(profile_sync, "apply", locked_once)

    profile_sync.refresh()
    assert _stored() == {}, "the failed attempt wrote nothing"
    clock[0] += profile_sync.RETRY_BACKOFF_SECONDS
    profile_sync.refresh()
    assert set(_stored()) == {("p", "daytime")}
    assert len(calls) == 2


def test_a_transient_failure_backs_off_and_logs_once(
        db, served, monkeypatch, clock, caplog):
    """A failing writer used to cost every read a full reparse and a wait on
    the busy timeout under `_lock`. Inside the backoff, a read serves the
    stored rows without touching the writer or the log."""
    _write_profile(served, "daytime", _profile("p"))
    profile_sync.refresh()
    stored = _include("p", "daytime")

    _write_profile(served, "daytime", _profile("q"))
    calls = []

    def always_locked(ident, loaded, gone):
        calls.append(ident)
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(profile_sync, "apply", always_locked)
    caplog.set_level("WARNING", logger="profile_sync")

    for _ in range(5):
        profile_sync.refresh()
    assert len(calls) == 1, "only the first read after the failure retries"
    failures = [r for r in caplog.records
                if "profile reload failed" in r.getMessage()]
    assert len(failures) == 1
    assert _include("p", "daytime") == stored, "the stored rows are served"

    clock[0] += profile_sync.RETRY_BACKOFF_SECONDS
    profile_sync.refresh()
    assert len(calls) == 2
