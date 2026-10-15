"""Closing a bypass left in `convivy/library#268`: a credential bound to one
crew's dispatch profile (`identity.Identity.profile`) could still reach
ANOTHER crew's profile through the four routes that resolve a profile the
REQUEST names rather than filtering `docs` by the credential's own binding —
`GET /profiles/{project}/{variant}`, `POST /bundles`, `POST /bundles/redeem`
and `/items/{id}/checkout`'s call-slip grant (issue_01M35QG0VANA5P1XE3PH6A7PS4).
`tests/test_reference_profile_binding.py` covers the six `/reference/*` read
routes and `GET /search`'s reference half, which these four are not.

**Two products, two profiles, one document each**, so a bound identity
naming the OTHER product's profile is naming something that genuinely
exists — the case `tests/test_reference_profile_binding.py`'s `BOUND_
UNRESOLVED` does not cover, where the named profile never existed at all.

**Its own fixture, built the way `test_call_slip_bundle.py`'s is** (the
`profiles`/`roster_agents` tables and a knowledge index, which
`test_checkout.py`'s shared fixture does not carry), extended with a second
product/profile pair and the bound credentials this file needs.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

import app as libapp
import crew_registry
import identity
import lease
import protocol
import reference
import black_box_tables
import store

AGENT_UNBOUND = "agent-token-unbound"
AGENT_BOUND_VIVY = "agent-token-bound-vivy"
AGENT_BOUND_COMPANY = "agent-token-bound-company"

ORCHESTRATOR = identity.Identity(actor="co-library-test", kind="orchestrator")

ITEM_VIVY = "item_vivy"
ITEM_COMPANY = "item_company"
ITEM_VIVY_B = "item_vivy_b"
ITEM_VIVY_C = "item_vivy_c"
ITEM_VIVY_D = "item_vivy_d"

# id, kind, project, status, frontmatter_extra (model pin, or None)
DOCS = [
    ("vivy/agents/coder", "agent", "vivy", "current", None),
    ("company/agents/coder", "agent", "company", "current", None),
    # A unique persona name per project, for `_persona_model`'s scope check
    # (round-3 fix, issue_01M35QG0VANA5P1XE3PH6A7PS4): `inside-vivy` is the
    # persona a bound vivy credential's own profile selects, and
    # `outside-vivy` is one that resolves, unambiguously, to a document
    # another crew's profile selects.
    ("vivy/agents/inside-vivy", "agent", "vivy", "current", {"model": "haiku"}),
    ("company/agents/outside-vivy", "agent", "company", "current",
     {"model": "opus"}),
]


@pytest.fixture
def client(tmp_path, monkeypatch, vector_index, ledger_tables):
    db = tmp_path / "control.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE issue_items (
            id TEXT PRIMARY KEY, product TEXT, title TEXT, body TEXT, refs TEXT,
            severity TEXT, state TEXT, claimed_by TEXT, claimed_by_crew TEXT, claimed_at TEXT,
            resolution TEXT, resolved_by TEXT, resolved_at TEXT, outcome_note TEXT,
            once_key TEXT UNIQUE, created_by TEXT, created_at TEXT, run_id TEXT,
            type TEXT, created_by_kind TEXT, needs TEXT, park_reason TEXT,
            unpark_condition TEXT, parked_at TEXT, embedded_at TEXT,
            parent_id TEXT, topics TEXT, maturity TEXT, specification TEXT,
            definition_of_done TEXT, prerequisites TEXT,
            model_choice TEXT, model_reason TEXT, model_set_by_client TEXT,
            model_acting_human TEXT, model_set_at TEXT, found_during TEXT, item_key TEXT, short_description TEXT);
        CREATE TABLE issue_item_key_counters (
            scope_kind TEXT, scope_id TEXT, next_value INTEGER,
            PRIMARY KEY (scope_kind, scope_id));
        CREATE TABLE issue_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT, actor TEXT,
            from_state TEXT, to_state TEXT, at TEXT, note TEXT);
        CREATE TABLE issue_leases (
            id TEXT PRIMARY KEY, root_id TEXT, taken_for TEXT, holder TEXT, holder_crew TEXT,
            run_id TEXT, taken_at TEXT, heartbeat_at TEXT, released_at TEXT,
            release_reason TEXT, released_by TEXT, note TEXT);
        CREATE UNIQUE INDEX ix_live_root ON issue_leases (root_id)
            WHERE released_at IS NULL;
        CREATE TABLE lease_refusals (
            id TEXT PRIMARY KEY, at TEXT, requested_item_id TEXT, product TEXT,
            requester TEXT, actor TEXT, blocking_lease_id TEXT,
            blocking_root_id TEXT, blocking_holder TEXT, blocks_by TEXT, blocking_taken_for TEXT);
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
        CREATE TABLE bundle_tokens (
            id TEXT PRIMARY KEY, agent TEXT NOT NULL, project TEXT NOT NULL,
            variant TEXT NOT NULL, scope_kind TEXT NOT NULL, item_id TEXT,
            repo TEXT, product TEXT, documents TEXT NOT NULL,
            issued_by TEXT NOT NULL, issued_at TEXT NOT NULL,
            expires_at TEXT NOT NULL, redeemed_at TEXT, redeemed_by TEXT);
    """ + black_box_tables.DDL)
    for item_id, product in ((ITEM_VIVY, "vivy"), (ITEM_COMPANY, "company"),
                             (ITEM_VIVY_B, "vivy"), (ITEM_VIVY_C, "vivy"),
                             (ITEM_VIVY_D, "vivy")):
        conn.execute(
            "INSERT INTO issue_items (id, product, title, type, state, needs, "
            "created_by_kind, parent_id, created_at, refs) VALUES "
            "(?,?,?,?,?,?,?,?,?,?)",
            (item_id, product, "t", "task", "open", "agent", "agent", None,
             "2026-01-01 00:00:00", "{}"))
    conn.commit()
    conn.close()

    index = tmp_path / "index.db"
    iconn = sqlite3.connect(index)
    iconn.executescript("""
        CREATE TABLE docs (
            id TEXT PRIMARY KEY, source_ref TEXT, kind TEXT, project TEXT,
            title TEXT, status TEXT, topics TEXT, audience TEXT,
            last_reviewed TEXT, updated_at TEXT, body TEXT, body_hash TEXT,
            frontmatter_extra TEXT);
    """)
    for doc_id, kind, project, status, extra in DOCS:
        body = f"body of {doc_id}"
        iconn.execute(
            "INSERT INTO docs (id, source_ref, kind, project, title, status, "
            "topics, audience, body, body_hash, last_reviewed, updated_at, "
            "frontmatter_extra) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (doc_id, f"{doc_id}.md", kind, project, f"title of {doc_id}",
             status, "[]", json.dumps(["coder"]), body,
             hashlib.sha256(body.encode()).hexdigest(),
             "2026-08-01", "2026-08-02",
             None if extra is None else json.dumps(extra)))
    iconn.commit()
    iconn.close()

    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        AGENT_UNBOUND: {"name": "unbound-agent", "products": ["*"]},
        AGENT_BOUND_VIVY: {"name": "vivy-crew", "products": ["*"],
                          "profile": "vivy/daytime"},
        AGENT_BOUND_COMPANY: {"name": "company-crew", "products": ["*"],
                             "profile": "company/daytime"},
    }))
    vector_index(db)
    ledger_tables(db)
    monkeypatch.setattr(store, "DB_PATH", db)
    black_box_tables.use_registry(tmp_path, monkeypatch, entries={
        "_meta": {}, "vivy": {"repos": []}, "company": {"repos": []}})
    monkeypatch.setattr(reference, "INDEX_DB", index)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    monkeypatch.setattr(lease, "TUNABLES", tmp_path / "absent.json")
    crew_registry.roster_add(ORCHESTRATOR, "coder")
    crew_registry.profile_put(
        ORCHESTRATOR, "vivy", "daytime",
        [{"kind": "agent", "project": ["vivy"]}], [])
    crew_registry.profile_put(
        ORCHESTRATOR, "company", "daytime",
        [{"kind": "agent", "project": ["company"]}], [])
    return TestClient(libapp.app)


def _h(token):
    return {"Authorization": f"Bearer {token}",
            protocol.HEADER: str(protocol.CURRENT)}


def _issue(client, token, item_id):
    return client.post("/bundles", json={"agent": "coder", "item_id": item_id},
                       headers=_h(token))


def _redeem(client, token_id, token):
    return client.post("/bundles/redeem", json={"token_id": token_id},
                       headers=_h(token))


def _checkout(client, token, item_id, profile, owner="s1"):
    return client.post(f"/items/{item_id}/checkout",
                       json={"owner": owner, "profile": profile},
                       headers=_h(token))


def _checkout_persona(client, token, item_id, persona, owner="s1"):
    return client.post(f"/items/{item_id}/checkout",
                       json={"owner": owner, "persona": persona},
                       headers=_h(token))


def _delete_company_profile():
    conn = sqlite3.connect(store.DB_PATH)
    conn.execute("DELETE FROM profiles WHERE project = 'company' "
                "AND variant = 'daytime'")
    conn.commit()
    conn.close()


# --- GET /profiles/{project}/{variant} ---------------------------------------


def test_bound_identity_resolves_its_own_profile(client):
    r = client.get("/profiles/vivy/daytime", headers=_h(AGENT_BOUND_VIVY))
    assert r.status_code == 200, r.text
    assert [d["id"] for d in r.json()["documents"]] == [
        "vivy/agents/coder", "vivy/agents/inside-vivy"]


def test_bound_identity_naming_another_profile_gets_the_same_404_as_nonexistent(
        client):
    other_crews_profile = client.get("/profiles/company/daytime",
                                     headers=_h(AGENT_BOUND_VIVY))
    nonexistent = client.get("/profiles/nope/nope", headers=_h(AGENT_BOUND_VIVY))
    assert other_crews_profile.status_code == nonexistent.status_code == 404
    assert other_crews_profile.json() == nonexistent.json()


def test_unbound_identity_resolves_either_profile(client):
    assert client.get("/profiles/vivy/daytime",
                      headers=_h(AGENT_UNBOUND)).status_code == 200
    assert client.get("/profiles/company/daytime",
                      headers=_h(AGENT_UNBOUND)).status_code == 200


# --- GET /profiles -------------------------------------------------------------


def _listed(client, token):
    r = client.get("/profiles", headers=_h(token))
    assert r.status_code == 200, r.text
    body = r.json()
    return [(p["project"], p["variant"]) for p in body["profiles"]], body["count"]


def test_bound_identity_lists_only_its_own_profile(client):
    assert _listed(client, AGENT_BOUND_VIVY) == ([("vivy", "daytime")], 1)


def test_bound_identity_list_is_the_same_whether_another_profile_exists(client):
    """The listing counterpart of the byte-identical 404: deleting the other
    crew's profile changes nothing a bound caller can see."""
    before = client.get("/profiles", headers=_h(AGENT_BOUND_VIVY)).json()
    _delete_company_profile()
    after = client.get("/profiles", headers=_h(AGENT_BOUND_VIVY)).json()
    assert before == after


def test_bound_identity_whose_profile_is_gone_lists_nothing(client):
    """Fail closed: a binding that no longer resolves sees no profile at all,
    never the others."""
    _delete_company_profile()
    assert _listed(client, AGENT_BOUND_COMPANY) == ([], 0)


def test_unbound_identity_lists_every_profile(client):
    assert _listed(client, AGENT_UNBOUND) == (
        [("company", "daytime"), ("vivy", "daytime")], 2)


# --- POST /bundles ------------------------------------------------------------


def test_bound_identity_issues_against_its_own_profile(client):
    r = _issue(client, AGENT_BOUND_VIVY, ITEM_VIVY)
    assert r.status_code == 201, r.text
    assert [d["id"] for d in r.json()["documents"]] == [
        "vivy/agents/coder", "vivy/agents/inside-vivy"]


def test_bound_identity_issuing_against_another_profile_gets_the_same_422_as_nonexistent(
        client):
    """`company/daytime` exists and is simply not `AGENT_BOUND_VIVY`'s own.
    The refusal is byte-identical to the one the same request gets once that
    profile row is gone — a bound caller cannot tell "not mine" from "does
    not exist"."""
    mismatch = _issue(client, AGENT_BOUND_VIVY, ITEM_COMPANY)
    assert mismatch.status_code == 422, mismatch.text
    assert mismatch.json()["error"] == "no profile company/daytime"

    _delete_company_profile()
    genuinely_missing = _issue(client, AGENT_UNBOUND, ITEM_COMPANY)
    assert genuinely_missing.status_code == 422
    assert genuinely_missing.json() == mismatch.json()


def test_unbound_identity_issues_against_either_profile(client):
    assert _issue(client, AGENT_UNBOUND, ITEM_VIVY).status_code == 201
    assert _issue(client, AGENT_UNBOUND, ITEM_COMPANY).status_code == 201


# --- POST /bundles/redeem -----------------------------------------------------


def test_bound_identity_redeems_its_own_profiles_bundle(client):
    token_id = _issue(client, AGENT_BOUND_VIVY, ITEM_VIVY).json()["token_id"]
    r = _redeem(client, token_id, AGENT_BOUND_VIVY)
    assert r.status_code == 200, r.text


def test_bound_identity_cannot_redeem_another_profiles_bundle(client):
    token_id = _issue(client, AGENT_BOUND_COMPANY, ITEM_COMPANY).json()["token_id"]
    mismatch = _redeem(client, token_id, AGENT_BOUND_VIVY)
    absent = _redeem(client, "bundle_NEVER_ISSUED", AGENT_BOUND_VIVY)
    assert mismatch.status_code == absent.status_code == 404
    assert mismatch.json() == absent.json()


def test_unbound_identity_redeems_either_profiles_bundle(client):
    vivy_token = _issue(client, AGENT_UNBOUND, ITEM_VIVY).json()["token_id"]
    company_token = _issue(client, AGENT_UNBOUND, ITEM_COMPANY).json()["token_id"]
    assert _redeem(client, vivy_token, AGENT_UNBOUND).status_code == 200
    assert _redeem(client, company_token, AGENT_UNBOUND).status_code == 200


# --- /items/{id}/checkout's call-slip grant -----------------------------------


def test_bound_identity_checks_out_naming_its_own_profile(client):
    r = _checkout(client, AGENT_BOUND_VIVY, ITEM_VIVY, "vivy/daytime")
    assert r.status_code == 200, r.text
    assert [d["id"] for d in r.json()["docs"]] == [
        "vivy/agents/coder", "vivy/agents/inside-vivy"]


def test_bound_identity_checking_out_naming_another_profile_gets_the_same_400_as_nonexistent(
        client):
    mismatch = _checkout(client, AGENT_BOUND_VIVY, ITEM_COMPANY, "company/daytime")
    assert mismatch.status_code == 400, mismatch.text
    assert mismatch.json()["error"] == "no profile named company/daytime"

    conn = sqlite3.connect(store.DB_PATH)
    assert conn.execute("SELECT 1 FROM issue_leases").fetchone() is None, (
        "a refused checkout must take no lease")
    conn.close()

    _delete_company_profile()
    genuinely_missing = _checkout(client, AGENT_UNBOUND, ITEM_COMPANY,
                                  "company/daytime", owner="s2")
    assert genuinely_missing.status_code == 400
    assert genuinely_missing.json() == mismatch.json()


def test_unbound_identity_checks_out_naming_either_profile(client):
    assert _checkout(client, AGENT_UNBOUND, ITEM_VIVY,
                     "vivy/daytime").status_code == 200
    assert _checkout(client, AGENT_UNBOUND, ITEM_COMPANY,
                     "company/daytime").status_code == 200


# --- the grant's `model`, round 3: `_persona_model` resolves by id-suffix ----
# match over every agent document with no scope at all, so a bound caller
# could learn whether an agent document of a given name exists anywhere in
# the Reference Room, and what model it pins, by varying `persona` on its own
# checkout and reading the call slip's `model` — outside its profile and
# with no `profile` argument involved (round-2 review of #268,
# https://github.com/convivy/library/pull/268#issuecomment-6149372153).
# `persona` names a kind of agent, independent of `profile`, so this is
# exercised with no `profile` in the request body at all.


def test_bound_identity_naming_a_persona_outside_its_profile_gets_the_same_model_as_nonexistent(
        client):
    """`outside-vivy` resolves, unambiguously, to `company/agents/outside-
    vivy` — a real document, pinning a real model, that `vivy/daytime`'s
    `include` rules (`project: [vivy]`) do not select. A bound vivy
    credential must learn nothing from it: the same `model` a persona that
    resolves to nothing at all gets."""
    outside = _checkout_persona(client, AGENT_BOUND_VIVY, ITEM_VIVY,
                                "outside-vivy")
    assert outside.status_code == 200, outside.text
    nonexistent = _checkout_persona(client, AGENT_BOUND_VIVY, ITEM_VIVY_B,
                                    "no-such-persona-at-all", owner="s2")
    assert nonexistent.status_code == 200, nonexistent.text
    assert outside.json()["model"] == nonexistent.json()["model"] == (
        libapp._MODEL_NOT_RESOLVED)


def test_bound_identity_pays_the_same_scope_check_whether_or_not_the_persona_exists(
        client, monkeypatch):
    """Round 3's security review: the scope check ran only when the persona
    resolved, so latency told a bound caller that a document of that name
    existed somewhere. Every bound checkout now makes the same scope calls
    for an out-of-scope persona as for one that resolves to nothing."""
    calls = []
    real = libapp._bound_scope_clause

    def counting(ident, **kw):
        calls.append(ident.profile)
        return real(ident, **kw)

    monkeypatch.setattr(libapp, "_bound_scope_clause", counting)
    assert _checkout_persona(client, AGENT_BOUND_VIVY, ITEM_VIVY,
                             "outside-vivy").status_code == 200
    outside_calls = len(calls)
    calls.clear()
    assert _checkout_persona(client, AGENT_BOUND_VIVY, ITEM_VIVY_B,
                             "no-such-persona-at-all",
                             owner="s2").status_code == 200
    assert outside_calls > 0
    assert len(calls) == outside_calls


def test_bound_identity_naming_a_persona_inside_its_profile_gets_its_pinned_model(
        client):
    """`inside-vivy` resolves to `vivy/agents/inside-vivy`, which `vivy/
    daytime`'s own `include` rules select — the model pin is served exactly
    as a persona lookup always has been for a document in scope."""
    r = _checkout_persona(client, AGENT_BOUND_VIVY, ITEM_VIVY_C, "inside-vivy")
    assert r.status_code == 200, r.text
    assert r.json()["model"] == "haiku"


def test_unbound_identity_gets_the_pinned_model_for_either_persona(client):
    """An unbound credential carries no profile to scope against
    (`ident.profile is None`), so `_bound_scope_clause` is the no-op it was
    before this scope check existed, for both the in-scope and the
    out-of-scope document."""
    outside = _checkout_persona(client, AGENT_UNBOUND, ITEM_COMPANY,
                                "outside-vivy")
    assert outside.status_code == 200, outside.text
    assert outside.json()["model"] == "opus"
    inside = _checkout_persona(client, AGENT_UNBOUND, ITEM_VIVY_D,
                               "inside-vivy", owner="s2")
    assert inside.status_code == 200, inside.text
    assert inside.json()["model"] == "haiku"


# --- the grant's `persona_md`: the same rule as `model` ----------------------
# (issue_01M4XEEPQ37JFM3SE1493WDHVG). `persona_md` now carries the resolved
# definition's body, so it would disclose an out-of-profile document's content
# outright, and `persona_unresolved_reason` would disclose that one exists,
# unless both answer exactly as for a persona nothing carries.


def test_bound_identity_naming_a_persona_outside_its_profile_gets_no_definition(
        client):
    outside = _checkout_persona(client, AGENT_BOUND_VIVY, ITEM_VIVY,
                                "outside-vivy")
    assert outside.status_code == 200, outside.text
    nonexistent = _checkout_persona(client, AGENT_BOUND_VIVY, ITEM_VIVY_B,
                                    "no-such-persona-at-all", owner="s2")
    assert nonexistent.status_code == 200, nonexistent.text
    o, n = outside.json(), nonexistent.json()
    assert o["persona_md"] == libapp._persona_md("outside-vivy")
    assert n["persona_md"] == libapp._persona_md("no-such-persona-at-all")
    assert (o["persona_resolved"], o["persona_unresolved_reason"]) == (
        n["persona_resolved"], n["persona_unresolved_reason"]) == (
        False, "unresolved")


def test_bound_identity_naming_a_persona_inside_its_profile_gets_its_definition(
        client):
    r = _checkout_persona(client, AGENT_BOUND_VIVY, ITEM_VIVY_C, "inside-vivy")
    assert r.status_code == 200, r.text
    assert r.json()["persona_resolved"] is True
    assert r.json()["persona_md"] != libapp._persona_md("inside-vivy")
