"""Declining and reproposing an epic (library/decisions/0005): the other two
transitions on the maturity ladder, gated exactly like `POST
/items/{item_id}/approve` and recorded the same way.

**DECLINE ONLY MOVES A `proposed` EPIC.** An `approved` one is refused with a
repropose hint; this file's `epic_c` is approved and on a live Course, so the
409 is checked against a real Course entry rather than an epic that was never
placed. The cascade resolves every open, claimed and pr-open child `wontfix`
in the same transaction, leaves a child already resolved exactly as it is,
and never touches `parent_id`.

**REPROPOSE MOVES `approved` OR `declined` BACK TO `proposed`.** From
`approved` it leaves the Course the same way a demoting `/correct` does,
touching no child; from `declined` it reopens nothing. A `completed` or
`canceled` approved epic — its own `state` already terminal — is refused,
because a finished epic is a record of what happened.

**NEITHER ROUTE READS AS `/correct`.** Both are gated on `epic.approve` and
`course.write`, plus an attested `acting_human`, exactly like `/approve` —
an agent credential reaches neither.
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
OPERATOR = "deck-token"
BOSUN = "bosun-token"
HUMAN = "operator@example.com"


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
        CREATE TABLE issue_item_key_counters (
            scope_kind TEXT, scope_id TEXT, next_value INTEGER,
            PRIMARY KEY (scope_kind, scope_id));
        CREATE TABLE issue_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT, actor TEXT,
            from_state TEXT, to_state TEXT, at TEXT, note TEXT);
        CREATE TABLE issue_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT, actor TEXT,
            kind TEXT, body TEXT, run_id TEXT, at TEXT);
        CREATE TABLE issue_leases (
            id TEXT PRIMARY KEY, root_id TEXT, taken_for TEXT, holder TEXT, holder_crew TEXT,
            run_id TEXT, taken_at TEXT, heartbeat_at TEXT, released_at TEXT,
            release_reason TEXT, released_by TEXT, note TEXT);
        CREATE UNIQUE INDEX ix_live_root ON issue_leases (root_id)
            WHERE released_at IS NULL;
        CREATE TABLE lease_refusals (
            id TEXT PRIMARY KEY, at TEXT, requested_item_id TEXT, product TEXT,
            requester TEXT, actor TEXT, blocking_lease_id TEXT,
            blocking_root_id TEXT, blocking_holder TEXT, blocks_by TEXT, blocking_taken_for TEXT);
        CREATE TABLE courses (
            id TEXT PRIMARY KEY, product TEXT, title TEXT, entries TEXT,
            status TEXT, version INTEGER, created_by TEXT, created_at TEXT,
            updated_by TEXT, updated_at TEXT);
        CREATE UNIQUE INDEX ix_courses_live_product
            ON courses (product) WHERE status = 'live';
        CREATE TABLE course_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, course_id TEXT, actor TEXT,
            action TEXT, entries TEXT, note TEXT, at TEXT);
        CREATE TABLE issue_epic_relations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT, epic_id TEXT,
            created_by TEXT, created_at TEXT);
        CREATE TABLE corrections_queues (
            id TEXT PRIMARY KEY, product TEXT UNIQUE, created_at TEXT);
    """)
    # (id, product, type, state, maturity, specification, definition_of_done)
    rows = [
        ("epic_a", "fennick", "epic", "open", "proposed", "scope a", "done a"),
        ("epic_b", "fennick", "epic", "open", "proposed", "scope b", "done b"),
        ("epic_c", "fennick", "epic", "open", "approved", "scope c", "done c"),
        ("epic_declined", "fennick", "epic", "open", "declined", "scope d",
         "done d"),
        ("epic_done", "fennick", "epic", "fixed", "approved", "s", "d"),
        ("epic_wontfixed", "fennick", "epic", "wontfix", "approved", "s", "d"),
        ("a_bug", "fennick", "bug", "open", None, None, None),
        ("epic_other", "marlow", "epic", "open", "proposed", "s", "d"),
    ]
    conn.executemany(
        "INSERT INTO issue_items (id, product, type, state, maturity, "
        "specification, definition_of_done, title, severity, needs, "
        "created_by_kind, created_at, refs, topics) "
        "VALUES (?,?,?,?,?,?,?,'a title','medium','agent','agent',?,'{}','[]')",
        [(*r, _ts(days=2)) for r in rows])
    # Children of epic_b: one of each active state, plus one already resolved.
    children = [
        ("b_open", "fennick", None, "open", "epic_b"),
        ("b_claimed", "fennick", None, "claimed", "epic_b"),
        ("b_pr", "fennick", None, "pr-open", "epic_b"),
        ("b_fixed", "fennick", None, "fixed", "epic_b"),
    ]
    conn.executemany(
        "INSERT INTO issue_items (id, product, type, state, parent_id, "
        "title, severity, needs, created_by_kind, created_at, refs, topics) "
        "VALUES (?,?,?,?,?,'a child','medium','agent','agent',?,'{}','[]')",
        [(*c, _ts(days=2)) for c in children])
    conn.execute(
        "INSERT INTO courses (id, product, title, entries, status, version, "
        "created_by, created_at) VALUES ('course_fennick', 'fennick', 'The route', ?, "
        "'live', 3, 'test', ?)", (json.dumps(["epic_c"]), _ts(days=3)))
    conn.commit()
    conn.close()

    agents, clients = tmp_path / "api-tokens.json", tmp_path / "client-tokens.json"
    agents.write_text(json.dumps({
        AGENT: {"name": "an-agent", "products": ["*"]},
    }))
    clients.write_text(json.dumps({
        OPERATOR: {"name": "deck", "products": ["*"]},
        BOSUN: {"name": "bosun", "products": ["*"],
                "withholds": ["epic.approve"]},
    }))
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "TOKENS", agents)
    monkeypatch.setattr(identity, "CLIENT_TOKENS", clients)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    monkeypatch.setattr(lease, "TUNABLES", tmp_path / "absent.json")
    return TestClient(libapp.app, raise_server_exceptions=False)


def _h(token):
    return {"Authorization": f"Bearer {token}",
            protocol.HEADER: str(protocol.CURRENT)}


def _db():
    conn = sqlite3.connect(store.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _row(item_id):
    conn = _db()
    row = conn.execute("SELECT * FROM issue_items WHERE id = ?",
                       (item_id,)).fetchone()
    conn.close()
    return row


def _course(course_id="course_fennick"):
    conn = _db()
    row = conn.execute("SELECT * FROM courses WHERE id = ?",
                       (course_id,)).fetchone()
    conn.close()
    return {"entries": json.loads(row["entries"]), "version": row["version"]}


def _notes(item_id):
    conn = _db()
    rows = [r["body"] for r in conn.execute(
        "SELECT body FROM issue_comments WHERE item_id = ? ORDER BY id",
        (item_id,)).fetchall()]
    conn.close()
    return rows


def _decline(client, item_id, token=OPERATOR, **body):
    body.setdefault("acting_human", HUMAN)
    return client.post(f"/items/{item_id}/decline", json=body, headers=_h(token))


def _repropose(client, item_id, token=OPERATOR, **body):
    body.setdefault("acting_human", HUMAN)
    return client.post(f"/items/{item_id}/repropose", json=body,
                       headers=_h(token))


# --- decline ----------------------------------------------------------------


def test_declining_a_proposed_epic_sets_declined(client):
    r = _decline(client, "epic_a")
    assert r.status_code == 200
    assert r.json()["item"]["maturity"] == "declined"
    assert _row("epic_a")["maturity"] == "declined"


def test_decline_cascades_open_children_wontfix_and_leaves_resolved_ones(
        client):
    r = _decline(client, "epic_b", reason="scope abandoned")
    assert r.status_code == 200
    assert sorted(r.json()["decline"]["children_resolved"]) == [
        "b_claimed", "b_open", "b_pr"]
    for child_id in ("b_open", "b_claimed", "b_pr"):
        row = _row(child_id)
        assert row["state"] == "wontfix"
        assert row["resolution"] == "wontfix"
        assert row["outcome_note"] == "scope abandoned"
        assert row["parent_id"] == "epic_b"
    # Already resolved, and left exactly as it was.
    fixed = _row("b_fixed")
    assert fixed["state"] == "fixed" and fixed["resolution"] is None


def test_declining_an_approved_epic_is_refused_with_a_repropose_hint(client):
    r = _decline(client, "epic_c")
    assert r.status_code == 409
    assert r.json()["reason"] == "approved"
    assert "repropose" in r.json()["hint"]
    assert _row("epic_c")["maturity"] == "approved"
    assert _course()["entries"] == ["epic_c"]


def test_declining_an_already_declined_epic_is_refused(client):
    r = _decline(client, "epic_declined")
    assert r.status_code == 409
    assert "proposed" in r.json()["error"]


def test_declining_a_non_epic_is_refused(client):
    r = _decline(client, "a_bug")
    assert r.status_code == 400
    assert "only an epic" in r.json()["error"]


def test_an_agent_credential_cannot_decline(client):
    r = _decline(client, "epic_a", token=AGENT)
    assert r.status_code == 403
    assert _row("epic_a")["maturity"] == "proposed"


def test_a_withheld_approved_client_cannot_decline(client):
    r = _decline(client, "epic_a", token=BOSUN)
    assert r.status_code == 403


def test_decline_without_acting_human_is_refused_and_writes_nothing(client):
    r = client.post("/items/epic_a/decline", json={}, headers=_h(OPERATOR))
    assert r.status_code == 403
    assert _row("epic_a")["maturity"] == "proposed"


def test_decline_records_actor_human_reason_and_digests(client):
    r = _decline(client, "epic_a", reason="will not happen")
    assert r.status_code == 200
    decline = r.json()["decline"]
    assert decline["declined_by"] == "deck"
    assert decline["acting_human"] == HUMAN
    assert decline["reason"] == "will not happen"
    assert decline["specification_sha256"] is not None
    assert decline["definition_of_done_sha256"] is not None
    assert "Declined" in _notes("epic_a")[0]


def test_decline_takes_a_live_course_entry_off_and_remembers_position(client):
    """Reached only through a data state this route does not itself produce —
    a `proposed` epic the fixture seeds directly onto a live Course — because
    the ordinary write path never approves and then un-approves an epic back
    to `proposed` while leaving it placed. `_leave_course` is exercised here
    anyway, matching the build list's instruction to reuse it."""
    conn = _db()
    conn.execute("UPDATE courses SET entries = ? WHERE id = 'course_fennick'",
                (json.dumps(["epic_c", "epic_a"]),))
    conn.commit()
    conn.close()
    r = _decline(client, "epic_a")
    assert r.status_code == 200
    assert r.json()["decline"]["left_course"] == {
        "course_id": "course_fennick", "position": 2}
    assert _course()["entries"] == ["epic_c"]


def _insert_epic(conn, item_id, *, parent_id=None, state="open",
                 maturity="proposed", product="fennick"):
    conn.execute(
        "INSERT INTO issue_items (id, product, type, state, maturity, "
        "parent_id, specification, definition_of_done, title, severity, "
        "needs, created_by_kind, created_at, refs, topics) VALUES "
        "(?,?,'epic',?,?,?,'scope','done','a title','medium','agent',"
        "'agent',?,'{}','[]')",
        (item_id, product, state, maturity, parent_id, _ts(days=2)))


def _insert_children(conn, rows):
    """`rows` is `(id, product, state, parent_id)`, type `None`."""
    conn.executemany(
        "INSERT INTO issue_items (id, product, type, state, parent_id, "
        "title, severity, needs, created_by_kind, created_at, refs, topics) "
        "VALUES (?,?,NULL,?,?,'a child','medium','agent','agent',?,'{}','[]')",
        [(r[0], r[1], r[2], r[3], _ts(days=2)) for r in rows])


def test_decline_cascades_through_a_nested_sub_epic_at_every_level(client):
    """An epic's parent may itself be an epic (ADR-0023 / `_validate_parent`),
    and the live vault holds one active sub-epic today (reviewer, PR #300).
    Declining from the top must close unfinished work at both levels and
    decline the sub-epic too, not stop one level deep."""
    conn = _db()
    _insert_epic(conn, "epic_top")
    _insert_epic(conn, "epic_sub", parent_id="epic_top")
    _insert_children(conn, [
        ("top_open", "fennick", "open", "epic_top"),
        ("top_fixed", "fennick", "fixed", "epic_top"),
        ("sub_open", "fennick", "open", "epic_sub"),
        ("sub_claimed", "fennick", "claimed", "epic_sub"),
        ("sub_fixed", "fennick", "fixed", "epic_sub"),
    ])
    conn.execute("UPDATE courses SET entries = ? WHERE id = 'course_fennick'",
                (json.dumps(["epic_top", "epic_sub", "epic_c"]),))
    conn.commit()
    conn.close()

    r = _decline(client, "epic_top", reason="scope abandoned")
    assert r.status_code == 200
    decline = r.json()["decline"]
    assert sorted(decline["children_resolved"]) == [
        "sub_claimed", "sub_open", "top_open"]
    assert decline["epics_declined"] == ["epic_sub"]
    for closed_id in ("top_open", "sub_open", "sub_claimed"):
        row = _row(closed_id)
        assert row["state"] == "wontfix"
        assert row["resolution"] == "wontfix"
        assert row["outcome_note"] == "scope abandoned"
    for untouched_id in ("top_fixed", "sub_fixed"):
        row = _row(untouched_id)
        assert row["state"] == "fixed" and row["resolution"] is None
    assert _row("epic_top")["maturity"] == "declined"
    assert _row("epic_sub")["maturity"] == "declined"
    assert _row("epic_sub")["parent_id"] == "epic_top"
    assert _course()["entries"] == ["epic_c"]
    assert "its ancestor epic_top was declined" in _notes("epic_sub")[0]


def test_declining_is_refused_when_an_unresolved_sub_epic_is_approved(client):
    """library/decisions/0005, rule 4: an approved epic is never declined,
    directly or by cascade. Nothing is written when one is found anywhere
    in the subtree, even though it is not the epic named in the request."""
    conn = _db()
    _insert_epic(conn, "epic_top5")
    _insert_epic(conn, "epic_sub5", parent_id="epic_top5",
                maturity="approved")
    _insert_children(conn, [
        ("top5_open", "fennick", "open", "epic_top5"),
        ("sub5_open", "fennick", "open", "epic_sub5"),
    ])
    conn.commit()
    conn.close()

    r = _decline(client, "epic_top5")
    assert r.status_code == 409
    assert r.json()["reason"] == "approved"
    assert "epic_sub5" in r.json()["error"]
    assert "repropose" in r.json()["hint"]
    assert _row("epic_top5")["maturity"] == "proposed"
    assert _row("epic_sub5")["maturity"] == "approved"
    assert _row("top5_open")["state"] == "open"
    assert _row("sub5_open")["state"] == "open"


def test_decline_cascades_through_three_levels(client):
    conn = _db()
    _insert_epic(conn, "epic_l1")
    _insert_epic(conn, "epic_l2", parent_id="epic_l1")
    _insert_epic(conn, "epic_l3", parent_id="epic_l2")
    _insert_children(conn, [
        ("l1_open", "fennick", "open", "epic_l1"),
        ("l2_open", "fennick", "open", "epic_l2"),
        ("l3_open", "fennick", "open", "epic_l3"),
    ])
    conn.commit()
    conn.close()

    r = _decline(client, "epic_l1")
    assert r.status_code == 200
    decline = r.json()["decline"]
    assert sorted(decline["children_resolved"]) == [
        "l1_open", "l2_open", "l3_open"]
    assert sorted(decline["epics_declined"]) == ["epic_l2", "epic_l3"]
    for epic_id in ("epic_l1", "epic_l2", "epic_l3"):
        assert _row(epic_id)["maturity"] == "declined"
    for item_id in ("l1_open", "l2_open", "l3_open"):
        assert _row(item_id)["state"] == "wontfix"


def test_decline_leaves_an_already_declined_sub_epic_as_it_is(client):
    conn = _db()
    _insert_epic(conn, "epic_top6")
    _insert_epic(conn, "epic_sub6", parent_id="epic_top6",
                maturity="declined")
    _insert_children(conn, [
        ("top6_open", "fennick", "open", "epic_top6"),
        ("sub6_open", "fennick", "open", "epic_sub6"),
        ("sub6_fixed", "fennick", "fixed", "epic_sub6"),
    ])
    conn.commit()
    conn.close()

    r = _decline(client, "epic_top6")
    assert r.status_code == 200
    decline = r.json()["decline"]
    assert "epic_sub6" not in decline["epics_declined"]
    assert sorted(decline["children_resolved"]) == ["sub6_open", "top6_open"]
    assert _row("epic_sub6")["maturity"] == "declined"
    assert _row("sub6_fixed")["state"] == "fixed"
    assert _row("sub6_open")["state"] == "wontfix"
    assert _notes("epic_sub6") == []


# --- repropose ----------------------------------------------------------------


def test_reproposing_an_approved_epic_sets_proposed_and_leaves_the_course(
        client):
    r = _repropose(client, "epic_c")
    assert r.status_code == 200
    assert r.json()["item"]["maturity"] == "proposed"
    assert r.json()["repropose"]["left_course"] == {
        "course_id": "course_fennick", "position": 1}
    assert _course()["entries"] == []


def test_reproposing_a_declined_epic_reopens_nothing(client):
    decline = _decline(client, "epic_b")
    assert decline.status_code == 200
    r = _repropose(client, "epic_b")
    assert r.status_code == 200
    assert r.json()["item"]["maturity"] == "proposed"
    assert r.json()["repropose"]["left_course"] is None
    for child_id in ("b_open", "b_claimed", "b_pr"):
        assert _row(child_id)["state"] == "wontfix"


def test_reproposing_a_completed_approved_epic_is_refused(client):
    r = _repropose(client, "epic_done")
    assert r.status_code == 409
    assert _row("epic_done")["maturity"] == "approved"


def test_reproposing_a_canceled_approved_epic_is_refused(client):
    r = _repropose(client, "epic_wontfixed")
    assert r.status_code == 409
    assert _row("epic_wontfixed")["maturity"] == "approved"


def test_reproposing_a_proposed_epic_is_refused(client):
    r = _repropose(client, "epic_a")
    assert r.status_code == 409
    assert _row("epic_a")["maturity"] == "proposed"


def test_reproposing_a_non_epic_is_refused(client):
    r = _repropose(client, "a_bug")
    assert r.status_code == 400


def test_an_agent_credential_cannot_repropose(client):
    r = _repropose(client, "epic_c", token=AGENT)
    assert r.status_code == 403
    assert _row("epic_c")["maturity"] == "approved"


def _approve(client, item_id):
    return client.post(f"/items/{item_id}/approve",
                       json={"acting_human": HUMAN}, headers=_h(OPERATOR))


def test_repropose_then_reapprove_restores_the_course_position(client):
    """platform/decisions/0087, through the same `_index_before_demotion`
    snapshot an ordinary demotion already uses. Built through real approvals
    rather than a raw Course edit, because the restore reads the COURSE
    EVENTS trail, not merely the current entries."""
    assert _approve(client, "epic_a").status_code == 200   # -> [epic_c, epic_a]
    assert _approve(client, "epic_b").status_code == 200   # -> [epic_c, epic_a, epic_b]
    assert _course()["entries"] == ["epic_c", "epic_a", "epic_b"]

    assert _repropose(client, "epic_a").status_code == 200
    assert _course()["entries"] == ["epic_c", "epic_b"]

    assert _approve(client, "epic_a").status_code == 200
    assert _course()["entries"] == ["epic_c", "epic_a", "epic_b"]
