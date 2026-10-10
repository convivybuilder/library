"""A credential that may READ customer content without being able to CREATE it.

Those two capabilities are the one write `agent` and `system` are designed to
differ on, and until this role existed they were welded together: `agent` had
neither, `system` had both, and capabilities are granted as whole roles. A
credential needing to read captured customer mail could only get there by also
gaining the ability to mint customer-class rows.

**The gap was live.** The Gmail capture job writes every captured message
`privacy_class = customer`, so all captured mail is invisible to every
`agent`-role credential — and invisible in the direction that reads as "not
captured" rather than "not permitted", because the read surface withholds rather
than refuses. That is correct behaviour and it is why the gap cost an afternoon
before anyone reached for the token file.

The tests here pin both halves: what the role may now do, and what it still may
not — plus that a token file cannot mint an `approved_client` by naming one.
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

AGENT = "agent-token"
READER = "customer-reader-token"
SYSTEM = "system-token"


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
        CREATE TABLE black_box_registrations (
            id INTEGER PRIMARY KEY, crew TEXT NOT NULL UNIQUE,
            model TEXT NOT NULL, gateway TEXT NOT NULL,
            created_by TEXT NOT NULL, created_at TEXT NOT NULL,
            updated_by TEXT NOT NULL, updated_at TEXT NOT NULL);
    """)
    conn.executemany(
        "INSERT INTO signals (id, product, kind, source, actor, ingested_by, "
        "body, refs, meta, privacy_class, ingested_at, dedup_key) "
        "VALUES (?,?,'note','gmail','a','capture',?,'{}','{}',?,?,?)",
        [("sig_internal", "vivy", "an internal note", "internal",
          "2026-08-01 07:30:00.000000", "k-int"),
         ("sig_customer", "vivy", "a captured customer email", "customer",
          "2026-08-01 08:30:00.000000", "k-cust")])
    conn.commit()
    conn.close()

    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        AGENT: {"name": "an-agent", "products": ["vivy"]},
        READER: {"name": "commercial", "products": ["vivy"],
                 "kind": "customer_reader"},
        SYSTEM: {"name": "capture", "products": ["*"], "kind": "system"},
    }))
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "TOKENS", tokens)
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


def _ids(r):
    return {s["id"] for s in r.json()["signals"]}


def _list(client, token):
    return client.get("/signals", params={"product": "vivy"}, headers=_h(token))


# --- the capability delta, asserted directly ----------------------------------


def test_the_role_adds_exactly_one_capability_over_agent(client):
    """A union over `agent` rather than a copy, so the two cannot drift. If this
    ever reports more than one added capability, something widened the role by
    accident."""
    added = identity.ROLE_CAPS["customer_reader"] - identity.ROLE_CAPS["agent"]
    assert added == {"signal.customer"}


def test_the_role_cannot_create_customer_content(client):
    """**The whole point of the role.** `signal.create_customer` is the one
    write `agent` and `system` are designed to differ on, and reading customer
    mail must not carry it. Structural — the union is over `agent`, which lacks
    it — and pinned here anyway, because structural is not asserted."""
    assert "signal.create_customer" not in identity.ROLE_CAPS["customer_reader"]


# --- what it can now do -------------------------------------------------------


def test_the_reader_sees_customer_rows_an_agent_cannot(client):
    assert _ids(_list(client, AGENT)) == {"sig_internal"}
    assert _ids(_list(client, READER)) == {"sig_internal", "sig_customer"}


def test_the_reader_can_fetch_a_customer_row_directly(client):
    """An agent gets 404 rather than 403 here, deliberately: a 403 would confirm
    the row exists to an identity that may not know it does."""
    assert client.get("/signals/sig_customer",
                      headers=_h(AGENT)).status_code == 404
    r = client.get("/signals/sig_customer", headers=_h(READER))
    assert r.status_code == 200 and r.json()["privacy_class"] == "customer"


def test_the_readers_total_counts_what_it_may_see(client):
    """The customer wall runs in the query rather than as a filter afterwards,
    so a `total` must move with the role rather than counting rows the caller
    cannot fetch."""
    assert _list(client, AGENT).json()["total"] == 1
    assert _list(client, READER).json()["total"] == 2


# --- what it still cannot do --------------------------------------------------


def _capture(client, token, privacy_class):
    return client.post("/signals", json={
        "source": "gmail", "kind": "note", "actor": "alice@example.org",
        "product": "vivy", "body": "b", "privacy_class": privacy_class,
    }, headers=_h(token))


def test_the_reader_is_refused_a_customer_class_write(client):
    r = _capture(client, READER, "customer")
    assert r.status_code == 403
    assert "signal.create_customer" in r.json()["error"]


def test_the_reader_keeps_the_ordinary_writes_an_agent_has(client):
    """It is an agent that may additionally read customer content, not a
    read-only credential — an internal-class capture still works."""
    assert _capture(client, READER, "internal").status_code == 201


def test_the_reader_cannot_attest_a_human(client):
    """`acting_human` stays approved-client-only. Reading customer content and
    vouching for a person are different claims."""
    r = client.post("/signals", json={
        "source": "gmail", "kind": "note", "actor": "a", "product": "vivy",
        "body": "b", "acting_human": "operator@example.com"}, headers=_h(READER))
    assert r.status_code == 403 and "approved-client" in r.json()["error"]


# --- what a token file may and may not name -----------------------------------


def test_a_token_file_cannot_mint_an_approved_client(tmp_path, monkeypatch):
    """**`api-tokens.json` is Orient's live credential store.** A kind there that
    resolved to `approved_client` would let that file grant the human-reserved
    capabilities ADR-0038 puts behind a separate file the Library alone reads."""
    tokens = tmp_path / "t.json"
    tokens.write_text(json.dumps({
        "sneaky": {"name": "x", "products": ["vivy"], "kind": "approved_client"}}))
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(identity, "CLIENT_TOKENS", tmp_path / "absent.json")
    ident = identity.resolve("Bearer sneaky")
    assert ident.kind == "agent"


@pytest.mark.parametrize("claimed", [
    # Strings that are simply not the right string.
    "customer_readerr", "CUSTOMER_READER", " customer_reader", "admin",
    "human", "", None, "Approved_Client",
    # **AND VALUES THAT ARE NOT STRINGS AT ALL** (security-reviewer, PR #49).
    # `in` against a frozenset hashes the probe, so an unhashable one raises
    # `TypeError` on the auth path — a 500 on every request that token presents,
    # not a fallback. The first version of this parametrization covered only
    # strings and `None`, which is exactly why the defect reached review.
    ["system"], {"kind": "system"}, {"a": 1}, 5, 0, True, False, 1.5])
def test_an_unrecognised_kind_falls_back_to_agent(tmp_path, monkeypatch, claimed):
    """Fail-closed: a typo in the token file must not mint a role with more
    capability than the one intended, and `agent` is the least of them."""
    entry = {"name": "x", "products": ["vivy"]}
    if claimed is not None:
        entry["kind"] = claimed
    tokens = tmp_path / "t.json"
    tokens.write_text(json.dumps({"tok": entry}))
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(identity, "CLIENT_TOKENS", tmp_path / "absent.json")
    assert identity.resolve("Bearer tok").kind == "agent"


def test_the_nameable_set_excludes_the_client_role():
    """Enumerated rather than derived from ROLE_CAPS, so adding a role does not
    silently make it nameable by a token file."""
    assert "approved_client" not in identity.NAMEABLE_KINDS
    assert identity.NAMEABLE_KINDS <= set(identity.ROLE_CAPS)


# --- every field a credential file can malform ---------------------------------
#
# `api-tokens.json` is Orient's live credential store and `client-tokens.json` is
# the Library's own; neither is written by this service. A malformed entry is
# data this code has to survive, and `kind` was only the field somebody happened
# to report (security-reviewer, PR #49 rounds 1 and 2).


def _resolve_with(tmp_path, monkeypatch, entry, *, client=False):
    toks = tmp_path / ("c.json" if client else "t.json")
    toks.write_text(json.dumps({"tok": entry}))
    other = tmp_path / "absent.json"
    monkeypatch.setattr(identity, "CLIENT_TOKENS", toks if client else other)
    monkeypatch.setattr(identity, "TOKENS", other if client else toks)
    return identity.resolve("Bearer tok")


NON_LIST = [5, 0, None, True, False, 3.14, "vivy", {"a": "vivy"}, {}]


@pytest.mark.parametrize("products", NON_LIST)
@pytest.mark.parametrize("client", [False, True])
def test_a_malformed_products_scopes_to_nothing_rather_than_crashing(
        tmp_path, monkeypatch, products, client):
    """`frozenset(5)` raises `TypeError: not iterable`, uncaught on the auth
    path — a 500 on every request that token presents, where the docstring
    promises a fail-closed refusal. Both credential files, since both build the
    same frozenset from the same untrusted shape."""
    ident = _resolve_with(tmp_path, monkeypatch,
                          {"name": "x", "products": products}, client=client)
    assert ident is not None
    assert ident.products == frozenset()
    # ...and scoped to nothing means seeing nothing, not seeing everything.
    assert identity.in_scope(ident, "vivy") is False


@pytest.mark.parametrize("client", [False, True])
def test_a_string_products_does_not_become_a_set_of_letters(
        tmp_path, monkeypatch, client):
    """**The quiet one.** `frozenset("reports")` is `{'r','e','p','o','t','s'}`,
    so a plausible typo scopes a token to six single-letter products: it DENIES
    the product actually meant and GRANTS ones nobody intended. It fails in both
    directions at once and reports neither."""
    ident = _resolve_with(tmp_path, monkeypatch,
                          {"name": "x", "products": "reports"}, client=client)
    assert ident.products == frozenset()
    assert identity.in_scope(ident, "r") is False
    assert identity.in_scope(ident, "reports") is False


def test_a_list_with_one_bad_entry_keeps_the_readable_ones(tmp_path, monkeypatch):
    """Dropping the whole list would turn a typo in one product name into a
    total outage for that token."""
    ident = _resolve_with(tmp_path, monkeypatch,
                          {"name": "x", "products": ["vivy", 5, None, "keel"]})
    assert ident.products == {"vivy", "keel"}


@pytest.mark.parametrize("name", [5, None, True, {"a": 1}, ["x"], 3.14])
@pytest.mark.parametrize("client", [False, True])
def test_a_malformed_name_becomes_unknown_rather_than_a_write_time_500(
        tmp_path, monkeypatch, name, client):
    """`actor` reaches SQLite's parameter binding on ~20 write paths, so a
    non-string here does not fail where it would be legible — it fails on the
    first authenticated WRITE as an `InterfaceError`, reading as a database
    fault rather than a bad credential.

    `"unknown"` rather than `str(name)`: stringifying would mint an actor named
    `{'a': 1}` and write it into an audit column."""
    ident = _resolve_with(tmp_path, monkeypatch,
                          {"name": name, "products": ["vivy"]}, client=client)
    assert ident.actor == "unknown"
    assert isinstance(ident.actor, str)


def test_a_wildcard_scope_still_works(tmp_path, monkeypatch):
    """The guard must not break the shape that actually ships."""
    ident = _resolve_with(tmp_path, monkeypatch,
                          {"name": "capture", "products": ["*"], "kind": "system"})
    assert ident.kind == "system" and identity.in_scope(ident, "anything") is True


# --- the ENTRY's shape and the FILE's shape ------------------------------------
#
# Guarding the fields inside an entry does nothing if the entry is not a mapping,
# or the file is not an object (security-reviewer, PR #49 round 3). Both parse as
# valid JSON, so `_tokens()`'s existing `except (OSError, ValueError)` never
# fires and the `.get()` chain raises `AttributeError` instead — a 500 where a
# malformed credential must be a 401.


def _resolve_raw(tmp_path, monkeypatch, raw_json, *, client=False):
    toks = tmp_path / ("c.json" if client else "t.json")
    toks.write_text(raw_json)
    other = tmp_path / "absent.json"
    monkeypatch.setattr(identity, "CLIENT_TOKENS", toks if client else other)
    monkeypatch.setattr(identity, "TOKENS", other if client else toks)
    return identity.resolve("Bearer tok")


@pytest.mark.parametrize("raw", [
    '{"tok": "oops"}', '{"tok": ["a", "b"]}', '{"tok": true}', '{"tok": 5}',
    '{"tok": 3.14}', '{"tok": []}', '{"tok": ""}', '{"tok": null}',
])
@pytest.mark.parametrize("client", [False, True])
def test_a_token_mapping_to_a_non_object_authenticates_nobody(
        tmp_path, monkeypatch, raw, client):
    """A truthy non-dict passed the old `if meta:` and then raised on `.get`."""
    assert _resolve_raw(tmp_path, monkeypatch, raw, client=client) is None


@pytest.mark.parametrize("raw", ['[1, 2, 3]', '"a string"', '5', 'true', 'null',
                                 '[]', '[{"tok": {"name": "x"}}]'])
@pytest.mark.parametrize("client", [False, True])
def test_a_credential_file_that_is_not_an_object_refuses_everyone(
        tmp_path, monkeypatch, raw, client):
    """**The sharper case.** One hand-edit turning Orient's live credential
    store's top level into an array takes auth down for EVERY token with a 500,
    which is worse than the clean 401 an unreadable file already produces. A
    wrong-shaped file now behaves exactly like an unreadable one."""
    assert _resolve_raw(tmp_path, monkeypatch, raw, client=client) is None


def test_a_wrong_shaped_client_file_does_not_disturb_agent_auth(
        tmp_path, monkeypatch):
    """`_client_tokens()`'s docstring promises a broken client file zeroes out
    approved clients ONLY, leaving agent and system resolving as before. That
    has to survive the new shape guard."""
    (tmp_path / "c.json").write_text("[1, 2, 3]")
    (tmp_path / "t.json").write_text(json.dumps({
        "tok": {"name": "an-agent", "products": ["vivy"]}}))
    monkeypatch.setattr(identity, "CLIENT_TOKENS", tmp_path / "c.json")
    monkeypatch.setattr(identity, "TOKENS", tmp_path / "t.json")
    ident = identity.resolve("Bearer tok")
    assert ident is not None and ident.kind == "agent"
