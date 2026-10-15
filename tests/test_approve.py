"""Approving an epic: its own route, its own capability, and the Course.

Four things carry the weight here.

**The capability is `epic.approve`, and only an `approved_client` resolves to
it.** An agent, a system and an orchestrator credential are each refused, and a
client whose entry withholds it is refused too, which is how an operator narrows
the Bosun.

**The route requires an attested human.** `acting_human` is recorded and never
replaced by the client's own name, and a request without one is refused before
anything is written.

**Approval and Course placement are one transaction.** Each write is failed with
a real database trigger rather than a seam of ours, and the test then reads both
tables. Only the second write of a pair can be observed rolling the first back,
so the two orderings are covered by failing a write that runs AFTER the other
(the Course write, then the last insert) rather than one that runs first.

**Nothing else sets the field.** `/correct` and `/revision` are exercised in
their own files; the source scan at the bottom fails when a second writer of
`approved` is added.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import itertools
import json
import re
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as libapp
import identity
import lease
import protocol
import store

AGENT = "agent-token"
SYSTEM = "system-token"
ORCH = "orchestrator-token"
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
        ("epic_a", "vivy", "epic", "open", "proposed", "scope a", "done a"),
        ("epic_b", "vivy", "epic", "open", "proposed", "scope b", "done b"),
        ("epic_c", "vivy", "epic", "open", "proposed", "scope c", "done c"),
        ("epic_bare", "vivy", "epic", "open", "proposed", None, None),
        ("epic_done", "vivy", "epic", "fixed", "proposed", "s", "d"),
        ("a_bug", "vivy", "bug", "open", None, None, None),
        ("epic_keel", "keel", "epic", "open", "proposed", "keel scope",
         "keel done"),
        ("epic_other", "constance", "epic", "open", "proposed", "s", "d"),
    ]
    conn.executemany(
        "INSERT INTO issue_items (id, product, type, state, maturity, "
        "specification, definition_of_done, title, severity, needs, "
        "created_by_kind, created_at, refs, topics) "
        "VALUES (?,?,?,?,?,?,?,'a title','medium','agent','agent',?,'{}','[]')",
        [(*r, _ts(days=2)) for r in rows])
    conn.execute(
        "INSERT INTO courses (id, product, title, entries, status, version, "
        "created_by, created_at) VALUES ('course_vivy', 'vivy', 'The route', ?, "
        "'live', 3, 'test', ?)", (json.dumps(["epic_c"]), _ts(days=3)))
    # keel has a Course, but a superseded one: the product has no LIVE Course.
    conn.execute(
        "INSERT INTO courses (id, product, title, entries, status, version, "
        "created_by, created_at) VALUES ('course_keel_old', 'keel', 'Old', '[]', "
        "'superseded', 1, 'test', ?)", (_ts(days=9),))
    conn.commit()
    conn.close()

    agents, clients = tmp_path / "api-tokens.json", tmp_path / "client-tokens.json"
    agents.write_text(json.dumps({
        AGENT: {"name": "an-agent", "products": ["*"]},
        SYSTEM: {"name": "capture", "products": ["*"], "kind": "system"},
        ORCH: {"name": "orch", "products": ["*"], "kind": "orchestrator"},
    }))
    clients.write_text(json.dumps({
        OPERATOR: {"name": "deck", "products": ["*"]},
        # The Bosun as the operator narrows it: an approved client whose entry
        # withholds the capability.
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


def _approve(client, item_id, token=OPERATOR, **body):
    body.setdefault("acting_human", HUMAN)
    return client.post(f"/items/{item_id}/approve", json=body, headers=_h(token))


def _row(item_id):
    conn = _db()
    row = conn.execute("SELECT * FROM issue_items WHERE id = ?",
                       (item_id,)).fetchone()
    conn.close()
    return row


def _course(course_id="course_vivy"):
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


def _events():
    conn = _db()
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM course_events ORDER BY id").fetchall()]
    conn.close()
    return rows


def _nothing_changed():
    assert _row("epic_a")["maturity"] == "proposed"
    assert _course() == {"entries": ["epic_c"], "version": 3}
    assert _events() == []
    assert _notes("epic_a") == []


# --- the approval ---------------------------------------------------------------


def test_approving_sets_the_maturity_and_appends_to_the_end_of_the_course(client):
    r = _approve(client, "epic_a")
    assert r.status_code == 200, r.text
    assert r.json()["item"]["maturity"] == "approved"
    assert _row("epic_a")["maturity"] == "approved"
    assert _course() == {"entries": ["epic_c", "epic_a"], "version": 4}


def test_the_course_is_read_inside_the_write_lock(client, monkeypatch):
    # The Course list is read, extended and written back with no version
    # predicate, so a read outside a transaction lets two overlapping
    # approvals each append to the same stale list. Asserting the mechanism,
    # because an interleaving test would pass by timing on a race it missed.
    seen = []
    real = libapp._live_course_of

    def spy(conn, product):
        seen.append(conn.in_transaction)
        return real(conn, product)

    monkeypatch.setattr(libapp, "_live_course_of", spy)
    r = _approve(client, "epic_a")
    assert r.status_code == 200, r.text
    assert seen and all(seen)


def test_the_record_names_the_client_the_human_the_course_and_the_position(client):
    spec_sha = hashlib.sha256(b"scope a").hexdigest()
    dod_sha = hashlib.sha256(b"done a").hexdigest()
    r = _approve(client, "epic_a")
    assert r.json()["approval"] == {
        "approved_by": "deck", "acting_human": HUMAN,
        "course_id": "course_vivy", "position": 2, "course_size": 2,
        "newly_placed": True, "restored_position": False,
        "specification_sha256": spec_sha,
        "definition_of_done_sha256": dod_sha}
    note = _notes("epic_a")[0]
    assert "deck" in note and HUMAN in note
    assert "course_vivy" in note and "position 2 of 2" in note
    assert f"specification_sha256: {spec_sha}" in note
    assert f"definition_of_done_sha256: {dod_sha}" in note
    event = _events()[0]
    assert event["action"] == store.COURSE_ACTION_APPROVED
    assert event["actor"] == "deck"
    assert (f"epic_a approved by deck for {HUMAN} "
            f"(specification_sha256={spec_sha}, "
            f"definition_of_done_sha256={dod_sha})") == event["note"]
    assert json.loads(event["entries"]) == ["epic_c", "epic_a"]


def test_the_human_is_recorded_as_sent_and_never_replaced_by_the_client(client):
    r = _approve(client, "epic_a", acting_human="  Sam Someone  ")
    assert r.json()["approval"]["acting_human"] == "Sam Someone"
    assert r.json()["approval"]["approved_by"] == "deck"


def test_a_second_approval_appends_after_the_first(client):
    assert _approve(client, "epic_a").status_code == 200
    assert _approve(client, "epic_b").status_code == 200
    assert _course()["entries"] == ["epic_c", "epic_a", "epic_b"]


# --- the text hashes (CQ·146, ADR-0030) ----------------------------------------


def test_the_digests_equal_hashlib_of_the_stored_text(client):
    r = _approve(client, "epic_a")
    approval = r.json()["approval"]
    assert approval["specification_sha256"] == hashlib.sha256(
        b"scope a").hexdigest()
    assert approval["definition_of_done_sha256"] == hashlib.sha256(
        b"done a").hexdigest()


def test_a_missing_or_empty_field_hashes_to_null():
    # The approve route refuses an epic missing either field outright
    # (`test_an_epic_with_no_specification_or_definition_of_done_is_refused`
    # below), so this is the hash helper's own contract: a field that was
    # never there and a field sent as `""` both record as `None`, never as
    # the hash of an empty string.
    assert libapp._full_sha256(None) is None
    assert libapp._full_sha256("") is None
    assert libapp._full_sha256("x") == hashlib.sha256(b"x").hexdigest()


def test_changing_the_specification_after_approval_no_longer_matches_the_recorded_digest(
        client):
    recorded = _approve(client, "epic_a").json()["approval"][
        "specification_sha256"]
    client.post("/items/epic_a/correct", json={"specification": "a new end state"},
                headers=_h(AGENT))
    current = hashlib.sha256(_row("epic_a")["specification"].encode()).hexdigest()
    assert recorded != current


# --- the credential --------------------------------------------------------------


@pytest.mark.parametrize("token", [AGENT, SYSTEM, ORCH])
def test_an_agent_system_or_orchestrator_credential_cannot_approve(client, token):
    r = _approve(client, "epic_a", token=token)
    assert r.status_code == 403
    assert "epic.approve" in r.json()["error"]
    _nothing_changed()


def test_an_approved_client_whose_entry_withholds_the_capability_cannot(client):
    """How the operator narrows the Bosun: the capability is defined on the
    role, then withheld by the credential's own entry."""
    r = _approve(client, "epic_a", token=BOSUN)
    assert r.status_code == 403 and "epic.approve" in r.json()["error"]
    _nothing_changed()


def test_no_credential_at_all_is_refused(client):
    r = client.post("/items/epic_a/approve", json={"acting_human": HUMAN},
                    headers={protocol.HEADER: str(protocol.CURRENT)})
    assert r.status_code == 401
    _nothing_changed()


@pytest.mark.parametrize("kind", [None, "system", "orchestrator",
                                  "customer_reader"])
def test_the_capability_is_reachable_from_no_role_but_the_approved_client(kind):
    if kind is None:
        held = identity.ROLE_CAPS["agent"]
    else:
        held = identity.ROLE_CAPS[kind]
    assert "epic.approve" not in held
    assert "epic.approve" in identity.ROLE_CAPS["approved_client"]
    assert "epic.approve" in identity.HUMAN_RESERVED_CAPS


# --- the attested human ------------------------------------------------------------


@pytest.mark.parametrize("payload", [
    {}, {"acting_human": None}, {"acting_human": ""}, {"acting_human": "   "},
    {"acting_human": 7}, {"acting_human": ["operator"]},
    {"acting_human": "x" * (store.MAX_ACTOR + 1)},
])
def test_a_request_without_a_usable_human_is_refused_and_writes_nothing(
        client, payload):
    r = client.post("/items/epic_a/approve", json=payload, headers=_h(OPERATOR))
    assert r.status_code == 403, r.text
    assert "acting_human" in r.json()["error"]
    _nothing_changed()


def test_a_malformed_body_is_refused(client):
    r = client.post("/items/epic_a/approve", content=b"not json",
                    headers=_h(OPERATOR))
    assert r.status_code == 400
    r = client.post("/items/epic_a/approve", json=["a"], headers=_h(OPERATOR))
    assert r.status_code == 400
    _nothing_changed()


# --- the correction routes no longer approve ------------------------------------------


def test_an_agent_credential_on_the_correction_route_is_refused_by_name(client):
    """The hole: `item.resolve` is in the agent role, and `/correct` set
    `maturity` on it. The refusal names the approval route."""
    r = client.post("/items/epic_a/correct", json={"maturity": "approved"},
                    headers=_h(AGENT))
    assert r.status_code == 400
    assert "POST /items/{item_id}/approve" in r.json()["error"]
    assert _row("epic_a")["maturity"] == "proposed"
    assert _course()["entries"] == ["epic_c"]


def test_an_operator_credential_is_refused_on_the_correction_route_too(client):
    """The refusal is on the FIELD, so holding `epic.approve` does not reopen
    the old door."""
    r = client.post("/items/epic_a/correct", json={"maturity": "approved"},
                    headers=_h(OPERATOR))
    assert r.status_code == 400
    assert _row("epic_a")["maturity"] == "proposed"


def test_the_revision_route_refuses_maturity_for_an_agent_credential(client):
    r = client.post("/items/epic_a/revision",
                    json={"maturity": "approved", "owner": "me"},
                    headers=_h(AGENT))
    assert r.status_code == 400
    assert r.json()["route"] == "POST /items/{item_id}/approve"
    assert _row("epic_a")["maturity"] == "proposed"


def test_only_one_statement_in_the_service_writes_the_approved_value():
    """A scan of the source, because a second writer of `approved` is exactly
    what this epic removed and a behavioural test only covers the routes it
    thinks of. Every assignment to the `maturity` column is either the approval
    route's own UPDATE or the demotion to `proposed`."""
    src = (Path(libapp.__file__)).read_text()
    assert len(re.findall(r"SET maturity\s*=", src)) == 1
    assert 'columns["maturity"] = store.ITEM_MATURITY_PROPOSED' in src
    assignments = re.findall(r'columns\["maturity"\]\s*=\s*(.+)', src)
    assert assignments and all(
        a.strip() == "store.ITEM_MATURITY_PROPOSED" for a in assignments), (
        assignments)
    for name in ("store.py", "lease.py", "reference.py", "call_slips.py"):
        text = (Path(libapp.__file__).parent / name).read_text()
        assert not re.search(r"SET[^\"']*\bmaturity\b", text), name


# --- what may be approved --------------------------------------------------------------


def test_only_an_epic_is_approved(client):
    r = _approve(client, "a_bug")
    assert r.status_code == 400 and "only an epic" in r.json()["error"]
    assert _row("a_bug")["maturity"] is None


def test_an_epic_with_no_specification_or_definition_of_done_is_refused(client):
    r = _approve(client, "epic_bare")
    assert r.status_code == 400
    assert r.json()["missing"] == ["specification", "definition_of_done"]
    assert _row("epic_bare")["maturity"] == "proposed"
    assert _course()["entries"] == ["epic_c"]


def test_a_terminal_epic_is_not_approved(client):
    r = _approve(client, "epic_done")
    assert r.status_code == 409 and "terminal" in r.json()["error"]


def test_an_epic_outside_the_callers_scope_reads_as_absent(client, tmp_path,
                                                          monkeypatch):
    scoped = tmp_path / "scoped.json"
    scoped.write_text(json.dumps({"scoped-token": {"name": "scoped-deck",
                                                   "products": ["vivy"]}}))
    monkeypatch.setattr(identity, "CLIENT_TOKENS", scoped)
    assert _approve(client, "epic_other", token="scoped-token").status_code == 404
    assert _approve(client, "no_such", token="scoped-token").status_code == 404
    assert _row("epic_other")["maturity"] == "proposed"


def _lease(root_id, holder="someone-else"):
    conn = _db()
    stamp = _ts(seconds=5)
    conn.execute(
        "INSERT INTO issue_leases (id, root_id, taken_for, holder, taken_at, "
        "heartbeat_at) VALUES ('l1',?,?,?,?,?)",
        (root_id, root_id, holder, stamp, stamp))
    conn.commit()
    conn.close()


def _leases():
    conn = _db()
    rows = [dict(r) for r in conn.execute("SELECT * FROM issue_leases")]
    conn.close()
    return rows


def test_another_holders_lease_on_the_epic_does_not_refuse_the_approval(client):
    _lease("epic_a")
    before = _leases()
    r = _approve(client, "epic_a")
    assert r.status_code == 200, r.text
    assert _row("epic_a")["maturity"] == "approved"
    assert _course()["entries"] == ["epic_c", "epic_a"]
    assert _leases() == before


def test_a_lease_on_a_child_of_the_epic_does_not_refuse_the_approval(client):
    conn = _db()
    conn.execute("UPDATE issue_items SET parent_id = 'epic_a' "
                 "WHERE id = 'a_bug'")
    conn.commit()
    conn.close()
    _lease("a_bug")
    before = _leases()
    assert _approve(client, "epic_a").status_code == 200
    assert _row("epic_a")["maturity"] == "approved"
    assert _leases() == before
    assert before[0]["holder"] == "someone-else"


def test_the_owner_field_is_still_accepted(client):
    assert _approve(client, "epic_a", owner="someone-else").status_code == 200


def test_the_holders_later_spec_edit_still_revokes_an_approval_made_under_its_lease(
        client):
    _lease("epic_a")
    assert _approve(client, "epic_a").status_code == 200
    r = client.post("/items/epic_a/correct",
                    json={"specification": "a new end state",
                          "owner": "someone-else"}, headers=_h(AGENT))
    assert r.status_code == 200, r.text
    assert r.json()["maturity"] == "proposed"
    assert "epic_a" not in _course()["entries"]


# --- the Course --------------------------------------------------------------------------


def test_a_product_with_no_live_course_refuses_and_names_that_as_the_cause(client):
    """keel's only Course is superseded, so it has no live one."""
    r = _approve(client, "epic_keel")
    assert r.status_code == 409
    assert r.json()["reason"] == "no_live_course"
    assert "no live Course" in r.json()["error"]
    assert _row("epic_keel")["maturity"] == "proposed"
    assert _course("course_keel_old")["entries"] == []


def test_re_approving_neither_duplicates_nor_moves_the_epic(client):
    assert _approve(client, "epic_a").status_code == 200
    assert _approve(client, "epic_b").status_code == 200
    before = _course()
    events_before = len(_events())
    notes_before = _notes("epic_a")

    again = _approve(client, "epic_a")
    assert again.status_code == 200
    assert again.json()["approval"]["position"] == 2
    assert again.json()["approval"]["newly_placed"] is False
    assert _course() == before, "re-approval moved or duplicated the epic"
    assert len(_events()) == events_before
    assert _notes("epic_a") == notes_before


def test_an_epic_already_on_the_course_keeps_its_position_when_approved(client):
    """epic_c stands first on the Course while still proposed, and approving it
    changes its maturity and nothing about where it stands."""
    r = _approve(client, "epic_c")
    assert r.status_code == 200
    assert r.json()["approval"]["position"] == 1
    assert r.json()["approval"]["newly_placed"] is False
    assert _row("epic_c")["maturity"] == "approved"
    assert _course() == {"entries": ["epic_c"], "version": 3}


def test_an_approved_epic_that_was_never_placed_is_placed_at_the_end(client):
    """A row approved before this route existed and left on no Course. Approving
    it again completes the placement instead of reporting nothing to do."""
    conn = _db()
    conn.execute("UPDATE issue_items SET maturity = 'approved' "
                 "WHERE id = 'epic_a'")
    conn.commit()
    conn.close()
    r = _approve(client, "epic_a")
    assert r.status_code == 200 and r.json()["approval"]["newly_placed"] is True
    assert _course()["entries"] == ["epic_c", "epic_a"]


def test_a_full_course_refuses_the_approval(client, monkeypatch):
    monkeypatch.setattr(store, "MAX_COURSE_ENTRIES", 1)
    r = _approve(client, "epic_a")
    assert r.status_code == 409 and r.json()["course_id"] == "course_vivy"
    _nothing_changed()


# --- one transaction ----------------------------------------------------------------------


def _trigger(sql):
    conn = _db()
    conn.execute(sql)
    conn.commit()
    conn.close()


def test_a_failing_course_write_leaves_the_epic_proposed(client):
    """The maturity write runs first, so it is committed nowhere only if the
    Course write's failure takes it back."""
    _trigger("CREATE TRIGGER no_course_write BEFORE UPDATE ON courses "
             "BEGIN SELECT RAISE(ABORT, 'forced'); END")
    r = _approve(client, "epic_a")
    assert r.status_code == 500
    _nothing_changed()


def test_a_failing_maturity_write_leaves_the_course_unchanged(client):
    _trigger("CREATE TRIGGER no_maturity_write BEFORE UPDATE OF maturity ON "
             "issue_items BEGIN SELECT RAISE(ABORT, 'forced'); END")
    r = _approve(client, "epic_a")
    assert r.status_code == 500
    _nothing_changed()


def test_a_failure_after_both_writes_takes_both_back(client):
    """The direction the two above cannot show on their own: both writes have
    happened and the LAST insert fails, so neither may have committed."""
    _trigger("CREATE TRIGGER no_note BEFORE INSERT ON issue_comments "
             "BEGIN SELECT RAISE(ABORT, 'forced'); END")
    r = _approve(client, "epic_a")
    assert r.status_code == 500
    _nothing_changed()


def test_a_failure_writing_the_course_event_takes_both_back(client):
    _trigger("CREATE TRIGGER no_event BEFORE INSERT ON course_events "
             "BEGIN SELECT RAISE(ABORT, 'forced'); END")
    assert _approve(client, "epic_a").status_code == 500
    _nothing_changed()


# --- demotion ----------------------------------------------------------------------------


def test_a_specification_edit_returns_the_epic_to_proposed_and_off_the_course(
        client):
    assert _approve(client, "epic_a").status_code == 200
    r = client.post("/items/epic_a/correct",
                    json={"specification": "a new end state"},
                    headers=_h(AGENT))
    assert r.status_code == 200 and r.json()["maturity"] == "proposed"
    assert _course()["entries"] == ["epic_c"]
    assert "Left the Course" in _notes("epic_a")[-1]
    assert "position 2" in _notes("epic_a")[-1]
    assert _events()[-1]["action"] == store.COURSE_ACTION_DEMOTED


def test_a_retype_returns_the_epic_to_proposed_and_off_the_course(client):
    assert _approve(client, "epic_a").status_code == 200
    r = client.post("/items/epic_a/correct", json={"type": None},
                    headers=_h(AGENT))
    assert r.status_code == 200 and r.json()["maturity"] == "proposed"
    assert "epic_a" not in _course()["entries"]


def test_the_edit_with_the_assertion_keeps_the_approval_and_the_place(client):
    assert _approve(client, "epic_a").status_code == 200
    r = client.post("/items/epic_a/correct",
                    json={"specification": "reflowed",
                          "end_state_unchanged": True}, headers=_h(AGENT))
    assert r.status_code == 200 and r.json()["maturity"] == "approved"
    assert _course()["entries"] == ["epic_c", "epic_a"]


def test_the_retype_launder_still_ends_proposed_and_off_the_course(client):
    """The existing launder, now with the Course in it: retype away, rewrite,
    retype back never ends approved or placed."""
    assert _approve(client, "epic_a").status_code == 200
    post = lambda body: client.post("/items/epic_a/correct", json=body,  # noqa: E731
                                    headers=_h(AGENT))
    assert post({"type": None}).status_code == 200
    assert post({"specification": "text nobody approved"}).status_code == 200
    # Retyping away from `epic` with no `parent_id` sent moved it into its
    # product's corrections queue (`library/specs/one-home-per-work-item`,
    # PR 2); clearing it is what the retype back to epic needs in the same
    # request.
    assert post({"type": "epic", "parent_id": None}).status_code == 200
    assert _row("epic_a")["maturity"] == "proposed"
    assert "epic_a" not in _course()["entries"]


# --- re-approval after a demotion (the operator, 2026-10-15; platform/decisions/0087) -----------


_edits = itertools.count()


def _demote(client, item_id):
    r = client.post(f"/items/{item_id}/correct",
                    json={"specification": f"changed {item_id} {next(_edits)}"},
                    headers=_h(AGENT))
    assert r.status_code == 200 and r.json()["maturity"] == "proposed", r.text


def _set_order(entries):
    """A reorder the way the Course route records one: the new array, and an
    event carrying it."""
    conn = _db()
    conn.execute("UPDATE courses SET entries = ?, version = version + 1 "
                 "WHERE id = 'course_vivy'", (json.dumps(entries),))
    conn.execute("INSERT INTO course_events (course_id, actor, action, entries, "
                 "note, at) VALUES ('course_vivy', 'test', 'reordered', ?, '', ?)",
                 (json.dumps(entries), _ts()))
    conn.commit()
    conn.close()


def test_a_demoted_epic_returns_to_where_it_stood_when_nothing_changed(client):
    assert _approve(client, "epic_a").status_code == 200
    assert _approve(client, "epic_b").status_code == 200
    _demote(client, "epic_a")
    assert _course()["entries"] == ["epic_c", "epic_b"]
    r = _approve(client, "epic_a")
    assert r.status_code == 200, r.text
    assert _course()["entries"] == ["epic_c", "epic_a", "epic_b"]
    approval = r.json()["approval"]
    assert approval["position"] == 2 and approval["restored_position"] is True
    assert "where it stood before its demotion" in _notes("epic_a")[-1]
    event = _events()[-1]
    assert event["action"] == store.COURSE_ACTION_APPROVED
    assert json.loads(event["entries"]) == ["epic_c", "epic_a", "epic_b"]
    assert "returned to position 2" in event["note"]


def test_epics_appended_since_the_demotion_do_not_stop_the_return(client):
    assert _approve(client, "epic_a").status_code == 200
    _demote(client, "epic_a")
    assert _approve(client, "epic_b").status_code == 200
    r = _approve(client, "epic_a")
    assert r.json()["approval"]["restored_position"] is True
    assert _course()["entries"] == ["epic_c", "epic_a", "epic_b"]


def _orderless_event(action):
    """A retitle, supersede or waypoint event: `entries` NULL, because the
    order did not move."""
    conn = _db()
    conn.execute("INSERT INTO course_events (course_id, actor, action, entries, "
                 "note, at) VALUES ('course_vivy', 'test', ?, NULL, '', ?)",
                 (action, _ts()))
    conn.commit()
    conn.close()


@pytest.mark.parametrize("action", ["retitled", "waypoint_added"])
def test_an_event_that_did_not_move_the_order_does_not_stop_the_return(
        client, action):
    """PR 286 review, round 1: a NULL snapshot read as an empty Course looked
    like the epic's departure, before and after the real demotion alike."""
    assert _approve(client, "epic_a").status_code == 200
    assert _approve(client, "epic_b").status_code == 200
    _orderless_event(action)
    _demote(client, "epic_a")
    _orderless_event(action)
    r = _approve(client, "epic_a")
    assert r.json()["approval"]["restored_position"] is True
    assert _course()["entries"] == ["epic_c", "epic_a", "epic_b"]


def test_an_unreadable_snapshot_sends_it_to_the_end(client):
    assert _approve(client, "epic_a").status_code == 200
    assert _approve(client, "epic_b").status_code == 200
    _demote(client, "epic_a")
    conn = _db()
    conn.execute("UPDATE course_events SET entries = 'not json' WHERE id = "
                 "(SELECT max(id) FROM course_events)")
    conn.commit()
    conn.close()
    r = _approve(client, "epic_a")
    assert r.json()["approval"]["restored_position"] is False
    assert _course()["entries"] == ["epic_c", "epic_b", "epic_a"]


def test_a_reorder_since_the_demotion_sends_it_to_the_end(client):
    assert _approve(client, "epic_a").status_code == 200
    assert _approve(client, "epic_b").status_code == 200
    _demote(client, "epic_a")
    _set_order(["epic_b", "epic_c"])
    r = _approve(client, "epic_a")
    assert r.json()["approval"]["restored_position"] is False
    assert _course()["entries"] == ["epic_b", "epic_c", "epic_a"]


def test_another_epics_demotion_since_sends_it_to_the_end(client):
    """Removing a neighbour shifts every later index, so the old position no
    longer names the same place."""
    assert _approve(client, "epic_a").status_code == 200
    assert _approve(client, "epic_b").status_code == 200
    _demote(client, "epic_a")
    _demote(client, "epic_b")
    r = _approve(client, "epic_a")
    assert r.json()["approval"]["restored_position"] is False
    assert _course()["entries"] == ["epic_c", "epic_a"]


def test_another_epics_demotion_before_it_left_does_not_stop_the_return(client):
    """The order it left behind is the one to compare against, so a change that
    landed before it left is part of that order."""
    assert _approve(client, "epic_b").status_code == 200
    assert _approve(client, "epic_a").status_code == 200
    _demote(client, "epic_b")
    _set_order(["epic_a", "epic_c"])
    _demote(client, "epic_a")
    r = _approve(client, "epic_a")
    assert r.json()["approval"]["restored_position"] is True
    assert _course()["entries"] == ["epic_a", "epic_c"]


def test_an_epic_removed_by_a_reorder_rather_than_a_demotion_goes_to_the_end(
        client):
    assert _approve(client, "epic_a").status_code == 200
    assert _approve(client, "epic_b").status_code == 200
    _set_order(["epic_c", "epic_b"])
    conn = _db()
    conn.execute("UPDATE issue_items SET maturity = 'proposed' "
                 "WHERE id = 'epic_a'")
    conn.commit()
    conn.close()
    r = _approve(client, "epic_a")
    assert r.json()["approval"]["restored_position"] is False
    assert _course()["entries"] == ["epic_c", "epic_b", "epic_a"]


def test_a_demotion_from_a_superseded_course_does_not_place_it_on_the_new_one(
        client):
    assert _approve(client, "epic_a").status_code == 200
    assert _approve(client, "epic_b").status_code == 200
    _demote(client, "epic_a")
    conn = _db()
    conn.execute("UPDATE courses SET status = 'superseded' "
                 "WHERE id = 'course_vivy'")
    conn.execute(
        "INSERT INTO courses (id, product, title, entries, status, version, "
        "created_by, created_at) VALUES ('course_vivy_2', 'vivy', 'Next', ?, "
        "'live', 1, 'test', ?)", (json.dumps(["epic_b", "epic_c"]), _ts()))
    conn.commit()
    conn.close()
    r = _approve(client, "epic_a")
    assert r.json()["approval"]["restored_position"] is False
    assert _course("course_vivy_2")["entries"] == ["epic_b", "epic_c", "epic_a"]


def test_a_second_demotion_returns_it_to_where_it_stood_most_recently(client):
    assert _approve(client, "epic_a").status_code == 200
    assert _approve(client, "epic_b").status_code == 200
    _demote(client, "epic_a")
    _set_order(["epic_b", "epic_c"])
    assert _approve(client, "epic_a").status_code == 200
    assert _course()["entries"] == ["epic_b", "epic_c", "epic_a"]
    _set_order(["epic_a", "epic_b", "epic_c"])
    _demote(client, "epic_a")
    r = _approve(client, "epic_a")
    assert r.json()["approval"]["restored_position"] is True
    assert _course()["entries"] == ["epic_a", "epic_b", "epic_c"]
