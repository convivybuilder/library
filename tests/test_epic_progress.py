"""An epic's progress, computed from its children at read time rather than
stored (issue_01M12NE0T3NZ445M5FEGXVH3ZY, clauses 1 and 6 of
issue_01KZFB4KXQD82G3PDHMADXD6NA): `GET /items/{id}` on an epic carries a
`progress` object built fresh from `issue_items` rows whose `parent_id` is
the epic, on every request.

**Built on a database from the real migration**, `test_ledger_routes.py`'s
reason: the behaviour under test is a `GROUP BY` over live column values, not
a shape a hand-written fixture schema could drift out of step with.
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
import protocol
import store

REPO = Path(__file__).resolve().parent.parent
WIDE = "wide-token"
NARROW = "narrow-token"


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat(" ")


@pytest.fixture
def client(tmp_path, monkeypatch, vector_index):
    db = tmp_path / "control.db"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LIBRARY_DB": str(db)})
    assert result.returncode == 0, result.stderr
    vector_index(db)

    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        WIDE: {"name": "wide-agent", "products": ["*"]},
        NARROW: {"name": "narrow-agent", "products": ["vivy"]},
    }))
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "TOKENS", tokens)
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


def resolve(item_id):
    """Move an item straight to a terminal state, bypassing `/resolve` —
    these tests are about what `GET /items/{id}` reports from a state, not
    about the resolve route's own gates."""
    conn = sqlite3.connect(store.DB_PATH)
    conn.execute("UPDATE issue_items SET state = 'fixed' WHERE id = ?",
                 (item_id,))
    conn.commit()
    conn.close()


def get(client, item_id, *, token=WIDE):
    r = client.get(f"/items/{item_id}", headers=_h(token))
    assert r.status_code == 200, r.text
    return r.json()


# --- mixed children ---------------------------------------------------------

def test_mixed_children_report_exact_counts_and_percentage(client):
    seed("epic_1", type=store.ITEM_TYPE_EPIC)
    seed("child_open", parent_id="epic_1", state="open")
    seed("child_claimed", parent_id="epic_1", state="claimed")
    seed("child_fixed", parent_id="epic_1", state="fixed")
    seed("child_wontfix", parent_id="epic_1", state="wontfix")

    progress = get(client, "epic_1")["progress"]
    assert progress["children"] == 4
    assert progress["by_state"] == {
        "open": 1, "claimed": 1, "fixed": 1, "wontfix": 1}
    assert progress["terminal"] == 2
    assert progress["percent_terminal"] == 0.5
    assert progress["reachable"] is False
    assert progress["reached"] is False


# --- zero children: the case clause 6 names ---------------------------------

def test_zero_children_epic_is_reachable_and_reached_only_when_resolved(client):
    seed("epic_1", type=store.ITEM_TYPE_EPIC, state="open")

    progress = get(client, "epic_1")["progress"]
    assert progress["children"] == 0
    assert progress["by_state"] == {}
    assert progress["terminal"] == 0
    assert progress["percent_terminal"] is None
    assert progress["reachable"] is True
    assert progress["reached"] is False

    resolve("epic_1")
    progress = get(client, "epic_1")["progress"]
    assert progress["reachable"] is True
    assert progress["reached"] is True


# --- reachable and reached diverge in both directions -----------------------

def test_all_children_terminal_but_epic_open_is_reachable_not_reached(client):
    seed("epic_1", type=store.ITEM_TYPE_EPIC, state="open")
    seed("child_a", parent_id="epic_1", state="fixed")
    seed("child_b", parent_id="epic_1", state="wontfix")

    progress = get(client, "epic_1")["progress"]
    assert progress["reachable"] is True
    assert progress["reached"] is False


def test_terminal_epic_with_open_child_is_reached_not_reachable(client):
    seed("epic_1", type=store.ITEM_TYPE_EPIC, state="fixed")
    seed("child_a", parent_id="epic_1", state="open")

    progress = get(client, "epic_1")["progress"]
    assert progress["reachable"] is False
    assert progress["reached"] is True


# --- a non-epic carries no progress key --------------------------------------

def test_a_task_has_no_progress_key(client):
    seed("task_1", type=None)
    body = get(client, "task_1")
    assert "progress" not in body


# --- scope: children counted the same way a listing would count them --------

def test_progress_children_match_the_scoped_listing(client):
    """The write path refuses a cross-product parent (`_validate_parent`,
    `app.py`): a caller cannot create a real child in a product its epic is
    not in, so this cannot forge a case where `progress`'s count and the
    listing's count would answer differently. What it can and does check is
    that they are the *same* query on the same scope — `epic_progress`
    reuses `_scope_clause` rather than re-deriving it, and this is the read
    that would catch the two drifting apart if it ever re-spelled the rule
    instead."""
    seed("epic_1", type=store.ITEM_TYPE_EPIC, product="vivy")
    seed("child_a", parent_id="epic_1", product="vivy", state="open")
    seed("child_b", parent_id="epic_1", product="vivy", state="fixed")

    progress = get(client, "epic_1", token=NARROW)["progress"]
    listing = client.get("/items?parent_id=epic_1", headers=_h(NARROW))
    assert listing.status_code == 200, listing.text
    assert progress["children"] == listing.json()["total"]
    assert progress["children"] == 2
