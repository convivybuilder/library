"""`docs` on a call slip's grant, resolved from the profile the checkout names
(issue_01M21B12X6BYZ76W04JK2QWPQF).

**Its own fixture rather than `test_checkout.py`'s**, because these tests need
two things that file's `client` deliberately does not have: the
`profiles`/`roster_agents` tables, and a knowledge index to resolve against.
Adding a knowledge index to the shared checkout fixture would change what every
other test in that file is running against, which is a large blast radius for a
fixture need that belongs to one feature.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

import app as libapp
import crew_profiles
import crew_registry
import identity
import lease
import profile_sync
import protocol
import reference
import black_box_tables
import store

AGENT = "agent-token-vivy"
ORCHESTRATOR = identity.Identity(actor="co-library-test", kind="orchestrator")

# Two documents the profile below selects, and one it does not — so a resolved
# bundle that quietly returned everything would be visible.
DOCS = [
    ("vivy/agents/coder", "agent", "vivy", "current"),
    ("vivy/agents/reviewer", "agent", "vivy", "current"),
    ("vivy/specs/unselected", "spec", "vivy", "current"),
]
PROFILE_INCLUDE = [{"kind": "agent", "project": ["vivy"]}]


def _ts(**delta):
    return (_dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
            - _dt.timedelta(**delta)).isoformat(" ")


@pytest.fixture
def client(tmp_path, monkeypatch, vector_index, ledger_tables):
    db = tmp_path / "control.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE issue_items (
            id TEXT PRIMARY KEY, product TEXT, title TEXT, body TEXT, refs TEXT,
            severity TEXT, state TEXT, claimed_by TEXT, claimed_at TEXT,
            resolution TEXT, resolved_by TEXT, resolved_at TEXT, outcome_note TEXT,
            once_key TEXT UNIQUE, created_by TEXT, created_at TEXT, run_id TEXT,
            type TEXT, created_by_kind TEXT, needs TEXT, park_reason TEXT,
            unpark_condition TEXT, parked_at TEXT, embedded_at TEXT,
            parent_id TEXT, topics TEXT, maturity TEXT, specification TEXT,
            definition_of_done TEXT, prerequisites TEXT,
            model_choice TEXT, model_reason TEXT, model_set_by_client TEXT,
            model_acting_human TEXT, model_set_at TEXT, found_during TEXT, item_key TEXT);
        CREATE TABLE issue_item_key_counters (
            scope_kind TEXT, scope_id TEXT, next_value INTEGER,
            PRIMARY KEY (scope_kind, scope_id));
        CREATE TABLE issue_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT, actor TEXT,
            from_state TEXT, to_state TEXT, at TEXT, note TEXT);
        CREATE TABLE issue_leases (
            id TEXT PRIMARY KEY, root_id TEXT, taken_for TEXT, holder TEXT,
            run_id TEXT, taken_at TEXT, heartbeat_at TEXT, released_at TEXT,
            release_reason TEXT, released_by TEXT, note TEXT);
        CREATE UNIQUE INDEX ix_live_root ON issue_leases (root_id)
            WHERE released_at IS NULL;
        CREATE TABLE lease_refusals (
            id TEXT PRIMARY KEY, at TEXT, requested_item_id TEXT, product TEXT,
            requester TEXT, actor TEXT, blocking_lease_id TEXT,
            blocking_root_id TEXT, blocking_holder TEXT, blocks_by TEXT);
        CREATE TABLE call_slips (
            id TEXT PRIMARY KEY, item_id TEXT, product TEXT, claim_owner_label TEXT,
            issued_at TEXT, expires_at TEXT, grant_json TEXT);
        CREATE TABLE roster_agents (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL, created_by TEXT NOT NULL,
            updated_at TEXT NOT NULL, updated_by TEXT NOT NULL);
        CREATE TABLE profiles (
            id INTEGER PRIMARY KEY, project TEXT NOT NULL, variant TEXT NOT NULL,
            include TEXT NOT NULL, claude_md_extras TEXT NOT NULL,
            created_at TEXT NOT NULL, created_by TEXT NOT NULL,
            updated_at TEXT NOT NULL, updated_by TEXT NOT NULL,
            UNIQUE (project, variant));
    """ + black_box_tables.DDL)
    conn.execute(
        "INSERT INTO issue_items (id, product, title, type, state, needs, "
        "created_by_kind, parent_id, created_at, refs) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("task_a", "vivy", "Task A", "task", "open", "agent", "agent", None,
         _ts(days=2), "{}"))
    conn.commit()
    conn.close()

    index = tmp_path / "index.db"
    iconn = sqlite3.connect(index)
    iconn.executescript("""
        CREATE TABLE docs (
            id TEXT PRIMARY KEY, source_ref TEXT, kind TEXT, project TEXT,
            title TEXT, status TEXT, topics TEXT, audience TEXT,
            last_reviewed TEXT, updated_at TEXT, body TEXT, body_hash TEXT);
    """)
    for doc_id, kind, project, status in DOCS:
        body = f"body of {doc_id}"
        iconn.execute(
            "INSERT INTO docs (id, source_ref, kind, project, title, status, "
            "topics, audience, body, body_hash, last_reviewed, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (doc_id, f"{doc_id}.md", kind, project, f"title of {doc_id}",
             status, "[]", '["team"]', body,
             hashlib.sha256(body.encode()).hexdigest(),
             "2026-08-01", "2026-08-02"))
    iconn.commit()
    iconn.close()

    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        AGENT: {"name": "vivy-agent", "products": ["vivy"]},
    }))
    vector_index(db)
    ledger_tables(db)
    monkeypatch.setattr(store, "DB_PATH", db)
    black_box_tables.use_registry(tmp_path, monkeypatch)
    monkeypatch.setattr(reference, "INDEX_DB", index)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    monkeypatch.setattr(lease, "TUNABLES", tmp_path / "absent.json")
    crew_registry.profile_put(ORCHESTRATOR, "vivy", "daytime",
                              PROFILE_INCLUDE, [])
    return TestClient(libapp.app)


def _h(token=AGENT):
    return {"Authorization": f"Bearer {token}",
            protocol.HEADER: str(protocol.CURRENT)}


def _co(client, item="task_a", owner="session-1", **extra):
    return client.post(f"/items/{item}/checkout",
                       json={"owner": owner, **extra}, headers=_h())


def _live_leases():
    conn = sqlite3.connect(store.DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM issue_leases WHERE released_at IS NULL").fetchall()
    conn.close()
    return rows


# --- the default, unchanged ---------------------------------------------------


def test_a_checkout_naming_no_profile_behaves_as_it_did_before(client):
    """The additive half of contract §1: a caller that never heard of
    `profile` gets exactly what it got before the field existed. `available`
    is TRUE here — nothing was asked for and nothing failed, which is a
    different fact from a bundle that could not be read."""
    r = _co(client)
    assert r.status_code == 200
    body = r.json()
    assert body["docs"] == []
    assert body["bundle"] == {"profile": None, "available": True}


@pytest.fixture
def served(tmp_path, monkeypatch):
    """A knowledge checkout that `profile_sync` reloads from, with the module's
    memory of earlier tests cleared on the way in and out."""
    root = tmp_path / "knowledge"
    (root / "projects").mkdir(parents=True)
    monkeypatch.setattr(profile_sync, "KNOWLEDGE_ROOT", str(root))
    profile_sync.reset()
    yield root
    profile_sync.reset()


def _write_vivy_daytime(root, include_kind):
    d = root / "projects" / "vivy" / "profiles"
    d.mkdir(parents=True, exist_ok=True)
    (d / "daytime.yaml").write_text(
        "profile: vivy/daytime\n"
        "include:\n"
        f"  - kind: {include_kind}\n"
        "    project: [vivy]\n")


# --- a named profile ----------------------------------------------------------


def test_a_named_profile_fills_docs_with_the_resolved_set(client):
    r = _co(client, profile="vivy/daytime")
    assert r.status_code == 200
    body = r.json()
    assert [d["id"] for d in body["docs"]] == [
        "vivy/agents/coder", "vivy/agents/reviewer"]
    assert body["bundle"] == {"profile": "vivy/daytime", "available": True}


def test_the_bundle_excludes_what_the_profile_does_not_select(client):
    """A resolver that quietly returned the whole index would satisfy "docs is
    non-empty", so the fixture carries a document the profile's rules exclude
    and this asserts its absence."""
    docs = _co(client, profile="vivy/daytime").json()["docs"]
    assert "vivy/specs/unselected" not in [d["id"] for d in docs]


def test_a_docs_entry_carries_an_id_and_a_hash_and_nothing_else(client):
    """**The context-economy property, pinned.** The epic's whole argument for
    naming documents by id is that a caller's context stays flat however large
    the bundle grows; a later addition of `title` or `body` here would undo
    that quietly, one convenient field at a time."""
    docs = _co(client, profile="vivy/daytime").json()["docs"]
    assert docs and all(set(d) == {"id", "body_sha256"} for d in docs)


def test_the_hash_is_the_indexed_body_hash(client):
    """Same digest the Reference Room serves, so a Individuator can compare a
    grant against a later fetch without knowing they came from two routes."""
    docs = {d["id"]: d["body_sha256"]
            for d in _co(client, profile="vivy/daytime").json()["docs"]}
    expected = hashlib.sha256(b"body of vivy/agents/coder").hexdigest()
    assert docs["vivy/agents/coder"] == expected


def test_the_bundle_matches_what_the_profile_route_resolves(client):
    """**Two surfaces, one evaluation.** The grant and `GET /profiles/{project}/
    {variant}` both go through `crew_profiles.resolve`, and this is what would
    fail if one of them ever grew its own copy of the rule."""
    from_grant = [d["id"] for d in _co(client, profile="vivy/daytime").json()["docs"]]
    from_route = [d["id"] for d in client.get(
        "/profiles/vivy/daytime", headers=_h()).json()["documents"]]
    assert from_grant == from_route


# --- refusals, and what they cost ---------------------------------------------


def test_an_unknown_profile_refuses_the_checkout_and_takes_no_lease(client):
    """**The refusal has to come before the lock, and this asserts the lock and
    not just the status code.** Taking the lease and handing back an empty
    bundle would let a dispatch individuate nothing, report success, and look
    exactly like a crew whose profile legitimately selects little — the silent
    shape this epic exists to remove. A 400 that still stranded a lease would
    be worse than the silence."""
    assert not _live_leases()
    r = _co(client, profile="vivy/nosuchvariant")
    assert r.status_code == 400
    assert "no profile named vivy/nosuchvariant" in r.json()["error"]
    assert not _live_leases()


def test_an_unknown_profile_leaves_the_item_claimable(client):
    """The other half of costing nothing: the caller can fix the profile name
    and retry, rather than finding the item claimed by its own failed attempt."""
    _co(client, profile="vivy/nosuchvariant")
    assert _co(client, profile="vivy/daytime").status_code == 200


@pytest.mark.parametrize("bad", ["nogap", "too/many/parts", "/novariant",
                                  "noproject/", 42, ["vivy", "daytime"]])
def test_a_malformed_profile_is_refused(client, bad):
    r = _co(client, profile=bad)
    assert r.status_code == 400
    assert not _live_leases()


def test_claim_ignores_profile_because_tightening_it_would_be_version_two(client):
    """`/claim` never read `profile`, and contract §1 permits only ADDING
    within version 1 — refusing a payload `/claim` used to accept is the
    version-2 move of tightening what a request must contain. Same reasoning
    `persona` carries, and the malformed value here is what proves it is not
    merely unused but unvalidated."""
    r = client.post("/items/task_a/claim",
                    json={"owner": "session-1", "profile": "garbage"},
                    headers=_h())
    assert r.status_code == 200


# --- the index going away -----------------------------------------------------


def test_an_unreadable_index_costs_the_bundle_and_not_the_lock(client, monkeypatch,
                                                                tmp_path):
    """**By the time the bundle resolves, the claim and the lease have
    committed**, so letting the index failure propagate would 503 a caller that
    already holds the lease and does not know it. This asserts both halves: the
    caller still gets its lock, and `available: false` tells it the empty
    `docs` is a failure to look rather than an answer."""
    monkeypatch.setattr(reference, "INDEX_DB", tmp_path / "gone.db")
    r = _co(client, profile="vivy/daytime")
    assert r.status_code == 200
    body = r.json()
    assert body["lease"]["id"]
    assert body["docs"] == []
    assert body["bundle"] == {"profile": "vivy/daytime", "available": False}
    assert len(_live_leases()) == 1


def test_a_query_failure_also_costs_the_bundle_and_not_the_lock(client, monkeypatch):
    """**The second window, which the first version of this PR left unguarded**
    (reviewer, PR #101). `reference.connect()` raises
    `ReferenceIndexUnavailable` for every way it can fail to hand back a
    connection, but a `SELECT` on the connection it returned raises
    `sqlite3.Error` — and nothing between `_slip_docs` and the client catches
    that, so the caller got a 500 over a lease it already held.

    The failure injected here is the one the knowledge watcher actually
    produces: it rebuilds the index in place, and a read landing mid-rebuild
    raises `database disk image is malformed`. The test above covers the
    file being gone; this covers it being unreadable while open.
    """
    def _corrupt(*a, **k):
        raise sqlite3.DatabaseError("database disk image is malformed")
    monkeypatch.setattr(crew_profiles, "resolve", _corrupt)
    r = _co(client, profile="vivy/daytime")
    assert r.status_code == 200
    body = r.json()
    assert body["lease"]["id"]
    assert body["docs"] == []
    assert body["bundle"] == {"profile": "vivy/daytime", "available": False}
    assert len(_live_leases()) == 1


@pytest.mark.parametrize("mode", ["open", "query"])
def test_an_unavailable_bundle_leaves_a_line_for_the_operator(client, monkeypatch,
                                                               tmp_path, capsys, mode):
    """**Catching here is what stops the registered handler from logging**
    (security-reviewer, PR #101). `misconfigured_database` and
    `reference_index_unavailable` both print the exception before returning a
    sanitized refusal; handling the failure inside `_slip_docs` means neither
    ever runs, so without this an index left dead would report
    `available: false` on every checkout forever with nothing to grep for.

    Both branches are covered, because they fail in different windows and only
    one of them is the exception the handler would have caught anyway.
    """
    if mode == "open":
        monkeypatch.setattr(reference, "INDEX_DB", tmp_path / "gone.db")
    else:
        def _corrupt(*a, **k):
            raise sqlite3.DatabaseError("database disk image is malformed")
        monkeypatch.setattr(crew_profiles, "resolve", _corrupt)
    assert _co(client, profile="vivy/daytime").status_code == 200
    err = capsys.readouterr().err
    assert "profile bundle unavailable for vivy/daytime" in err


def test_available_false_is_distinguishable_from_an_empty_profile(client):
    """The distinction `available` exists for: a profile whose rules match
    nothing returns an empty `docs` with `available: true`, which is an answer,
    where the test above returns the same empty list as a failure to look."""
    crew_registry.profile_put(ORCHESTRATOR, "vivy", "empty",
                              [{"kind": "runbook", "project": ["vivy"]}], [])
    body = _co(client, profile="vivy/empty").json()
    assert body["docs"] == []
    assert body["bundle"] == {"profile": "vivy/empty", "available": True}


def test_the_resolver_is_not_called_when_no_profile_is_named(client, monkeypatch):
    """A checkout that names no profile must not open the knowledge index at
    all — the common path stays free of a read it has no use for, and a
    missing index cannot affect it."""
    def _boom(*a, **k):
        raise AssertionError("resolve() called with no profile named")
    monkeypatch.setattr(crew_profiles, "resolve", _boom)
    assert _co(client).status_code == 200


# --- the checkout reads the profile as the checkout holds it now ---------------


def test_a_checkout_naming_a_profile_reads_the_file_as_it_is_now(client, served):
    """The stored row selects `vivy` agents. The checkout now holds a file
    selecting `vivy` specs, so a checkout that reads the stored row returns
    the agents, and one that reconciles first returns the spec."""
    _write_vivy_daytime(served, "spec")
    r = _co(client, profile="vivy/daytime")
    assert r.status_code == 200, r.text
    assert [d["id"] for d in r.json()["docs"]] == ["vivy/specs/unselected"]


def test_an_out_of_scope_checkout_reconciles_nothing(client, served,
                                                     tmp_path, monkeypatch):
    """The reload waits until the item is in scope and the identity may claim,
    so a refused caller cannot make the Library write the profiles table."""
    other = tmp_path / "other-tokens.json"
    other.write_text(json.dumps({
        AGENT: {"name": "vivy-agent", "products": ["vivy"]},
        "agent-token-other": {"name": "other-agent", "products": ["other"]},
    }))
    monkeypatch.setattr(identity, "TOKENS", other)
    _write_vivy_daytime(served, "spec")
    reconciles = []
    monkeypatch.setattr(profile_sync, "apply",
                        lambda *args: reconciles.append(args))

    r = client.post("/items/task_a/checkout", json={"owner": "session-1",
                                                    "profile": "vivy/daytime"},
                    headers={"Authorization": "Bearer agent-token-other",
                             protocol.HEADER: str(protocol.CURRENT)})
    assert r.status_code == 404, r.text
    assert reconciles == []


def test_a_checkout_refused_for_lacking_item_claim_reconciles_nothing(
        client, served, tmp_path, monkeypatch):
    """The 403 is the capability refusal the reload waits behind, and the 404
    test above covers the scope refusal. The permitted caller's request after
    it reconciles once, so the spy is shown to catch a reload that happens."""
    other = tmp_path / "other-tokens.json"
    other.write_text(json.dumps({
        AGENT: {"name": "vivy-agent", "products": ["vivy"]},
        "agent-token-unclaimer": {"name": "unclaimer", "products": ["vivy"],
                                  "withholds": ["item.claim"]},
    }))
    monkeypatch.setattr(identity, "TOKENS", other)
    _write_vivy_daytime(served, "spec")
    reconciles = []
    monkeypatch.setattr(profile_sync, "apply",
                        lambda *args: reconciles.append(args))

    r = client.post("/items/task_a/checkout", json={"owner": "session-1",
                                                    "profile": "vivy/daytime"},
                    headers={"Authorization": "Bearer agent-token-unclaimer",
                             protocol.HEADER: str(protocol.CURRENT)})
    assert r.status_code == 403, r.text
    assert "item.claim" in r.text
    assert reconciles == []

    assert _co(client, profile="vivy/daytime").status_code == 200
    assert len(reconciles) == 1


def test_a_bundle_refused_for_scope_reconciles_nothing(
        client, served, tmp_path, monkeypatch):
    """A caller outside the item's product is refused with 404, and the
    changed profile file stays unreloaded. The same request from a caller in
    scope reconciles once."""
    other = tmp_path / "other-tokens.json"
    other.write_text(json.dumps({
        AGENT: {"name": "vivy-agent", "products": ["vivy"]},
        "agent-token-other": {"name": "other-agent", "products": ["other"]},
    }))
    monkeypatch.setattr(identity, "TOKENS", other)
    crew_registry.roster_add(ORCHESTRATOR, "coder")
    _write_vivy_daytime(served, "spec")
    reconciles = []
    monkeypatch.setattr(profile_sync, "apply",
                        lambda *args: reconciles.append(args))
    body = {"agent": "coder", "item_id": "task_a", "variant": "daytime"}

    r = client.post("/bundles", json=body,
                    headers={"Authorization": "Bearer agent-token-other",
                             protocol.HEADER: str(protocol.CURRENT)})
    assert r.status_code == 404, r.text
    assert reconciles == []

    client.post("/bundles", json=body, headers=_h())
    assert len(reconciles) == 1
