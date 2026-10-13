"""Capturing a signal FOR a person, and reading back who collided with whom.

Two things (`issue_01M0K2X44W12A1ACH06FZSWW84`, and the refusals half of the
Chart House's visibility gap).

**`acting_human` exists because `ingested_by` answers a different question.**
`ingested_by` is the CREDENTIAL that captured a row. When Orient captures a
customer email on the operator's behalf that credential is Orient, and before this the
person was simply lost — a capture made for someone read as a capture made by a
service. The acknowledge pair already had this shape; capture did not.

**`GET /lease-refusals` exists because a rate names nobody.**
`GET /metrics/librarian` reports how many refusals happened and in which
direction. It cannot report that two particular crews keep colliding on one
subtree, which is the thing somebody has to act on.
"""
from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

import app as libapp
import identity
import projects
import protocol
import store

AGENT = "agent-token-vivy"
SYSTEM = "system-token"
CLIENT = "orient-token"
WIDE = "agent-token-wide"

REFUSALS = [
    # (id, at, item, product, requester, actor, blocking_root, holder, blocks_by)
    ("ref_1", "2026-08-20 09:00:00", "issue_a", "vivy", "deck-co", "ops-mcp",
     "root_vivy", "vivy-co", "ancestor"),
    ("ref_2", "2026-08-20 10:00:00", "issue_b", "vivy", "vivy-co", "ops-mcp",
     "root_vivy", "deck-co", "descendant"),
    # SAME SECOND as ref_2's successor, which is what a collision storm looks
    # like and what makes ordering on `at` alone unstable.
    ("ref_3", "2026-08-20 10:00:00", "issue_c", "vivy", "deck-co", "ops-mcp",
     "root_hidden", "vivy-co", "ancestor"),
    # ANOTHER PRODUCT, so the scope wall has something to exclude.
    ("ref_4", "2026-08-20 11:00:00", "issue_d", "constance", "cst-co",
     "ops-mcp", "root_hidden", "other-co", "ancestor"),
]


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = tmp_path / "control.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE signals (
            id TEXT PRIMARY KEY, source TEXT, stream TEXT, kind TEXT, actor TEXT,
            ingested_by TEXT, product TEXT, refs TEXT, body TEXT, meta TEXT,
            privacy_class TEXT, produced_at TEXT, ingested_at TEXT,
            embedded_at TEXT, dedup_key TEXT UNIQUE, acting_human TEXT);
        CREATE TABLE cold_dedup_keys (
            dedup_key TEXT PRIMARY KEY, signal_id TEXT NOT NULL);
        CREATE TABLE issue_items (
            id TEXT PRIMARY KEY, product TEXT, title TEXT, type TEXT,
            state TEXT, parent_id TEXT);
        CREATE TABLE lease_refusals (
            id TEXT PRIMARY KEY, at TEXT, requested_item_id TEXT, product TEXT,
            requester TEXT, actor TEXT, blocking_lease_id TEXT,
            blocking_root_id TEXT, blocking_holder TEXT, blocks_by TEXT);
        CREATE TABLE black_box_registrations (
            id INTEGER PRIMARY KEY, crew TEXT NOT NULL UNIQUE,
            model TEXT NOT NULL, gateway TEXT NOT NULL,
            created_by TEXT NOT NULL, created_at TEXT NOT NULL,
            updated_by TEXT NOT NULL, updated_at TEXT NOT NULL);
    """)
    # The blocking ROOTS, whose product is a separate fact from the refused
    # item's. `root_vivy` is in scope for a vivy token; `root_hidden` is not.
    conn.executemany(
        "INSERT INTO issue_items (id, product, title, type, state) "
        "VALUES (?,?,'t','epic','open')",
        [("root_vivy", "vivy"), ("root_hidden", "constance")])
    conn.executemany(
        "INSERT INTO lease_refusals (id, at, requested_item_id, product, "
        "requester, actor, blocking_lease_id, blocking_root_id, "
        "blocking_holder, blocks_by) VALUES (?,?,?,?,?,?,'lease_x',?,?,?)",
        REFUSALS)
    conn.commit()
    conn.close()

    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        AGENT: {"name": "vivy-agent", "products": ["vivy"]},
        WIDE: {"name": "wide-agent", "products": ["*"]},
        SYSTEM: {"name": "capture", "products": ["*"], "kind": "system"},
    }))
    client_tokens = tmp_path / "client-tokens.json"
    client_tokens.write_text(json.dumps({
        CLIENT: {"name": "orient", "products": ["*"]},
    }))

    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(identity, "CLIENT_TOKENS", client_tokens)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)

    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({"_meta": {}}))
    monkeypatch.setattr(projects, "REGISTRY_PATH", registry)
    projects.reload()
    yield TestClient(libapp.app)
    projects.reload()


def _h(token):
    return {"Authorization": f"Bearer {token}",
            protocol.HEADER: str(protocol.CURRENT)}


def _capture(client, token=CLIENT, **body):
    payload = {"source": "gmail", "kind": "inbound_email",
               "actor": "alice@example.org", "product": "vivy",
               "body": "loved the demo", **body}
    return client.post("/signals", json=payload, headers=_h(token))


def _stored(signal_id):
    conn = sqlite3.connect(store.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM signals WHERE id = ?",
                            (signal_id,)).fetchone()
    finally:
        conn.close()


# --- capturing for a person ---------------------------------------------------


def test_an_approved_client_records_the_person_it_captured_for(client):
    r = _capture(client, acting_human="operator@example.com")
    assert r.status_code == 201, r.json()
    assert r.json()["acting_human"] == "operator@example.com"
    assert _stored(r.json()["id"])["acting_human"] == "operator@example.com"


def test_the_credential_is_still_recorded_separately(client):
    """`ingested_by` answers WHICH CREDENTIAL and `acting_human` answers WHICH
    PERSON. Collapsing them would make a captured customer email say the
    customer performed the capture."""
    body = _capture(client, acting_human="operator@example.com").json()
    assert body["ingested_by"] == "orient"
    assert body["actor"] == "alice@example.org"
    assert body["acting_human"] == "operator@example.com"


def test_a_capture_with_no_human_is_still_accepted(client):
    """Most captures are machines acting on their own — the Gmail poller, the CI
    ingress, an agent filing a note. Requiring a person would break all of them
    and would invite callers to invent one."""
    r = _capture(client, token=SYSTEM)
    assert r.status_code == 201
    assert r.json()["acting_human"] is None


def test_an_agent_may_not_claim_a_person(client):
    """`attested_human` refuses any credential that is not an approved client,
    so an agent naming a human is refused rather than believed."""
    r = _capture(client, token=AGENT, acting_human="operator@example.com")
    assert r.status_code == 403
    assert "approved-client" in r.json()["error"]


def test_a_system_token_may_not_claim_a_person_either(client):
    """`system` is broad — it holds `signal.create_customer` — and is still not
    a credential that has verified anybody."""
    assert _capture(client, token=SYSTEM,
                    acting_human="operator@example.com").status_code == 403


@pytest.mark.parametrize("value", ["", "   ", 5, [], {}, True, None])
def test_a_malformed_acting_human_is_refused_rather_than_stored(client, value):
    """A non-string reaching `store.clip` or the column would be the same
    type-confusion class PR #4 and PR #42 hardened other fields against.
    `None` is the one that must NOT refuse — it means absent."""
    r = _capture(client, acting_human=value)
    if value is None:
        assert r.status_code == 201 and r.json()["acting_human"] is None
    else:
        assert r.status_code == 403, f"{value!r} was accepted"


def test_an_over_long_acting_human_is_refused(client):
    r = _capture(client, acting_human="j" * (store.MAX_ACTOR + 1))
    assert r.status_code == 403 and "longer than" in r.json()["error"]


def test_the_person_is_never_defaulted_to_the_credential(client):
    """`attested_human`'s docstring names this as the thing that must not
    happen: falling back to `ident.actor` records the CLIENT as having acted,
    which is the identity ADR-0038 draws the line to keep out of this field."""
    body = _capture(client).json()
    assert body["acting_human"] is None
    assert body["acting_human"] != body["ingested_by"]


def test_customer_class_capture_carries_the_person(client):
    """The case the whole column exists for: a customer email captured through a
    client that verified a human."""
    r = _capture(client, privacy_class="customer",
                 acting_human="operator@example.com")
    assert r.status_code == 201
    assert r.json()["privacy_class"] == "customer"
    assert r.json()["acting_human"] == "operator@example.com"


# --- reading the collisions ---------------------------------------------------


def _refusals(client, token=WIDE, **params):
    return client.get("/lease-refusals", params=params, headers=_h(token))


def _ids(r):
    return [x["id"] for x in r.json()["refusals"]]


def test_the_fleet_wide_view_is_the_unfiltered_one(client):
    """The operator, 2026-08-22: one route serves both views."""
    assert set(_ids(_refusals(client))) == {"ref_1", "ref_2", "ref_3", "ref_4"}


def test_the_crew_scoped_view_is_the_same_route_with_product(client):
    assert set(_ids(_refusals(client, product="vivy"))) == {
        "ref_1", "ref_2", "ref_3"}


def test_refusals_refuses_an_unrecognised_parameter(client):
    r = _refusals(client, since="2026-01-01")
    assert r.status_code == 400
    assert r.json()["unknown"] == ["since"]
    assert r.json()["supported"] == sorted(
        ["blocking_holder", "blocks_by", "limit", "offset", "product",
         "requester"])


def test_a_blocker_outside_the_callers_scope_is_not_named(client):
    """**The requested item's product is not the blocking lease's**
    (security-reviewer, PR #48). `record_refusal` stamps `product` from the item
    that was refused, so the scope clause proves only that the caller may see
    THAT. ref_3 is a vivy refusal whose blocking subtree sits in constance."""
    rows = {x["id"]: x for x in _refusals(client, token=AGENT).json()["refusals"]}
    assert rows["ref_3"]["blocked"] is True
    assert "blocking_holder" not in rows["ref_3"]
    # ...while the direction and the caller's own item survive, because those
    # are facts about its own refused request.
    assert rows["ref_3"]["blocks_by"] == "ancestor"
    assert rows["ref_3"]["requested_item_id"] == "issue_c"
    # ...and an in-scope blocker is still named.
    assert rows["ref_1"]["blocking_holder"] == "vivy-co"


def test_a_refusal_names_who_collided_with_whom(client):
    """The whole point: the aggregate reports a count, this reports the pair."""
    row = next(x for x in _refusals(client).json()["refusals"]
               if x["id"] == "ref_1")
    assert row["requester"] == "deck-co"
    assert row["blocking_holder"] == "vivy-co"
    assert row["blocks_by"] == "ancestor"
    assert row["requested_item_id"] == "issue_a"


def test_scoping_is_the_identitys_and_a_product_param_cannot_widen_it(client):
    """`product` narrows WITHIN what the identity may see. A single-product
    token naming another product gets an empty page, not that product's rows."""
    assert _ids(_refusals(client, token=AGENT, product="constance")) == []
    # ...and its unfiltered view is its own product only.
    assert set(_ids(_refusals(client, token=AGENT))) == {
        "ref_1", "ref_2", "ref_3"}


def test_the_total_excludes_rows_the_scope_wall_hides(client):
    """A `total` counting rows the caller cannot see is how a listing leaks the
    existence of another product's work."""
    assert _refusals(client, token=AGENT).json()["total"] == 3
    assert _refusals(client).json()["total"] == 4


def test_ordering_is_newest_first_and_stable_within_a_second(client):
    """`at` is second-resolution and two refusals inside one second are exactly
    what a collision storm looks like, so `id` breaks the tie."""
    assert _ids(_refusals(client)) == ["ref_4", "ref_3", "ref_2", "ref_1"]


def test_paging_does_not_repeat_or_skip(client):
    walked, offset = [], 0
    while True:
        page = _refusals(client, limit=2, offset=offset)
        walked += _ids(page)
        if not page.json()["truncated"]:
            break
        offset += page.json()["count"]
    assert walked == ["ref_4", "ref_3", "ref_2", "ref_1"]


@pytest.mark.parametrize("field,value,expected", [
    ("requester", "deck-co", {"ref_1", "ref_3"}),
    ("blocking_holder", "deck-co", {"ref_2"}),
    ("blocks_by", "descendant", {"ref_2"}),
])
def test_filters(client, field, value, expected):
    assert set(_ids(_refusals(client, **{field: value}))) == expected


def test_refusals_need_item_read(client, monkeypatch):
    monkeypatch.setattr(identity, "authorized", lambda ident, cap: False)
    r = _refusals(client)
    assert r.status_code == 403 and "item.read" in r.json()["error"]


# --- who may read back the person ---------------------------------------------


def _read_one(client, signal_id, token):
    return client.get(f"/signals/{signal_id}", headers=_h(token))


def _read_list(client, token):
    return client.get("/signals", params={"product": "vivy"}, headers=_h(token))


def test_an_agent_cannot_read_back_the_person(client):
    """**The asymmetry this closes** (security-reviewer, PR #48). WRITING
    `acting_human` is restricted to an approved-client credential because a
    human's identity is human-reserved data (ADR-0038). Serving it to every
    reader holding `signal.read` made that restriction decorative: the fleet's
    lowest trust tier could read off which named individual is behind a
    capture."""
    sid = _capture(client, acting_human="operator@example.com").json()["id"]
    body = _read_one(client, sid, AGENT).json()
    assert "acting_human" not in body
    # ...and the rest of the row is still served, so this is a withheld field
    # rather than a withheld row.
    assert body["ingested_by"] == "orient"


def test_a_capability_holder_reads_it_back(client):
    sid = _capture(client, acting_human="operator@example.com").json()["id"]
    assert _read_one(client, sid, SYSTEM).json()["acting_human"] == "operator@example.com"


def test_the_listing_withholds_it_from_an_agent_too(client):
    """`_customer_wall` cannot cover this field: a human may be attested on an
    INTERNAL-class capture, so the field travels outside the class that wall
    knows about. The listing needs its own gate."""
    _capture(client, acting_human="operator@example.com", privacy_class="internal")
    rows = _read_list(client, AGENT).json()["signals"]
    assert rows and all("acting_human" not in r for r in rows)
    assert any("acting_human" in r
               for r in _read_list(client, SYSTEM).json()["signals"])


def test_the_field_is_omitted_rather_than_nulled(client):
    """A key present as `null` cannot be told apart from "no human was
    attested", so nulling it would answer a question the caller may not ask."""
    sid = _capture(client, acting_human="operator@example.com").json()["id"]
    assert _read_one(client, sid, AGENT).json().get("acting_human", "ABSENT") == "ABSENT"


def test_an_idempotent_hit_does_not_leak_another_callers_person(client):
    """A 200 is a `dedup_key` collision answering with a row this call did NOT
    create, whose attested human may be somebody else's — so the read gate
    decides, not the status code."""
    first = _capture(client, dedup_key="k1", acting_human="operator@example.com")
    assert first.status_code == 201
    again = client.post("/signals", json={
        "source": "gmail", "kind": "inbound_email", "actor": "alice@example.org",
        "product": "vivy", "body": "loved the demo", "dedup_key": "k1"},
        headers=_h(SYSTEM))
    assert again.status_code == 200 and again.json().get("idempotent") is True
    assert again.json()["acting_human"] == "operator@example.com"  # SYSTEM holds it


def test_filtering_on_a_hidden_holder_is_not_an_oracle(client):
    """**Withholding the name from the BODY is not enough while the same value
    can be filtered on** (security-reviewer, PR #48 round 2). A caller guesses a
    holder, reads `total`, and learns whether that holder collided with its
    scope — and crew identities are a small enumerable set, so a boolean count
    is a full oracle for the fact the withholding exists to keep.

    ref_3 is a vivy refusal (so it passes the listing's own scope clause) whose
    blocking subtree is in constance. `vivy-co` genuinely holds it. A correct
    guess must be indistinguishable from a wrong one."""
    correct = _refusals(client, token=AGENT, blocking_holder="vivy-co")
    wrong = _refusals(client, token=AGENT, blocking_holder="no-such-crew")
    # ref_1 is the in-scope row `vivy-co` also holds, so a correct guess still
    # returns what the caller may legitimately see — and nothing more.
    assert _ids(correct) == ["ref_1"]
    assert correct.json()["total"] == 1
    assert _ids(wrong) == [] and wrong.json()["total"] == 0
    # THE ORACLE ITSELF: ref_3 must not be reachable by naming its holder.
    assert "ref_3" not in _ids(correct)


def test_a_wide_token_still_sees_the_hidden_row_by_holder(client):
    """The guard narrows to what the caller may see, and must not break the
    filter for a caller that may see everything."""
    r = _refusals(client, token=WIDE, blocking_holder="vivy-co")
    assert set(_ids(r)) == {"ref_1", "ref_2", "ref_3"} - {"ref_2"}


def test_the_blocked_flag_and_the_named_fields_are_mutually_exclusive(client):
    """A row says either who blocked it or that something did, never both and
    never neither."""
    for row in _refusals(client, token=AGENT).json()["refusals"]:
        named = "blocking_holder" in row
        assert named != row.get("blocked", False), row
