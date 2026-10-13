"""Clause 8 of the individuation epic (issue_01M092R482KSQHKKVG4XJ8X257),
built for issue_01M3VN94VGM9F88SDS6FRN5WY5: "A black-box crew's content is
absent from the Library: a search across every vault for that crew's agent
names, personas and transcripts returns nothing, while the tool
permissions, model and gateway bindings it declared are present and
auditable. Tested against a crew that a decision names a black box and that
no registry entry or repository list records."

`tests/test_black_box_registration.py` already proves the registration
route itself discloses no content. This file is the wider claim: given a
registered black box, nothing it might have contributed ever reaches a
read surface, while its declared bindings do.

**`ask` is the crew (`platform/decisions/0043-black-box-crews`).** CFO has
a registry entry (`private: true`); Ask is "deliberately UNREGISTERED...
protected only by the instruction in `~/Convivy/ask/CLAUDE.md`" — no
registry entry and no repository list records it, exactly what the clause
asks for. The fixture's own project registry below lists only the ordinary
crews, `vivy` and `constance`, so `projects.resolve("ask")` returns `None`
for the same reason ADR-0043 calls that refusal "accidental": a readable
registry that simply never heard of the name.

**The absence claims are not taken on faith — each one is preceded by a
real attempt through the route that would have carried the content**, so a
guard that regressed would fail this file rather than leave it vacuously
green. `POST /items` and `POST /signals` are attempted with `product="ask"`
and refused; only then are the four read surfaces checked for the marker
text those attempts carried. `test_write_surface.py` already pins the
`/signals` guard directly against ADR-0043 (clause 7); this file reuses
that same guard as the precondition for a clause-8 read-side claim, not as
a second proof of clause 7 itself.

**The positive control is `vivy`.** The same four surfaces are searched for
an ordinary crew's own marker text, seeded through the same real write
routes, and found — proving a search returning nothing is this corpus
saying so, not a broken query or an empty index.

**`PUT /roster/{name}` was a real gap found while writing this test, and
is now closed** (issue_01M4PMVNHVSM4698ME4W72P0E8). `crew_registry.
roster_add` consulted neither `projects.private_crew_names()` nor
`store.black_box_registration_crews()`, so an identity holding
`roster.write` could add an `ask` agent name and `GET /roster` disclosed it,
against ADR-0043's layer 3 ("Orient's roster never renders them"). It now
withholds a name a black-box crew declared in its registration, and a
private or black-box crew's own name or that name plus a hyphen, and the
roster assertion below is an ordinary test.

**`POST /ledger-entries` (`report_spend`) was a second gap, found the same
way, and is now closed** (issue_01M4PMVGGFVGDGBSJBTFA2T3RP). It checked
only `identity.in_scope(ident, product)`, so an `agent`-kind credential
could report spend with `product: "ask"` and an `ask` agent name, and
`GET /ledger-entries` and `GET /ledgers` disclosed both. It now refuses an
unregistered or private product before any ledger is made, and the ledger
assertion below is an ordinary test.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as libapp
import identity
import projects
import protocol
import reference
import store

REPO = Path(__file__).resolve().parent.parent

WIDE = "agent-token-wide"
CLIENT = "orient-token"
ORCH = "orchestrator-token"

BLACKBOX_CREW = "ask"
BLACKBOX_AGENT_NAME = "ask-scribe"
# Two invented, disjoint-from-the-positive-control words each — never a real
# dictionary word like "persona" or "transcript" shared across the two sets,
# because `reference.build_match` ORs its terms together: a shared word
# would make the ordinary control's row match an `ask` query for the wrong
# reason, and the count-0 assertions below would pass on a broken query.
BLACKBOX_PERSONA = "velderquist tanglewood"
BLACKBOX_TRANSCRIPT = "quonsetbray emberfall"

ORDINARY_CREW = "vivy"
ORDINARY_AGENT_NAME = "vivy-scribe"
ORDINARY_PERSONA = "wrenfield glimmerstone"
ORDINARY_TRANSCRIPT = "palmcross driftwood"

# Only the ordinary crews are registered — never `ask` or `cfo` — matching
# ADR-0043's own description of Ask as unregistered. `_meta` mirrors the
# real registry's own top-level key, excluded from iteration the same way
# `projects.py`'s snapshot already skips any key starting with `_`.
REGISTRY = {"_meta": {}, "vivy": {"repos": []}, "constance": {"repos": []}}


@pytest.fixture
def client(tmp_path, monkeypatch, vector_index):
    db = tmp_path / "control.db"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LIBRARY_DB": str(db)})
    assert result.returncode == 0, result.stderr

    index = tmp_path / "index.db"
    iconn = sqlite3.connect(index)
    iconn.executescript("""
        CREATE TABLE docs (
            id TEXT PRIMARY KEY, source_ref TEXT, kind TEXT, project TEXT,
            title TEXT, status TEXT, topics TEXT, audience TEXT,
            last_reviewed TEXT, updated_at TEXT, body TEXT, body_hash TEXT);
        CREATE VIRTUAL TABLE docs_fts USING fts5(
            id, title, body, topics, content='docs', content_rowid='rowid');
    """)
    # The one reference document this fixture ever writes — the positive
    # control. Nothing for `ask` is inserted here: the Reference Room has no
    # HTTP write that lands index content (`POST /reference/admission` only
    # answers "may this land", per its own docstring), so the only way its
    # content would ever appear here is through the same ingestion that
    # would have to read it out of the `knowledge` repo first — and ADR-0043
    # layer 3 is that Ask carries `kb: local`, so no central KB slice exists
    # to ingest.
    iconn.execute(
        "INSERT INTO docs (id, source_ref, kind, project, title, status, "
        "topics, audience, body, body_hash, last_reviewed, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("vivy/specs/scribe", "vivy/specs/scribe.md", "spec", "vivy",
         "the scribe's persona", "current", "[]", "[]",
         f"this document carries the {ORDINARY_PERSONA}", "deadbeef",
         "2026-08-01", "2026-08-02"))
    iconn.execute("INSERT INTO docs_fts(rowid, id, title, body, topics) "
                 "SELECT rowid, id, title, body, topics FROM docs")
    iconn.commit()
    iconn.close()

    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        WIDE: {"name": "wide-agent", "products": ["*"]},
        ORCH: {"name": "co-library", "products": ["*"],
               "kind": "orchestrator"},
    }))
    client_tokens = tmp_path / "client-tokens.json"
    client_tokens.write_text(json.dumps({
        CLIENT: {"name": "orient", "products": ["*"]},
    }))

    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps(REGISTRY))
    monkeypatch.setattr(projects, "REGISTRY_PATH", registry)
    projects.reload()

    vector_index(db)
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(reference, "INDEX_DB", index)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(identity, "CLIENT_TOKENS", client_tokens)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    yield TestClient(libapp.app)
    projects.reload()


def _h(token):
    return {"Authorization": f"Bearer {token}",
            protocol.HEADER: str(protocol.CURRENT)}


# --- declare the black box, through the real registration route -------------


def _register_ask(client):
    return client.post(
        "/black-box/registrations",
        json={"acting_human": "operator", "crew": BLACKBOX_CREW,
              "model": "sonnet", "gateway": "anthropic",
              "permissions": [
                  {"agent": BLACKBOX_AGENT_NAME, "model": "sonnet",
                   "target": "WebFetch", "decision": "allow"}]},
        headers=_h(CLIENT))


def test_registering_ask_as_a_black_box_succeeds(client):
    """The crew a decision names a black box, and that no registry entry
    records (`projects.resolve("ask")` is `None` against this fixture's own
    registry), registers exactly as `test_black_box_registration.py`'s own
    `ask` case does — the precondition every assertion below builds on."""
    r = _register_ask(client)
    assert r.status_code == 200
    assert r.json()["crew"] == BLACKBOX_CREW


# --- seed the positive control through the real write routes ----------------


def _report_ledger_spend(client, *, product, agent, model):
    return client.post(
        "/ledger-entries",
        json={"product": product, "source": "agentctl", "usd": 0.01,
              "agent": agent, "model": model},
        headers=_h(WIDE))


def _seed_ordinary_content(client):
    item = client.post(
        "/items",
        json={"product": ORDINARY_CREW, "title": "the scribe's transcript",
              "body": f"this item carries the {ORDINARY_TRANSCRIPT}",
              "severity": "medium"},
        headers=_h(WIDE))
    assert item.status_code == 201

    signal = client.post(
        "/signals",
        json={"source": "test", "kind": "note", "actor": "test",
              "product": ORDINARY_CREW,
              "body": f"this signal carries the {ORDINARY_TRANSCRIPT}"},
        headers=_h(WIDE))
    assert signal.status_code == 201

    roster = client.put(f"/roster/{ORDINARY_AGENT_NAME}", headers=_h(ORCH))
    assert roster.status_code == 200

    ledger = _report_ledger_spend(
        client, product=ORDINARY_CREW, agent=ORDINARY_AGENT_NAME,
        model=ORDINARY_TRANSCRIPT)
    assert ledger.status_code == 200


# --- attempt to carry ask's content through the same real write routes ------
# (the precondition for every absence assertion below: each one follows a
# real attempt that was refused, not an assumption that nobody tried)


def test_a_chart_house_item_for_ask_is_refused(client):
    """`ask` is unregistered, so `projects.resolve("ask")` is `None` —
    `POST /items` carries no ADR-0043 check of its own (only `/signals`
    does; see `app.py`'s own comment beside `register_black_box`), and is
    refused anyway for the registry's ordinary "no such project" reason."""
    _register_ask(client)
    r = client.post(
        "/items",
        json={"product": BLACKBOX_CREW, "title": "ask's own item",
              "body": f"this item carries the {BLACKBOX_PERSONA}",
              "severity": "medium"},
        headers=_h(WIDE))
    assert r.status_code == 400
    assert BLACKBOX_CREW in r.json()["error"]


def test_an_archives_signal_for_ask_is_refused(client):
    """Registering `ask` adds it to `store.black_box_registration_crews()`,
    which `create_signal` checks directly (ADR-0043 clause 7,
    `test_write_surface.py`'s own coverage of this guard) — refused with
    the same 404 an out-of-scope product gets, so this route is not an
    oracle for which crews are registered as black boxes."""
    _register_ask(client)
    r = client.post(
        "/signals",
        json={"source": "test", "kind": "note", "actor": "test",
              "product": BLACKBOX_CREW,
              "body": f"this signal carries the {BLACKBOX_TRANSCRIPT}"},
        headers=_h(WIDE))
    assert r.status_code == 404


# --- the bindings: present and auditable -------------------------------------


def test_asks_declared_model_and_gateway_read_back(client):
    _register_ask(client)
    r = client.get(f"/black-box/registrations/{BLACKBOX_CREW}", headers=_h(CLIENT))
    assert r.status_code == 200
    assert r.json()["model"] == "sonnet"
    assert r.json()["gateway"] == "anthropic"


def test_asks_declared_tool_permission_reads_back_through_policy_resolve(client):
    _register_ask(client)
    r = client.get(
        "/policy/resolve",
        params={"agent": BLACKBOX_AGENT_NAME, "crew": BLACKBOX_CREW,
                "model": "sonnet"},
        headers=_h(WIDE))
    assert r.status_code == 200
    assert r.json()["explicit"] == {"WebFetch": "allow"}


# --- the content: absent from every vault, with a positive control ----------


def _search(client, **params):
    return client.get("/search", params=params, headers=_h(WIDE))


def test_the_positive_control_is_found_in_every_surface(client):
    """Seeded and searched before any absence claim is trusted — the same
    corpus the `ask` queries below run against, so an empty result there
    cannot be mistaken for a broken query or an unindexed fixture."""
    _seed_ordinary_content(client)

    search_body = _search(client, q=ORDINARY_PERSONA).json()
    assert any(r["vault"] == "reference" for r in search_body["results"])

    search_body = _search(client, q=ORDINARY_TRANSCRIPT).json()
    assert any(r["vault"] == "chart-house" for r in search_body["results"])
    assert any(r["vault"] == "archives" for r in search_body["results"])

    ref = client.get("/reference/search", params={"q": ORDINARY_PERSONA},
                     headers=_h(WIDE))
    assert ref.json()["count"] >= 1

    roster = client.get("/roster", headers=_h(WIDE))
    assert ORDINARY_AGENT_NAME in {a["name"] for a in roster.json()["agents"]}

    sigsearch = client.get("/signals/search", params={"q": ORDINARY_TRANSCRIPT},
                           headers=_h(WIDE))
    assert any(s["id"] for s in sigsearch.json()["signals"])

    entries = client.get("/ledger-entries", headers=_h(WIDE)).json()["entries"]
    assert any(
        e["product"] == ORDINARY_CREW and e.get("agent") == ORDINARY_AGENT_NAME
        and e.get("model") == ORDINARY_TRANSCRIPT for e in entries)

    ledgers = client.get("/ledgers", headers=_h(WIDE)).json()["ledgers"]
    assert any(ledger["scope_id"] == ORDINARY_CREW for ledger in ledgers)


def test_unified_search_finds_nothing_of_asks(client):
    _register_ask(client)
    _seed_ordinary_content(client)
    client.post(
        "/items",
        json={"product": BLACKBOX_CREW, "title": "ask's own item",
              "body": f"this item carries the {BLACKBOX_PERSONA}",
              "severity": "medium"},
        headers=_h(WIDE))
    client.post(
        "/signals",
        json={"source": "test", "kind": "note", "actor": "test",
              "product": BLACKBOX_CREW,
              "body": f"this signal carries the {BLACKBOX_TRANSCRIPT}"},
        headers=_h(WIDE))

    # By content: the marker text that was attempted for `ask` appears in no
    # returned result, across any vault the search touches.
    for marker in (BLACKBOX_PERSONA, BLACKBOX_TRANSCRIPT):
        body = _search(client, q=marker).json()
        assert not any(
            marker in (r.get("snippet") or "") or marker in (r.get("title") or "")
            for r in body["results"]), (
            f"{marker!r} reached a search result despite the write that "
            f"carried it being refused")

    # By product: nothing in any vault carries `product == "ask"` at all,
    # since the write that would have stored it was refused.
    scoped = _search(client, q=BLACKBOX_PERSONA, product=BLACKBOX_CREW).json()
    assert scoped["results"] == []


def test_reference_search_finds_nothing_of_asks(client):
    _seed_ordinary_content(client)
    r = client.get("/reference/search", params={"q": BLACKBOX_PERSONA},
                   headers=_h(WIDE))
    assert r.json()["count"] == 0


def test_signals_search_finds_nothing_of_asks(client):
    _register_ask(client)
    _seed_ordinary_content(client)
    client.post(
        "/signals",
        json={"source": "test", "kind": "note", "actor": "test",
              "product": BLACKBOX_CREW,
              "body": f"this signal carries the {BLACKBOX_TRANSCRIPT}"},
        headers=_h(WIDE))

    body = client.get("/signals/search", params={"q": BLACKBOX_TRANSCRIPT},
                      headers=_h(WIDE)).json()
    assert not any(BLACKBOX_TRANSCRIPT in (s.get("body") or "")
                   for s in body["signals"])

    scoped = client.get(
        "/signals/search",
        params={"q": BLACKBOX_TRANSCRIPT, "product": BLACKBOX_CREW},
        headers=_h(WIDE)).json()
    assert scoped["signals"] == []


def test_ledger_entries_are_not_the_place_a_black_box_crew_should_surface(client):
    _register_ask(client)
    _seed_ordinary_content(client)
    refused = _report_ledger_spend(
        client, product=BLACKBOX_CREW, agent=BLACKBOX_AGENT_NAME,
        model=BLACKBOX_PERSONA)
    # The same 404 an out-of-scope product gets, so the refusal does not
    # say which crews are black boxes.
    assert refused.status_code == 404

    entries = client.get("/ledger-entries", headers=_h(WIDE)).json()["entries"]
    assert not any(
        e["product"] == BLACKBOX_CREW or e.get("agent") == BLACKBOX_AGENT_NAME
        or e.get("model") == BLACKBOX_PERSONA for e in entries)

    ledgers = client.get("/ledgers", headers=_h(WIDE)).json()["ledgers"]
    assert not any(ledger["scope_id"] == BLACKBOX_CREW for ledger in ledgers)


def _use_registry(tmp_path, monkeypatch, registry):
    path = tmp_path / "projects-override.json"
    path.write_text(json.dumps(registry))
    monkeypatch.setattr(projects, "REGISTRY_PATH", path)
    projects.reload()


def _ledger_scopes(client):
    return {ledger["scope_id"]
            for ledger in client.get("/ledgers", headers=_h(WIDE)).json()["ledgers"]}


def test_spend_for_a_project_the_registry_marks_private_is_refused(
        client, tmp_path, monkeypatch):
    """`ask` above is refused because the registry never heard of it. A crew
    the registry DOES list, as `private: true`, is the other way a black box
    is declared, and the refusal has to hold for it too."""
    _use_registry(tmp_path, monkeypatch,
                  {**REGISTRY, "cfo": {"repos": [], "private": True}})
    r = _report_ledger_spend(client, product="cfo", agent="cfo-scribe",
                             model="sonnet")
    assert r.status_code == 404
    assert "cfo" not in _ledger_scopes(client)


def test_spend_for_a_registered_project_that_declared_itself_a_black_box_is_refused(
        client):
    """A self-registration makes a crew a black box whatever the registry's
    `private` flag says, so `constance`, listed as ordinary here, is refused
    once it registers, while `vivy` beside it still books spend."""
    r = client.post(
        "/black-box/registrations",
        json={"acting_human": "operator", "crew": "constance",
              "model": "sonnet", "gateway": "anthropic", "permissions": []},
        headers=_h(CLIENT))
    assert r.status_code == 200
    refused = _report_ledger_spend(client, product="constance",
                                   agent="constance-scribe", model="sonnet")
    assert refused.status_code == 404
    booked = _report_ledger_spend(client, product=ORDINARY_CREW,
                                  agent=ORDINARY_AGENT_NAME, model="sonnet")
    assert booked.status_code == 200
    assert _ledger_scopes(client) == {ORDINARY_CREW}


def test_a_ledger_cannot_be_granted_to_a_black_box_crew(client):
    """`PUT /ledgers` is the other route that creates a ledger row
    (security-reviewer, PR #254 round 1). It takes the same refusal, so a
    black-box crew's name cannot reach `GET /ledgers` by a grant either,
    while an ordinary crew's grant still lands."""
    _register_ask(client)
    refused = client.put(
        "/ledgers", headers=_h(CLIENT),
        json={"scope_kind": "project", "scope_id": BLACKBOX_CREW,
              "acting_human": "operator"})
    assert refused.status_code == 404
    granted = client.put(
        "/ledgers", headers=_h(CLIENT),
        json={"scope_kind": "project", "scope_id": ORDINARY_CREW,
              "acting_human": "operator"})
    assert granted.status_code == 200, granted.text
    assert _ledger_scopes(client) == {ORDINARY_CREW}


def test_spend_is_refused_when_the_registry_cannot_be_read(
        client, tmp_path, monkeypatch):
    """Fails closed: an unreadable registry cannot tell a black box from an
    ordinary crew, so nothing is booked, even for a crew that would pass."""
    monkeypatch.setattr(projects, "REGISTRY_PATH", tmp_path / "missing.json")
    projects.reload()
    r = _report_ledger_spend(client, product=ORDINARY_CREW,
                             agent=ORDINARY_AGENT_NAME, model="sonnet")
    assert r.status_code == 503
    assert _ledger_scopes(client) == set()


def _roster_names(client):
    return {a["name"] for a in
            client.get("/roster", headers=_h(WIDE)).json()["agents"]}


def test_the_roster_is_not_the_place_a_black_box_name_should_surface(client):
    _register_ask(client)
    _seed_ordinary_content(client)
    refused = client.put(f"/roster/{BLACKBOX_AGENT_NAME}", headers=_h(ORCH))
    # The same 404 a missing name gets, so the roster says nothing about
    # which names are black-box.
    assert refused.status_code == 404
    assert BLACKBOX_AGENT_NAME not in _roster_names(client)


def test_a_name_a_black_box_crew_declared_is_withheld_whatever_its_prefix(client):
    """Membership comes from the registration, not the spelling: a declared
    agent whose name carries no `ask-` prefix is still withheld."""
    r = client.post(
        "/black-box/registrations",
        json={"acting_human": "operator", "crew": BLACKBOX_CREW,
              "model": "sonnet", "gateway": "anthropic",
              "permissions": [{"agent": "quillmoss", "model": "sonnet",
                               "target": "WebFetch", "decision": "allow"}]},
        headers=_h(CLIENT))
    assert r.status_code == 200
    assert client.put("/roster/quillmoss", headers=_h(ORCH)).status_code == 404
    assert "quillmoss" not in _roster_names(client)


def test_a_private_crews_prefixed_name_is_withheld_and_an_ordinary_one_lands(
        client, tmp_path, monkeypatch):
    """A crew the registry marks private need not register, so it has
    declared no agents; its name prefix is what withholds `cfo-scribe`.
    `vivy-scribe` beside it lands, which shows the refusal is not blanket."""
    _use_registry(tmp_path, monkeypatch,
                  {**REGISTRY, "cfo": {"repos": [], "private": True}})
    assert client.put("/roster/cfo-scribe", headers=_h(ORCH)).status_code == 404
    landed = client.put(f"/roster/{ORDINARY_AGENT_NAME}", headers=_h(ORCH))
    assert landed.status_code == 200
    names = _roster_names(client)
    assert "cfo-scribe" not in names
    assert ORDINARY_AGENT_NAME in names


@pytest.mark.parametrize("suffix", ["​", " ", "⁠", "е"])
def test_a_lookalike_of_a_withheld_name_is_refused_as_malformed(client, suffix):
    """A zero-width or lookalike character renders as the withheld name
    while comparing unequal to it (security-reviewer, PR #255 round 1). A
    roster name is plain ASCII, so the spelling is refused before any
    comparison runs."""
    _register_ask(client)
    r = client.put(f"/roster/{BLACKBOX_AGENT_NAME}{suffix}", headers=_h(ORCH))
    assert r.status_code == 400
    assert not any(n.startswith(BLACKBOX_AGENT_NAME) for n in _roster_names(client))


def test_the_roster_refuses_every_add_when_the_registry_cannot_be_read(
        client, tmp_path, monkeypatch):
    monkeypatch.setattr(projects, "REGISTRY_PATH", tmp_path / "missing.json")
    projects.reload()
    r = client.put(f"/roster/{ORDINARY_AGENT_NAME}", headers=_h(ORCH))
    assert r.status_code == 503
    assert ORDINARY_AGENT_NAME not in _roster_names(client)
