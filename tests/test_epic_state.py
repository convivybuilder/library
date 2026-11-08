"""`epic_state` (library/decisions/0005): additive, computed at read time,
`None` unless the row is an approved epic.

`not_started`/`in_progress` read the epic's own children; `completed`/
`canceled` read the epic's own `state` once it is terminal — `fixed` is
`completed`, anything else terminal is `canceled`. Checked through `GET
/items/{id}` (`store.epic_state_of`) and `GET /items` (`store.epic_states_of`,
the batched form), since the two must not disagree about one row.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

import app as libapp
import identity
import lease
import protocol
import store

AGENT = "agent-token"


def _ts(**delta):
    return (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
            - dt.timedelta(**delta)).isoformat(" ")


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = tmp_path / "control.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE issue_items (
            id TEXT PRIMARY KEY, product TEXT, title TEXT, body TEXT, refs TEXT,
            severity TEXT, state TEXT, claimed_by TEXT, claimed_by_crew TEXT, claimed_at TEXT,
            resolution TEXT, resolved_by TEXT, resolved_at TEXT, outcome_note TEXT,
            once_key TEXT UNIQUE, created_by TEXT, created_at TEXT, run_id TEXT,
            type TEXT, created_by_kind TEXT, needs TEXT, park_reason TEXT,
            unpark_condition TEXT, parked_at TEXT, embedded_at TEXT,
            parent_id TEXT, topics TEXT, maturity TEXT, specification TEXT,
            definition_of_done TEXT, prerequisites TEXT,
            model_choice TEXT, model_reason TEXT, model_set_by_client TEXT,
            model_acting_human TEXT, model_set_at TEXT, found_during TEXT, item_key TEXT, short_description TEXT);
        CREATE TABLE issue_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT, actor TEXT,
            from_state TEXT, to_state TEXT, at TEXT, note TEXT);
        CREATE TABLE issue_leases (
            id TEXT PRIMARY KEY, root_id TEXT, taken_for TEXT, holder TEXT, holder_crew TEXT,
            run_id TEXT, taken_at TEXT, heartbeat_at TEXT, released_at TEXT,
            release_reason TEXT, released_by TEXT, note TEXT);
        CREATE UNIQUE INDEX ix_live_root ON issue_leases (root_id)
            WHERE released_at IS NULL;
        CREATE TABLE issue_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT, actor TEXT,
            kind TEXT, body TEXT, run_id TEXT, at TEXT);
        CREATE TABLE issue_epic_relations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT, epic_id TEXT,
            created_by TEXT, created_at TEXT);
    """)
    # (id, type, state, maturity, parent_id)
    rows = [
        ("not_epic", None, "open", None, None),
        ("epic_proposed", "epic", "open", "proposed", None),
        ("epic_declined", "epic", "open", "declined", None),
        ("epic_not_started", "epic", "open", "approved", None),
        ("epic_in_progress_claimed", "epic", "open", "approved", None),
        ("claimed_child", None, "claimed", None, "epic_in_progress_claimed"),
        ("epic_in_progress_pr", "epic", "open", "approved", None),
        ("pr_child", None, "pr-open", None, "epic_in_progress_pr"),
        ("epic_in_progress_fixed_child", "epic", "open", "approved", None),
        ("fixed_child", None, "fixed", None, "epic_in_progress_fixed_child"),
        ("epic_not_started_with_open_child", "epic", "open", "approved", None),
        ("open_child", None, "open", None, "epic_not_started_with_open_child"),
        ("epic_completed", "epic", "fixed", "approved", None),
        ("epic_canceled_wontfix", "epic", "wontfix", "approved", None),
        ("epic_canceled_duplicate", "epic", "duplicate", "approved", None),
    ]
    conn.executemany(
        "INSERT INTO issue_items (id, product, title, type, state, maturity, "
        "parent_id, severity, needs, created_by_kind, created_at, refs, "
        "topics) VALUES (?,'fennick',?,?,?,?,?,'medium','agent','agent',?,"
        "'{}','[]')",
        [(r[0], r[0], r[1], r[2], r[3], r[4], _ts(days=1)) for r in rows])
    conn.commit()
    conn.close()

    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        AGENT: {"name": "an-agent", "products": ["*"]},
    }))
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    monkeypatch.setattr(lease, "TUNABLES", tmp_path / "absent.json")
    return TestClient(libapp.app)


def _h():
    return {"Authorization": f"Bearer {AGENT}",
            protocol.HEADER: str(protocol.CURRENT)}


def _get(client, item_id):
    return client.get(f"/items/{item_id}", headers=_h())


@pytest.mark.parametrize("item_id,expected", [
    ("not_epic", None),
    ("epic_proposed", None),
    ("epic_declined", None),
    ("epic_not_started", "not_started"),
    ("epic_not_started_with_open_child", "not_started"),
    ("epic_in_progress_claimed", "in_progress"),
    ("epic_in_progress_pr", "in_progress"),
    ("epic_in_progress_fixed_child", "in_progress"),
    ("epic_completed", "completed"),
    ("epic_canceled_wontfix", "canceled"),
    ("epic_canceled_duplicate", "canceled"),
])
def test_epic_state_on_get_item(client, item_id, expected):
    r = _get(client, item_id)
    assert r.status_code == 200
    assert r.json()["epic_state"] == expected


def test_epic_state_agrees_on_the_list_route(client):
    """`store.epic_states_of`'s batched form must not disagree with
    `store.epic_state_of`'s single-row form for the same rows."""
    r = client.get("/items", params={"product": "fennick", "limit": 100},
                   headers=_h())
    assert r.status_code == 200
    by_id = {item["id"]: item["epic_state"] for item in r.json()["items"]}
    assert by_id["epic_not_started"] == "not_started"
    assert by_id["epic_in_progress_claimed"] == "in_progress"
    assert by_id["epic_completed"] == "completed"
    assert by_id["epic_canceled_wontfix"] == "canceled"
    assert by_id["epic_proposed"] is None
    assert by_id["epic_declined"] is None
    assert by_id["not_epic"] is None
