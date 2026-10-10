"""The Archives write path's three isolation properties, demonstrated rather
than argued from the schema — the Archives-submission epic's definition of
done (issue_01M2M634HK37NWJ5MM13WYYREM), clauses 3-5.

**Clause 3, submitter identity.** `POST /signals` sets `ingested_by` from the
authenticated caller, never from the payload, so a caller cannot write
another submitter's name into the audit trail.

**Clause 4, append-only.** No route amends or deletes a signal once written:
`/signals/{id}` answers only `GET`, and a `dedup_key` collision on a second
`POST` returns the ORIGINAL row rather than overwriting it with the new body.

**Clause 5, product scope on read.** A reader scoped to one product sees
nothing of another's, across every read route: the list, the single-signal
fetch, semantic search, and the facet aggregates — each re-applies the same
scope clause `app._scope_clause` builds, and a route that forgot to would be
exactly the gap this file exists to catch.

**Fixture note, for compatibility with library#172.** That open PR makes
`POST /signals` consult `projects.py`'s registry and fail closed (503) when
it cannot be read, and adds a `black_box_registrations` table `store.py`
reads at registration. Neither exists on `origin/main` today, so this module
pins `projects.REGISTRY_PATH` to a tmp registry naming the two test products
and creates the table in the test schema, matching `test_item_writes.py`'s
own pattern for the registry pin. A module that runs clean today and after
that PR lands needs both regardless of which one is live when it runs.
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

AGENT = "agent-token-vivy"       # scoped to product "vivy"
OTHER = "agent-token-constance"  # scoped to product "constance"

# Neither project is private — `projects.private_repos()` and
# `projects.private_crew_names()` are ADR-0043's disclosure wall, orthogonal
# to the scope property this file is about.
REGISTRY = {"vivy": {}, "constance": {}}


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
        AGENT: {"name": "vivy-agent", "products": ["vivy"]},
        OTHER: {"name": "constance-agent", "products": ["constance"]},
    }))
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps(REGISTRY))

    # The vec0 index, because the write routes reach it: a signal created here
    # is embedded after its commit.
    vector_index(db)
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(projects, "REGISTRY_PATH", registry)
    # PINNED, so an edit to the real registry cannot break this module, and so
    # a test cannot silently depend on what projects exist today — matching
    # `test_item_writes.py`'s own reasoning for the same pin.
    projects.reload()
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
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


def _row(signal_id: str) -> dict | None:
    conn = sqlite3.connect(store.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM signals WHERE id = ?", (signal_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# --- clause 3: a submitter cannot write another submitter's memories -----------


def test_ingested_by_ignores_a_payload_naming_another_submitter(client):
    """`ingested_by` and `actor` set in the payload to another credential's own
    name must not land in the audit column — it is taken from the token."""
    created = client.post(
        "/signals",
        json=_body(ingested_by="constance-agent", actor="constance-agent"),
        headers=_h(AGENT)).json()
    stored = _row(created["id"])
    assert stored["ingested_by"] == "vivy-agent"
    got = client.get(f"/signals/{created['id']}", headers=_h(AGENT))
    assert got.status_code == 200
    assert got.json()["ingested_by"] == "vivy-agent"


def test_two_submitters_rows_are_distinguishable_by_ingested_by(client):
    """Two different credentials, each writing in its own scope, each leave a
    row that names its own actor rather than a shared or borrowed one."""
    mine = client.post("/signals", json=_body(product="vivy"),
                       headers=_h(AGENT)).json()
    theirs = client.post("/signals", json=_body(product="constance"),
                         headers=_h(OTHER)).json()
    mine_row, theirs_row = _row(mine["id"]), _row(theirs["id"])
    assert mine_row["ingested_by"] == "vivy-agent"
    assert theirs_row["ingested_by"] == "constance-agent"
    assert mine_row["ingested_by"] != theirs_row["ingested_by"]


# --- clause 4: append-only holds at the path -----------------------------------


def test_amend_and_delete_are_refused_and_the_row_is_unchanged(client):
    """`/signals/{id}` answers only `GET` — no route amends or deletes a
    signal, so `PATCH`, `PUT` and `DELETE` fall through to Starlette's own
    405 for a path registered under other methods, rather than to any
    application code."""
    created = client.post("/signals", json=_body(), headers=_h()).json()
    before = _row(created["id"])

    patch = client.patch(f"/signals/{created['id']}",
                         json={"body": "amended"}, headers=_h())
    assert patch.status_code == 405
    put = client.put(f"/signals/{created['id']}",
                     json={"body": "amended"}, headers=_h())
    assert put.status_code == 405
    delete = client.delete(f"/signals/{created['id']}", headers=_h())
    assert delete.status_code == 405

    after = _row(created["id"])
    assert after == before, "a refused amend or delete must not touch the row"


def test_a_resubmission_with_the_same_dedup_key_does_not_alter_the_row(client):
    """A second `POST` colliding on `dedup_key` answers with the ORIGINAL row
    (`insert_signal`'s idempotent-reply branch) rather than overwriting it
    with the new body — the write is never applied a second time."""
    created = client.post(
        "/signals", json=_body(dedup_key="dk-append-only", body="original"),
        headers=_h()).json()
    before = _row(created["id"])

    again = client.post(
        "/signals",
        json=_body(dedup_key="dk-append-only", body="a completely different body"),
        headers=_h())
    assert again.status_code == 200
    assert again.json()["idempotent"] is True
    assert again.json()["id"] == created["id"]
    assert again.json()["body"] == "original"

    after = _row(created["id"])
    assert after == before
    assert after["body"] == "original"


# --- clause 5: a product-scoped reader does not see another product's rows -----


@pytest.fixture
def two_products(client):
    """One signal in `vivy`, one in `constance`, each created (and embedded)
    by the credential scoped to that product — real writes through the route
    under test, not a schema fixture standing in for it."""
    vivy = client.post(
        "/signals",
        json=_body(product="vivy", body="vivy team shipped the archives release"),
        headers=_h(AGENT)).json()
    constance = client.post(
        "/signals",
        json=_body(product="constance",
                  body="constance team shipped the archives release"),
        headers=_h(OTHER)).json()
    return vivy, constance


def test_list_signals_excludes_the_other_products_row(client, two_products):
    vivy, constance = two_products
    body = client.get("/signals", headers=_h(AGENT)).json()
    ids = {s["id"] for s in body["signals"]}
    assert vivy["id"] in ids
    assert constance["id"] not in ids
    # THE COUNT ITSELF, not only the page — `_scope_clause` is applied in the
    # query, so `total` must not count a row the caller cannot list either.
    assert body["total"] == 1


def test_get_signal_for_the_other_products_row_is_404(client, two_products):
    """404 rather than 403, matching `_readable_signal`'s own reasoning: a 403
    would confirm the row exists to a caller out of scope for it."""
    vivy, constance = two_products
    assert client.get(f"/signals/{constance['id']}",
                      headers=_h(AGENT)).status_code == 404
    assert client.get(f"/signals/{vivy['id']}",
                      headers=_h(AGENT)).status_code == 200


def test_search_signals_excludes_the_other_products_row(client, two_products):
    """The scope clause and the customer wall are re-applied to the joined
    rows after the KNN — `search_signals`'s own docstring names this as the
    route's one security property. Both rows share the query phrase, so a
    scoped reader that saw the other product's row would have to have found
    it through similarity rather than through a filter that let it slip."""
    vivy, constance = two_products
    r = client.get("/signals/search",
                   params={"q": "shipped the archives release"},
                   headers=_h(AGENT))
    assert r.status_code == 200
    body = r.json()
    ids = {s["id"] for s in body["signals"]}
    assert vivy["id"] in ids
    assert constance["id"] not in ids


def test_signal_facets_exclude_the_other_products_counts(client, two_products):
    """`total`, `by_source`, `by_kind` and `products` all run through the same
    scope clause and customer wall `GET /signals` applies — the route's own
    docstring names this as its one security property, because a `total`
    that counts an unlistable row is the same disclosure as listing it. Only
    the fixture's side effect (two rows, one per product) is needed here."""
    body = client.get("/signals/facets", headers=_h(AGENT)).json()
    assert body["total"] == 1
    assert body["products"] == ["vivy"]
    assert sum(n for _, n in body["by_source"]) == 1
    assert sum(n for _, n in body["by_kind"]) == 1
