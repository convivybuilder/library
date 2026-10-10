"""A typed relation for "this item is part of that epic's work", written
through its own API call rather than through `parent_id`
(issue_01M1JSPH7DBHG0BZFPJPNNZGSC).

`_validate_parent` refuses a `parent_id` across products, unconditionally,
because `lease.ancestor_ids` and `lease.subtree_ids` are plain `parent_id`
walks and every edge being same-product is what keeps three lease
disclosures and the epic-retype descendant count safe. This relation is the
carrier for exactly the case that refusal leaves unmet — an item in one
product naming an epic in another — and it has to reach neither of those
walks to stay safe the same way.

**Built on a database from the real migration**, `test_epic_progress.py`'s
reason: `issue_epic_relations` is new DDL, and running it through
`alembic upgrade head` is what proves the migration chained onto the single
current head actually produces a schema this module's queries can read.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as libapp
import identity
import lease
import protocol
import store

REPO = Path(__file__).resolve().parent.parent
WIDE = "wide-token"
NARROW_VIVY = "narrow-vivy-token"
NARROW_CONSTANCE = "narrow-constance-token"


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat(" ")


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = tmp_path / "control.db"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LIBRARY_DB": str(db)})
    assert result.returncode == 0, result.stderr

    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        WIDE: {"name": "wide-agent", "products": ["*"]},
        NARROW_VIVY: {"name": "vivy-agent", "products": ["vivy"]},
        NARROW_CONSTANCE: {"name": "constance-agent", "products": ["constance"]},
    }))
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    monkeypatch.setattr(lease, "TUNABLES", tmp_path / "absent.json")
    return TestClient(libapp.app)


def _h(token=WIDE):
    return {"Authorization": f"Bearer {token}",
            protocol.HEADER: str(protocol.CURRENT)}


def seed(item_id, **cols):
    """One item, with only the columns a case cares about."""
    cols.setdefault("product", "vivy")
    cols.setdefault("title", item_id)
    cols.setdefault("body", "synthetic body")
    cols.setdefault("refs", "{}")
    cols.setdefault("severity", "medium")
    cols.setdefault("created_by", "test-fixture")
    cols.setdefault("type", None)
    cols.setdefault("state", "open")
    cols.setdefault("created_at", _now())
    conn = sqlite3.connect(store.DB_PATH)
    names = ", ".join(["id", *cols])
    conn.execute(
        f"INSERT INTO issue_items ({names}) "
        f"VALUES ({','.join('?' * (len(cols) + 1))})",
        [item_id, *cols.values()])
    conn.commit()
    conn.close()


def _db():
    conn = sqlite3.connect(store.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _take_lease(root_id, *, holder="someone-else", alive=True):
    stamp = _now()
    conn = _db()
    conn.execute(
        "INSERT INTO issue_leases (id, root_id, taken_for, holder, taken_at, "
        "heartbeat_at) VALUES (?,?,?,?,?,?)",
        (f"lease_{root_id}", root_id, root_id, holder, stamp, stamp))
    conn.commit()
    conn.close()
    del alive  # every case here wants a live lease; kept for readability


def relate(client, item_id, epic_id, *, token=WIDE, owner=None):
    body = {"epic_id": epic_id}
    if owner is not None:
        body["owner"] = owner
    return client.post(f"/items/{item_id}/epic-relations", json=body,
                       headers=_h(token))


def get(client, item_id, *, token=WIDE):
    r = client.get(f"/items/{item_id}", headers=_h(token))
    assert r.status_code == 200, r.text
    return r.json()


# --- writing the relation -----------------------------------------------------


def test_relating_across_products_succeeds(client):
    """The whole point: an item in one product names an epic in another,
    which `_validate_parent` would refuse as a `parent_id`."""
    seed("epic_1", type=store.ITEM_TYPE_EPIC, product="vivy")
    seed("item_1", type=None, product="constance")

    r = relate(client, "item_1", "epic_1")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["item_id"] == "item_1"
    assert body["epic_id"] == "epic_1"
    assert body["created_by"]
    assert body["created_at"]


def test_relating_without_item_resolve_is_forbidden(client, monkeypatch):
    seed("epic_1", type=store.ITEM_TYPE_EPIC, product="vivy")
    seed("item_1", type=None, product="vivy")
    tokens = Path(identity.TOKENS)
    tokens.write_text(json.dumps({
        "no-resolve-token": {"name": "reader", "products": ["*"],
                             "withholds": ["item.resolve"]},
    }))
    r = relate(client, "item_1", "epic_1", token="no-resolve-token")
    assert r.status_code == 403 and "item.resolve" in r.json()["error"]


def test_target_must_be_an_epic(client):
    seed("task_1", type=None, product="vivy")
    seed("item_1", type=None, product="vivy")
    r = relate(client, "item_1", "task_1")
    assert r.status_code == 400 and "epic" in r.json()["error"]


def test_an_epic_cannot_be_related_to_itself(client):
    seed("epic_1", type=store.ITEM_TYPE_EPIC, product="vivy")
    r = relate(client, "epic_1", "epic_1")
    assert r.status_code == 400 and "itself" in r.json()["error"]
    assert _db().execute(
        "SELECT COUNT(*) FROM issue_epic_relations").fetchone()[0] == 0


def test_target_must_exist(client):
    seed("item_1", type=None, product="vivy")
    r = relate(client, "item_1", "no-such-item")
    assert r.status_code == 400 and "epic" in r.json()["error"]


def test_epic_id_is_required(client):
    seed("item_1", type=None, product="vivy")
    r = client.post("/items/item_1/epic-relations", json={}, headers=_h())
    assert r.status_code == 400 and "epic_id" in r.json()["error"]


def test_an_item_in_another_product_than_the_caller_is_not_found(client):
    seed("epic_1", type=store.ITEM_TYPE_EPIC, product="vivy")
    seed("item_1", type=None, product="constance")
    r = relate(client, "item_1", "epic_1", token=NARROW_VIVY)
    assert r.status_code == 404


def test_a_terminal_item_cannot_be_related(client):
    seed("epic_1", type=store.ITEM_TYPE_EPIC, product="vivy")
    seed("item_1", type=None, product="vivy", state="fixed")
    r = relate(client, "item_1", "epic_1")
    assert r.status_code == 409 and "terminal" in r.json()["error"]


def test_writing_the_same_pair_twice_is_idempotent(client):
    seed("epic_1", type=store.ITEM_TYPE_EPIC, product="vivy")
    seed("item_1", type=None, product="constance")

    first = relate(client, "item_1", "epic_1")
    assert first.status_code == 201, first.text
    second = relate(client, "item_1", "epic_1")
    assert second.status_code == 200, second.text
    assert second.json()["id"] == first.json()["id"]

    conn = _db()
    n = conn.execute(
        "SELECT COUNT(*) FROM issue_epic_relations WHERE item_id = 'item_1' "
        "AND epic_id = 'epic_1'").fetchone()[0]
    conn.close()
    assert n == 1


def test_an_item_held_by_another_session_refuses_the_write(client):
    seed("epic_1", type=store.ITEM_TYPE_EPIC, product="vivy")
    seed("item_1", type=None, product="vivy")
    _take_lease("item_1", holder="someone-else")

    r = relate(client, "item_1", "epic_1", owner="me")
    assert r.status_code == 409 and r.json().get("reason") == "held"


# --- reading it back: the epic's own listing ----------------------------------


def test_the_epic_lists_related_items_beside_its_children(client):
    seed("epic_1", type=store.ITEM_TYPE_EPIC, product="vivy")
    seed("child_1", type=None, product="vivy", parent_id="epic_1")
    seed("related_1", type=None, product="constance")
    relate(client, "related_1", "epic_1")

    body = get(client, "epic_1")
    assert body["progress"]["children"] == 1          # unaffected: parent_id only
    related_ids = [r["id"] for r in body["related_items"]]
    assert related_ids == ["related_1"]
    assert body["related_items"][0]["product"] == "constance"


def test_a_non_epic_carries_no_related_items_key(client):
    seed("task_1", type=None, product="vivy")
    body = get(client, "task_1")
    assert "related_items" not in body


def test_an_epic_with_no_relations_has_an_empty_list(client):
    seed("epic_1", type=store.ITEM_TYPE_EPIC, product="vivy")
    assert get(client, "epic_1")["related_items"] == []


def test_a_narrowly_scoped_caller_does_not_learn_the_related_items_product(client):
    """The scope rule `_lease_root_visible` applies to a lease's blocking
    root applies here to a relation's target: a caller scoped to one
    product must not learn another product's item ids through this read."""
    seed("epic_1", type=store.ITEM_TYPE_EPIC, product="vivy")
    seed("related_1", type=None, product="constance")
    relate(client, "related_1", "epic_1")

    body = get(client, "epic_1", token=NARROW_VIVY)
    assert body["related_items"] == []

    body = get(client, "epic_1", token=WIDE)
    assert [r["id"] for r in body["related_items"]] == ["related_1"]


# --- the invariant: never fed to the lease walk or the retype count -----------


def test_the_relation_never_feeds_the_ancestor_or_subtree_walk(client):
    """Direct proof against `lease.py`'s own functions: an item related to
    an epic only through `issue_epic_relations`, with no `parent_id` edge,
    is invisible to both walks that every lease disclosure and the
    epic-retype descendant count rest on."""
    seed("epic_1", type=store.ITEM_TYPE_EPIC, product="vivy")
    seed("related_1", type=None, product="constance")
    relate(client, "related_1", "epic_1")

    conn = _db()
    assert "related_1" not in lease.subtree_ids(conn, "epic_1")
    assert "epic_1" not in lease.ancestor_ids(conn, "related_1")
    conn.close()


def test_a_lease_on_the_epic_does_not_fence_the_related_item(client):
    """A checkout refusal's fence list walks the same `parent_id` tree —
    this proves a lease taken on the epic leaves a cross-product related
    item free to check out and correct, with no 409 naming the epic's
    holder."""
    seed("epic_1", type=store.ITEM_TYPE_EPIC, product="vivy")
    seed("related_1", type=None, product="constance")
    relate(client, "related_1", "epic_1")
    _take_lease("epic_1", holder="someone-else")

    r = client.post("/items/related_1/checkout", json={"owner": "me"},
                    headers=_h())
    assert r.status_code == 200, r.text
    assert r.json()["lease"]["holder"] == "me"


def test_the_relation_does_not_count_toward_the_epic_retype_descendant_check(client):
    """`_do_correct` refuses to retype an epic that still has `parent_id`
    children, counted through `lease.subtree_ids`. A relation-only item must
    not be counted there, or an epic with none of its own children could
    never be retyped."""
    seed("epic_1", type=store.ITEM_TYPE_EPIC, product="vivy")
    seed("related_1", type=None, product="constance")
    relate(client, "related_1", "epic_1")

    r = client.post("/items/epic_1/correct", json={"type": None}, headers=_h())
    assert r.status_code == 200, r.text
    assert r.json()["type"] is None
