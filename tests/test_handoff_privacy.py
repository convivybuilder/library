"""A handoff (`kind: "handoff"`) is an ordinary Archive record, read back by
`product` like any other — except for a product the registry marks `private`,
where the steward's "Handoffs are memory" epic (`issue_01M383JHPMME74P4HDM46D2WA5`)
requires it refused on every path it reaches: the write, and all three ways to
read it back.

**`projects.private_products()` is this file's other subject.** It is
`private_repos()`'s counterpart for a signal's `product` field, which names a
product slug rather than a repo — see its own docstring for the fail-closed
contract asserted here on the routes that consume it.

**Rows for a private product are seeded directly through `store.insert_signal`,
never through `POST /signals`.** That route already refuses a private
product's write before this feature existed (`projects.private_repos()` /
`projects.private_crew_names()`, consulted unconditionally of `kind`), so a
test proving the READ routes refuse a handoff needs one to already exist —
the shape a row written before its product was marked private would take.
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
import vectors

WIDE = "agent-token-wide"          # scoped "*", so scope alone admits everything
CFO_SCOPED = "agent-token-cfo"     # scoped to "cfo" itself
CLIENT_WIDE = "client-token-wide"  # approved_client, scoped "*" — signal.ack

# `cfo`'s `aliases` entry exists so a row can be seeded under a non-canonical
# spelling that still resolves to the private slug — see
# `test_handoff_wall_matches_a_stored_alias_spelling`, which is the one test
# that needs it; every other test in this file seeds under the canonical
# `"cfo"`.
REGISTRY = {"vivy": {},
           "cfo": {"private": True, "aliases": ["chief-financial"]}}


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
        CREATE TABLE signal_acks (
            id TEXT PRIMARY KEY, signal_id TEXT, identity TEXT, acked_at TEXT);
        CREATE UNIQUE INDEX uq_signal_acks_signal_identity
            ON signal_acks (signal_id, identity);
        CREATE TABLE review_records (
            id TEXT PRIMARY KEY, signal_id TEXT, product TEXT);
        -- Empty in every test here except the cold-tier section below, but
        -- `store.insert_signal` (library#181) queries this table on every
        -- `dedup_key` miss in hot, unconditionally of whether cold holds
        -- anything, so its absence breaks `_seed` itself.
        CREATE TABLE cold_dedup_keys (
            dedup_key TEXT PRIMARY KEY, signal_id TEXT NOT NULL);
        CREATE TABLE black_box_registrations (
            id INTEGER PRIMARY KEY, crew TEXT NOT NULL UNIQUE,
            model TEXT NOT NULL, gateway TEXT NOT NULL,
            created_by TEXT NOT NULL, created_at TEXT NOT NULL,
            updated_by TEXT NOT NULL, updated_at TEXT NOT NULL);
    """)
    conn.commit()
    conn.close()

    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        WIDE: {"name": "wide-agent", "products": ["*"]},
        CFO_SCOPED: {"name": "cfo-agent", "products": ["cfo"]},
    }))
    client_tokens = tmp_path / "client-tokens.json"
    client_tokens.write_text(json.dumps({
        CLIENT_WIDE: {"name": "wide-client", "products": ["*"]},
    }))
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps(REGISTRY))

    vector_index(db)
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(identity, "CLIENT_TOKENS", client_tokens)
    monkeypatch.setattr(projects, "REGISTRY_PATH", registry)
    projects.reload()
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    yield TestClient(libapp.app)
    projects.reload()


def _h(token=WIDE):
    return {"Authorization": f"Bearer {token}",
            protocol.HEADER: str(protocol.CURRENT)}


def _body(**over):
    out = {"source": "co", "kind": "handoff", "actor": "someone",
           "product": "vivy", "body": "a handoff worth remembering"}
    out.update(over)
    return out


_SEED_COUNT = 0


def _seed(product: str, kind: str = "handoff", body: str = "a handoff worth remembering",
         refs="{}", dedup_key: str | None = None):
    """Writes one signal straight through the store, bypassing `POST
    /signals` and the write-side gate it already runs — see the module
    docstring for why a read-route test needs this.

    Embedded the same way `create_signal`'s own `_embed_after_commit` would,
    since nothing else will for a row seeded outside the route."""
    global _SEED_COUNT
    _SEED_COUNT += 1
    row = {
        "id": f"sig_seed_{_SEED_COUNT}",
        "source": "seed", "kind": kind, "actor": "seed",
        "ingested_by": "seed", "product": product, "stream": None,
        "body": body, "refs": refs, "meta": "{}",
        "privacy_class": "internal", "dedup_key": dedup_key, "produced_at": None,
        "acting_human": None,
    }
    status, out = store.insert_signal(row, lambda ex: False)
    assert status == 201
    vectors.embed_item_by_id(vectors.VAULT_ARCHIVES, out["id"])
    return out


# --- the write path --------------------------------------------------------


def test_a_private_products_handoff_write_is_refused(client):
    r = client.post("/signals", json=_body(product="cfo"), headers=_h())
    assert r.status_code == 404


def test_a_private_products_handoff_write_is_refused_even_in_its_own_scope(client):
    """Not merely the usual scope wall: a credential scoped to `cfo` itself
    still cannot write a handoff under it."""
    r = client.post("/signals", json=_body(product="cfo"), headers=_h(CFO_SCOPED))
    assert r.status_code == 404


def test_a_non_private_products_handoff_lands_and_reads_back(client):
    """Reads back by `kind`, `product` and time — not by search, which
    `library/decisions/0001` leaves `handoff` out of
    `vectors.EMBEDDED_SIGNAL_KINDS` for regardless of this feature, so a
    handoff is never a semantic-search hit at all. `test_search_signals_...`
    below covers the search route's own wall by lifting that restriction for
    one test."""
    created = client.post("/signals", json=_body(product="vivy"), headers=_h())
    assert created.status_code == 201
    signal_id = created.json()["id"]

    by_id = client.get(f"/signals/{signal_id}", headers=_h())
    assert by_id.status_code == 200
    assert by_id.json()["kind"] == "handoff"

    listed = client.get(
        "/signals",
        params={"kind": "handoff", "product": "vivy",
                "order": "produced_at"},
        headers=_h())
    assert listed.status_code == 200
    ids = {s["id"] for s in listed.json()["signals"]}
    assert signal_id in ids


def test_an_unreadable_registry_refuses_a_handoff_write(client, tmp_path, monkeypatch):
    monkeypatch.setattr(projects, "REGISTRY_PATH", tmp_path / "gone.json")
    projects.reload()
    r = client.post("/signals", json=_body(product="vivy"), headers=_h())
    assert r.status_code == 503


# --- the read paths ---------------------------------------------------------


def test_list_signals_excludes_a_private_products_handoff(client):
    private_id = _seed("cfo")["id"]
    other_id = _seed("vivy")["id"]
    body = client.get("/signals", params={"kind": "handoff"}, headers=_h()).json()
    ids = {s["id"] for s in body["signals"]}
    assert private_id not in ids
    assert other_id in ids
    # `total` has to agree with the page, or the filter leaks through the count.
    assert body["total"] == 1


def test_get_signal_for_a_private_products_handoff_is_404(client):
    private_id = _seed("cfo")["id"]
    r = client.get(f"/signals/{private_id}", headers=_h())
    assert r.status_code == 404


def test_search_signals_excludes_a_private_products_handoff(client, monkeypatch):
    """`handoff` is not in `vectors.EMBEDDED_SIGNAL_KINDS` today
    (`library/decisions/0001`), so a handoff is never a search hit regardless
    of this feature. Lifted for this one test so the route's own wall —
    rather than that unrelated, orthogonal fact — is what the assertion
    exercises."""
    monkeypatch.setattr(vectors, "EMBEDDED_SIGNAL_KINDS",
                        vectors.EMBEDDED_SIGNAL_KINDS | {"handoff"})
    private_id = _seed("cfo", body="the quarterly handoff notes")
    other = _seed("vivy", body="the quarterly handoff notes")
    r = client.get("/signals/search", params={"q": "the quarterly handoff notes"},
                   headers=_h())
    assert r.status_code == 200
    ids = {s["id"] for s in r.json()["signals"]}
    assert private_id["id"] not in ids
    assert other["id"] in ids


def test_an_unreadable_registry_hides_a_handoff_rather_than_admitting_it(
        client, tmp_path, monkeypatch):
    """Fails CLOSED on the read side too: with no slug list to clear a
    row's product against, `_handoff_wall`/`_private_handoff` hide every
    handoff rather than none — `projects.private_products()` returning
    `None` must not read as "nothing is private"."""
    seeded = _seed("vivy")["id"]
    monkeypatch.setattr(projects, "REGISTRY_PATH", tmp_path / "gone.json")
    projects.reload()

    listed = client.get("/signals", params={"kind": "handoff"}, headers=_h()).json()
    assert seeded not in {s["id"] for s in listed["signals"]}
    assert client.get(f"/signals/{seeded}", headers=_h()).status_code == 404


def test_another_kind_for_the_same_private_product_is_unaffected(client):
    """The new wall is keyed on `kind`, not on the product alone: a `note`
    row for the same private product `cfo` is unaffected by it — listable,
    fetchable by id, and findable by search, exactly like any other row a
    wildcard-scoped reader may see."""
    note_id = _seed("cfo", kind="note", body="an ordinary note")["id"]

    listed = client.get("/signals", params={"kind": "note"}, headers=_h()).json()
    assert note_id in {s["id"] for s in listed["signals"]}

    by_id = client.get(f"/signals/{note_id}", headers=_h())
    assert by_id.status_code == 200

    searched = client.get("/signals/search", params={"q": "an ordinary note"},
                          headers=_h())
    assert note_id in {s["id"] for s in searched.json()["signals"]}


# --- round 2: the gaps both reviews found on PR #185's first head -----------
#
# Each test below is written to fail on `ce1d691c4` (the reviewed commit) and
# pass once its matching fix lands — see the PR body's round-2 section for
# which fix closes which test.


def test_dedup_collision_cannot_return_a_private_products_handoff(client):
    """`_can_return_existing` is the gate a `dedup_key` collision consults
    (`store.insert_signal`), and `dedup_key` is unique across every kind and
    product — so a caller writing an ORDINARY row at a product it can see,
    whose `dedup_key` happens to match an existing private handoff's, must
    not get that handoff back wholesale. Before this fix, a wide-scoped
    caller held every capability `_can_return_existing` checked (`signal.
    create`, scope, `signal.read`), so the collision answered `200` with the
    private handoff's own body."""
    private = _seed("cfo", dedup_key="shared-dedup-key",
                    body="the cfo's private handoff body")
    r = client.post("/signals", json=_body(
        kind="note", product="vivy", dedup_key="shared-dedup-key"),
        headers=_h())
    assert r.status_code == 409
    assert "the cfo's private handoff body" not in r.text
    assert private["id"] not in r.text


def test_facets_excludes_a_private_products_handoff(client):
    """`GET /signals/facets` applied the scope clause and the customer wall
    but no handoff wall, so a private handoff inflated `total`, appeared in
    `by_kind["handoff"]`, and its product surfaced in `products`."""
    _seed("cfo")
    _seed("vivy")
    body = client.get("/signals/facets", headers=_h()).json()
    assert body["total"] == 1
    assert dict(body["by_kind"]).get("handoff") == 1
    assert "cfo" not in body["products"]
    assert "vivy" in body["products"]


def _ack_row(signal_id: str, human: str = "operator"):
    conn = sqlite3.connect(store.DB_PATH)
    try:
        conn.execute(
            "INSERT INTO signal_acks (id, signal_id, identity, acked_at) "
            "VALUES (?, ?, ?, ?)",
            (f"ack_{signal_id}", signal_id, human, "2026-09-30 00:00:00"))
        conn.commit()
    finally:
        conn.close()


def test_get_signal_acks_excludes_a_private_products_handoff(client):
    """`_ack_visible` checked the customer wall and scope but not the
    handoff wall, so a private handoff's ack state was readable through
    `GET /signals/acks` by any identity in scope for its product."""
    private_id = _seed("cfo")["id"]
    _ack_row(private_id, "operator")
    r = client.get("/signals/acks",
                   params={"signal_id": private_id, "acting_human": "operator"},
                   headers=_h(CLIENT_WIDE))
    assert r.status_code == 200
    assert private_id not in r.json()["acked"]


def test_post_ack_refuses_a_private_products_handoff(client):
    private_id = _seed("cfo")["id"]
    r = client.post(f"/signals/{private_id}/ack",
                    json={"acting_human": "operator"}, headers=_h(CLIENT_WIDE))
    assert r.status_code == 404


def test_delete_ack_refuses_a_private_products_handoff(client):
    private_id = _seed("cfo")["id"]
    _ack_row(private_id, "operator")
    r = client.request("DELETE", f"/signals/{private_id}/ack",
                       json={"acting_human": "operator"}, headers=_h(CLIENT_WIDE))
    assert r.status_code == 404


@pytest.mark.parametrize("kind,expected", [
    ("handoff", True), ("Handoff", True), ("HANDOFF", True),
    (" handoff ", True), ("handoff_note", False), ("note", False),
    (None, False), ("", False),
])
def test_is_handoff_is_case_and_whitespace_folded(kind, expected):
    """`app._is_handoff` is the one test the write gate and all three read
    gates share; this exercises it directly rather than through an HTTP
    call, because the write gate's own case-sensitivity bug has no HTTP path
    that demonstrates it today: `POST /signals` refuses every kind for a
    registry-marked-private product unconditionally (the pre-existing
    `private_repos()`/`private_crew_names()` black-box check,
    `test_a_private_products_handoff_write_is_refused` above), before the
    `kind`-specific handoff gate this fixes is even reached — matching the
    original PR body's own "Worth knowing, not a blocker" note. The read
    gates (`_handoff_wall`, `_readable_signal`, `_ack_visible`) have no such
    earlier gate and are exercised through HTTP below
    (`test_read_gates_match_a_differently_cased_stored_kind` and the ack
    tests), via `_seed`, the same bypass the module docstring explains."""
    assert libapp._is_handoff(kind) is expected


def test_read_gates_match_a_differently_cased_stored_kind(client):
    """`_handoff_wall`'s SQL and `_readable_signal`'s `row["kind"] ==
    "handoff"` were both exact-match, so a row stored as `"Handoff"` (the
    shape a write from before this feature, or from a writer other than
    this route, could take) was listable and fetchable like any other row.
    No `kind` filter on the list call, since a stored `"Handoff"` would not
    match a lowercase `?kind=handoff` query param regardless of this wall."""
    private_id = _seed("cfo", kind="Handoff")["id"]
    other_id = _seed("vivy", kind="HANDOFF")["id"]
    listed = client.get("/signals", headers=_h()).json()
    ids = {s["id"] for s in listed["signals"]}
    assert private_id not in ids
    assert other_id in ids
    assert client.get(f"/signals/{private_id}", headers=_h()).status_code == 404


def test_handoff_wall_matches_a_stored_alias_spelling(client):
    """`_handoff_wall`'s SQL matched `product` against the canonical private
    slug alone, while `_private_handoff` calls `projects.resolve()` first —
    so a row stored under `cfo`'s alias `chief-financial` was listed and
    searched (the SQL exact-match missed it) while 404ing on single-fetch
    (the per-row `resolve()` caught it). `_handoff_wall` now calls
    `_private_handoff` itself as a registered SQL function, so the SQL wall
    runs the exact `resolve()` call the per-row check does rather than a
    second, hand-written approximation of it."""
    private_id = _seed("chief-financial")["id"]
    listed = client.get("/signals", params={"kind": "handoff"},
                        headers=_h()).json()
    assert private_id not in {s["id"] for s in listed["signals"]}
    assert client.get(f"/signals/{private_id}", headers=_h()).status_code == 404


def test_search_all_excludes_a_private_products_handoff(client, monkeypatch):
    """`GET /search`'s Archives half (`_search_vector_page`) passed
    `_customer_wall` alone as its `wall_fn`, missing the handoff wall
    `GET /signals/search` applies — so a private handoff was rankable and
    returnable through the merged search route even once the dedicated
    Archives route excluded it."""
    monkeypatch.setattr(vectors, "EMBEDDED_SIGNAL_KINDS",
                        vectors.EMBEDDED_SIGNAL_KINDS | {"handoff"})
    private = _seed("cfo", body="the quarterly handoff notes")
    other = _seed("vivy", body="the quarterly handoff notes")
    r = client.get("/search", params={"q": "the quarterly handoff notes",
                                      "vault": "archives"}, headers=_h())
    assert r.status_code == 200
    ids = {row["id"] for row in r.json()["results"]}
    assert private["id"] not in ids
    assert other["id"] in ids


def test_unreferenced_by_does_not_count_a_private_handoffs_reference(client):
    """The `?unreferenced_by=` subquery took the scope clause and the
    customer wall but not the handoff wall, so a private handoff that
    referenced another row made that row read as "referenced" — disclosing,
    by its absence from an `unreferenced` listing, that some invisible row
    points at it. That is the exact leak the subquery's own comment already
    refuses for an unreadable decision; a private handoff is the same leak
    through the one `ref.kind` this test checks."""
    proposal = _seed("vivy", kind="proposal", body="a proposal")
    _seed("cfo", kind="handoff", body="references the proposal",
         refs=json.dumps([proposal["id"]]))
    listed = client.get(
        "/signals",
        params={"kind": "proposal", "unreferenced_by": "handoff"},
        headers=_h()).json()
    assert proposal["id"] in {s["id"] for s in listed["signals"]}


# --- round 2 continued: a private handoff aged into the cold tier -----------
#
# library#181 (merged into this branch for round 2) added the Archives cold
# tier: a mover relocates a signal's row into a second file, `cold.db`, once
# it passes its kind's hot window, and every read path above now reads hot
# UNION cold rather than `main.signals` alone
# (library/specs/archives-cold-tier.md §1). The wall every test above proves
# on a hot row has to hold exactly as hard once the row the mover already
# relocated is the only copy left.

COLD_SIGNALS_SCHEMA = """
    CREATE TABLE signals (
        id TEXT PRIMARY KEY, source TEXT, stream TEXT, kind TEXT, actor TEXT,
        ingested_by TEXT, product TEXT, refs TEXT, body TEXT, meta TEXT,
        privacy_class TEXT, produced_at TEXT, ingested_at TEXT,
        embedded_at TEXT, dedup_key TEXT, acting_human TEXT);
"""

_MOVE_COLS = ("id", "source", "kind", "actor", "ingested_by", "product",
             "stream", "body", "refs", "meta", "privacy_class", "dedup_key",
             "produced_at", "acting_human", "ingested_at", "embedded_at")


@pytest.fixture
def cold(client, cold_db):
    """A real cold-tier `signals` table at `LIBRARY_COLD_DB`, shaped like
    the `client` fixture's own hand-written hot `signals` table above.
    Returns the cold file's path, for `_move` below — the suite's own
    factory fixture (`conftest.py`), rather than a second hand-rolled file,
    so this module's cold schema cannot drift from what `attach_cold`
    actually attaches to in every other test that uses it."""
    return cold_db(COLD_SIGNALS_SCHEMA)


def _move(cold_path, signal_id: str):
    """Moves one already-seeded hot row cold by hand — copy then delete, the
    same shape `cold_tier.py` uses (archives-cold-tier.md §3). These tests
    are about the read side's wall, not the mover's own eligibility rules,
    so this skips the window/kind policy and just relocates the one row a
    test names."""
    hot = sqlite3.connect(store.DB_PATH)
    row = hot.execute(
        f"SELECT {','.join(_MOVE_COLS)} FROM signals WHERE id = ?",
        (signal_id,)).fetchone()
    hot.execute("DELETE FROM signals WHERE id = ?", (signal_id,))
    hot.commit()
    hot.close()
    coldconn = sqlite3.connect(cold_path)
    coldconn.execute(
        f"INSERT INTO signals ({','.join(_MOVE_COLS)}) "
        f"VALUES ({','.join('?' * len(_MOVE_COLS))})", row)
    coldconn.commit()
    coldconn.close()


def test_list_signals_excludes_a_cold_private_products_handoff(client, cold):
    private_id = _seed("cfo")["id"]
    _move(cold, private_id)
    other_id = _seed("vivy")["id"]
    body = client.get("/signals", params={"kind": "handoff"}, headers=_h()).json()
    ids = {s["id"] for s in body["signals"]}
    assert private_id not in ids
    assert other_id in ids
    assert body["total"] == 1


def test_get_signal_for_a_cold_private_products_handoff_is_404(client, cold):
    private_id = _seed("cfo")["id"]
    _move(cold, private_id)
    r = client.get(f"/signals/{private_id}", headers=_h())
    assert r.status_code == 404


def test_search_signals_excludes_a_cold_private_products_handoff(
        client, cold, monkeypatch):
    """Embedded WHILE HOT, matching the real mover's own rule
    (`embedded_at IS NOT NULL` is required before a row ever moves) — `_seed`
    already embeds every row it creates, so `_move` here only has to carry
    that stamp across, the same as the real mover does."""
    monkeypatch.setattr(vectors, "EMBEDDED_SIGNAL_KINDS",
                        vectors.EMBEDDED_SIGNAL_KINDS | {"handoff"})
    private = _seed("cfo", body="the quarterly handoff notes")
    _move(cold, private["id"])
    other = _seed("vivy", body="the quarterly handoff notes")
    r = client.get("/signals/search", params={"q": "the quarterly handoff notes"},
                   headers=_h())
    assert r.status_code == 200
    ids = {s["id"] for s in r.json()["signals"]}
    assert private["id"] not in ids
    assert other["id"] in ids


def test_facets_excludes_a_cold_private_products_handoff(client, cold):
    private_id = _seed("cfo")["id"]
    _move(cold, private_id)
    _seed("vivy")
    body = client.get("/signals/facets", headers=_h()).json()
    assert body["total"] == 1
    assert dict(body["by_kind"]).get("handoff") == 1
    assert "cfo" not in body["products"]
    assert "vivy" in body["products"]


def test_get_signal_acks_excludes_a_cold_private_products_handoff(client, cold):
    private_id = _seed("cfo")["id"]
    _move(cold, private_id)
    _ack_row(private_id, "operator")
    r = client.get("/signals/acks",
                   params={"signal_id": private_id, "acting_human": "operator"},
                   headers=_h(CLIENT_WIDE))
    assert r.status_code == 200
    assert private_id not in r.json()["acked"]


def test_post_ack_refuses_a_cold_private_products_handoff(client, cold):
    private_id = _seed("cfo")["id"]
    _move(cold, private_id)
    r = client.post(f"/signals/{private_id}/ack",
                    json={"acting_human": "operator"}, headers=_h(CLIENT_WIDE))
    assert r.status_code == 404


def test_unreferenced_by_does_not_count_a_cold_private_handoffs_reference(
        client, cold):
    proposal = _seed("vivy", kind="proposal", body="a proposal")
    handoff = _seed("cfo", kind="handoff", body="references the proposal",
                    refs=json.dumps([proposal["id"]]))
    _move(cold, handoff["id"])
    listed = client.get(
        "/signals",
        params={"kind": "proposal", "unreferenced_by": "handoff"},
        headers=_h()).json()
    assert proposal["id"] in {s["id"] for s in listed["signals"]}


# --- round 3: the SQL wall normalised kind and product differently from the --
# Python-side checks ---------------------------------------------------------
#
# Both round-2 reviews, on the revision that fixed round 1's seven findings,
# found the same defect class: `_handoff_wall`'s SQL re-derived `_is_handoff`'s
# and `_private_handoff`'s folds instead of calling them, and the two re-
# derivations were each narrower than the function they stood in for.
# `_handoff_wall` now registers `_is_handoff` and `_private_handoff`
# themselves as SQL functions (`app._register_handoff_sql_functions`) and
# calls those, so there is one definition of each test rather than two that
# can drift. Each test below is written to fail on `3e343767949c` (the
# reviewed commit) and pass once that registration lands.

# Tab, newline and a non-breaking space — SQLite's `TRIM()`, which
# `_handoff_wall`'s SQL used to call directly, strips only the space
# character; Python's `.strip()`, which `_is_handoff` has always used, strips
# all three (reviewer, PR #185 round 3).
TAB_NEWLINE_NBSP_PADDED_KIND = "\thandoff\n "

# A plain space on both sides — `_handoff_wall`'s SQL folded `product`'s case
# but never trimmed it, while `_private_handoff`'s `projects.resolve()` call
# strips before comparing (security-reviewer, PR #185 round 3).
WHITESPACE_PADDED_PRODUCT = " cfo "

# `cfo`'s alias `chief-financial` (`REGISTRY`, above), padded and wrong-cased
# — the alias-resolution gap round 2 fixed and the whitespace gap round 3
# fixes, both at once, on the one field they both touch.
PADDED_MIXED_CASE_ALIAS_PRODUCT = "  Chief-Financial  "


def test_list_signals_excludes_a_tab_padded_kind_handoff(client):
    private_id = _seed("cfo", kind=TAB_NEWLINE_NBSP_PADDED_KIND)["id"]
    other_id = _seed("vivy")["id"]
    listed = client.get("/signals", headers=_h()).json()
    ids = {s["id"] for s in listed["signals"]}
    assert private_id not in ids
    assert other_id in ids


def test_get_signal_for_a_tab_padded_kind_handoff_is_404(client):
    private_id = _seed("cfo", kind=TAB_NEWLINE_NBSP_PADDED_KIND)["id"]
    r = client.get(f"/signals/{private_id}", headers=_h())
    assert r.status_code == 404


def test_search_signals_excludes_a_tab_padded_kind_handoff(client, monkeypatch):
    monkeypatch.setattr(vectors, "EMBEDDED_SIGNAL_KINDS",
                        vectors.EMBEDDED_SIGNAL_KINDS | {"handoff"})
    private_id = _seed("cfo", kind=TAB_NEWLINE_NBSP_PADDED_KIND,
                       body="the quarterly handoff notes")
    other = _seed("vivy", body="the quarterly handoff notes")
    r = client.get("/signals/search", params={"q": "the quarterly handoff notes"},
                   headers=_h())
    assert r.status_code == 200
    ids = {s["id"] for s in r.json()["signals"]}
    assert private_id["id"] not in ids
    assert other["id"] in ids


def test_facets_excludes_a_tab_padded_kind_handoff(client):
    _seed("cfo", kind=TAB_NEWLINE_NBSP_PADDED_KIND)
    _seed("vivy")
    body = client.get("/signals/facets", headers=_h()).json()
    assert body["total"] == 1
    assert "cfo" not in body["products"]
    assert "vivy" in body["products"]


def test_search_all_excludes_a_tab_padded_kind_handoff(client, monkeypatch):
    monkeypatch.setattr(vectors, "EMBEDDED_SIGNAL_KINDS",
                        vectors.EMBEDDED_SIGNAL_KINDS | {"handoff"})
    private = _seed("cfo", kind=TAB_NEWLINE_NBSP_PADDED_KIND,
                    body="the quarterly handoff notes")
    other = _seed("vivy", body="the quarterly handoff notes")
    r = client.get("/search", params={"q": "the quarterly handoff notes",
                                      "vault": "archives"}, headers=_h())
    assert r.status_code == 200
    ids = {row["id"] for row in r.json()["results"]}
    assert private["id"] not in ids
    assert other["id"] in ids


def test_list_signals_excludes_a_whitespace_padded_product_handoff(client):
    private_id = _seed(WHITESPACE_PADDED_PRODUCT)["id"]
    other_id = _seed("vivy")["id"]
    listed = client.get("/signals", params={"kind": "handoff"}, headers=_h()).json()
    ids = {s["id"] for s in listed["signals"]}
    assert private_id not in ids
    assert other_id in ids
    assert listed["total"] == 1


def test_get_signal_for_a_whitespace_padded_product_handoff_is_404(client):
    private_id = _seed(WHITESPACE_PADDED_PRODUCT)["id"]
    r = client.get(f"/signals/{private_id}", headers=_h())
    assert r.status_code == 404


def test_search_signals_excludes_a_whitespace_padded_product_handoff(
        client, monkeypatch):
    monkeypatch.setattr(vectors, "EMBEDDED_SIGNAL_KINDS",
                        vectors.EMBEDDED_SIGNAL_KINDS | {"handoff"})
    private_id = _seed(WHITESPACE_PADDED_PRODUCT,
                       body="the quarterly handoff notes")
    other = _seed("vivy", body="the quarterly handoff notes")
    r = client.get("/signals/search", params={"q": "the quarterly handoff notes"},
                   headers=_h())
    assert r.status_code == 200
    ids = {s["id"] for s in r.json()["signals"]}
    assert private_id["id"] not in ids
    assert other["id"] in ids


def test_facets_excludes_a_whitespace_padded_product_handoff(client):
    _seed(WHITESPACE_PADDED_PRODUCT)
    _seed("vivy")
    body = client.get("/signals/facets", headers=_h()).json()
    assert body["total"] == 1
    assert dict(body["by_kind"]).get("handoff") == 1
    assert "cfo" not in body["products"]
    assert "vivy" in body["products"]


def test_search_all_excludes_a_whitespace_padded_product_handoff(client, monkeypatch):
    monkeypatch.setattr(vectors, "EMBEDDED_SIGNAL_KINDS",
                        vectors.EMBEDDED_SIGNAL_KINDS | {"handoff"})
    private = _seed(WHITESPACE_PADDED_PRODUCT, body="the quarterly handoff notes")
    other = _seed("vivy", body="the quarterly handoff notes")
    r = client.get("/search", params={"q": "the quarterly handoff notes",
                                      "vault": "archives"}, headers=_h())
    assert r.status_code == 200
    ids = {row["id"] for row in r.json()["results"]}
    assert private["id"] not in ids
    assert other["id"] in ids


def test_handoff_wall_matches_a_padded_mixed_case_stored_alias(client):
    """The alias gap round 2 fixed and the whitespace gap round 3 fixes,
    together: a row stored under a padded, wrong-cased spelling of `cfo`'s
    alias still has to resolve to the private slug."""
    private_id = _seed(PADDED_MIXED_CASE_ALIAS_PRODUCT)["id"]
    listed = client.get("/signals", params={"kind": "handoff"},
                        headers=_h()).json()
    assert private_id not in {s["id"] for s in listed["signals"]}
    assert client.get(f"/signals/{private_id}", headers=_h()).status_code == 404


def test_list_signals_excludes_a_cold_tab_padded_kind_handoff(client, cold):
    private_id = _seed("cfo", kind=TAB_NEWLINE_NBSP_PADDED_KIND)["id"]
    _move(cold, private_id)
    other_id = _seed("vivy")["id"]
    body = client.get("/signals", headers=_h()).json()
    ids = {s["id"] for s in body["signals"]}
    assert private_id not in ids
    assert other_id in ids


def test_get_signal_for_a_cold_tab_padded_kind_handoff_is_404(client, cold):
    private_id = _seed("cfo", kind=TAB_NEWLINE_NBSP_PADDED_KIND)["id"]
    _move(cold, private_id)
    r = client.get(f"/signals/{private_id}", headers=_h())
    assert r.status_code == 404


def test_search_signals_excludes_a_cold_tab_padded_kind_handoff(
        client, cold, monkeypatch):
    monkeypatch.setattr(vectors, "EMBEDDED_SIGNAL_KINDS",
                        vectors.EMBEDDED_SIGNAL_KINDS | {"handoff"})
    private = _seed("cfo", kind=TAB_NEWLINE_NBSP_PADDED_KIND,
                    body="the quarterly handoff notes")
    _move(cold, private["id"])
    other = _seed("vivy", body="the quarterly handoff notes")
    r = client.get("/signals/search", params={"q": "the quarterly handoff notes"},
                   headers=_h())
    assert r.status_code == 200
    ids = {s["id"] for s in r.json()["signals"]}
    assert private["id"] not in ids
    assert other["id"] in ids


def test_facets_excludes_a_cold_tab_padded_kind_handoff(client, cold):
    private_id = _seed("cfo", kind=TAB_NEWLINE_NBSP_PADDED_KIND)["id"]
    _move(cold, private_id)
    _seed("vivy")
    body = client.get("/signals/facets", headers=_h()).json()
    assert body["total"] == 1
    assert "cfo" not in body["products"]
    assert "vivy" in body["products"]


def test_search_all_excludes_a_cold_tab_padded_kind_handoff(client, cold, monkeypatch):
    monkeypatch.setattr(vectors, "EMBEDDED_SIGNAL_KINDS",
                        vectors.EMBEDDED_SIGNAL_KINDS | {"handoff"})
    private = _seed("cfo", kind=TAB_NEWLINE_NBSP_PADDED_KIND,
                    body="the quarterly handoff notes")
    _move(cold, private["id"])
    other = _seed("vivy", body="the quarterly handoff notes")
    r = client.get("/search", params={"q": "the quarterly handoff notes",
                                      "vault": "archives"}, headers=_h())
    assert r.status_code == 200
    ids = {row["id"] for row in r.json()["results"]}
    assert private["id"] not in ids
    assert other["id"] in ids


def test_list_signals_excludes_a_cold_whitespace_padded_product_handoff(client, cold):
    private_id = _seed(WHITESPACE_PADDED_PRODUCT)["id"]
    _move(cold, private_id)
    other_id = _seed("vivy")["id"]
    body = client.get("/signals", params={"kind": "handoff"}, headers=_h()).json()
    ids = {s["id"] for s in body["signals"]}
    assert private_id not in ids
    assert other_id in ids
    assert body["total"] == 1


def test_get_signal_for_a_cold_whitespace_padded_product_handoff_is_404(client, cold):
    private_id = _seed(WHITESPACE_PADDED_PRODUCT)["id"]
    _move(cold, private_id)
    r = client.get(f"/signals/{private_id}", headers=_h())
    assert r.status_code == 404


def test_search_signals_excludes_a_cold_whitespace_padded_product_handoff(
        client, cold, monkeypatch):
    monkeypatch.setattr(vectors, "EMBEDDED_SIGNAL_KINDS",
                        vectors.EMBEDDED_SIGNAL_KINDS | {"handoff"})
    private = _seed(WHITESPACE_PADDED_PRODUCT, body="the quarterly handoff notes")
    _move(cold, private["id"])
    other = _seed("vivy", body="the quarterly handoff notes")
    r = client.get("/signals/search", params={"q": "the quarterly handoff notes"},
                   headers=_h())
    assert r.status_code == 200
    ids = {s["id"] for s in r.json()["signals"]}
    assert private["id"] not in ids
    assert other["id"] in ids


def test_facets_excludes_a_cold_whitespace_padded_product_handoff(client, cold):
    private_id = _seed(WHITESPACE_PADDED_PRODUCT)["id"]
    _move(cold, private_id)
    _seed("vivy")
    body = client.get("/signals/facets", headers=_h()).json()
    assert body["total"] == 1
    assert dict(body["by_kind"]).get("handoff") == 1
    assert "cfo" not in body["products"]
    assert "vivy" in body["products"]


def test_search_all_excludes_a_cold_whitespace_padded_product_handoff(
        client, cold, monkeypatch):
    monkeypatch.setattr(vectors, "EMBEDDED_SIGNAL_KINDS",
                        vectors.EMBEDDED_SIGNAL_KINDS | {"handoff"})
    private = _seed(WHITESPACE_PADDED_PRODUCT, body="the quarterly handoff notes")
    _move(cold, private["id"])
    other = _seed("vivy", body="the quarterly handoff notes")
    r = client.get("/search", params={"q": "the quarterly handoff notes",
                                      "vault": "archives"}, headers=_h())
    assert r.status_code == 200
    ids = {row["id"] for row in r.json()["results"]}
    assert private["id"] not in ids
    assert other["id"] in ids


def test_registry_reading_function_is_not_registered_deterministic():
    # SQLite refuses a non-deterministic function in an index expression,
    # which makes the registration flag observable. is_private_handoff_product
    # reads the projects registry a reload replaces, so SQLite must not be
    # licensed to reuse its answer (security-reviewer, PR #185 round 3).
    conn = sqlite3.connect(":memory:")
    store._apply_connection_functions(conn)
    conn.execute("CREATE TABLE t (kind TEXT, product TEXT)")
    conn.execute("CREATE INDEX t_kind ON t (is_handoff_kind(kind))")
    with pytest.raises(sqlite3.OperationalError, match="non-deterministic"):
        conn.execute(
            "CREATE INDEX t_product ON t (is_private_handoff_product(product))")
