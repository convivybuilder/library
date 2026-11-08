"""A draft Writing Desk post (`kind: "post_draft"`) is an ordinary Archive
record once Orient's E26 (`issue_01M584RKNA5KKFXE00GSHKEEYD`) moves it into
the Archives, except that it is readable only by an identity authorized for
`signal.post_draft` (CQ·228, `issue_01M5GBH6W45N8Y8BFY43C83Z7D`) — the
specification's "a draft is readable only by people who hold Writing Desk
write access", enforced here per read route rather than trusted to Orient's
own client-side filtering.

**Unlike `test_handoff_privacy.py`'s subject, writing a `post_draft` row
needs no extra gate** — only the ordinary `signal.create` every row needs —
so every row here is seeded through `POST /signals` itself rather than
`store.insert_signal` directly. The wall is read-only, and the capability
that lifts it is granted through an entry's own `grants`
(`identity.GRANT_ONLY_CAPS`), the same mechanism `machine.verify` and
`ledger.spend.subscription` use.
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

PLAIN = "agent-token-plain"              # scoped "company", no signal.post_draft
DRAFT_READER = "agent-token-draft-reader"  # scoped "company", holds signal.post_draft


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
        PLAIN: {"name": "plain-agent", "products": ["company"]},
        DRAFT_READER: {"name": "draft-reader-agent", "products": ["company"],
                       "grants": ["signal.post_draft"]},
    }))
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({"_meta": {}}))

    vector_index(db)
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(projects, "REGISTRY_PATH", registry)
    projects.reload()
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    yield TestClient(libapp.app)
    projects.reload()


def _h(token=PLAIN):
    return {"Authorization": f"Bearer {token}",
            protocol.HEADER: str(protocol.CURRENT)}


def _body(**over):
    out = {"source": "writing-desk", "kind": "post_draft", "actor": "someone",
           "product": "company", "body": "a draft worth reviewing"}
    out.update(over)
    return out


def _create(client, **over):
    r = client.post("/signals", json=_body(**over), headers=_h(DRAFT_READER))
    assert r.status_code == 201
    return r.json()


# --- the capability itself: granted to no role by default -------------------


def test_signal_post_draft_is_on_no_role():
    """`signal.post_draft` must not reach a caller through `agent`, `system`
    or `approved_client` — only through an entry's own `grants`
    (`identity.GRANT_ONLY_CAPS`), matching `test_ledger_spend_subscription_
    is_on_no_role`'s assertion for `ledger.spend.subscription`."""
    for role, caps in identity.ROLE_CAPS.items():
        assert "signal.post_draft" not in caps, role


# --- the write path: no new gate -------------------------------------------


def test_a_post_draft_write_needs_only_signal_create(client):
    """Neither token above holds anything but the ordinary `agent` role's
    `signal.create` — the wall is read-only, matching the item's "writing a
    post_draft row still needs only signal.create"."""
    r = client.post("/signals", json=_body(), headers=_h(PLAIN))
    assert r.status_code == 201


# --- the read paths ----------------------------------------------------------


def test_list_signals_excludes_a_post_draft_without_the_capability(client):
    draft = _create(client)
    other = _create(client, kind="note", body="an ordinary note")
    body = client.get("/signals", headers=_h(PLAIN)).json()
    ids = {s["id"] for s in body["signals"]}
    assert draft["id"] not in ids
    assert other["id"] in ids
    # `total` and `count` both have to agree with the page, or the filter
    # leaks through either number.
    assert body["total"] == 1
    assert body["count"] == 1


def test_list_signals_includes_a_post_draft_with_the_capability(client):
    draft = _create(client)
    listed = client.get("/signals", headers=_h(DRAFT_READER)).json()
    assert draft["id"] in {s["id"] for s in listed["signals"]}
    assert listed["total"] == 1


def test_get_signal_for_a_post_draft_is_404_without_the_capability(client):
    draft = _create(client)
    r = client.get(f"/signals/{draft['id']}", headers=_h(PLAIN))
    assert r.status_code == 404


def test_get_signal_for_a_post_draft_is_200_with_the_capability(client):
    draft = _create(client)
    r = client.get(f"/signals/{draft['id']}", headers=_h(DRAFT_READER))
    assert r.status_code == 200
    assert r.json()["kind"] == "post_draft"


def test_get_signal_blob_for_a_post_draft_is_404_without_the_capability(
        client, monkeypatch, tmp_path):
    monkeypatch.setattr(libapp.blobs, "BLOB_DIR", tmp_path / "blobs")
    draft = _create(client, content="the exact draft text")
    assert "blob_ref" in draft["meta"]
    r = client.get(f"/signals/{draft['id']}/blob", headers=_h(PLAIN))
    assert r.status_code == 404


def test_get_signal_blob_for_a_post_draft_is_200_with_the_capability(
        client, monkeypatch, tmp_path):
    monkeypatch.setattr(libapp.blobs, "BLOB_DIR", tmp_path / "blobs")
    draft = _create(client, content="the exact draft text")
    r = client.get(f"/signals/{draft['id']}/blob", headers=_h(DRAFT_READER))
    assert r.status_code == 200
    assert r.text == "the exact draft text"


def test_search_signals_excludes_a_post_draft_without_the_capability(
        client, monkeypatch):
    """`post_draft` is not in `vectors.EMBEDDED_SIGNAL_KINDS` today, so it is
    never a search hit regardless of this feature. Lifted for this one test
    so the route's own wall — rather than that unrelated, orthogonal fact —
    is what the assertion exercises, matching `test_handoff_privacy.py`'s
    own `test_search_signals_excludes_a_private_products_handoff`."""
    monkeypatch.setattr(vectors, "EMBEDDED_SIGNAL_KINDS",
                        vectors.EMBEDDED_SIGNAL_KINDS | {"post_draft"})
    draft = _create(client, body="the quarterly report draft")
    other = _create(client, kind="note", body="the quarterly report draft")
    r = client.get("/signals/search",
                   params={"q": "the quarterly report draft"}, headers=_h(PLAIN))
    assert r.status_code == 200
    ids = {s["id"] for s in r.json()["signals"]}
    assert draft["id"] not in ids
    assert other["id"] in ids


def test_search_signals_includes_a_post_draft_with_the_capability(
        client, monkeypatch):
    monkeypatch.setattr(vectors, "EMBEDDED_SIGNAL_KINDS",
                        vectors.EMBEDDED_SIGNAL_KINDS | {"post_draft"})
    draft = _create(client, body="the quarterly report draft")
    r = client.get("/signals/search",
                   params={"q": "the quarterly report draft"},
                   headers=_h(DRAFT_READER))
    assert r.status_code == 200
    assert draft["id"] in {s["id"] for s in r.json()["signals"]}


def test_facets_excludes_a_post_draft_without_the_capability(client):
    _create(client)
    _create(client, kind="note", body="an ordinary note")
    body = client.get("/signals/facets", headers=_h(PLAIN)).json()
    assert body["total"] == 1
    assert dict(body["by_kind"]).get("post_draft") is None
    assert dict(body["by_kind"]).get("note") == 1


def test_facets_includes_a_post_draft_with_the_capability(client):
    _create(client)
    _create(client, kind="note", body="an ordinary note")
    body = client.get("/signals/facets", headers=_h(DRAFT_READER)).json()
    assert body["total"] == 2
    assert dict(body["by_kind"]).get("post_draft") == 1


def test_search_all_excludes_a_post_draft_without_the_capability(
        client, monkeypatch):
    monkeypatch.setattr(vectors, "EMBEDDED_SIGNAL_KINDS",
                        vectors.EMBEDDED_SIGNAL_KINDS | {"post_draft"})
    draft = _create(client, body="the quarterly report draft")
    other = _create(client, kind="note", body="the quarterly report draft")
    r = client.get("/search", params={"q": "the quarterly report draft",
                                      "vault": "archives"}, headers=_h(PLAIN))
    assert r.status_code == 200
    ids = {row["id"] for row in r.json()["results"]}
    assert draft["id"] not in ids
    assert other["id"] in ids


def test_search_all_includes_a_post_draft_with_the_capability(
        client, monkeypatch):
    monkeypatch.setattr(vectors, "EMBEDDED_SIGNAL_KINDS",
                        vectors.EMBEDDED_SIGNAL_KINDS | {"post_draft"})
    draft = _create(client, body="the quarterly report draft")
    r = client.get("/search", params={"q": "the quarterly report draft",
                                      "vault": "archives"},
                   headers=_h(DRAFT_READER))
    assert r.status_code == 200
    assert draft["id"] in {row["id"] for row in r.json()["results"]}


def test_unreferenced_by_does_not_count_a_post_drafts_reference(client):
    """The `?unreferenced_by=` subquery's ref side takes the post-draft wall
    too (`_post_draft_wall("ref")`) — without it a draft the caller cannot
    read would make the row it references read as "referenced", disclosing
    that an invisible row points at it, the same leak
    `test_handoff_privacy.py`'s `test_unreferenced_by_does_not_count_a_
    private_handoffs_reference` proves for a handoff."""
    proposal = _create(client, kind="proposal", body="a proposal")
    _create(client, kind="post_draft", body="references the proposal",
           refs={"proposal": proposal["id"]})
    listed = client.get(
        "/signals",
        params={"kind": "proposal", "unreferenced_by": "post_draft"},
        headers=_h(PLAIN)).json()
    assert proposal["id"] in {s["id"] for s in listed["signals"]}


def test_a_post_row_is_unaffected(client):
    """`kind: "post"` — a published revision — gets no new wall, matching
    the item's "A post row (a published revision) gets no new wall"."""
    post = _create(client, kind="post", body="a published post")
    listed = client.get("/signals", headers=_h(PLAIN)).json()
    assert post["id"] in {s["id"] for s in listed["signals"]}
    by_id = client.get(f"/signals/{post['id']}", headers=_h(PLAIN))
    assert by_id.status_code == 200
    body = client.get("/signals/facets", headers=_h(PLAIN)).json()
    assert dict(body["by_kind"]).get("post") == 1


@pytest.mark.parametrize("kind,expected", [
    ("post_draft", True), ("Post_Draft", True), ("POST_DRAFT", True),
    (" post_draft ", True), ("post_draft_note", False), ("post", False),
    (None, False), ("", False),
])
def test_is_post_draft_is_case_and_whitespace_folded(kind, expected):
    """`app._is_post_draft` is the one test `_post_draft_wall` and
    `_readable_signal` share — exercised directly, mirroring
    `test_handoff_privacy.py`'s `test_is_handoff_is_case_and_whitespace_
    folded` for `_is_handoff`."""
    assert libapp._is_post_draft(kind) is expected


def test_read_gates_match_a_differently_cased_stored_post_draft(client):
    """`post_draft` is open vocabulary, uncanonicalized by the write gate, so
    a row stored as `"Post_Draft"` must fold to the same wall a lowercase
    spelling does."""
    draft = _create(client, kind="Post_Draft")
    other = _create(client, kind="note", body="an ordinary note")
    listed = client.get("/signals", headers=_h(PLAIN)).json()
    ids = {s["id"] for s in listed["signals"]}
    assert draft["id"] not in ids
    assert other["id"] in ids
    assert client.get(f"/signals/{draft['id']}", headers=_h(PLAIN)).status_code == 404


# --- the dedup collision and the ack routes (PR #305 round 1) -----------------


def test_a_dedup_collision_cannot_return_a_post_draft_without_the_capability(client):
    """`dedup_key` is unique across every kind, so an ordinary write whose key
    matches a draft's must not be answered with the draft (reviewer and
    security-reviewer, PR #305 round 1)."""
    draft = _create(client, dedup_key="shared-dedup-key", body="the draft's own body")
    r = client.post("/signals", json=_body(kind="note", dedup_key="shared-dedup-key"),
                    headers=_h(PLAIN))
    assert r.status_code == 409
    assert "the draft's own body" not in r.text
    assert draft["id"] not in r.text


def test_a_dedup_collision_returns_a_post_draft_with_the_capability(client):
    draft = _create(client, dedup_key="shared-dedup-key")
    r = client.post("/signals", json=_body(dedup_key="shared-dedup-key"),
                    headers=_h(DRAFT_READER))
    assert r.status_code == 200
    assert r.json()["id"] == draft["id"]


CLIENT_PLAIN = "client-token-plain"   # approved_client, scoped "company"
CLIENT_DRAFT = "client-token-draft"   # the same, holding signal.post_draft


@pytest.fixture
def ack_clients(tmp_path, monkeypatch):
    path = tmp_path / "client-tokens.json"
    path.write_text(json.dumps({
        CLIENT_PLAIN: {"name": "plain-client", "products": ["company"]},
        CLIENT_DRAFT: {"name": "draft-client", "products": ["company"],
                       "grants": ["signal.post_draft"]},
    }))
    monkeypatch.setattr(identity, "CLIENT_TOKENS", path)


def test_the_ack_routes_do_not_see_a_post_draft_without_the_capability(client, ack_clients):
    """`_ack_visible` gates `POST`/`DELETE /signals/{id}/ack` and `GET
    /signals/acks`; without the draft wall there, an ack succeeding or an
    ack state read back confirms the draft exists (PR #305 round 1)."""
    draft_id = _create(client)["id"]
    r = client.post(f"/signals/{draft_id}/ack", json={"acting_human": "operator"},
                    headers=_h(CLIENT_PLAIN))
    assert r.status_code == 404
    r = client.request("DELETE", f"/signals/{draft_id}/ack",
                       json={"acting_human": "operator"}, headers=_h(CLIENT_PLAIN))
    assert r.status_code == 404
    # Acked by the capability holder, so a leak in the read would show it.
    assert client.post(f"/signals/{draft_id}/ack", json={"acting_human": "operator"},
                       headers=_h(CLIENT_DRAFT)).status_code in (200, 201)
    r = client.get("/signals/acks",
                   params={"signal_id": draft_id, "acting_human": "operator"},
                   headers=_h(CLIENT_PLAIN))
    assert r.status_code == 200
    assert draft_id not in r.json()["acked"]
    r = client.get("/signals/acks",
                   params={"signal_id": draft_id, "acting_human": "operator"},
                   headers=_h(CLIENT_DRAFT))
    assert draft_id in r.json()["acked"]
