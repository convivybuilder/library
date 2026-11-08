"""`agent_sync.refresh()` reconciles `roster_agents` from `projects/<crew>/
agents/<name>.md` in the served knowledge tree, with no manual load
(issue_01M5H8RPFPM6VBVJCPBND1XVBT; the mechanism follows `profile_sync.py`,
library#206).

**Every assertion reads the stored row or a served answer.** The unit tests
read `roster_agents` directly, after calling `agent_sync.refresh()` against a
hand-built database. The HTTP tests issue `POST /bundles` and `GET /profiles`
against a real control database built by `alembic upgrade head`, to show the
call sites this item adds actually run the reconcile. Each knowledge checkout
is a `tmp_path` tree, and `LIBRARY_KNOWLEDGE_ROOT` is never read — crew and
agent names below are made up (no Convivy crew, company or person name).
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import agent_sync
import app as libapp
import black_box_tables
import crew_profiles
import crew_registry
import identity
import profile_sync
import protocol
import reference
import store

REPO = Path(__file__).resolve().parent.parent

AGENT_TOKEN = "agent-token-wayfarer"
ORCHESTRATOR_TOKEN = "orchestrator-token-co"
ORCHESTRATOR = identity.Identity(actor="co-library-test", kind="orchestrator")


# --- fixtures ----------------------------------------------------------------


@pytest.fixture
def served(tmp_path, monkeypatch):
    """An empty knowledge checkout served by `agent_sync`, with the module's
    memory of earlier tests cleared on the way in and out."""
    root = tmp_path / "knowledge"
    (root / "projects").mkdir(parents=True)
    monkeypatch.setattr(agent_sync, "KNOWLEDGE_ROOT", str(root))
    agent_sync.reset()
    yield root
    agent_sync.reset()


@pytest.fixture
def db(tmp_path, monkeypatch):
    """`roster_agents`, `profiles`, the black-box tables, and a `signals`
    table shaped for `insert_signal` — the unit tests that never issue a
    bundle, matching `test_profile_reload.py`'s own `db` fixture plus the
    tables `_flag_missing` needs."""
    path = tmp_path / "control.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE roster_agents (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            source_doc TEXT,
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
        CREATE TABLE signals (
            id TEXT PRIMARY KEY, source TEXT, stream TEXT, kind TEXT,
            actor TEXT, ingested_by TEXT, product TEXT, refs TEXT, body TEXT,
            meta TEXT, privacy_class TEXT, produced_at TEXT, ingested_at TEXT,
            embedded_at TEXT, dedup_key TEXT UNIQUE, acting_human TEXT);
        CREATE TABLE cold_dedup_keys (
            dedup_key TEXT PRIMARY KEY, signal_id TEXT NOT NULL);
    """ + black_box_tables.DDL)
    conn.commit()
    conn.close()
    monkeypatch.setattr(store, "DB_PATH", path)
    black_box_tables.use_registry(tmp_path, monkeypatch)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    return path


def _build_index(path, docs):
    iconn = sqlite3.connect(path)
    iconn.executescript("""
        CREATE TABLE docs (
            id TEXT PRIMARY KEY, source TEXT NOT NULL DEFAULT 'kb',
            source_ref TEXT, kind TEXT, project TEXT, title TEXT, status TEXT,
            topics TEXT DEFAULT '[]', audience TEXT DEFAULT '[]',
            last_reviewed TEXT, updated_at TEXT, body TEXT, body_hash TEXT,
            frontmatter_extra TEXT NOT NULL DEFAULT '{}');
    """)
    for doc_id, kind, project, status, audience in docs:
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
def client(migrated_db, served, tmp_path, monkeypatch):
    """A real control database at head, with `p/daytime` resolving `p`'s
    agent kind, so a bundle issue (or a profile read) is the thing that
    proves the reconcile ran — not a direct call to `agent_sync.refresh()`."""
    db = tmp_path / "library.db"
    shutil.copy(migrated_db, db)
    index = tmp_path / "index.db"
    _build_index(index, [("p/agents/wayfarer", "agent", "p", "current",
                          ["wayfarer"])])
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
    black_box_tables.use_registry(tmp_path, monkeypatch)
    monkeypatch.setattr(profile_sync, "KNOWLEDGE_ROOT", str(served))
    profile_sync.reset()
    crew_registry.profile_put(
        ORCHESTRATOR, "p", "daytime",
        [{"kind": "agent", "project": ["p"], "status": "current",
          "audience_contains": ["wayfarer"]}], [])
    return TestClient(libapp.app)


# --- helpers -----------------------------------------------------------------


def _write_agent(root: Path, crew: str, name: str, *, kind: str = "agent",
                 body: str = "its body.\n") -> Path:
    """Write `projects/<crew>/agents/<name>.md`, the way a checkout does: a
    new file renamed over the old one, so the inode moves as well as the
    contents (matching `test_profile_reload._write_profile`)."""
    d = root / "projects" / crew / "agents"
    d.mkdir(parents=True, exist_ok=True)
    target = d / f"{name}.md"
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(f"---\nkind: {kind}\n---\n{body}")
    os.replace(tmp, target)
    return target


def _write_non_agent_path(root: Path, crew: str, name: str) -> Path:
    """A `kind: agent` document outside `projects/<crew>/agents/`, which the
    glob this module reconciles against never reaches."""
    d = root / "projects" / crew / "skills"
    d.mkdir(parents=True, exist_ok=True)
    target = d / f"{name}.md"
    target.write_text("---\nkind: agent\n---\nits body.\n")
    return target


def _roster(db) -> dict[str, dict]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return {r["name"]: dict(r) for r in
                conn.execute("SELECT * FROM roster_agents ORDER BY name")}
    finally:
        conn.close()


def _signals(db) -> list[dict]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM signals")]
    finally:
        conn.close()


def _h(token=AGENT_TOKEN):
    return {"Authorization": f"Bearer {token}",
            protocol.HEADER: str(protocol.CURRENT)}


def _issue(client, agent="wayfarer", variant="daytime"):
    return client.post("/bundles", json={"agent": agent, "repo": "p",
                                         "variant": variant}, headers=_h())


# --- the reconcile itself ----------------------------------------------------


def test_a_new_agent_definition_is_added(db, served):
    _write_agent(served, "atoll", "wayfarer")
    agent_sync.refresh()
    row = _roster(db)["wayfarer"]
    assert row["source_doc"] == "atoll/agents/wayfarer"
    assert row["created_by"] == agent_sync.AUTOMATIC_ACTOR
    assert row["updated_by"] == agent_sync.AUTOMATIC_ACTOR


def test_a_rerun_with_no_change_adds_nothing(db, served, monkeypatch):
    _write_agent(served, "atoll", "wayfarer")
    agent_sync.refresh()

    def no_reconcile(*args, **kwargs):
        raise AssertionError("reconciled a checkout that did not change")
    monkeypatch.setattr(agent_sync, "apply", no_reconcile)
    agent_sync.refresh()
    agent_sync.refresh()


def test_a_rerun_after_the_name_already_exists_touches_nothing(db, served):
    """The reconcile is `add when absent`, never an upsert — a name already
    on the roster keeps its row exactly as it stood, even when its file
    changes (unlike `profile_sync`, which revises a profile's content)."""
    _write_agent(served, "atoll", "wayfarer")
    agent_sync.refresh()
    before = _roster(db)["wayfarer"]

    _write_agent(served, "atoll", "wayfarer", body="an edited body.\n")
    agent_sync.reset()
    agent_sync.refresh()
    after = _roster(db)["wayfarer"]
    assert after == before


def test_an_edit_to_an_existing_definition_makes_no_roster_write(
        db, served, monkeypatch):
    """The reviewed finding: a pre-filter skips a name already on the
    roster before any write, so editing one existing file's body never
    calls `roster_add_from_document` for it at all."""
    _write_agent(served, "atoll", "wayfarer")
    agent_sync.refresh()

    def _no_insert(*args, **kwargs):
        raise AssertionError(
            "roster_add_from_document called for a name already on the "
            "roster")
    monkeypatch.setattr(crew_registry, "roster_add_from_document", _no_insert)

    _write_agent(served, "atoll", "wayfarer", body="an edited body.\n")
    agent_sync.reset()
    agent_sync.refresh()


def test_a_second_crews_document_does_not_touch_an_existing_name(db, served):
    """A name already on the roster from one crew's document keeps its row,
    `source_doc` included, when a second crew's document later claims the
    same name."""
    _write_agent(served, "atoll", "wayfarer")
    agent_sync.refresh()
    before = _roster(db)["wayfarer"]

    _write_agent(served, "harbor", "wayfarer")
    agent_sync.reset()
    agent_sync.refresh()
    after = _roster(db)["wayfarer"]
    assert after == before


def test_a_wrong_kind_is_not_added(db, served, caplog):
    _write_agent(served, "atoll", "notary", kind="skill")
    agent_sync.refresh()
    assert _roster(db) == {}


def test_a_path_outside_agents_is_not_added(db, served):
    _write_non_agent_path(served, "atoll", "impostor")
    with pytest.raises(agent_sync.AgentRootError):
        agent_sync.load(str(served))
    # The directory exists but holds no agent definition — refresh logs the
    # refusal and adds nothing, the same shape `profile_sync` gives an
    # equivalent empty-looking checkout.
    agent_sync.refresh()
    assert _roster(db) == {}


def test_a_bad_slug_is_refused_and_reported(db, served, caplog):
    _write_agent(served, "atoll", "_ghost")
    caplog.set_level("WARNING", logger="agent_sync")
    agent_sync.refresh()
    assert _roster(db) == {}
    refusals = [r for r in caplog.records if "not added to the roster" in
               r.getMessage()]
    assert len(refusals) == 1
    assert "_ghost" in refusals[0].getMessage()


def test_a_black_box_registered_crew_name_is_refused(db, served, caplog):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO black_box_registrations (crew, model, gateway, "
        "created_at, created_by, updated_at, updated_by) "
        "VALUES ('nightwatch', 'm', 'g', '2026-01-01', 'x', '2026-01-01', "
        "'x')")
    conn.commit()
    conn.close()
    _write_agent(served, "nightwatch", "shadow")
    caplog.set_level("WARNING", logger="agent_sync")
    agent_sync.refresh()
    assert _roster(db) == {}
    assert any("black-box" in r.getMessage() for r in caplog.records)


def test_a_private_crew_name_is_refused(db, served, tmp_path, monkeypatch,
                                        caplog):
    black_box_tables.use_registry(tmp_path, monkeypatch, entries={
        "_meta": {}, "harbor": {"private": True, "repos": []}})
    _write_agent(served, "harbor", "keeper")
    caplog.set_level("WARNING", logger="agent_sync")
    agent_sync.refresh()
    assert _roster(db) == {}
    assert any("black-box" in r.getMessage() for r in caplog.records)


def test_a_withheld_crews_claim_on_an_existing_name_is_still_reported(
        db, served, tmp_path, monkeypatch, caplog):
    """The pre-filter skips names already on the roster, but a private
    crew's document naming one must still be refused and reported, not
    skipped in silence (issue_01M5H8RPFPM6VBVJCPBND1XVBT: "a refused name
    is reported, never added")."""
    _write_agent(served, "atoll", "wayfarer")
    agent_sync.refresh()
    before = _roster(db)["wayfarer"]
    black_box_tables.use_registry(tmp_path, monkeypatch, entries={
        "_meta": {}, "harbor": {"private": True, "repos": []}})
    _write_agent(served, "harbor", "wayfarer")
    caplog.set_level("WARNING", logger="agent_sync")
    agent_sync.refresh()
    assert _roster(db)["wayfarer"] == before
    assert any("harbor" in r.getMessage() and "black-box" in r.getMessage()
               for r in caplog.records)


def test_a_deleted_definition_is_flagged_not_removed(db, served):
    """A second, unrelated definition stays present throughout — an empty
    tree is refused outright (see `test_a_path_outside_agents_is_not_added`),
    the same way `profile_sync`'s own removal tests keep one file standing."""
    _write_agent(served, "atoll", "wayfarer")
    _write_agent(served, "atoll", "lighthouse")
    agent_sync.refresh()
    assert "wayfarer" in _roster(db)

    (served / "projects" / "atoll" / "agents" / "wayfarer.md").unlink()
    agent_sync.reset()
    agent_sync.refresh()

    row = _roster(db)["wayfarer"]
    assert row["source_doc"] == "atoll/agents/wayfarer"
    assert agent_sync.source_missing(str(served), row["source_doc"])
    signals = _signals(db)
    assert len(signals) == 1
    assert signals[0]["kind"] == "roster_agent_source_missing"
    assert signals[0]["dedup_key"] == "roster-agent-source-missing:wayfarer"


def test_a_deleted_definition_is_flagged_once_not_once_per_scan(db, served):
    _write_agent(served, "atoll", "wayfarer")
    _write_agent(served, "atoll", "lighthouse")
    agent_sync.refresh()
    (served / "projects" / "atoll" / "agents" / "wayfarer.md").unlink()
    agent_sync.reset()
    agent_sync.refresh()
    agent_sync.reset()
    agent_sync.refresh()
    assert len(_signals(db)) == 1


def test_crew_profiles_roster_exposes_source_doc_missing(db, served):
    _write_agent(served, "atoll", "wayfarer")
    agent_sync.refresh()

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        rows = {r["name"]: r for r in crew_profiles.roster(conn)}
    finally:
        conn.close()
    assert rows["wayfarer"]["source_doc_missing"] is False

    (served / "projects" / "atoll" / "agents" / "wayfarer.md").unlink()
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        rows = {r["name"]: r for r in crew_profiles.roster(conn)}
    finally:
        conn.close()
    assert rows["wayfarer"]["source_doc_missing"] is True


def test_a_name_seeded_by_hand_never_reads_missing(db, served):
    """A row with no `source_doc` — seeded, or added through `PUT /roster/
    {name}` — always reads `False`, since it was never this reconcile's to
    track."""
    crew_registry.roster_add(ORCHESTRATOR, "handwritten")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        rows = {r["name"]: r for r in crew_profiles.roster(conn)}
    finally:
        conn.close()
    assert rows["handwritten"]["source_doc"] is None
    assert rows["handwritten"]["source_doc_missing"] is False


# --- the call sites -----------------------------------------------------


def test_bundle_issue_refuses_a_just_landed_agent_until_another_route_reconciles(
        client, served):
    """`POST /bundles`' own `on_roster` check runs BEFORE this call site's
    `agent_sync.refresh()` (`_do_issue_bundle`'s own comment: moving the
    lookup below the reload would let an unscoped request's `agent` trigger
    a write). So a definition that only just landed still gets this
    request's refusal; `GET /profiles` reconciles unconditionally, and the
    next bundle issue sees the name — the exact failure this item exists to
    fix, with no manual roster load either way."""
    _write_agent(served, "p", "wayfarer")
    first = _issue(client)
    assert first.status_code == 422, first.text
    assert "not on the roster" in first.text

    listed = client.get("/profiles", headers=_h(ORCHESTRATOR_TOKEN))
    assert listed.status_code == 200, listed.text
    second = _issue(client)
    assert second.status_code == 201, second.text


def test_bundle_issue_still_refuses_an_agent_with_no_definition_at_all(
        client, served):
    refused = _issue(client, agent="nobody")
    assert refused.status_code == 422, refused.text
    assert "not on the roster" in refused.text


def test_get_profiles_reconciles_the_roster_too(client, served):
    """`GET /profiles` calls `profile_sync.refresh()` for the `profiles`
    table; this shows it now also runs `agent_sync.refresh()` for
    `roster_agents`, by issuing a bundle afterwards with no second read in
    between."""
    _write_agent(served, "p", "wayfarer")
    listed = client.get("/profiles", headers=_h(ORCHESTRATOR_TOKEN))
    assert listed.status_code == 200, listed.text
    issued = _issue(client)
    assert issued.status_code == 201, issued.text


def test_get_profile_resolve_reconciles_the_roster_too(client, served):
    _write_agent(served, "p", "wayfarer")
    resolved = client.get("/profiles/p/daytime", headers=_h(ORCHESTRATOR_TOKEN))
    assert resolved.status_code == 200, resolved.text
    issued = _issue(client)
    assert issued.status_code == 201, issued.text
