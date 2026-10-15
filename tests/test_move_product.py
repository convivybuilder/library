"""Moving a work item, and its subtree, to another product
(issue_01M0AT44FBFJ7CHP84V027PG7J).

The schema is the real one: built once by `alembic upgrade head` and copied for
each test, so the move runs against the unique `(product, item_key)` index and
every column the migrations define, not a hand-written approximation of them.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as libapp
import identity
import lease
import projects
import protocol
import store

REPO = Path(__file__).resolve().parent.parent
WIDE = "agent-token-wide"
VIVY = "agent-token-vivy"
TWO = "agent-token-two"
REGISTRY = {
    "vivy": {"aliases": ["viv"], "repos": ["vivy-app"]},
    "constance": {"aliases": [], "repos": []},
}
T0 = "2026-08-01 12:00:00.000000"


@pytest.fixture(scope="module")
def schema(tmp_path_factory):
    db = tmp_path_factory.mktemp("schema") / "library.db"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=str(REPO),
        capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(REPO), "LIBRARY_DB": str(db)})
    assert result.returncode == 0, result.stderr
    return db


def _item(conn, id_, product, key, *, type_=None, parent=None, state="open",
          found_during=None):
    conn.execute(
        "INSERT INTO issue_items (id, product, title, body, refs, severity, "
        "state, type, created_by, created_at, parent_id, item_key, "
        "found_during, topics, prerequisites) "
        "VALUES (?,?,?,'',  '{}','low',?,?,'t',?,?,?,?,'[]','[]')",
        (id_, product, f"title {id_}", state, type_, T0, parent, key,
         found_during))


@pytest.fixture
def client(schema, tmp_path, monkeypatch):
    db = tmp_path / "library.db"
    shutil.copy(schema, db)
    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO corrections_queues (id, product, created_at) VALUES (?,?,?)",
        [("cq_vivy", "vivy", T0), ("cq_constance", "constance", T0)])
    # vivy: E1 with two sub-items and a sub-epic E2 holding one of its own.
    _item(conn, "e1", "vivy", "E1", type_="epic")
    _item(conn, "w1", "vivy", "E1·1", parent="e1")
    _item(conn, "w2", "vivy", "E1·2", parent="e1")
    _item(conn, "e2", "vivy", "E2", type_="epic", parent="e1")
    _item(conn, "w3", "vivy", "E2·1", parent="e2")
    _item(conn, "c1", "vivy", "CQ·1", parent="cq_vivy")
    # constance already has an E1 and a CQ·1, so keeping keys would collide.
    _item(conn, "ce1", "constance", "E1", type_="epic")
    _item(conn, "cc1", "constance", "CQ·1", parent="cq_constance")
    conn.executemany(
        "INSERT INTO issue_item_key_counters (scope_kind, scope_id, next_value) "
        "VALUES (?,?,?)",
        [("epic", "vivy", 3), ("item", "e1", 3), ("item", "e2", 2),
         ("correction", "vivy", 2), ("epic", "constance", 2),
         ("correction", "constance", 2)])
    conn.commit()
    conn.close()
    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        WIDE: {"name": "wide-agent", "products": ["*"]},
        VIVY: {"name": "vivy-agent", "products": ["vivy"]},
        TWO: {"name": "two-agent", "products": ["vivy", "constance"]},
    }))
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps(REGISTRY))
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(projects, "REGISTRY_PATH", registry)
    monkeypatch.setattr(lease, "TUNABLES", tmp_path / "absent.json")
    projects.reload()
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    yield TestClient(libapp.app)
    projects.reload()


def _h(token=WIDE):
    return {"Authorization": f"Bearer {token}",
            protocol.HEADER: str(protocol.CURRENT)}


def _move(client, item_id, product, *, owner="mover", token=WIDE):
    return client.post(f"/items/{item_id}/product",
                       json={"product": product, "owner": owner},
                       headers=_h(token))


def _rows():
    conn = sqlite3.connect(store.DB_PATH)
    conn.row_factory = sqlite3.Row
    out = {r["id"]: r for r in conn.execute("SELECT * FROM issue_items")}
    conn.close()
    return out


def test_an_epic_moves_with_its_whole_subtree_and_new_keys(client):
    r = _move(client, "e1", "constance")
    assert r.status_code == 200, r.text
    rows = _rows()
    for mid in ("e1", "w1", "w2", "e2", "w3"):
        assert rows[mid]["product"] == "constance"
    # Epics take the next numbers in constance; sub-items keep their suffix
    # under their epic's new key.
    assert {m: rows[m]["item_key"] for m in ("e1", "w1", "w2", "e2", "w3")} == {
        "e1": "E2", "w1": "E2·1", "w2": "E2·2", "e2": "E3", "w3": "E3·1"}
    assert rows["e1"]["parent_id"] is None
    assert rows["e2"]["parent_id"] == "e1"
    assert {(m["id"], m["old_key"], m["new_key"]) for m in r.json()["moved"]} == {
        ("e1", "E1", "E2"), ("w1", "E1·1", "E2·1"), ("w2", "E1·2", "E2·2"),
        ("e2", "E2", "E3"), ("w3", "E2·1", "E3·1")}
    # Untouched: the item left in vivy and constance's own items.
    assert rows["c1"]["product"] == "vivy"
    assert rows["ce1"]["item_key"] == "E1"


def test_every_moved_item_records_the_move_in_its_history(client):
    assert _move(client, "e1", "constance").status_code == 200
    conn = sqlite3.connect(store.DB_PATH)
    notes = dict(conn.execute("SELECT item_id, note FROM issue_events"))
    conn.close()
    assert set(notes) >= {"e1", "w1", "w2", "e2", "w3"}
    assert "'vivy' to 'constance'" in notes["e1"]
    assert "'E1' → 'E2'" in notes["e1"]


def test_a_correction_lands_in_the_new_products_queue_with_a_new_key(client):
    r = _move(client, "c1", "constance")
    assert r.status_code == 200, r.text
    row = _rows()["c1"]
    assert (row["product"], row["parent_id"], row["item_key"]) == (
        "constance", "cq_constance", "CQ·2")


def test_a_sub_item_moved_alone_leaves_its_epic_for_the_new_queue(client):
    r = _move(client, "w1", "constance")
    assert r.status_code == 200, r.text
    row = _rows()["w1"]
    assert (row["parent_id"], row["item_key"]) == ("cq_constance", "CQ·2")


def test_the_new_product_resolves_through_the_registry(client):
    assert _move(client, "c1", "VIV").status_code == 409   # already there
    r = _move(client, "c1", "no-such-product")
    assert r.status_code == 400 and "no project or scope" in r.json()["error"]


def test_the_caller_must_hold_both_products(client):
    # vivy-only: the destination is out of scope, answered as not found.
    assert _move(client, "c1", "constance", token=VIVY).status_code == 404
    # ...and the source, for an item in a product the caller cannot see.
    assert _move(client, "cc1", "vivy", token=VIVY).status_code == 404
    assert _rows()["c1"]["product"] == "vivy"


def test_another_holders_lease_inside_the_subtree_refuses_the_whole_move(client):
    conn = sqlite3.connect(store.DB_PATH)
    conn.execute(
        "INSERT INTO issue_leases (id, root_id, taken_for, holder, taken_at, "
        "heartbeat_at) VALUES ('l1', 'e2', 'w3', 'someone-else', ?, ?)",
        (_now(), _now()))
    conn.commit()
    conn.close()
    r = _move(client, "e1", "constance")
    assert r.status_code == 409 and r.json()["reason"] == "held"
    assert "someone-else" in r.json()["error"]
    assert all(row["product"] == "vivy" for k, row in _rows().items()
               if k in ("e1", "w1", "w2", "e2", "w3"))


def test_a_lease_beneath_the_item_outside_the_callers_scope_withholds_its_holder(
        client):
    """`_lease_root_visible`, as at every other disclosure site. Only a write
    that bypassed `_validate_parent` could put a descendant in another product,
    so the fixture makes that write directly: the caller holds both products
    of the move but not the one the blocking lease sits in."""
    conn = sqlite3.connect(store.DB_PATH)
    conn.execute("UPDATE issue_items SET product = 'elsewhere' WHERE id = 'e2'")
    conn.execute(
        "INSERT INTO issue_leases (id, root_id, taken_for, holder, taken_at, "
        "heartbeat_at) VALUES ('l1', 'e2', 'w3', 'someone-else', ?, ?)",
        (_now(), _now()))
    conn.commit()
    conn.close()
    r = _move(client, "e1", "constance", token=TWO)
    assert r.status_code == 409 and r.json()["reason"] == "held"
    assert "someone-else" not in r.text
    assert "lease" not in r.json()
    # A caller who can see that product is told who holds it.
    r = _move(client, "e1", "constance")
    assert r.status_code == 409 and "someone-else" in r.json()["error"]


def test_found_during_that_would_cross_products_refuses_by_name(client):
    conn = sqlite3.connect(store.DB_PATH)
    conn.execute("UPDATE issue_items SET found_during = 'e1' WHERE id = 'c1'")
    conn.commit()
    conn.close()
    r = _move(client, "e1", "constance")
    assert r.status_code == 409
    assert r.json()["left_behind_naming_a_moved_epic"] == ["c1"]
    r = _move(client, "c1", "constance")
    assert r.status_code == 409
    assert r.json()["moving_away_from_their_epic"] == ["c1"]


def test_an_approved_epic_leaves_the_old_products_course(client):
    conn = sqlite3.connect(store.DB_PATH)
    conn.execute("UPDATE issue_items SET maturity = 'approved' WHERE id = 'e1'")
    conn.execute(
        "INSERT INTO courses (id, product, title, entries, status, version, "
        "created_by, created_at) VALUES ('course_v', 'vivy', 'Route', ?, "
        "'live', 1, 't', ?)", (json.dumps(["e1"]), T0))
    conn.commit()
    conn.close()
    r = _move(client, "e1", "constance")
    assert r.status_code == 200, r.text
    assert r.json()["left_courses"] == [
        {"item_id": "e1", "course_id": "course_v", "position": 1}]
    conn = sqlite3.connect(store.DB_PATH)
    entries = conn.execute("SELECT entries FROM courses").fetchone()[0]
    conn.close()
    assert json.loads(entries) == []


def test_a_terminal_item_does_not_move(client):
    conn = sqlite3.connect(store.DB_PATH)
    conn.execute("UPDATE issue_items SET state = 'fixed' WHERE id = 'c1'")
    conn.commit()
    conn.close()
    assert _move(client, "c1", "constance").status_code == 409


def test_the_epic_retype_count_stays_same_product_after_a_move(client):
    """The invariant the move exists to keep: no parent edge crosses products."""
    assert _move(client, "e1", "constance").status_code == 200
    rows = _rows()
    for row in rows.values():
        parent = rows.get(row["parent_id"])
        if parent is not None:
            assert parent["product"] == row["product"], row["id"]


def _now():
    import datetime as dt
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat(" ")


# --- a crewed lease beneath the item (CQ·10; security-reviewer, PR 287) -----


def _plant_lease(root, holder, crew):
    conn = sqlite3.connect(store.DB_PATH)
    conn.execute(
        "INSERT INTO issue_leases (id, root_id, taken_for, holder, holder_crew, "
        "taken_at, heartbeat_at) VALUES (?,?,?,?,?, "
        "strftime('%Y-%m-%d %H:%M:%f','now'), "
        "strftime('%Y-%m-%d %H:%M:%f','now'))",
        (f"lease_{root}", root, root, holder, crew))
    conn.commit()
    conn.close()


def _crewed_tokens(tmp_path, monkeypatch):
    tokens = tmp_path / "crewed-tokens.json"
    tokens.write_text(json.dumps({
        "crew-a-token": {"name": "a", "products": ["*"], "crew": "crew-alpha"},
        "crew-b-token": {"name": "b", "products": ["*"], "crew": "crew-bravo"},
    }))
    monkeypatch.setattr(identity, "TOKENS", tokens)


def test_a_move_over_another_crews_lease_is_refused_with_its_name(
        client, tmp_path, monkeypatch):
    """Typing the holder's name used to skip a lease beneath the item whatever
    crew took it, and the whole subtree moved out from under it."""
    _crewed_tokens(tmp_path, monkeypatch)
    _plant_lease("e2", "mover", "crew-alpha")
    r = _move(client, "e1", "constance", owner="mover", token="crew-b-token")
    assert r.status_code == 409
    assert r.json()["reason"] == "held"
    assert _rows()["e1"]["product"] == "vivy"


def test_a_move_over_its_own_crews_lease_proceeds(client, tmp_path, monkeypatch):
    _crewed_tokens(tmp_path, monkeypatch)
    _plant_lease("e2", "mover", "crew-alpha")
    r = _move(client, "e1", "constance", owner="mover", token="crew-a-token")
    assert r.status_code == 200, r.text
