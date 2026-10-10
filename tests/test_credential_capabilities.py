"""A credential's capabilities resolve from its own entry plus its role.

Driven through the real app (TestClient) wherever a route is the claim, and
through `identity` directly where the claim is about resolution itself. The
live credential files are only READ, by the inertness test, and it skips when a
file is absent (CI).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as libapp
import identity
import protocol
import store

AGENT = "agent-token"
GRANTED = "granted-agent-token"
CLIENT = "client-token"
WITHHELD = "withheld-client-token"


def _h(token):
    return {"Authorization": f"Bearer {token}",
            protocol.HEADER: str(protocol.CURRENT)}


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = tmp_path / "control.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE signals (
            id TEXT PRIMARY KEY, source TEXT, stream TEXT, kind TEXT, actor TEXT,
            ingested_by TEXT, product TEXT, refs TEXT, body TEXT, meta TEXT,
            privacy_class TEXT, produced_at TEXT, ingested_at TEXT,
            embedded_at TEXT, dedup_key TEXT, acting_human TEXT);
        CREATE TABLE signal_acks (
            id TEXT PRIMARY KEY, signal_id TEXT, identity TEXT, acked_at TEXT);
        CREATE UNIQUE INDEX uq_signal_acks_signal_identity
            ON signal_acks (signal_id, identity);
    """)
    conn.execute(
        "INSERT INTO signals (id, product, kind, body, privacy_class, ingested_at)"
        " VALUES ('sig_customer','vivy','note','a customer wrote this','customer',"
        "'2026-08-01 07:31:00.000000')")
    conn.execute(
        "INSERT INTO signals (id, product, kind, body, privacy_class, ingested_at)"
        " VALUES ('sig_internal','vivy','note','an internal note','internal',"
        "'2026-08-01 07:30:00.000000')")
    conn.commit()
    conn.close()
    tokens, clients = tmp_path / "api-tokens.json", tmp_path / "client-tokens.json"
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(identity, "CLIENT_TOKENS", clients)

    def write(agent_entries=None, client_entries=None):
        tokens.write_text(json.dumps({
            AGENT: {"name": "plain", "products": ["*"]},
            **(agent_entries or {})}))
        clients.write_text(json.dumps({
            CLIENT: {"name": "orient", "products": ["*"]},
            **(client_entries or {})}))
    write()
    return TestClient(libapp.app), write, tokens, clients


# --- grants and withholdings reach the running app -------------------------


def test_an_entry_grant_reaches_a_route_its_role_does_not_admit(env):
    client, write, *_ = env
    write({GRANTED: {"name": "g", "products": ["*"],
                     "grants": ["signal.customer"]}})
    assert client.get("/signals/sig_customer", headers=_h(AGENT)).status_code == 404
    r = client.get("/signals/sig_customer", headers=_h(GRANTED))
    assert r.status_code == 200, r.text


def test_an_entry_withholding_is_refused_a_route_its_role_admits(env):
    client, write, *_ = env
    write(client_entries={WITHHELD: {"name": "bosun", "products": ["*"],
                                     "withholds": ["signal.ack"]}})
    ok = client.request("POST", "/signals/sig_internal/ack",
                        json={"acting_human": "operator"}, headers=_h(CLIENT))
    assert ok.status_code != 403, ok.text
    r = client.request("POST", "/signals/sig_internal/ack",
                       json={"acting_human": "operator"}, headers=_h(WITHHELD))
    assert r.status_code == 403
    assert "signal.ack" in r.json()["error"]


def test_whoami_separates_role_from_entry(env):
    client, write, *_ = env
    write({GRANTED: {"name": "g", "products": ["*"],
                     "grants": ["signal.customer"]}},
          {WITHHELD: {"name": "bosun", "products": ["*"],
                      "withholds": ["signal.ack"]}})
    g = client.get("/whoami/capabilities", headers=_h(GRANTED)).json()
    assert g["from_entry"] == ["signal.customer"]
    assert "signal.customer" in g["capabilities"]
    assert "signal.customer" not in g["from_role"]
    w = client.get("/whoami/capabilities", headers=_h(WITHHELD)).json()
    assert w["withheld_by_entry"] == ["signal.ack"]
    assert "signal.ack" not in w["capabilities"]
    assert "signal.ack" not in w["from_role"]
    # /whoami keeps its exact shape for callers that compare it whole.
    assert set(client.get("/whoami", headers=_h(GRANTED)).json()) == {
        "actor", "kind", "products", "capabilities"}
    assert client.get("/whoami/capabilities?actor=x",
                      headers=_h(GRANTED)).status_code == 400


# --- refusal at load -------------------------------------------------------


def test_an_unknown_capability_in_the_client_store_stops_every_request(
        env, capfd):
    client, write, *_ = env
    write(client_entries={WITHHELD: {"name": "bosun", "products": ["*"],
                                     "grants": ["no.such.cap"]}})
    for path in ("/health", "/whoami"):
        r = client.get(path, headers=_h(AGENT))
        assert r.status_code == 503, path
    err = capfd.readouterr().err
    assert "'bosun'" in err and "'no.such.cap'" in err
    assert WITHHELD not in err  # the bearer token is never printed


def test_an_unknown_capability_in_withholds_resolves_serves_and_is_reported(
        env):
    """The counterpart to the `grants` case above (issue_01M339339P1WA80Q218SSTYKMT):
    a `grants` entry naming a capability no role defines is a real error, but a
    `withholds` entry naming one asks for something already true — nobody can
    hold a capability no role defines, so withholding it is a no-op. Removing
    the last role that defines a capability a live credential withholds must
    not take the whole store down."""
    client, write, *_ = env
    write(client_entries={WITHHELD: {"name": "bosun", "products": ["*"],
                                     "withholds": ["no.such.cap"]}})
    assert identity.credential_store_error() is None
    for path in ("/health", "/whoami"):
        assert client.get(path, headers=_h(AGENT)).status_code == 200, path
    body = client.get("/whoami/capabilities", headers=_h(WITHHELD)).json()
    assert body["dead_withholds"] == ["no.such.cap"]
    # Ignored for authorization: no role ever held it, so withholding it
    # removes nothing from the resolved set.
    assert "no.such.cap" not in body["capabilities"]


def test_an_unknown_withhold_is_logged_once_at_load(env, capfd):
    """`describe_credentials` is the boot-time report (`app.lifespan` calls it
    once); confirms the dead withhold reaches that line rather than only the
    per-request `dead_withholds` field, and that it is silent until then —
    `capfd` here only sees what this call itself prints."""
    _, write, *_ = env
    write(client_entries={WITHHELD: {"name": "bosun", "products": ["*"],
                                     "withholds": ["no.such.cap"]}})
    capfd.readouterr()  # drain app startup output from the `env` fixture
    line = identity.describe_credentials()
    # Not the store-unresolvable error path (that message also names the entry
    # and the capability, so asserting their presence alone would not tell the
    # two apart).
    assert "ERROR" not in line
    assert "no.such.cap" in line and "'bosun'" in line
    assert WITHHELD not in line  # the bearer token is never printed


@pytest.mark.parametrize("kind", [None, "system", "customer_reader", "orchestrator"])
def test_an_unknown_capability_in_withholds_on_the_agent_store_is_refused(
        env, kind):
    """The relaxation above is scoped to `kind == "approved_client"`
    (security-reviewer, PR #187 round 1): `api-tokens.json` already fails
    safely per entry, with no store-wide outage to guard against, so a
    `withholds` entry there naming a capability no role defines must keep
    refusing that one credential exactly as it did on `origin/main` before
    this PR, covering every non-client kind (`kind=None` defaults to
    `agent`)."""
    client, write, *_ = env
    entry = {"name": "sneaky", "products": ["*"], "withholds": ["no.such.cap"]}
    if kind:
        entry["kind"] = kind
    write({GRANTED: entry})
    assert client.get("/whoami", headers=_h(GRANTED)).status_code == 401
    # The entry alone is refused; every other token keeps resolving.
    assert client.get("/whoami", headers=_h(AGENT)).status_code == 200


@pytest.mark.parametrize(
    "kind", ["agent", "system", "customer_reader", "orchestrator",
             "approved_client"])
def test_a_known_withhold_still_resolves_in_every_kind(kind):
    """The kind-gate above only changes the UNKNOWN-capability case; a
    `withholds` entry naming a capability some role defines is carried into
    the returned frozenset unchanged, in the client store and in
    api-tokens.json alike, so `resolve_capabilities`'s subtraction still
    narrows the resolved capability set in both."""
    _, withholds = identity._entry_capabilities(
        {"withholds": ["item.read"]}, kind, "x")
    assert withholds == frozenset({"item.read"})


@pytest.mark.parametrize("body", ["not json", "[1, 2]"])
def test_a_broken_client_file_zeroes_clients_only_as_it_always_did(env, body):
    """Existing behaviour, deliberately kept (see `_client_tokens`): nothing is
    widened, so this is not the fatal case."""
    client, _, _, clients = env
    clients.write_text(body)
    assert client.get("/whoami", headers=_h(AGENT)).status_code == 200
    assert client.get("/whoami", headers=_h(CLIENT)).status_code == 401


def test_a_missing_client_file_is_an_empty_store_not_an_outage(env):
    client, _, _, clients = env
    clients.unlink()
    assert client.get("/whoami", headers=_h(AGENT)).status_code == 200
    assert client.get("/whoami", headers=_h(CLIENT)).status_code == 401


def test_startup_reports_a_store_it_cannot_resolve_and_boots(env):
    """The boot line names the fault instead of raising out of the lifespan;
    the 503 on every request is pinned above."""
    _, write, *_ = env
    write(client_entries={WITHHELD: {"name": "x", "grants": ["no.such.cap"]}})
    line = identity.describe_credentials()
    assert "ERROR" in line and "no.such.cap" in line and "503" in line
    assert WITHHELD not in line


def test_a_dead_withholding_is_reported():
    ident = identity.Identity(actor="a", kind="system",
                              withholds=frozenset({"policy.write"}))
    rep = identity.capability_report(ident)
    assert rep["dead_withholds"] == ["policy.write"]
    assert rep["withheld_by_entry"] == []


def test_the_nameable_roles_already_reach_everything_an_entry_can_grant():
    """WHY THIS EXISTS: `_known_capabilities()` unions every role including
    `approved_client`. A capability added to that set alone would become
    grantable from an `api-tokens.json` entry (Orient's file), widening what
    that file can reach without anyone deciding to. If this fails, you are
    doing that; decide it on purpose, or add the capability to a nameable role
    or to HUMAN_RESERVED_CAPS deliberately.

    `GRANT_ONLY_CAPS` is the one deliberate exception, excluded here the same
    way `HUMAN_RESERVED_CAPS` is: `machine.verify` is known and grantable, but
    on no role at all (its own comment in `identity.py` says why), so no
    nameable role's set reaching it would be the gap, not the proof."""
    reachable = frozenset().union(
        *(identity.ROLE_CAPS[k] for k in identity.NAMEABLE_KINDS))
    grantable = (identity._known_capabilities() - identity.HUMAN_RESERVED_CAPS
                 - identity.GRANT_ONLY_CAPS)
    assert grantable <= reachable, sorted(grantable - reachable)


def test_machine_verify_is_on_no_role_and_is_not_human_reserved():
    """`machine.verify` (platform/decisions/0066): known, so an entry can
    `grant` it, but absent from every `ROLE_CAPS` value including
    `approved_client` — it reaches a credential only through its own entry's
    `grants`, never through a role."""
    assert "machine.verify" in identity._known_capabilities()
    assert "machine.verify" not in identity.HUMAN_RESERVED_CAPS
    for role, caps in identity.ROLE_CAPS.items():
        assert "machine.verify" not in caps, role


def test_an_agent_entry_can_grant_machine_verify(env):
    client, write, *_ = env
    write({GRANTED: {"name": "bosun", "products": ["*"],
                     "grants": ["machine.verify"]}})
    assert "machine.verify" not in client.get(
        "/whoami/capabilities", headers=_h(AGENT)).json()["capabilities"]
    g = client.get("/whoami/capabilities", headers=_h(GRANTED)).json()
    assert "machine.verify" in g["capabilities"]
    assert g["from_entry"] == ["machine.verify"]


@pytest.mark.parametrize("bad", [{"grants": "signal.ack"}, {"grants": [1]},
                                 {"withholds": {"a": 1}}])
def test_a_grant_field_of_the_wrong_shape_is_refused(env, bad):
    _, write, *_ = env
    write(client_entries={WITHHELD: {"name": "x", **bad}})
    assert identity.credential_store_error() is not None


@pytest.mark.parametrize("field", ["grants", "withholds"])
@pytest.mark.parametrize("cap", sorted(identity.HUMAN_RESERVED_CAPS))
@pytest.mark.parametrize("kind", [None, "system", "orchestrator"])
def test_an_agent_or_system_entry_naming_a_human_reserved_cap_is_refused(
        env, kind, cap, field):
    """Through the file Orient can write: the entry authenticates nobody, the
    service stays up, and every other token keeps resolving."""
    client, write, *_ = env
    entry = {"name": "sneaky", "products": ["*"], field: [cap]}
    if kind:
        entry["kind"] = kind
    write({GRANTED: entry})
    assert client.get("/whoami", headers=_h(GRANTED)).status_code == 401
    assert client.get("/whoami", headers=_h(AGENT)).status_code == 200


@pytest.mark.parametrize("kind", [None, "system", "agent"])
def test_a_non_orchestrator_entry_naming_roster_write_in_grants_is_refused(
        env, kind):
    """`roster.write` is orchestrator-only at the entry level too
    (`identity._entry_capabilities`): the github-capture poller resolves to
    `kind: system`, and an entry granting it there would let that poller
    change which agents exist."""
    client, write, *_ = env
    entry = {"name": "sneaky", "products": ["*"], "grants": ["roster.write"]}
    if kind:
        entry["kind"] = kind
    write({GRANTED: entry})
    assert client.get("/whoami", headers=_h(GRANTED)).status_code == 401
    assert client.get("/whoami", headers=_h(AGENT)).status_code == 200


def test_an_approved_client_entry_naming_roster_write_in_grants_is_refused(
        env):
    """The client store is fatal to the whole service on an unresolvable
    entry (`_client_tokens`'s own docstring), the same as any other
    unresolvable grant there — this is not a special case."""
    client, write, *_ = env
    write(client_entries={WITHHELD: {"name": "bosun", "products": ["*"],
                                     "grants": ["roster.write"]}})
    assert client.get("/whoami", headers=_h(WITHHELD)).status_code == 503
    assert client.get("/whoami", headers=_h(CLIENT)).status_code == 503


def test_an_orchestrator_entry_naming_roster_write_in_grants_loads(env):
    client, write, *_ = env
    write({GRANTED: {"name": "orc", "products": ["*"], "kind": "orchestrator",
                     "grants": ["roster.write"]}})
    r = client.get("/whoami", headers=_h(GRANTED))
    assert r.status_code == 200, r.text


def test_roster_write_in_withholds_is_unaffected_on_a_non_orchestrator_entry(
        env):
    """`withholds` only narrows, so naming `roster.write` there on a kind
    that never holds it is a no-op, not a refusal — unlike `grants`."""
    client, write, *_ = env
    write({GRANTED: {"name": "sys", "products": ["*"], "kind": "system",
                     "withholds": ["roster.write"]}})
    assert client.get("/whoami", headers=_h(GRANTED)).status_code == 200


def test_a_bad_entry_in_orients_file_never_halts_the_service(env):
    client, write, *_ = env
    write({GRANTED: {"name": "g", "grants": ["no.such.cap"]}})
    assert client.get("/health").status_code == 200
    assert client.get("/whoami", headers=_h(GRANTED)).status_code == 401
    assert client.get("/whoami", headers=_h(AGENT)).status_code == 200


def test_a_human_reserved_grant_cannot_be_carried_by_a_constructed_identity():
    ident = identity.Identity(actor="a", kind="agent",
                              grants=frozenset({"signal.ack"}))
    assert not identity.authorized(ident, "signal.ack")


# --- resolution semantics --------------------------------------------------


def test_withholding_beats_granting_and_absent_stays_denied():
    ident = identity.Identity(actor="a", kind="agent",
                              grants=frozenset({"signal.customer"}),
                              withholds=frozenset({"signal.customer"}))
    assert not identity.authorized(ident, "signal.customer")
    assert not identity.authorized(identity.Identity("a", "agent"),
                                   "signal.customer")
    assert not identity.authorized(identity.Identity("a", "agent"), "made.up")


def test_the_role_table_is_consulted_only_inside_identity():
    """No route reaches `ROLE_CAPS`; resolution is one function."""
    import ast
    root = Path(identity.__file__).parent
    offenders = []
    for p in root.glob("*.py"):
        if p.name == "identity.py":
            continue
        for node in ast.walk(ast.parse(p.read_text())):
            # A real reference, not a mention in a comment or docstring.
            if ((isinstance(node, ast.Attribute) and node.attr == "ROLE_CAPS")
                    or (isinstance(node, ast.Name) and node.id == "ROLE_CAPS")):
                offenders.append(f"{p.name}:{node.lineno}")
    assert offenders == []


# --- inert for every credential that exists today --------------------------


def test_every_live_credential_resolves_consistently(monkeypatch):
    """READ-ONLY over the real stores, at `identity.CLIENT_TOKENS` and
    `identity.TOKENS` (so `LIBRARY_CLIENT_TOKENS` moves it). Skipped where the
    client store does not exist. Asserts invariants that survive the operator
    adding grants and withholdings to live entries: every entry resolves, the
    two provenance sets partition the caps, and an entry naming neither field
    resolves to exactly its role's set less what its `holds_reserved`, if it
    has one, excludes (the change is inert for it)."""
    if not identity.CLIENT_TOKENS.exists():
        pytest.skip("no live client store on this machine")
    checked = {"client": 0, "agent": 0}
    client_tokens = identity._client_tokens()
    stores = [("client", client_tokens)]
    if identity.TOKENS.exists():
        stores.append(("agent", identity._tokens()))
    for label, entries in stores:
        for token, meta in entries.items():
            if not isinstance(meta, dict) or not meta:
                continue
            ident = identity.resolve(f"Bearer {token}")
            if label == "client":
                # A client entry that authenticates nobody is the failure this
                # test exists to see, so it is asserted rather than skipped.
                assert ident is not None, (
                    f"client entry {identity._entry_label(token, meta)} "
                    "resolves to no identity")
            elif ident is None or token in client_tokens:
                continue
            r = identity.resolve_capabilities(ident)
            assert r.from_role | r.from_entry == r.caps
            assert r.from_role & r.from_entry == frozenset()
            if not meta.get("grants") and not meta.get("withholds"):
                # `holds_reserved` is the third entry delta: a live approved
                # client with `holds_reserved: []` holds its role minus every
                # human-reserved capability, and that is its whole difference.
                # The expected set is built from the entry and the constants,
                # not from `r`, so the function is not checked against itself.
                expected = identity.ROLE_CAPS[ident.kind]
                if "holds_reserved" in meta:
                    expected = expected - (identity.HUMAN_RESERVED_CAPS
                                           - set(meta["holds_reserved"]))
                assert r.caps == expected
                assert r.from_entry == frozenset() and r.withheld == frozenset()
            checked[label] += 1
    # Counted per store: an empty client file must not pass on agent rows.
    assert checked["client"] > 0, (
        "the client store is present but no entry in it was examined")


def test_the_human_reserved_set_is_what_the_approved_client_union_reads():
    assert identity.HUMAN_RESERVED_CAPS <= identity.ROLE_CAPS["approved_client"]
    assert identity.HUMAN_RESERVED_CAPS.isdisjoint(identity.ROLE_CAPS["system"])
    assert (identity.ROLE_CAPS["approved_client"]
            == identity.ROLE_CAPS["system"] | identity.HUMAN_RESERVED_CAPS)


# --- holds_reserved: an allowlist of the human-reserved capabilities ---------

HOLDER = "holds-reserved-client-token"


def _caps(client, token):
    return client.get("/whoami/capabilities", headers=_h(token)).json()


def test_holds_reserved_empty_holds_no_reserved_capability(env):
    client, write, *_ = env
    write(client_entries={HOLDER: {"name": "bosun", "products": ["*"],
                                   "holds_reserved": []}})
    body = _caps(client, HOLDER)
    assert not set(body["capabilities"]) & identity.HUMAN_RESERVED_CAPS
    assert body["excluded_by_holds_reserved"] == sorted(
        identity.HUMAN_RESERVED_CAPS)
    assert body["withheld_by_entry"] == []
    # It keeps the non-reserved capabilities an approved_client has.
    assert set(body["capabilities"]) == (identity.ROLE_CAPS["system"])


def test_holds_reserved_stays_empty_after_a_reserved_capability_is_added(
        env, monkeypatch):
    client, write, *_ = env
    write(client_entries={HOLDER: {"name": "bosun", "products": ["*"],
                                   "holds_reserved": []}})
    monkeypatch.setattr(identity, "HUMAN_RESERVED_CAPS",
                        identity.HUMAN_RESERVED_CAPS | {"future.cap"})
    monkeypatch.setitem(identity.ROLE_CAPS, "approved_client",
                        identity.ROLE_CAPS["approved_client"] | {"future.cap"})
    # The unrestricted client picks the new capability up, which is the
    # widening holds_reserved exists to prevent.
    assert "future.cap" in _caps(client, CLIENT)["capabilities"]
    body = _caps(client, HOLDER)
    assert "future.cap" not in body["capabilities"]
    assert "future.cap" in body["excluded_by_holds_reserved"]


def test_holds_reserved_lists_exactly_the_reserved_capabilities_held(env):
    client, write, *_ = env
    write(client_entries={HOLDER: {"name": "deck", "products": ["*"],
                                   "holds_reserved": ["epic.approve"]}})
    body = _caps(client, HOLDER)
    assert set(body["capabilities"]) & identity.HUMAN_RESERVED_CAPS == {
        "epic.approve"}
    assert "epic.approve" not in body["excluded_by_holds_reserved"]


def test_grants_cannot_re_add_a_capability_holds_reserved_excludes(env):
    # The exclusion is subtracted after `grants` is added, and that order is the
    # whole property: subtracting first would hand this credential epic.approve
    # back through `grants` while every other test here still passed.
    client, write, *_ = env
    write(client_entries={HOLDER: {"name": "bosun", "products": ["*"],
                                   "holds_reserved": [],
                                   "grants": ["epic.approve"]}})
    body = _caps(client, HOLDER)
    assert "epic.approve" not in body["capabilities"]
    assert "epic.approve" in body["excluded_by_holds_reserved"]


def test_a_reserved_route_refuses_a_credential_that_holds_none(env):
    # `/whoami/capabilities` reports the computation; this asserts the gate a
    # reserved route actually calls refuses it too.
    client, write, *_ = env
    write(client_entries={HOLDER: {"name": "bosun", "products": ["*"],
                                   "holds_reserved": []}})
    for route, cap in (("/items/no-such-item/model", "item.model"),
                       ("/items/no-such-item/approve", "epic.approve")):
        r = client.post(route, headers=_h(HOLDER),
                        json={"acting_human": "operator", "model": "x"})
        assert r.status_code == 403, (route, r.text)
        assert cap in r.json()["error"], (route, r.text)


def test_an_entry_without_holds_reserved_is_unchanged(env):
    client, *_ = env
    body = _caps(client, CLIENT)
    assert set(body["capabilities"]) == identity.ROLE_CAPS["approved_client"]
    assert body["excluded_by_holds_reserved"] == []


@pytest.mark.parametrize("entry", [
    {"holds_reserved": ["no.such.cap"]},
    {"holds_reserved": ["item.read"]},  # known, but not human-reserved
    {"holds_reserved": "epic.approve"},
    {"holds_reserved": [1]},
    {"holds_reserved": None},
    {"holds_reserved": ["epic.approve"], "withholds": ["epic.approve"]},
])
def test_a_bad_holds_reserved_on_a_client_stops_every_request(env, entry):
    client, write, *_ = env
    write(client_entries={HOLDER: {"name": "bosun", "products": ["*"], **entry}})
    for path in ("/health", "/whoami"):
        assert client.get(path, headers=_h(AGENT)).status_code == 503, path


@pytest.mark.parametrize("kind", ["agent", "system", "orchestrator",
                                  "customer_reader"])
def test_holds_reserved_on_a_non_client_entry_refuses_that_entry(env, kind):
    client, write, *_ = env
    write({GRANTED: {"name": "g", "products": ["*"], "kind": kind,
                     "holds_reserved": []}})
    assert client.get("/whoami", headers=_h(GRANTED)).status_code == 401
    assert client.get("/whoami", headers=_h(AGENT)).status_code == 200
