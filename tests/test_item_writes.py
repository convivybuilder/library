"""Filing a Chart House item: the registry, the nesting rules, the once_key gate.

Three things carry real history and are what this file is mostly about.

**`product` is resolved, not accepted.** Taking any short string is how
`Substrate` came to sit beside `substrate` in the live store — one item,
invisible to every view asking for `substrate`.

**The order of the parent checks is a control.** An out-of-scope parent must be
indistinguishable from a missing one, or a caller holding one product's token can
probe ids across every product and learn which exist, what type they are, and
which product they belong to.

**`once_key` is an existence oracle** in exactly the way `dedup_key` is, so the
collision reply is gated the same way.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

import app as libapp
import identity
import projects
import protocol
import store

AGENT = "agent-token-vivy"
WIDE = "agent-token-wide"


def _ts(**delta):
    return (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
            - dt.timedelta(**delta)).isoformat(" ")

REGISTRY = {
    "_scopes": {"platform": {}, "commercial": {}},
    "vivy": {"aliases": ["viv"], "repos": ["vivy-app"]},
    "constance": {"aliases": [], "repos": []},
    "keel": {"aliases": ["steward"], "repos": ["substrate", "agent-platform"]},
}


@pytest.fixture
def client(tmp_path, monkeypatch, vector_index):
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
            from_state TEXT, to_state TEXT, note TEXT, at TEXT);
        CREATE TABLE issue_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT, actor TEXT,
            kind TEXT, body TEXT, run_id TEXT, at TEXT);
        CREATE TABLE issue_leases (
            id TEXT PRIMARY KEY, root_id TEXT, taken_for TEXT, holder TEXT,
            run_id TEXT, taken_at TEXT, heartbeat_at TEXT, released_at TEXT,
            release_reason TEXT, released_by TEXT, note TEXT);
        CREATE UNIQUE INDEX ix_live_root ON issue_leases (root_id)
            WHERE released_at IS NULL;
        CREATE TABLE corrections_queues (
            id TEXT PRIMARY KEY, product TEXT UNIQUE, created_at TEXT);
    """)
    conn.executemany(
        "INSERT INTO corrections_queues (id, product, created_at) "
        "VALUES (?,?,?)",
        [("cq_vivy", "vivy", "2026-08-01 00:00:00.000000"),
         ("cq_constance", "constance", "2026-08-01 00:00:00.000000")])
    conn.executemany(
        "INSERT INTO issue_items (id, product, title, type, state, created_at, "
        "once_key) VALUES (?,?,?,?,?,?,?)",
        [("issue_epic_v", "vivy", "A vivy epic", "epic", "open",
          "2026-08-01 12:00:00.000000", "k-epic-v"),
         ("issue_task_v", "vivy", "A vivy task", "task", "open",
          "2026-08-01 12:01:00.000000", "k-task-v"),
         # THE PROBE TARGETS. A caller scoped to vivy must not learn anything
         # about either row through `parent_id`.
         ("issue_epic_c", "constance", "A constance epic", "epic", "open",
          "2026-08-01 12:02:00.000000", "k-epic-c"),
         # **THIS ONE IS THE PROBE THAT BITES, AND IT HAS TO BE A NON-EPIC.** A
         # first version of the ordering test used the epic above, which passes
         # the type check either way and so answered "parent not found" even with
         # the scope check moved after the type check — a test that watched the
         # right door and could not see it open. Only a target that would FAIL
         # the type check distinguishes the two orderings.
         ("issue_task_c", "constance", "A constance task", "task", "open",
          "2026-08-01 12:03:00.000000", "k-task-c")])
    # **A CLAIMED ROW WITH A LIVE LEASE, so the idempotent hit has a real claim to
    # get wrong.** Every other row here is `open`, and `claim_status` answers an
    # open row `(False, None)` without reaching the lease table — which is the
    # same answer the removed default gave, so a fixture of open rows cannot tell
    # a real lookup from no lookup at all.
    conn.execute(
        "INSERT INTO issue_items (id, product, title, type, state, claimed_by, "
        "claimed_at, created_at, once_key) VALUES (?,?,?,?,?,?,?,?,?)",
        ("issue_held_v", "vivy", "A vivy task somebody holds", "task",
         "claimed", "session-1", _ts(hours=1), _ts(hours=2), "k-held-v"))
    conn.execute(
        "INSERT INTO issue_leases (id, root_id, taken_for, holder, run_id, "
        "taken_at, heartbeat_at) VALUES (?,?,?,?,?,?,?)",
        ("lease_held", "issue_held_v", "issue_held_v", "session-1", "run-1",
         _ts(hours=1), _ts(minutes=5)))
    conn.commit()
    conn.close()

    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        AGENT: {"name": "vivy-agent", "products": ["vivy"]},
        WIDE: {"name": "wide-agent", "products": ["*"]},
    }))
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps(REGISTRY))
    # The vec0 index, because the write routes reach it: an item or signal
    # created here is embedded after its commit, and a revision drops its
    # vectors inside the transaction that writes the new text.
    vector_index(db)
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(projects, "REGISTRY_PATH", registry)
    # PINNED, so an edit to the real registry cannot break these tests, and so a
    # test cannot silently depend on what projects happen to exist today.
    projects.reload()
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    yield TestClient(libapp.app)
    projects.reload()


def _h(token=AGENT):
    return {"Authorization": f"Bearer {token}",
            protocol.HEADER: str(protocol.CURRENT)}


def _body(**over):
    out = {"product": "vivy", "title": "something to do", "severity": "medium"}
    out.update(over)
    return out


# --- the product registry ------------------------------------------------------


@pytest.mark.parametrize("given", ["vivy", "VIVY", "Vivy", " vivy ", "viv",
                                   "vivy-app"])
def test_a_product_resolves_to_its_canonical_slug(client, given):
    """**The bug this closes**: `Substrate` beside `substrate`, one item invisible
    to every view asking for the real slug. A slug, an alias, a repo name or any
    casing resolves, and one canonical value is stored."""
    r = client.post("/items", json=_body(product=given), headers=_h(WIDE))
    assert r.status_code == 201
    assert r.json()["product"] == "vivy"


def test_a_non_project_scope_is_a_legal_product(client):
    """ARCH-426 makes the vocabulary uniform across the vaults, so an item may be
    scoped `platform` exactly as a signal may be. Resolving against projects
    alone refused three legal values."""
    r = client.post("/items", json=_body(product="platform"), headers=_h(WIDE))
    assert r.status_code == 201
    assert r.json()["product"] == "platform"


def test_an_unknown_product_is_refused_and_says_what_is_accepted(client):
    r = client.post("/items", json=_body(product="not-a-project"),
                    headers=_h(WIDE))
    assert r.status_code == 400
    assert "vivy" in r.json()["known"]


def test_scope_is_checked_against_the_resolved_slug(client):
    """A token scoped to `vivy` must not be refused for writing `viv`, and must
    not be admitted for something that merely resembles its product."""
    assert client.post("/items", json=_body(product="viv"),
                       headers=_h()).status_code == 201
    assert client.post("/items", json=_body(product="constance"),
                       headers=_h()).status_code == 404


def test_an_unreadable_registry_accepts_rather_than_refusing(client, monkeypatch,
                                                             tmp_path):
    """**Fail-open, deliberately.** An unreadable registry is an operational
    fault; a service that cannot accept a filing is a work stoppage, and a
    wrongly-scoped item is a row someone can fix."""
    monkeypatch.setattr(projects, "REGISTRY_PATH", tmp_path / "gone.json")
    projects.reload()
    assert not projects.available()
    assert projects.resolve("anything-at-all") == "anything-at-all"
    r = client.post("/items", json=_body(product="vivy"), headers=_h())
    assert r.status_code == 201


def test_reload_drops_every_cache(client, tmp_path, monkeypatch):
    """Orient's version cleared only the raw read, leaving the resolution map
    stale — so the registry reported one thing and `resolve` answered from the
    old one, with no symptom until a filing landed under a slug that no longer
    existed.

    `private_repos()` is asserted here too: a third `functools`-cached read of
    `_raw()`, added for `GET /runs` (PR #51), that went stale the same way
    `_resolution_map` once did. `reload()` now documents itself as dropping
    every cache this module keeps — this test is what makes that true, and what
    a fourth cache escaping it in the future fails against. `private_products()`
    is the fourth (issue_01M383JHPMME74P4HDM46D2WA5), added beside
    `private_repos()` for a signal's `product` rather than a repo.

    PR #185 round 2 added a fifth cache here, `private_product_spellings()`,
    for `_handoff_wall`'s SQL to match a private project's alias and repo
    spellings in `product` as well as its canonical slug. Round 3 removed it:
    `_handoff_wall` now calls `_private_handoff` itself as a registered SQL
    function (`app._register_handoff_sql_functions`), which already resolves
    `product` through `projects.resolve()` and `private_products()` per row —
    the hand-written spelling set was a second, drifting approximation of
    that same resolution, not a fifth cache this test still needs to cover.
    """
    # SELF-CONTAINED, because it was not (reviewer, PR #5). It caught a broken
    # `reload` only because an earlier test in file order had already populated
    # the process-wide cache; run alone it passed even with `reload` clearing one
    # cache. Resolving against the fixture registry FIRST is what makes the stale
    # value the assertion looks for actually exist at that point.
    assert projects.resolve("vivy") == "vivy"
    assert projects.private_repos() == frozenset()
    assert projects.private_products() == frozenset()
    other = tmp_path / "other.json"
    other.write_text(json.dumps({
        "newproject": {"aliases": [], "repos": []},
        "secretproject": {"private": True, "repos": ["secret-repo"],
                          "aliases": ["secret-alias"]},
    }))
    monkeypatch.setattr(projects, "REGISTRY_PATH", other)
    projects.reload()
    assert projects.resolve("newproject") == "newproject"
    assert projects.resolve("vivy") is None, "the resolution map was stale"
    assert projects.private_repos() == frozenset({"secretproject", "secret-repo"}), \
        "private_repos was stale"
    assert projects.private_products() == frozenset({"secretproject"}), \
        "private_products was stale"


# --- the nesting rules ---------------------------------------------------------


def test_an_item_can_be_filed_under_an_epic(client):
    r = client.post("/items", json=_body(parent_id="issue_epic_v"), headers=_h())
    assert r.status_code == 201
    assert r.json()["parent_id"] == "issue_epic_v"


def test_a_parent_that_is_not_an_epic_is_refused(client):
    """Items do not nest; an item's parent is an epic or nothing. That is what
    keeps the tree's interior all epics and its leaves all items."""
    r = client.post("/items", json=_body(parent_id="issue_task_v"), headers=_h())
    assert r.status_code == 400
    assert "epic" in r.json()["error"]


def test_an_out_of_scope_parent_reads_exactly_like_a_missing_one(client):
    """**The probe this ordering exists to stop.** Reporting the parent's type
    before checking scope let a caller holding one product's token learn which
    ids existed, what type they were, and which product they were in."""
    absent = client.post("/items", json=_body(parent_id="issue_nope"),
                         headers=_h())
    # A NON-EPIC out-of-scope target, deliberately. The epic sibling passes the
    # type check whichever order the checks run in, so it cannot tell a safe
    # implementation from a leaking one; only a target that would FAIL the type
    # check does.
    foreign = client.post("/items", json=_body(parent_id="issue_task_c"),
                          headers=_h())
    assert absent.status_code == foreign.status_code == 400
    # Each message is the same template carrying only the id the CALLER supplied,
    # so neither reveals anything the caller did not already know. Asserting the
    # exact bodies rather than hunting for substrings, because the substrings that
    # would betray a real leak — the product and the type — also occur inside the
    # echoed id itself, which an earlier version of this test mistook for one.
    assert absent.json() == {"error": "parent not found: issue_nope"}
    assert foreign.json() == {"error": "parent not found: issue_task_c"}


def test_a_parent_in_another_product_is_refused_once_it_is_visible(client):
    """Past the scope check the caller can already read the parent, so a specific
    error discloses nothing and a vague one would only make a real mistake harder
    to fix."""
    r = client.post("/items", json=_body(product="vivy",
                                         parent_id="issue_epic_c"),
                    headers=_h(WIDE))
    assert r.status_code == 400
    assert "constance" in r.json()["error"]


def test_a_malformed_parent_id_is_refused(client):
    assert client.post("/items", json=_body(parent_id="x" * 41),
                       headers=_h()).status_code == 400
    assert client.post("/items", json=_body(parent_id=["a"]),
                       headers=_h()).status_code == 400


# --- a corrections queue as a parent (issue_01M12NE0RFPJY12TT8CF1169V1,
# library/specs/one-home-per-work-item) ------------------------------------


def test_an_item_can_be_filed_under_its_products_corrections_queue(client):
    r = client.post("/items", json=_body(parent_id="cq_vivy"), headers=_h())
    assert r.status_code == 201, r.text
    assert r.json()["parent_id"] == "cq_vivy"
    # The same sequence a NULL parent gets — `mint_item_key` treats an
    # explicit queue parent and no parent as the one case, "a correction".
    assert r.json()["key"] == "CQ·1"


def test_another_products_queue_is_refused_exactly_like_a_missing_parent(client):
    """Same status, same wording, same shape `test_an_out_of_scope_parent_
    reads_exactly_like_a_missing_one` already asserts for an epic — a queue
    id must not let a caller learn a product name it has no scope over
    either."""
    absent = client.post("/items", json=_body(parent_id="cq_nope"),
                         headers=_h())
    foreign = client.post("/items", json=_body(parent_id="cq_constance"),
                          headers=_h())
    assert absent.status_code == foreign.status_code == 400
    assert absent.json() == {"error": "parent not found: cq_nope"}
    assert foreign.json() == {"error": "parent not found: cq_constance"}


def test_an_epic_with_a_queue_parent_is_refused(client):
    """An epic's parent is an epic or nothing — never a corrections queue,
    which holds work items."""
    r = client.post("/items", json=_body(type="epic", parent_id="cq_vivy"),
                    headers=_h())
    assert r.status_code == 400
    assert "epic" in r.json()["error"]


def test_a_queue_id_as_a_prerequisite_is_refused_as_an_unknown_item_id(client):
    """`_validate_prerequisites` resolves against `issue_items` only, and a
    corrections queue has no row there — the same refusal a made-up id
    gets."""
    r = client.post("/items", json=_body(
        prerequisites=[{"item": "cq_vivy"}]), headers=_h())
    assert r.status_code == 400
    assert "unknown item id" in r.json()["error"]


# --- found_during, which epic's work turned an item up without the lane change
# `parent_id` carries (issue_01M3BFN34Y8P8K39RWS6MM89ZT) -----------------------


def test_an_item_can_be_filed_with_found_during(client):
    r = client.post("/items", json=_body(found_during="issue_epic_v"),
                    headers=_h())
    assert r.status_code == 201
    assert r.json()["found_during"] == "issue_epic_v"
    # NEITHER `parent_id` NOR THE CORRECTIONS LANE MOVES: `found_during`
    # attributes the item to an epic's work without nesting it beneath that
    # epic.
    assert r.json()["parent_id"] is None


def test_found_during_absent_is_none(client):
    r = client.post("/items", json=_body(), headers=_h())
    assert r.status_code == 201
    assert r.json()["found_during"] is None


def test_a_found_during_that_is_not_an_epic_is_refused(client):
    r = client.post("/items", json=_body(found_during="issue_task_v"),
                    headers=_h())
    assert r.status_code == 400
    assert r.json() == {"error": "unknown item id(s)", "ids": ["issue_task_v"]}


def test_a_found_during_naming_a_missing_id_is_refused(client):
    r = client.post("/items", json=_body(found_during="issue_nope"),
                    headers=_h())
    assert r.status_code == 400
    assert r.json() == {"error": "unknown item id(s)", "ids": ["issue_nope"]}


def test_a_found_during_in_another_product_is_refused_once_it_is_visible(client):
    r = client.post("/items", json=_body(found_during="issue_epic_c"),
                    headers=_h(WIDE))
    assert r.status_code == 400
    assert r.json() == {"error": "unknown item id(s)", "ids": ["issue_epic_c"]}


def test_a_found_during_out_of_scope_reads_exactly_like_missing(client):
    """**One wording for all five refusals, deliberately**: unlike `parent_id`,
    nothing about this request later discloses which of a non-epic, a missing
    id, a cross-product epic or an out-of-scope epic the caller hit, so a
    distinct wording for any one of them would make `found_during` an
    existence oracle one guess at a time."""
    missing = client.post("/items", json=_body(found_during="issue_nope"),
                          headers=_h())
    out_of_scope = client.post("/items",
                               json=_body(found_during="issue_epic_c"),
                               headers=_h())
    assert missing.status_code == out_of_scope.status_code == 400
    assert (missing.json()["error"] == out_of_scope.json()["error"]
            == "unknown item id(s)")


def test_a_malformed_found_during_is_refused(client):
    assert client.post("/items", json=_body(found_during="x" * 41),
                       headers=_h()).status_code == 400
    assert client.post("/items", json=_body(found_during=["a"]),
                       headers=_h()).status_code == 400


# --- vocabularies --------------------------------------------------------------


@pytest.mark.parametrize("field,value", [
    ("type", "epicc"), ("severity", "urgent"), ("needs", "somebody")])
def test_a_value_outside_a_vocabulary_is_refused(client, field, value):
    r = client.post("/items", json=_body(**{field: value}), headers=_h())
    assert r.status_code == 400
    assert value in r.json()["error"]


def test_feature_is_refused_like_any_other_unknown_type(client):
    r = client.post("/items", json=_body(type="feature"), headers=_h())
    assert r.status_code == 400
    assert "feature" in r.json()["error"]


@pytest.mark.parametrize("dropped", ["bug", "nit", "improvement", "task"])
def test_a_dropped_type_is_accepted_and_the_response_says_so(client, dropped):
    """Transitional: agent-platform's `bin/mcp-ops-server` still sends `type`
    on every create, defaulting to `bug` (issue_01M3VN8RZN8D3NT19KG744FQG7,
    comment 5133), so this stores no type rather than refusing the filing, and
    names the dropped value in `type_ignored`."""
    r = client.post("/items", json=_body(type=dropped), headers=_h())
    assert r.status_code == 201, r.json()
    body = r.json()
    assert body["type"] is None
    assert body["type_ignored"] == dropped


def test_the_defaults_leave_the_item_untyped(client):
    """A work item carries no type; `epic` is the only value left
    (issue_01M0EEPS591VQHYEBJY7NGF4C3), so omitting `type` stores no type
    rather than defaulting to one."""
    r = client.post("/items", json=_body(), headers=_h()).json()
    assert r["type"] is None and r["needs"] == "agent" and r["state"] == "open"
    assert "type_ignored" not in r


def test_severity_is_required_the_way_orient_requires_it(client):
    """**Optional here in the first version, storing NULL when omitted**
    (reviewer, PR #5). Orient refuses. That is a silent drop rather than a
    documented divergence, and §4 carries Orient's semantics across."""
    payload = _body()
    del payload["severity"]
    r = client.post("/items", json=payload, headers=_h())
    assert r.status_code == 400
    assert "severity" in r.json()["error"]


def test_an_unknown_product_lists_only_names_this_caller_may_use(client):
    """Returning the whole registry turned a slow oracle into a directory dump,
    and told a scoped agent about products it cannot file into anyway
    (security-reviewer, PR #5)."""
    scoped = client.post("/items", json=_body(product="nope"),
                         headers=_h()).json()["known"]
    assert "constance" not in scoped
    assert "vivy" in scoped
    wide = client.post("/items", json=_body(product="nope"),
                       headers=_h(WIDE)).json()["known"]
    assert "constance" in wide


def test_a_registry_holding_only_metadata_is_not_available(client, tmp_path,
                                                           monkeypatch):
    """**The dangerous middle** (reviewer on Orient's PR #143, and this port
    dropped the fix). A file holding only metadata is valid JSON and non-empty, so
    keying `available()` on the raw read calls it healthy — while every product
    resolves to None and every filing is refused. Worse than an unreadable file,
    because nothing says anything is wrong."""
    meta_only = tmp_path / "meta.json"
    meta_only.write_text(json.dumps({"_meta": {"version": 3}}))
    monkeypatch.setattr(projects, "REGISTRY_PATH", meta_only)
    projects.reload()
    assert projects._snapshot().raw, "the fixture should parse to something non-empty"
    assert not projects.available(), "a metadata-only registry is not usable"
    # Fails open rather than refusing everything.
    assert projects.resolve("vivy") == "vivy"
    assert client.post("/items", json=_body(), headers=_h()).status_code == 201


def test_a_title_longer_than_the_cap_is_refused(client):
    r = client.post("/items", json=_body(title="t" * (store.MAX_TITLE + 1)),
                    headers=_h())
    assert r.status_code == 400


# --- the once_key gate ---------------------------------------------------------


def test_a_collision_in_scope_returns_the_existing_item(client):
    r = client.post("/items", json=_body(once_key="k-task-v"), headers=_h())
    assert r.status_code == 200
    assert r.json()["id"] == "issue_task_v"
    assert r.json()["idempotent"] is True


def test_an_idempotent_hit_reports_the_claim_the_item_actually_has(client):
    """**THE IDEMPOTENT REPLY IS AN ECHO OF SOMEBODY ELSE'S ROW, so it has to
    read the claim rather than assume one** (reviewer, PR #20 round 2). The row a
    `once_key` collision answers with is not the row this call would have
    created, and by the time a caller retries it may have been checked out. Both
    idempotent branches echoed `item_dict`'s old `(False, None)` default, so a
    retry reported `claim_expires_at: null` on a genuinely held item — the same
    wrong-data-as-real shape the round-1 fix closed at eleven other sites and
    missed here, because these two live in `store` where `lease` cannot be
    imported.
    """
    r = client.post("/items", json=_body(once_key="k-held-v"), headers=_h())
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["id"] == "issue_held_v"
    assert out["idempotent"] is True
    assert out["state"] == "claimed"
    assert out["claim_expires_at"] is not None
    assert out["claim_expired"] is False


def test_a_fresh_create_reports_no_claim_because_it_has_none(client):
    """The 201's other half. It asks for the claim like every other branch even
    though a row inserted `open` one line earlier cannot have one — the answer
    has to be the unclaimed one, and asserting it here is what stops the uniform
    call from being mistaken for an oversight later."""
    r = client.post("/items", json=_body(), headers=_h())
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["state"] == "open"
    assert out["claim_expires_at"] is None
    assert out["claim_expired"] is False


def test_a_collision_out_of_scope_carries_nothing(client):
    """`once_key` is globally unique, so a collision can land on an item in a
    product this caller cannot read. Answering with it makes idempotency an
    existence oracle."""
    r = client.post("/items", json=_body(once_key="k-epic-c"), headers=_h())
    assert r.status_code == 409
    assert r.json() == {"error": "once_key already in use"}
    assert "constance" not in r.text


def test_the_gate_asks_for_the_capability_as_well_as_the_scope():
    """The same two halves the signal gate needs, exercised directly because the
    route's own `item.create` check refuses first and would mask it."""
    incapable = identity.Identity(actor="future", kind="reader",
                                  products=frozenset({"vivy"}))
    assert not libapp._can_file_into(incapable, {"product": "vivy"})
    capable = identity.Identity(actor="a", kind="agent",
                                products=frozenset({"vivy"}))
    assert libapp._can_file_into(capable, {"product": "vivy"})


def test_a_create_only_caller_is_refused_the_row_at_the_route(
        client, monkeypatch):
    """**The read half of the once_key gate, exercised through the route**
    (issue_01KZQ0ER8HG312VBQYY2DVP55Z). The route's own `item.create` door
    passes a create-only caller, so only `_can_file_into` stands between it and a
    full in-scope item it could not read through the read surface. Agent is
    stripped of `item.read` for this request — the role shape the identity table
    could grow tomorrow — and the collision answers with the bare 409."""
    monkeypatch.setattr(identity, "ROLE_CAPS", {
        **identity.ROLE_CAPS,
        "agent": identity.ROLE_CAPS["agent"] - {"item.read"}})
    r = client.post("/items", json=_body(once_key="k-task-v"), headers=_h())
    assert r.status_code == 409
    assert r.json() == {"error": "once_key already in use"}
    assert "A vivy task" not in r.text


def test_the_gate_asks_for_the_read_capability_as_well(monkeypatch):
    """A synthetic role holding `item.create` without `item.read`, asserted
    against the gate directly so a future edit that drops the read conjunct fails
    here even while no current role can mint such a credential."""
    monkeypatch.setattr(identity, "ROLE_CAPS", {
        **identity.ROLE_CAPS,
        "create_only": frozenset({"item.create"})})
    create_only = identity.Identity(actor="future-role", kind="create_only",
                                    products=frozenset({"vivy"}))
    assert identity.authorized(create_only, "item.create")
    assert not identity.authorized(create_only, "item.read")
    assert not libapp._can_file_into(create_only, {"product": "vivy"})


# --- what lands ----------------------------------------------------------------


def test_the_creation_event_is_written_with_the_item(client):
    """An item whose history does not start with its own creation is a row the
    audit trail cannot explain, so the two commit together."""
    created = client.post("/items", json=_body(), headers=_h()).json()
    conn = sqlite3.connect(store.DB_PATH)
    rows = conn.execute(
        "SELECT actor, from_state, to_state FROM issue_events WHERE item_id = ?",
        (created["id"],)).fetchall()
    conn.close()
    assert rows == [("vivy-agent", None, "open")]


def test_a_validation_failure_reaches_no_write_at_all(client):
    """A payload refused before `insert_item` writes nothing, which is true and
    cheap to check — and is NOT the atomicity guarantee. The test below is."""
    before = client.get("/items?limit=200", headers=_h(WIDE)).json()["total"]
    client.post("/items", json=_body(product="not-a-project"), headers=_h(WIDE))
    conn = sqlite3.connect(store.DB_PATH)
    events = conn.execute("SELECT COUNT(*) FROM issue_events").fetchone()[0]
    conn.close()
    after = client.get("/items?limit=200", headers=_h(WIDE)).json()["total"]
    assert before == after and events == 0


def test_the_item_is_rolled_back_when_its_event_cannot_be_written(client):
    """**The atomicity guarantee, actually exercised** (reviewer, PR #5).

    The first version of this test used a payload refused during validation, so
    it never reached `insert_item` at all — the reviewer proved it vacuous by
    splitting the single commit into two and watching all 183 tests still pass.
    A docstring claimed the guarantee and nothing checked it.

    This makes the EVENT insert fail, inside the transaction, after the item row
    is already written. If the two do not share a commit, the item survives with
    no creation event, which is the row the audit trail cannot explain.
    """
    conn = sqlite3.connect(store.DB_PATH)
    conn.execute("""
        CREATE TRIGGER no_events BEFORE INSERT ON issue_events
        BEGIN SELECT RAISE(ABORT, 'planted'); END""")
    conn.commit()
    conn.close()
    try:
        r = client.post("/items", json=_body(title="should not survive"),
                        headers=_h())
        assert r.status_code == 409, "the aborted write should not report success"
        conn = sqlite3.connect(store.DB_PATH)
        orphans = conn.execute(
            "SELECT COUNT(*) FROM issue_items WHERE title = ?",
            ("should not survive",)).fetchone()[0]
        conn.close()
        assert orphans == 0, "the item was committed without its creation event"
    finally:
        conn = sqlite3.connect(store.DB_PATH)
        conn.execute("DROP TRIGGER no_events")
        conn.commit()
        conn.close()


def test_the_filer_is_recorded_from_the_token(client):
    created = client.post("/items", json=_body(created_by="somebody-else"),
                          headers=_h()).json()
    conn = sqlite3.connect(store.DB_PATH)
    stored = conn.execute("SELECT created_by, created_by_kind FROM issue_items "
                          "WHERE id = ?", (created["id"],)).fetchone()
    conn.close()
    assert stored == ("vivy-agent", "agent")


def test_a_created_item_is_readable_back(client):
    created = client.post("/items", json=_body(title="read me back"),
                          headers=_h()).json()
    got = client.get(f"/items/{created['id']}", headers=_h())
    assert got.status_code == 200 and got.json()["title"] == "read me back"


def test_a_write_is_refused_while_the_shadow_is_running(client, monkeypatch):
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    assert client.post("/items", json=_body(),
                       headers=_h()).status_code == 503


# --- topics at CREATE time (2026-09-08) -----------------------------------------
# The correction path grew a validator bounding count, length and characters.
# THIS route writes the same field, and checked only `isinstance(list)` — so
# every bound the guard enforced was reachable by anyone holding `item.create`,
# which is the ordinary filing permission (security-reviewer, PR #99).

def _topics_of(client, item_id):
    r = client.get(f"/items/{item_id}", headers=_h(WIDE))
    assert r.status_code == 200, r.text
    return r.json()["topics"]


def test_topics_are_accepted_at_create(client):
    r = client.post("/items", json=_body(topics=["DESIGN", "API"]),
                    headers=_h(WIDE))
    assert r.status_code == 201, r.text
    assert _topics_of(client, r.json()["id"]) == ["DESIGN", "API"]


def test_create_normalises_topics_the_same_way_correct_does(client):
    r = client.post("/items", json=_body(topics=["  DESIGN  ", "API", "DESIGN"]),
                    headers=_h(WIDE))
    assert r.status_code == 201, r.text
    assert _topics_of(client, r.json()["id"]) == ["DESIGN", "API"]


def test_absent_or_null_topics_create_an_empty_list(client):
    r = client.post("/items", json=_body(), headers=_h(WIDE))
    assert r.status_code == 201, r.text
    assert _topics_of(client, r.json()["id"]) == []
    r = client.post("/items", json=_body(topics=None), headers=_h(WIDE))
    assert r.status_code == 201, r.text
    assert _topics_of(client, r.json()["id"]) == []


@pytest.mark.parametrize("bad, why", [
    ("DESIGN", "a bare string is not an array"),
    ([1], "a non-string member"),
    ([""], "an empty topic"),
    (["x" * 65], "over MAX_TOPIC"),
    ([f"t{i}" for i in range(17)], "over MAX_TOPICS_PER_ITEM"),
    (["DESIGН"], "a Cyrillic lookalike"),
])
def test_the_create_path_enforces_the_same_bounds_as_correct(client, bad, why):
    """THE ASYMMETRY THIS CLOSES. Guarding only the correction path would leave
    every bound reachable by a create call, and `sort=topic` runs json_extract
    over every row in the result set — so one oversized blob filed here degrades
    every sorted listing for every caller."""
    r = client.post("/items", json=_body(topics=bad), headers=_h(WIDE))
    assert r.status_code == 400, f"{why}: {r.text}"


def test_refs_is_still_validated_after_the_topics_split(client):
    """The two checks shared one refusal before; splitting them must not drop
    the other half."""
    r = client.post("/items", json=_body(refs="not-an-object"), headers=_h(WIDE))
    assert r.status_code == 400 and "refs" in r.json()["error"]


# --- the stable item key (issue_01M2P5CBY5JY8SK5W344CV82AE) --------------------


def test_an_epic_gets_a_per_product_key(client):
    r = client.post("/items", json=_body(type="epic"), headers=_h())
    assert r.status_code == 201, r.text
    assert r.json()["key"] == "E1"


def test_a_second_epic_in_the_same_product_continues_the_sequence(client):
    first = client.post("/items", json=_body(type="epic"), headers=_h()).json()
    second = client.post("/items", json=_body(type="epic"), headers=_h()).json()
    assert (first["key"], second["key"]) == ("E1", "E2")


def test_a_sub_items_key_extends_its_epics(client):
    epic = client.post("/items", json=_body(type="epic"), headers=_h()).json()
    first = client.post("/items", json=_body(parent_id=epic["id"]),
                        headers=_h()).json()
    second = client.post("/items", json=_body(parent_id=epic["id"]),
                         headers=_h()).json()
    assert first["key"] == f"{epic['key']}·1"
    assert second["key"] == f"{epic['key']}·2"


def test_a_sub_epic_gets_its_own_per_product_key_not_its_parents(client):
    """An epic always gets the next per-product `E<n>`, whether or not it has
    a parent of its own — a sub-epic's key does not extend its parent's,
    the same way `mint_item_key` checks `item_type` before `parent_id`."""
    top = client.post("/items", json=_body(type="epic"), headers=_h()).json()
    sub = client.post("/items", json=_body(type="epic", parent_id=top["id"]),
                      headers=_h()).json()
    assert top["key"] == "E1"
    assert sub["key"] == "E2"


def test_a_sub_epics_own_children_extend_the_sub_epics_key(client):
    """A non-epic nested under a sub-epic extends the SUB-epic's key, not
    the top epic's — the same per-epic scoping an ordinary epic gets."""
    top = client.post("/items", json=_body(type="epic"), headers=_h()).json()
    sub = client.post("/items", json=_body(type="epic", parent_id=top["id"]),
                      headers=_h()).json()
    leaf = client.post("/items", json=_body(parent_id=sub["id"]),
                       headers=_h()).json()
    assert sub["key"] == "E2"
    assert leaf["key"] == f"{sub['key']}·1"


def test_a_correction_gets_its_own_sequence_independent_of_epics(client):
    """A correction — an item with no parent that is not itself an epic — is
    numbered CQ·<n>, a sequence the product's epics never touch."""
    first = client.post("/items", json=_body(), headers=_h()).json()
    epic = client.post("/items", json=_body(type="epic"), headers=_h()).json()
    second = client.post("/items", json=_body(), headers=_h()).json()
    assert first["key"] == "CQ·1"
    assert epic["key"] == "E1"
    assert second["key"] == "CQ·2"


def test_two_products_number_their_epics_independently(client):
    vivy_epic = client.post("/items", json=_body(type="epic"),
                            headers=_h(WIDE)).json()
    constance_epic = client.post(
        "/items", json=_body(type="epic", product="constance"),
        headers=_h(WIDE)).json()
    assert vivy_epic["key"] == "E1"
    assert constance_epic["key"] == "E1"


def test_a_sub_items_numbering_is_scoped_to_its_own_epic(client):
    """Two epics in the same product do not share a counter — each epic's
    sub-items start at 1 of their own."""
    epic_a = client.post("/items", json=_body(type="epic"), headers=_h()).json()
    epic_b = client.post("/items", json=_body(type="epic"), headers=_h()).json()
    under_a = client.post("/items", json=_body(parent_id=epic_a["id"]),
                          headers=_h()).json()
    under_b = client.post("/items", json=_body(parent_id=epic_b["id"]),
                          headers=_h()).json()
    assert under_a["key"] == f"{epic_a['key']}·1"
    assert under_b["key"] == f"{epic_b['key']}·1"


def test_a_keys_number_is_stable_once_a_sibling_resolves_and_once_another_joins(
        client):
    epic = client.post("/items", json=_body(type="epic"), headers=_h()).json()
    first = client.post("/items", json=_body(parent_id=epic["id"]),
                        headers=_h()).json()
    client.post(f"/items/{first['id']}/resolve", json={"resolution": "fixed"},
               headers=_h())
    second = client.post("/items", json=_body(parent_id=epic["id"]),
                         headers=_h()).json()
    assert second["key"] == f"{epic['key']}·2", \
        "the resolved sibling's number must not be reused"
    reread = client.get(f"/items/{first['id']}", headers=_h()).json()
    assert reread["key"] == first["key"], \
        "resolving an item must not change its own key"


def test_a_number_is_never_reissued_once_its_highest_sibling_resolves(client):
    """The store has no delete route for a Chart House item, so resolving the
    highest-numbered sibling is the one way to retire it and check the number
    stays retired rather than being handed to the next create."""
    client.post("/items", json=_body(), headers=_h())
    second = client.post("/items", json=_body(), headers=_h()).json()
    assert second["key"] == "CQ·2"
    client.post(f"/items/{second['id']}/resolve",
               json={"resolution": "wontfix"}, headers=_h())
    third = client.post("/items", json=_body(), headers=_h()).json()
    assert third["key"] == "CQ·3"


def test_the_key_is_unchanged_by_reparenting(client):
    """The simplest of the rules the issue allows, and the one this store
    implements: a key is minted once, at create, and nothing after that —
    `/correct`'s re-parenting included — ever changes it."""
    epic = client.post("/items", json=_body(type="epic"), headers=_h()).json()
    correction = client.post("/items", json=_body(), headers=_h()).json()
    assert correction["key"] == "CQ·1"
    r = client.post(f"/items/{correction['id']}/correct",
                    json={"parent_id": epic["id"]}, headers=_h())
    assert r.status_code == 200, r.text
    assert r.json()["parent_id"] == epic["id"]
    assert r.json()["key"] == "CQ·1", \
        "re-parenting must not mint a new key"
    reread = client.get(f"/items/{correction['id']}", headers=_h()).json()
    assert reread["key"] == "CQ·1"


def test_the_key_is_present_on_both_the_list_and_the_detail_response(client):
    created = client.post("/items", json=_body(title="findable"),
                          headers=_h()).json()
    detail = client.get(f"/items/{created['id']}", headers=_h())
    assert detail.status_code == 200
    assert detail.json()["key"] == created["key"]
    listed = client.get("/items?limit=200", headers=_h()).json()["items"]
    [row] = [i for i in listed if i["id"] == created["id"]]
    assert row["key"] == created["key"]


def test_concurrent_creates_in_the_same_scope_never_collide(client):
    """`mint_item_key`'s counter is a check-then-act, made safe by the
    `BEGIN IMMEDIATE` `insert_item` now opens before it runs — the same
    precondition this file's `grant_or_revoke` and `upsert_review_record`
    state for their own counters. Eight real threads racing to file a
    correction in the same product must still come out with eight distinct,
    consecutive numbers — a number handed out twice is the one failure mode
    a counter exists to rule out."""
    results = []
    results_lock = threading.Lock()

    def _create():
        r = client.post("/items", json=_body(), headers=_h())
        with results_lock:
            results.append(r)

    threads = [threading.Thread(target=_create) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(r.status_code == 201 for r in results), [r.text for r in results]
    keys = [r.json()["key"] for r in results]
    assert len(set(keys)) == len(keys), f"a number was reused: {keys}"
    numbers = sorted(int(k.split("·")[1]) for k in keys)
    assert numbers == list(range(1, 9))
