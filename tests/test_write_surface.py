"""The Archives write route, and the two controls that make it safe to exist.

**The shadow is enforced by the connection.** §4 requires Orient to own writes for
the whole read-only shadow, so a Library that could write to the vault during it
would be the second writer ADR-0020 exists to prevent — and both processes would
succeed, so the failure would be silent. `store.connect_rw` refuses unless writes
are explicitly enabled, which is why these tests have to turn them on deliberately
and why one of them checks that a route refuses when they are not.

**The dedup reply is an oracle unless it is gated.** `dedup_key` is globally
unique, so a collision can match a row in another product or a customer-class row
the caller could never read. Most of this file is that one rule, because it is the
whole design of the route.
"""
from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

import app as libapp
import conftest
import identity
import projects
import protocol
import store

AGENT = "agent-token-vivy"
WIDE = "agent-token-wide"
SYSTEM = "system-token"
NOSCOPE = "agent-token-noscope"

# A private project (ADR-0043) with one repo, alongside the ordinary products
# the rest of this file writes to. Neither "vivy" nor "constance" is private,
# so the black-box guard is a no-op for every test that predates it.
#
# `privatecrew` is a second private project whose `display`, `crew` and
# `aliases` entries are spelled differently from its slug and its `repos` —
# `private_repos()` alone never sees those spellings (it keys only the slug
# and `repos`), so a submission naming one is the live `exampleco`/
# `exampleco.com` case the security-reviewer found on PR #172: a private
# project reachable by a spelling `private_repos()` does not carry.
REGISTRY = {
    "_meta": {},
    "vivy": {"repos": []},
    "constance": {"repos": []},
    "secretproject": {"private": True, "repos": ["secret-repo"]},
    "privatecrew": {
        "private": True,
        "repos": ["privcrew-repo"],
        "display": "Private Crew Product",
        "crew": "privcrew-team",
        "aliases": ["privcrew-alias"],
    },
}


@pytest.fixture
def client(tmp_path, monkeypatch, vector_index):
    db = tmp_path / "control.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE signals (
            id TEXT PRIMARY KEY, source TEXT, stream TEXT, kind TEXT, actor TEXT,
            ingested_by TEXT, product TEXT, refs TEXT, body TEXT, meta TEXT,
            privacy_class TEXT, produced_at TEXT, ingested_at TEXT,
            embedded_at TEXT, dedup_key TEXT UNIQUE, acting_human TEXT);
        CREATE TABLE black_box_registrations (
            id INTEGER PRIMARY KEY, crew TEXT NOT NULL UNIQUE,
            model TEXT NOT NULL, gateway TEXT NOT NULL,
            created_by TEXT NOT NULL, created_at TEXT NOT NULL,
            updated_by TEXT NOT NULL, updated_at TEXT NOT NULL);
    """)
    conn.executemany(
        "INSERT INTO signals (id, product, kind, body, privacy_class, "
        "ingested_at, dedup_key) VALUES (?,?,?,?,?,?,?)",
        # One row per collision case the no-leak rule has to answer differently:
        # in the caller's scope, in another product, and customer-class.
        [("sig_mine", "vivy", "note", "already captured", "internal",
          "2026-08-01 07:30:00.000000", "key-mine"),
         ("sig_other", "constance", "note", "another product's row", "internal",
          "2026-08-01 07:31:00.000000", "key-other"),
         ("sig_cust", "vivy", "note", "a customer wrote this", "customer",
          "2026-08-01 07:32:00.000000", "key-customer")])
    conn.commit()
    conn.close()

    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        AGENT: {"name": "vivy-agent", "products": ["vivy"]},
        WIDE: {"name": "wide-agent", "products": ["*"]},
        SYSTEM: {"name": "capture", "products": ["*"], "kind": "system"},
        NOSCOPE: {"name": "scopeless", "products": []},
    }))
    # The vec0 index, because the write routes reach it: an item or signal
    # created here is embedded after its commit, and a revision drops its
    # vectors inside the transaction that writes the new text.
    vector_index(db)
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    # Writes on, because that is the state this route is FOR. The test that the
    # refusal works turns them back off explicitly.
    monkeypatch.setattr(store, "WRITES_ENABLED", True)

    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps(REGISTRY))
    monkeypatch.setattr(projects, "REGISTRY_PATH", registry)
    projects.reload()
    yield TestClient(libapp.app)
    projects.reload()


def _h(token=AGENT):
    return {"Authorization": f"Bearer {token}",
            protocol.HEADER: str(protocol.CURRENT)}


def _body(**over):
    out = {"source": "github", "kind": "note", "actor": "someone",
           "product": "vivy", "body": "a thing happened"}
    out.update(over)
    return out


# --- the shadow gate -----------------------------------------------------------


def test_a_write_is_refused_while_the_shadow_is_running(client, monkeypatch):
    """Not a 500. The service is working exactly as configured and the caller
    should retry after the cutover rather than report a fault."""
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    r = client.post("/signals", json=_body(), headers=_h())
    assert r.status_code == 503
    assert "write path" in r.json()["error"]


def test_a_database_that_stopped_being_ours_refuses_in_the_contract_shape(
        client, monkeypatch, tmp_path):
    """The post-boot re-check reaches a CALLER, unlike the same refusal at import
    (reviewer, convivy/library#32).

    A path repointed at Orient's database while this process runs raises
    `MisconfiguredDatabase` from `connect_rw`, and no route catches it — twenty
    sites wrap `WritesDisabled` and none wrap this. Without a handler it is an
    uncaught exception and a generic 500 carrying no hint, which is the one shape
    every other refusal on these routes avoids.
    """
    live = tmp_path / "orients-control.db"
    conn = sqlite3.connect(live)
    conn.execute("CREATE TABLE reading_rooms (id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(store, "ORIENT_DB_DEFAULT", live)
    monkeypatch.setattr(store, "DB_PATH", live)

    r = client.post("/signals", json=_body(), headers=_h())
    assert r.status_code == 503
    assert "cannot identify its own database" in r.json()["error"]
    assert r.json()["hint"], "the refusal has to say what went wrong"
    # **AND IT SAYS SO WITHOUT THE PATH** (security-reviewer, convivy/library#32).
    # The refusal's own message names the file it refused, and forwarding that
    # verbatim would hand the service account's home directory and on-disk layout
    # to any caller holding an ordinary write capability.
    assert str(live) not in r.text, "the refusal disclosed the server's path"
    assert "reading_rooms" not in r.text


def test_the_refusal_names_what_has_to_change(client, monkeypatch):
    """A refusal that does not say what would make it stop is a puzzle. This one
    is read by whoever runs the cutover."""
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    hint = client.post("/signals", json=_body(), headers=_h()).json()["hint"]
    assert "LIBRARY_WRITES_ENABLED" in hint


def test_connect_rw_is_the_only_way_to_a_writable_handle(monkeypatch):
    """The gate is in the connection rather than the route, so a future route
    that forgets to check still cannot write."""
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    with pytest.raises(store.WritesDisabled):
        store.connect_rw()


def test_the_read_connection_is_read_only_even_when_writes_are_enabled(
        client, monkeypatch, tmp_path):
    """Enabling writes must not quietly widen the read path. `connect()` stays
    `mode=ro` whatever the flag says."""
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    conn = store.connect()
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("DELETE FROM signals")
    conn.close()


# --- capability and scope ------------------------------------------------------


def test_an_unknown_token_cannot_write(client):
    assert client.post("/signals", json=_body(),
                       headers={"Authorization": "Bearer nope",
                                protocol.HEADER: "1"}).status_code == 401


def test_writing_outside_scope_is_404_not_403(client):
    """The same answer the read surface gives. A caller must not learn which
    products exist by being told it may not write to one."""
    r = client.post("/signals", json=_body(product="constance"), headers=_h())
    assert r.status_code == 404


def test_a_scopeless_identity_writes_nothing(client):
    assert client.post("/signals", json=_body(), headers=_h(NOSCOPE)
                       ).status_code == 404


# --- a black-box crew contributes nothing through this path (ADR-0043) --------
#
# issue_01M2M634HK37NWJ5MM13WYYREM clause 7: "A black-box crew contributes
# nothing through this path, tested rather than assumed."


def _stored_signals():
    conn = sqlite3.connect(store.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT id, product FROM signals").fetchall()
    finally:
        conn.close()


def _register_black_box(crew):
    conn = sqlite3.connect(store.DB_PATH)
    try:
        conn.execute(
            "INSERT INTO black_box_registrations "
            "(crew, model, gateway, created_by, created_at, updated_by, "
            " updated_at) VALUES (?, 'sonnet', 'anthropic', 'orient', "
            " '2026-09-01 00:00:00', 'orient', '2026-09-01 00:00:00')",
            (crew,))
        conn.commit()
    finally:
        conn.close()


def test_a_private_projects_own_name_is_refused(client):
    before = _stored_signals()
    r = client.post("/signals", json=_body(product="secretproject"),
                    headers=_h(WIDE))
    assert r.status_code == 404
    assert _stored_signals() == before


def test_a_private_projects_repo_spelling_is_refused_case_insensitively(client):
    """The registry spells the project `secretproject` and its repo
    `secret-repo` — a submission naming either, in any case, is the same
    crew."""
    before = _stored_signals()
    r = client.post("/signals", json=_body(product="Secret-Repo"),
                    headers=_h(WIDE))
    assert r.status_code == 404
    assert _stored_signals() == before


@pytest.mark.parametrize("spelling", [
    "Private Crew Product",  # `display`, absent from `private_repos()` and
                              # unresolved by `projects.resolve()`
    "privcrew-alias",        # `aliases`
    "privcrew-team",         # `crew`
])
def test_a_private_projects_display_alias_and_crew_name_are_all_refused(
        client, spelling):
    """`private_repos()` keys only a private project's slug and its `repos` —
    `private_crew_names()` is what has to catch the rest (security-reviewer,
    PR #172: the live private project `exampleco` has `display:
    "exampleco.com"`, a spelling `private_repos()` never sees and
    `projects.resolve()` does not recognise either, so it passed this guard
    before `private_crew_names()` was added here)."""
    before = _stored_signals()
    r = client.post("/signals", json=_body(product=spelling), headers=_h(WIDE))
    assert r.status_code == 404
    assert _stored_signals() == before


def test_a_self_registered_black_box_is_refused_even_when_not_marked_private(
        client):
    """A crew that registered itself through `POST /black-box/registrations`
    is a black box whether or not the project registry's `private` flag
    agrees — the project `vivy` is not private in this fixture's registry."""
    _register_black_box("vivy")
    before = _stored_signals()
    r = client.post("/signals", json=_body(product="vivy"), headers=_h())
    assert r.status_code == 404
    assert _stored_signals() == before


def test_an_unreadable_registry_refuses_rather_than_risking_a_black_box(
        client, tmp_path, monkeypatch):
    """This route fails CLOSED, inverting `projects.py`'s usual fail-open
    trade, matching `GET /runs`: the cost of guessing wrong is a black-box
    crew's content landing in the Archives, which no later edit can
    retract."""
    monkeypatch.setattr(projects, "REGISTRY_PATH", tmp_path / "gone.json")
    projects.reload()
    before = _stored_signals()
    r = client.post("/signals", json=_body(product="vivy"), headers=_h())
    assert r.status_code == 503
    assert "registry" in r.json()["error"]
    assert _stored_signals() == before


def test_an_ordinary_product_still_gets_captured(client):
    """The guard is a no-op for a product that is neither privately
    registered nor self-registered as a black box."""
    r = client.post("/signals", json=_body(product="vivy"), headers=_h())
    assert r.status_code == 201
    stored = _stored_signals()
    assert any(row["id"] == r.json()["id"] for row in stored)


def test_a_plain_agent_cannot_create_customer_class_content(client):
    """The capability gating customer content on the way out gates it on the way
    in. An agent that could CREATE a customer-class row could plant content it is
    not allowed to read back."""
    r = client.post("/signals", json=_body(privacy_class="customer"),
                    headers=_h(WIDE))
    assert r.status_code == 403
    assert "signal.create_customer" in r.json()["error"]


def test_a_system_identity_may_create_customer_class_content(client):
    r = client.post("/signals", json=_body(privacy_class="customer"),
                    headers=_h(SYSTEM))
    assert r.status_code == 201
    assert r.json()["privacy_class"] == "customer"


# --- privacy_class is caller-controlled, so it is caller-checked ---------------


@pytest.mark.parametrize("value", ["Customer", "CUSTOMER", "customer ",
                                   "custome", "internal-ish", "secret"])
def test_an_unrecognised_privacy_class_is_refused(client, value):
    """**The vulnerability this route introduced** (security-reviewer, PR #4).

    Four places compare a row's class against `"customer"` byte for byte: this
    write gate, the dedup no-leak check, the list filter and the single-signal
    wall. So `"Customer"` passed the write gate with a 201 and then matched no
    read-side wall — customer content by intent, internal content to every
    filter.

    It was safe until this route existed, because Orient was the sole producer
    and the column was never caller-controlled. Handing it to a caller is the new
    trust boundary, and inherited exact-match enforcement does not survive it.
    """
    r = client.post("/signals", json=_body(privacy_class=value), headers=_h(WIDE))
    assert r.status_code == 400, f"{value!r} was accepted"
    assert sorted(store.PRIVACY_CLASSES) == r.json()["supported"]


def test_a_case_variant_cannot_smuggle_customer_content_past_the_wall(client):
    """The attack end to end, run the way the reviewer ran it: create with a case
    variant, then read it back as an identity with neither customer capability.
    The row must not exist to be read."""
    created = client.post("/signals",
                          json=_body(privacy_class="Customer",
                                     body="a customer wrote this"),
                          headers=_h(WIDE))
    assert created.status_code == 400
    listed = client.get("/signals?product=vivy", headers=_h()).json()
    assert not any("a customer wrote this" in s["body"]
                   for s in listed["signals"])


def test_an_unrecognised_class_is_refused_rather_than_normalised(client):
    """Coercing `"Customer"` to customer-class would silently accept a typo as a
    claim about PII, and coercing it the other way is the bug. Neither guess is
    the service's to make."""
    hint = client.post("/signals", json=_body(privacy_class="Customer"),
                       headers=_h(SYSTEM)).json()["hint"]
    assert "refused rather than normalised" in hint


def test_the_store_refuses_an_unknown_class_even_bypassing_the_route(client):
    """The second layer. The route is where a caller gets a readable 400; this is
    what stops the NEXT write route storing a class no filter recognises because
    its author did not know the check existed."""
    with pytest.raises(ValueError, match="privacy_class"):
        store.insert_signal({"privacy_class": "Customer", "dedup_key": None},
                            lambda ex: True)


@pytest.mark.parametrize("value", [None, ""])
def test_an_unspecified_class_defaults_to_internal(client, value):
    """Absent and empty both mean "not stated", and both default to the class
    that needs no extra capability — matching Orient. This is not a hole in the
    allow-list: the default is a real member of it, so the stored row matches
    every downstream filter. The bug was storing a value that matched NONE."""
    r = client.post("/signals", json=_body(privacy_class=value), headers=_h())
    assert r.status_code == 201
    assert r.json()["privacy_class"] == "internal"


# --- malformed input fails closed rather than 500ing ---------------------------


def test_a_non_string_product_is_refused_rather_than_crashing(client):
    """It reached `identity.in_scope`, where `product in ident.products` raised
    `TypeError: unhashable type` for any non-wildcard token — a caller-triggered
    500 inside the function that enforces product scoping."""
    r = client.post("/signals", json=_body(product=["vivy"]), headers=_h())
    assert r.status_code == 400
    assert "product" in r.json()["error"]


def test_a_non_string_dedup_key_is_refused_rather_than_crashing(client):
    """It reached sqlite3's parameter binding unguarded."""
    assert client.post("/signals", json=_body(dedup_key={"a": 1}),
                       headers=_h()).status_code == 400


def test_an_int_dedup_key_is_refused_with_400_not_500(client):
    """`payload.get("dedup_key") or None` ran `len(dedup_key)` on whatever
    survived the `or`, unconditionally, a few lines below (security-reviewer,
    PR #201 round 2) — an int would have reached that `len()` call uncaught
    and raised `TypeError` as a 500, the same mechanism `product` and
    `privacy_class` were once vulnerable to on this same route. The type-check
    loop above this route's dedup lookup already refuses a non-string
    `dedup_key` before that line runs, so this stays a 400 both before and
    after this round's fix; it is pinned here as a standing regression check
    in the field the finding named."""
    r = client.post("/signals", json=_body(dedup_key=5), headers=_h())
    assert r.status_code == 400
    assert "dedup_key" in r.json()["error"]


def test_a_non_string_produced_at_is_refused(client):
    assert client.post("/signals", json=_body(produced_at=12345),
                       headers=_h()).status_code == 400


@pytest.mark.parametrize("value", [["Customer"], {"a": 1}, 7, True])
def test_a_non_string_privacy_class_is_refused_rather_than_crashing(
        client, value):
    """**Left out of the type-check loop when it was written**, and it 500'd two
    lines later on the same mechanism the loop existed to stop: `in` against a
    frozenset hashes the probe, so a list raised `TypeError: unhashable type`
    (security-reviewer, PR #4 round 2).

    The field the round was about was the field the guard skipped, which is the
    part worth remembering.
    """
    r = client.post("/signals", json=_body(privacy_class=value), headers=_h())
    assert r.status_code == 400
    assert "privacy_class" in r.json()["error"]


def test_every_caller_supplied_scalar_is_type_checked(client):
    """A standing check rather than one per field, so the next scalar added to
    this route cannot repeat the omission above. Each of these must answer 400
    rather than 500 when handed a list."""
    for field in ("product", "dedup_key", "produced_at", "privacy_class",
                  "source", "kind", "actor", "stream"):
        r = client.post("/signals", json=_body(**{field: ["x"]}), headers=_h())
        assert r.status_code in (400, 404), (
            f"{field} answered {r.status_code} for a non-string value")


def test_an_absent_optional_field_is_still_fine(client):
    """The type check must not turn optional into required."""
    assert client.post("/signals", json=_body(), headers=_h()).status_code == 201


# --- the dedup no-leak rule ----------------------------------------------------


def test_a_collision_in_the_callers_own_scope_returns_the_row(client):
    """The ordinary idempotent case: the caller could have created this row, so
    replying with it tells them nothing they could not already read."""
    r = client.post("/signals", json=_body(dedup_key="key-mine"), headers=_h())
    assert r.status_code == 200
    assert r.json()["id"] == "sig_mine"
    assert r.json()["idempotent"] is True


def test_a_collision_in_another_product_carries_nothing(client):
    """**The oracle this rule exists to close.** The key matched a `constance`
    row; answering with it would report content the read surface refuses to this
    caller, purely because they guessed a key."""
    r = client.post("/signals", json=_body(dedup_key="key-other"), headers=_h())
    assert r.status_code == 409
    assert r.json() == {"error": "dedup_key already in use"}
    assert "constance" not in r.text and "another product" not in r.text


def test_a_collision_on_customer_content_carries_nothing(client):
    """Same rule, customer wall instead of product scope. The caller is in scope
    for `vivy` and still must not receive this row."""
    r = client.post("/signals", json=_body(dedup_key="key-customer"),
                    headers=_h())
    assert r.status_code == 409
    assert r.json() == {"error": "dedup_key already in use"}
    assert "customer wrote" not in r.text


def test_the_two_refusals_are_word_for_word_identical(client):
    """A caller that could tell "exists but not yours" from "does not exist"
    would have the oracle back, in the error text instead of the body."""
    other = client.post("/signals", json=_body(dedup_key="key-other"),
                        headers=_h())
    cust = client.post("/signals", json=_body(dedup_key="key-customer"),
                       headers=_h())
    assert other.status_code == cust.status_code
    assert other.json() == cust.json()


def test_a_wildcard_identity_still_needs_the_customer_capability(client):
    """Scope is not the customer wall. `wide-agent` can create in every product
    and still cannot be handed a customer-class row."""
    r = client.post("/signals", json=_body(dedup_key="key-customer"),
                    headers=_h(WIDE))
    assert r.status_code == 409


def test_the_gate_asks_for_the_capability_as_well_as_the_scope():
    """**The first version asked only about scope** (reviewer, PR #4). "Could
    have created it" has two halves, and nothing was exposed today only because
    both roles that can authenticate hold `signal.create` — which is what made
    the gap invisible rather than what made it safe.

    Exercised against `_can_return_existing` directly, because the route's own
    `signal.create` check refuses first and would mask this entirely. A first
    version of this test went through the route, asserted the 403 that check
    produces, and passed with the gate's capability half deleted — a test that
    watched the wrong door.
    """
    scoped_but_incapable = identity.Identity(
        actor="future-role", kind="reader", products=frozenset({"vivy"}))
    assert not identity.authorized(scoped_but_incapable, "signal.create")
    assert identity.in_scope(scoped_but_incapable, "vivy")
    assert not libapp._can_return_existing(
        scoped_but_incapable, {"product": "vivy", "privacy_class": "internal"})


def test_the_gate_still_admits_a_caller_that_holds_both_halves():
    """The other direction, so the check above cannot be satisfied by a gate that
    refuses everyone."""
    capable = identity.Identity(actor="a", kind="agent",
                                products=frozenset({"vivy"}))
    assert libapp._can_return_existing(
        capable, {"product": "vivy", "privacy_class": "internal"})


def test_a_create_only_caller_is_refused_the_row_at_the_route(
        client, monkeypatch):
    """**The read half of the gate, exercised through the route**
    (issue_01KZQ0ER8HG312VBQYY2DVP55Z). The route's own `signal.create` door
    passes a create-only caller, so only `_can_return_existing` stands between it
    and a full in-scope row it could not read through the read surface. Agent is
    stripped of `signal.read` for this request — the role shape the identity
    table could grow tomorrow — and the collision answers with the bare 409."""
    monkeypatch.setattr(identity, "ROLE_CAPS", {
        **identity.ROLE_CAPS,
        "agent": identity.ROLE_CAPS["agent"] - {"signal.read"}})
    r = client.post("/signals", json=_body(dedup_key="key-mine"), headers=_h())
    assert r.status_code == 409
    assert r.json() == {"error": "dedup_key already in use"}
    assert "already captured" not in r.text


def test_the_read_half_is_asked_as_well_as_the_create_half(monkeypatch):
    """A synthetic role holding `signal.create` without `signal.read`, asserted
    against the gate directly so a future edit that drops the read conjunct fails
    here even while no current role can mint such a credential."""
    monkeypatch.setattr(identity, "ROLE_CAPS", {
        **identity.ROLE_CAPS,
        "create_only": frozenset({"signal.create"})})
    create_only = identity.Identity(actor="future-role", kind="create_only",
                                    products=frozenset({"vivy"}))
    assert identity.authorized(create_only, "signal.create")
    assert not identity.authorized(create_only, "signal.read")
    assert not libapp._can_return_existing(
        create_only, {"product": "vivy", "privacy_class": "internal"})


def test_the_customer_wall_asks_for_the_read_capability_as_well(monkeypatch):
    """**The customer branch keeps its own read wall.** A role that holds
    `signal.create_customer` AND `signal.read` still must not be handed a
    customer-class row through the echo when it cannot read customer rows on the
    read surface — `signal.customer` is what `_readable_signal` demands there,
    and `signal.create_customer` co-occurs with it only by accident of the role
    table."""
    monkeypatch.setattr(identity, "ROLE_CAPS", {
        **identity.ROLE_CAPS,
        "mint_no_customer_read": frozenset(
            {"signal.create", "signal.create_customer", "signal.read"})})
    ident = identity.Identity(actor="future-role", kind="mint_no_customer_read",
                              products=frozenset({"vivy"}))
    assert identity.authorized(ident, "signal.create_customer")
    assert not identity.authorized(ident, "signal.customer")
    assert not libapp._can_return_existing(
        ident, {"product": "vivy", "privacy_class": "customer"})


def test_a_system_identity_may_be_handed_the_customer_row(client):
    """The gate is the capability, so an identity that holds it gets the ordinary
    idempotent reply."""
    r = client.post("/signals", json=_body(dedup_key="key-customer"),
                    headers=_h(SYSTEM))
    assert r.status_code == 200
    assert r.json()["id"] == "sig_cust"


# --- what actually lands -------------------------------------------------------


def test_a_created_signal_is_readable_back_through_the_read_surface(client):
    created = client.post("/signals", json=_body(body="a new capture"),
                          headers=_h()).json()
    got = client.get(f"/signals/{created['id']}", headers=_h())
    assert got.status_code == 200
    assert got.json()["body"] == "a new capture"


def test_the_ingester_is_recorded_from_the_token_not_the_payload(client):
    """`ingested_by` is an audit field. Taking it from the body would let a caller
    write someone else's name into the record of who captured it.

    Asserted against the stored row rather than the response, because the read
    surface deliberately does not return `ingested_by` — a difference the shadow
    harness reports as a schema finding against Orient, and one this test would
    otherwise quietly depend on changing.
    """
    created = client.post("/signals",
                          json=_body(ingested_by="somebody-else"),
                          headers=_h()).json()
    conn = sqlite3.connect(store.DB_PATH)
    stored = conn.execute("SELECT ingested_by FROM signals WHERE id = ?",
                          (created["id"],)).fetchone()[0]
    conn.close()
    assert stored == "vivy-agent"


@pytest.mark.parametrize("field,cap", [
    ("source", store.MAX_IDENT), ("kind", store.MAX_IDENT),
    ("product", store.MAX_IDENT), ("actor", store.MAX_ACTOR)])
def test_an_oversized_identifier_is_refused_rather_than_truncated(
        client, field, cap):
    """**This test asserted the opposite, with a docstring claiming it matched
    Orient** (reviewer, PR #4). Orient's route refuses these with a 400; the
    `_clip` inside its insert core is a last-resort bound for that function's
    other caller, the CI ingress. The port copied the backstop and dropped the
    validation, so a caller writing a legitimately oversized identifier got a 201
    and silently corrupted data.
    """
    over = "a" * (cap + 1)
    body = _body(**{field: over})
    if field == "product":
        r = client.post("/signals", json=body, headers=_h(WIDE))
    else:
        r = client.post("/signals", json=body, headers=_h())
    assert r.status_code == 400
    assert field in r.json()["error"]


def test_the_store_still_clips_as_a_last_resort(client):
    """The route refuses; the store bounds. Both, for the reason Orient keeps
    both: a second caller must not be able to write past a column's bound just
    because it did not come through this route."""
    assert len(store.clip("a" * 5000, store.MAX_ACTOR)) == store.MAX_ACTOR


def test_an_oversized_dedup_key_is_refused(client):
    r = client.post("/signals",
                    json=_body(dedup_key="k" * (store.MAX_DEDUP_KEY + 1)),
                    headers=_h())
    assert r.status_code == 400
    assert "dedup_key" in r.json()["error"]


@pytest.mark.parametrize("value", ["a string", ["a", "list"], 7])
def test_refs_and_meta_must_be_objects(client, value):
    """Serialised unconditionally before, so a string or list was stored as if it
    were a mapping rather than refused."""
    assert client.post("/signals", json=_body(refs=value),
                       headers=_h()).status_code == 400
    assert client.post("/signals", json=_body(meta=value),
                       headers=_h()).status_code == 400


@pytest.mark.parametrize("field", ["refs", "meta"])
@pytest.mark.parametrize("value", [0, False, [], ""])
def test_a_falsy_non_dict_refs_or_meta_is_refused_rather_than_emptied(
        client, field, value):
    """`payload.get("refs") or {}` (and the same for `meta`) coerced every one
    of these to `{}` before the `isinstance` check above ever saw them, so a
    submission carrying `refs: 0` stored as a silent empty `{}` with a 201
    rather than being refused (security-reviewer, PR #201 round 2). `refs` is
    not cosmetic: `unreferenced_by` walks every row's `refs` to decide whether
    a prior row is still live, so a row whose `refs` emptied this way would
    fail to retire the row it was meant to reference, with no error ever
    surfacing to the submitter.

    Checked against the row count, not just the status: a 400 that still
    inserted a row with the coerced `{}` would be the exact silent-corruption
    bug this closes, wearing a passing status code.
    """
    conn = sqlite3.connect(store.DB_PATH)
    before = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    conn.close()

    r = client.post("/signals", json=_body(**{field: value}), headers=_h())
    assert r.status_code == 400
    assert field in r.json()["error"]

    conn = sqlite3.connect(store.DB_PATH)
    after = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    conn.close()
    assert after == before, "a refused submission must write nothing"


@pytest.mark.parametrize("value", [0, False, [], "", {}])
def test_a_falsy_non_string_dedup_key_is_refused_rather_than_dropped(
        client, value):
    """`payload.get("dedup_key") or None` coerced every one of these to
    `None` — silently discarding the caller's `dedup_key` rather than storing
    it or refusing it — before anything downstream could tell "no dedup_key
    was sent" from "one was sent and it was wrong" (security-reviewer, PR
    #201 round 2). Of these, only `""` actually reaches that line as a
    `dedup_key`-shaped bug on the current head: `0`, `False`, `[]` and `{}`
    are already refused earlier by the type-check loop this route applies to
    `dedup_key` before the dedup lookup, as `test_a_non_string_dedup_key_...`
    above also covers; they are included here so the whole falsy set is
    pinned at the one place that now refuses all of it by the same rule.

    Checked against the row count, not just the status: a 400 that still
    inserted a row with `dedup_key` silently set to `NULL` would be the exact
    silent-drop bug this closes, wearing a passing status code.
    """
    conn = sqlite3.connect(store.DB_PATH)
    before = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    conn.close()

    r = client.post("/signals", json=_body(dedup_key=value), headers=_h())
    assert r.status_code == 400
    assert "dedup_key" in r.json()["error"]

    conn = sqlite3.connect(store.DB_PATH)
    after = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    conn.close()
    assert after == before, "a refused submission must write nothing"


def test_an_oversized_stream_is_refused(client):
    assert client.post("/signals",
                       json=_body(stream="s" * (store.MAX_STREAM + 1)),
                       headers=_h()).status_code == 400


def test_an_oversized_body_is_refused_rather_than_truncated(client):
    """`body` got the same clip-not-refuse treatment the identifier fields had
    before PR #4, missed because it sits in a different column family
    (issue_01M3VN91AP1198SX7HE148ZE1B). Checked against the row count, not
    just the status: a 400 that still inserted a truncated row would be the
    exact silent-corruption bug this closes, wearing a passing status code.
    """
    conn = sqlite3.connect(store.DB_PATH)
    before = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    conn.close()

    over = "a" * (store.MAX_TEXT + 1)
    r = client.post("/signals", json=_body(body=over), headers=_h())
    assert r.status_code == 400
    assert "body" in r.json()["error"]
    assert str(store.MAX_TEXT) in r.json()["error"]

    conn = sqlite3.connect(store.DB_PATH)
    after = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    conn.close()
    assert after == before, "a refused submission must write nothing"


def test_a_body_exactly_at_the_limit_is_accepted(client):
    at_limit = "a" * store.MAX_TEXT
    r = client.post("/signals", json=_body(body=at_limit), headers=_h())
    assert r.status_code == 201
    conn = sqlite3.connect(store.DB_PATH)
    stored = conn.execute("SELECT body FROM signals WHERE id = ?",
                          (r.json()["id"],)).fetchone()[0]
    conn.close()
    assert len(stored) == store.MAX_TEXT


@pytest.mark.parametrize("value", [0, False, [], {}, 12345])
def test_a_non_string_body_is_refused_rather_than_silently_emptied(
        client, value):
    """Before this field had its own check, a non-string `body` reached
    `store.clip`, which turns anything that is not a `str` into `""` rather
    than raising — so a caller's number or array was accepted and stored as
    an empty capture with no sign anything was wrong.

    `0`, `False`, `[]` and `{}` are the falsy cases: `payload.get("body") or
    ""` coerces each of them to `""` before the `isinstance` check ever runs,
    so the fix has to distinguish "absent" from "present but falsy" the way
    `stream` already does, not just add a type check after an `or` default.
    """
    conn = sqlite3.connect(store.DB_PATH)
    before = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    conn.close()

    r = client.post("/signals", json=_body(body=value), headers=_h())
    assert r.status_code == 400
    assert "body" in r.json()["error"]

    conn = sqlite3.connect(store.DB_PATH)
    after = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    conn.close()
    assert after == before, "a refused submission must write nothing"


# --- produced_at is normalised, never stored raw -------------------------------


@pytest.mark.parametrize("given,expected", [
    ("2026-08-01T12:00:00Z", "2026-08-01 12:00:00"),
    ("2026-08-01T14:00:00+02:00", "2026-08-01 12:00:00"),
    ("2026-08-01 12:00:00", "2026-08-01 12:00:00"),
])
def test_produced_at_is_normalised_to_naive_utc(client, given, expected):
    """Stored verbatim before, which would put a non-normalised format into a
    column both services read from once the vault is merged."""
    created = client.post("/signals", json=_body(produced_at=given),
                          headers=_h()).json()
    conn = sqlite3.connect(store.DB_PATH)
    stored = conn.execute("SELECT produced_at FROM signals WHERE id = ?",
                          (created["id"],)).fetchone()[0]
    conn.close()
    assert stored == expected


def test_an_unparseable_produced_at_becomes_null_rather_than_failing(client):
    """Matching Orient: a capture must not be lost because a source stamped it
    oddly, and the capture is usually the only copy."""
    created = client.post("/signals", json=_body(produced_at="not a date"),
                          headers=_h())
    assert created.status_code == 201
    conn = sqlite3.connect(store.DB_PATH)
    stored = conn.execute("SELECT produced_at FROM signals WHERE id = ?",
                          (created.json()["id"],)).fetchone()[0]
    conn.close()
    assert stored is None


def test_a_missing_required_field_is_refused_and_named(client):
    r = client.post("/signals", json={"product": "vivy", "kind": "note",
                                      "actor": "x"}, headers=_h())
    assert r.status_code == 400
    assert "source" in r.json()["error"]


def test_a_body_that_is_not_an_object_is_refused(client):
    assert client.post("/signals", json=["not", "an", "object"],
                       headers=_h()).status_code == 400


def test_ids_are_sortable_by_creation(client):
    """A ULID rather than a random id, matching Orient, so the two halves of one
    vault stay sortable against each other after the migration."""
    a = client.post("/signals", json=_body(), headers=_h()).json()["id"]
    b = client.post("/signals", json=_body(), headers=_h()).json()["id"]
    assert a.startswith("sig_") and b.startswith("sig_")
    assert a < b


# --- embedding is kind-gated, library/decisions/0001 ----------------------------


def test_a_not_embedded_kind_is_stored_but_never_vectorised(client):
    """`alert` is one of the decision's named not-embedded kinds: the row lands
    exactly as any other capture does, and gets no vectors."""
    created = client.post(
        "/signals", json=_body(kind="alert",
                               body="a quetzalhollow alert about the pool"),
        headers=_h())
    assert created.status_code == 201
    signal_id = created.json()["id"]

    got = client.get(f"/signals/{signal_id}", headers=_h())
    assert got.status_code == 200
    assert got.json()["body"] == "a quetzalhollow alert about the pool"

    listed = client.get("/signals?kind=alert", headers=_h()).json()
    assert signal_id in {s["id"] for s in listed["signals"]}

    assert conftest.vectors_for(store.DB_PATH, signal_id) == []

    found = client.get("/signals/search?q=quetzalhollow", headers=_h()).json()
    assert signal_id not in {s["id"] for s in found["signals"]}


def test_an_embedded_kind_still_gets_vectors_and_is_found_by_search(client):
    created = client.post(
        "/signals", json=_body(kind="finding",
                               body="a marzipandrake finding was filed"),
        headers=_h())
    assert created.status_code == 201
    signal_id = created.json()["id"]

    assert len(conftest.vectors_for(store.DB_PATH, signal_id)) == 1

    found = client.get("/signals/search?q=marzipandrake", headers=_h()).json()
    assert signal_id in {s["id"] for s in found["signals"]}


def test_an_unnamed_kind_is_stored_and_not_vectorised(client):
    """A kind the decision does not name defaults to not embedded, per its own
    Defaults section — an allow-list, not a deny-list."""
    created = client.post(
        "/signals", json=_body(kind="some-new-kind", body="whatever this is"),
        headers=_h())
    assert created.status_code == 201
    signal_id = created.json()["id"]
    assert conftest.vectors_for(store.DB_PATH, signal_id) == []
