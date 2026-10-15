"""Correcting a misfiled field.

**This is a correction path, not a general edit API.** Four of its six fields
are presence-gated, because an explicit `null` is a meaningful value on each:
resetting a maturity to "not yet assessed", detaching an item to top level, and
clearing a specification that turned out to be wrong all have to be reachable, or
a wrong bucket and a wrong nesting become one-way doors.

Two gates carry across from Orient with their reasoning. Retyping an epic
that still has children would leave them hanging off a node that cannot hold a
lease. And ADR-0030's approval invariant is stated on the RESULTING state rather
than on the transition, because a transition guard has to enumerate every route
into a state and an invariant on the state does not.
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

AGENT = "agent-token-vivy"
WIDE = "agent-token-wide"


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
        CREATE TABLE course_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, course_id TEXT, actor TEXT,
            action TEXT, entries TEXT, note TEXT, at TEXT);
        CREATE TABLE signals (
            id TEXT PRIMARY KEY, source TEXT, stream TEXT, kind TEXT, actor TEXT,
            ingested_by TEXT, product TEXT, refs TEXT, body TEXT, meta TEXT,
            privacy_class TEXT, produced_at TEXT, ingested_at TEXT,
            embedded_at TEXT, dedup_key TEXT UNIQUE, acting_human TEXT);
        CREATE TABLE corrections_queues (
            id TEXT PRIMARY KEY, product TEXT UNIQUE, created_at TEXT);
    """)
    conn.executemany(
        "INSERT INTO corrections_queues (id, product, created_at) "
        "VALUES (?,?,?)",
        [("cq_vivy", "vivy", "2026-08-01 00:00:00.000000"),
         ("cq_constance", "constance", "2026-08-01 00:00:00.000000")])
    # (id, product, title, type, state, severity, maturity, parent_id, refs,
    #  specification, definition_of_done)
    #
    # **OPEN NON-EPIC ROWS SEED `type` AS `None`, MATCHING THE LIVE MIGRATION.**
    # A work item carries no type; `epic` is the only value left
    # (issue_01M0EEPS591VQHYEBJY7NGF4C3). `done`, below, is TERMINAL and keeps
    # the type it closed with, exactly as a real closed row does.
    rows = [
        ("task", "vivy", "A task", None, "open", "medium", None, None, "{}",
         None, None),
        ("bug", "vivy", "A bug", None, "open", "low", None, None, "{}",
         None, None),
        ("epic", "vivy", "An epic", "epic", "open", "medium", "proposed", None,
         "{}", "what it is", "when it is done"),
        ("child", "vivy", "Under the epic", None, "open", "low", None, "epic",
         "{}", None, None),
        ("epic_bare", "vivy", "A childless epic", "epic", "open", "medium",
         "proposed", None, "{}", None, None),
        ("epic_ok", "vivy", "An approved epic", "epic", "open", "medium",
         "approved", None, "{}", "the long form", "the finish line"),
        ("epic_approved_bare", "vivy", "Approved with nothing said", "epic",
         "open", "medium", "approved", None, "{}", None, None),
        ("impr_ok", "vivy", "An approved non-epic", None, "open",
         "low", "approved", None, "{}", None, None),
        ("done", "vivy", "Done", "bug", "fixed", "low", None, None, "{}",
         None, None),
        ("other", "constance", "Other product", None, "open", "low", None, None,
         "{}", None, None),
        ("other_epic", "constance", "Other epic", "epic", "open", "low", None,
         None, "{}", None, None),
    ]
    conn.executemany(
        "INSERT INTO issue_items (id, product, title, type, state, severity, "
        "maturity, parent_id, refs, specification, definition_of_done, needs, "
        "created_by_kind, created_at, topics) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,'agent','agent',?,'[]')",
        [(*r, _ts(days=2)) for r in rows])
    # THE LIVE COURSE THE APPROVED EPIC STANDS ON, because approval and Course
    # membership are one fact: a demotion has to take it off, and the tests
    # below read this row to see that it did.
    conn.execute(
        "INSERT INTO courses (id, product, title, entries, status, version, "
        "created_by, created_at) VALUES ('course_vivy', 'vivy', 'The route', ?, "
        "'live', 1, 'test', ?)",
        (json.dumps(["epic", "epic_ok", "other_epic_in_order"]), _ts(days=3)))
    conn.commit()
    conn.close()

    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        AGENT: {"name": "vivy-agent", "products": ["vivy"]},
        WIDE: {"name": "wide-agent", "products": ["*"]},
    }))
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    monkeypatch.setattr(lease, "TUNABLES", tmp_path / "absent.json")
    return TestClient(libapp.app)


def _h(token=AGENT):
    return {"Authorization": f"Bearer {token}",
            protocol.HEADER: str(protocol.CURRENT)}


def _db():
    conn = sqlite3.connect(store.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _correct(client, item_id, body, token=AGENT):
    return client.post(f"/items/{item_id}/correct", json=body, headers=_h(token))


def _row(item_id):
    conn = _db()
    row = conn.execute("SELECT * FROM issue_items WHERE id = ?",
                       (item_id,)).fetchone()
    conn.close()
    return row


def _notes(item_id):
    conn = _db()
    rows = [r["body"] for r in conn.execute(
        "SELECT body FROM issue_comments WHERE item_id = ? ORDER BY id",
        (item_id,)).fetchall()]
    conn.close()
    return rows


def _course_entries():
    conn = _db()
    row = conn.execute("SELECT entries FROM courses WHERE id = 'course_vivy'"
                       ).fetchone()
    conn.close()
    return json.loads(row["entries"])


def _set_maturity(item_id, value):
    """Approval written straight to the row, because no correction route writes
    `maturity` any more and a test that needs an approved item has to stand one
    up. The approval ROUTE is exercised in `test_approve.py`."""
    conn = _db()
    conn.execute("UPDATE issue_items SET maturity = ? WHERE id = ?",
                 (value, item_id))
    conn.commit()
    conn.close()


def _take_lease(root_id, *, holder="someone-else", alive=True):
    conn = _db()
    stamp = _ts(seconds=5) if alive else _ts(days=3)
    conn.execute(
        "INSERT INTO issue_leases (id, root_id, taken_for, holder, taken_at, "
        "heartbeat_at) VALUES (?,?,?,?,?,?)",
        (f"lease_{root_id}", root_id, root_id, holder, stamp, stamp))
    conn.commit()
    conn.close()


# --- the shape of a request ----------------------------------------------------


def test_a_correction_with_no_fields_is_refused(client):
    r = _correct(client, "bug", {})
    assert r.status_code == 400 and "at least one of" in r.json()["error"]


def test_an_item_in_another_product_is_not_found(client):
    assert _correct(client, "other", {"severity": "high"}).status_code == 404


def test_a_terminal_item_cannot_be_corrected(client):
    r = _correct(client, "done", {"severity": "high"})
    assert r.status_code == 409 and "terminal" in r.json()["error"]


@pytest.mark.parametrize("body", [
    {"type": "nonsense"}, {"type": 7}, {"severity": "urgent"},
    {"severity": []}, {"specification": 7}, {"definition_of_done": ["a"]},
])
def test_a_value_outside_its_closed_set_is_refused(client, body):
    assert _correct(client, "bug", body).status_code == 400


def test_the_item_is_read_inside_the_write_lock(client, monkeypatch):
    """CQ·100. Read before the lock, two corrections to different columns
    each judged the approval rule against the same stale row and both landed.
    Asserting the order, because an interleaving test would pass by timing on
    a race it missed."""
    seen = []
    real = store.connect_rw

    class Spy:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *args):
            if sql.startswith("SELECT * FROM issue_items"):
                seen.append(self._conn.in_transaction)
            return self._conn.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    monkeypatch.setattr(store, "connect_rw", lambda: Spy(real()))
    r = _correct(client, "bug", {"severity": "high"})
    assert r.status_code == 200, r.text
    assert seen and seen[0] is True


def test_a_correction_that_changes_nothing_is_a_no_op(client):
    """The same value re-sent is not an edit, which is what stops an idempotent
    retry knocking an approved epic back to proposed."""
    r = _correct(client, "bug", {"severity": "low"})
    assert r.status_code == 200
    assert _notes("bug") == []


def test_writes_are_refused_while_the_shadow_is_running(client, monkeypatch):
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    assert _correct(client, "bug", {"severity": "high"}).status_code == 503


# --- presence, not truthiness --------------------------------------------------


@pytest.mark.parametrize("value", [None, "approved", "proposed", "built",
                                   "superseded", "shipped", 3])
def test_maturity_is_refused_with_every_value_and_names_the_approval_route(
        client, value):
    """**THE HOLE THIS CLOSES**: `item.resolve` is in the agent role, so any
    dispatched agent credential could set `maturity: approved` here. The refusal
    is on PRESENCE, so `null`, the other rungs and a nonsense value are all
    turned away with the same answer, and it names the route to use."""
    r = _correct(client, "epic", {"maturity": value})
    assert r.status_code == 400
    assert "POST /items/{item_id}/approve" in r.json()["error"]
    assert r.json()["route"] == "POST /items/{item_id}/approve"
    assert _row("epic")["maturity"] == "proposed"


def test_a_refused_maturity_stages_nothing_from_the_rest_of_the_request(client):
    """The other fields in the same request are not half-applied: the whole
    request is refused, so a caller cannot read a 400 over a partial write."""
    r = _correct(client, "epic", {"maturity": "approved", "severity": "high"})
    assert r.status_code == 400
    assert _row("epic")["severity"] == "medium"
    assert _notes("epic") == []


def test_the_correction_route_still_corrects_every_other_field(client):
    """The refusal is about `maturity` alone."""
    r = _correct(client, "epic", {"severity": "high", "topics": ["DESIGN"],
                                  "specification": "new scope"})
    assert r.status_code == 200
    assert r.json()["severity"] == "high"
    assert _row("epic")["specification"] == "new scope"


def test_an_explicit_null_moves_a_work_item_to_its_corrections_queue(client):
    """A work item's parent is never NULL (`library/specs/one-home-per-
    work-item`, PR 2): an explicit `null` moves it into its product's
    corrections queue, which an epic's `null` does not do (that case is
    covered separately). The move writes no note."""
    r = _correct(client, "child", {"parent_id": None})
    assert r.status_code == 200 and r.json()["parent_id"] == "cq_vivy"
    assert _notes("child") == []


def test_an_explicit_null_clears_a_specification(client):
    r = _correct(client, "epic", {"specification": None})
    assert r.status_code == 200 and r.json()["specification"] is None


def test_retyping_to_a_dropped_type_is_refused_by_name(client):
    """`bug`, `nit`, `improvement` and `task` are gone from the vocabulary
    (the operator, 2026-09-24, issue_01M0EEPS591VQHYEBJY7NGF4C3); retyping any
    work item to one of them is refused with the value named."""
    r = _correct(client, "bug", {"type": "task"})
    assert r.status_code == 400
    assert "task" in r.json()["error"]
    assert _row("bug")["type"] is None


def test_retyping_to_feature_is_refused(client):
    r = _correct(client, "bug", {"type": "feature"})
    assert r.status_code == 400
    assert "feature" in r.json()["error"]


def test_an_untyped_item_may_be_promoted_to_epic(client):
    assert _correct(client, "task", {"type": "epic"}).status_code == 200
    assert _row("task")["type"] == "epic"


# --- an epic with children keeps its type --------------------------------------


def test_retyping_an_epic_with_children_is_refused(client):
    """They would point at an interior node that is no longer an epic, and the
    lease would fence a subtree hanging off something that cannot hold one."""
    r = _correct(client, "epic", {"type": None})
    assert r.status_code == 409
    assert "1 descendant" in r.json()["error"]
    assert _row("epic")["type"] == "epic"


def test_a_childless_epic_may_be_retyped(client):
    assert _correct(client, "epic_bare", {"type": None}).status_code == 200


def test_a_parentless_epic_retyped_to_a_work_item_gets_the_queue(client):
    """Otherwise the retype leaves a non-epic row with a NULL parent, the
    shape `library/specs/one-home-per-work-item` retires — this is the
    other direction from `test_an_item_in_a_queue_cannot_be_retyped_to_epic_
    in_a_later_request`'s epic-from-queue case, and no `parent_id` is sent
    in the request either way."""
    r = _correct(client, "epic_bare", {"type": None})
    assert r.status_code == 200, r.text
    assert _row("epic_bare")["parent_id"] == "cq_vivy"


# --- ADR-0030's approval invariant ---------------------------------------------


def test_editing_an_approved_epics_text_returns_it_to_proposed(client):
    """An approval attaches to the TEXT, so text that moves afterwards leaves
    `approved` standing over something nobody agreed to."""
    r = _correct(client, "epic_ok", {"specification": "a different scope"})
    assert r.status_code == 200 and r.json()["maturity"] == "proposed"
    assert "Returned to `proposed`" in _notes("epic_ok")[0]


def test_a_demotion_takes_the_epic_off_its_course_and_records_why(client):
    """**APPROVAL IS WHAT PUTS AN EPIC ON A COURSE, so losing it takes it off**,
    in the same transaction. The other entries keep their order, the Course's
    version moves, and both the thread and the Course's history say where the
    epic stood and why it left."""
    r = _correct(client, "epic_ok", {"specification": "a different scope"})
    assert r.status_code == 200
    assert _course_entries() == ["epic", "other_epic_in_order"]
    note = _notes("epic_ok")[0]
    assert "Left the Course" in note and "course_vivy" in note
    assert "position 2" in note
    conn = _db()
    course = conn.execute("SELECT version FROM courses").fetchone()
    event = conn.execute("SELECT * FROM course_events").fetchone()
    conn.close()
    assert course["version"] == 2
    assert event["action"] == store.COURSE_ACTION_DEMOTED
    assert "epic_ok left the Course" in event["note"]
    assert "end_state_unchanged" in event["note"]
    assert json.loads(event["entries"]) == ["epic", "other_epic_in_order"]


def test_a_retype_takes_the_epic_off_its_course_with_the_retype_as_the_reason(
        client):
    r = _correct(client, "epic_ok", {"type": None})
    assert r.status_code == 200 and r.json()["maturity"] == "proposed"
    assert "epic_ok" not in _course_entries()
    conn = _db()
    note = conn.execute("SELECT note FROM course_events").fetchone()["note"]
    conn.close()
    assert "retyped" in note


def test_asserting_the_end_state_keeps_the_epic_on_its_course(client):
    """The negative control for the two above: the same edit with the assertion
    demotes nothing, so nothing leaves. Without it those two would pass against
    a build that removes an epic on every edit."""
    r = _correct(client, "epic_ok", {"specification": "reflowed",
                                     "end_state_unchanged": True})
    assert r.status_code == 200 and r.json()["maturity"] == "approved"
    assert _course_entries() == ["epic", "epic_ok", "other_epic_in_order"]
    assert _db().execute("SELECT COUNT(*) FROM course_events"
                         ).fetchone()[0] == 0


def test_a_demotion_and_its_course_removal_are_one_transaction(client):
    """A failure AFTER both writes rolls both back: the epic stays approved and
    stays on the Course. The failure is a real one, a trigger on the last insert
    the route makes, so no seam of ours decides what is rolled back."""
    conn = _db()
    conn.execute("CREATE TRIGGER boom BEFORE INSERT ON issue_comments "
                 "BEGIN SELECT RAISE(ABORT, 'forced'); END")
    conn.commit()
    conn.close()
    # A client of its own that answers the forced failure as a 500, so only
    # this test reads a server error as a response rather than an exception.
    lenient = TestClient(libapp.app, raise_server_exceptions=False)
    r = _correct(lenient, "epic_ok", {"specification": "a different scope"})
    assert r.status_code == 500
    assert _row("epic_ok")["maturity"] == "approved"
    assert _course_entries() == ["epic", "epic_ok", "other_epic_in_order"]


def test_clearing_the_spec_of_an_approved_epic_demotes_it(client):
    r = _correct(client, "epic_ok", {"specification": None})
    assert r.status_code == 200 and r.json()["maturity"] == "proposed"
    assert _row("epic_ok")["specification"] is None
    assert "epic_ok" not in _course_entries()


def test_resending_the_same_text_does_not_un_approve(client):
    r = _correct(client, "epic_ok", {"specification": "the long form"})
    assert r.status_code == 200 and r.json()["maturity"] == "approved"


def test_a_non_epic_keeps_its_approval_when_its_text_moves(client):
    """A non-epic may be approved with no specification at all, so its approval
    never rested on that text."""
    r = _correct(client, "impr_ok", {"specification": "a note"})
    assert r.status_code == 200 and r.json()["maturity"] == "approved"


def test_retyping_an_approved_item_revokes_the_approval(client):
    """An approval attaches to what the item IS as well as to what it says, so a
    retype is a change to the thing that was approved. Un-scoped to epics on
    purpose: a retype is what carried an approval between the epic and non-epic
    worlds."""
    r = _correct(client, "impr_ok", {"type": "epic"})
    assert r.status_code == 200 and r.json()["maturity"] == "proposed"
    assert "Returned to `proposed`" in _notes("impr_ok")[0]


def test_an_approval_cannot_be_laundered_through_a_retype_round_trip(client):
    """Three ordinary calls, none holding `decide`: retype the approved epic
    away, rewrite the specification under the non-epic exemption, retype it back.
    Stating the invariant on the resulting state of ONE call did not stop it,
    because an approval outlives the call."""
    assert _correct(client, "epic_ok", {"type": None}).status_code == 200
    assert _row("epic_ok")["maturity"] == "proposed"          # closed at step one
    # Retyping away from `epic` with no parent sent moved it into its
    # product's corrections queue (`library/specs/one-home-per-work-item`,
    # PR 2); clearing `parent_id` is what the retype back to epic needs in
    # the same request, the same door `test_an_item_in_a_queue_cannot_be_
    # retyped_to_epic_in_a_later_request` documents.
    assert _row("epic_ok")["parent_id"] == "cq_vivy"
    assert _correct(client, "epic_ok",
                    {"specification": "text nobody approved"}).status_code == 200
    assert _correct(client, "epic_ok",
                    {"type": "epic", "parent_id": None}).status_code == 200
    assert _row("epic_ok")["maturity"] == "proposed"


def test_a_text_demotion_is_told_the_assertion_would_have_kept_it(client):
    """**A CALLER THAT LEARNS THE OUTCOME AND NOT ITS CHOICE MAKES THE SAME CALL
    NEXT TIME.** Six approvals were destroyed in one evening on 2026-08-27 by
    rewrites that stripped narrative while leaving the end state exactly as
    approved — every one recoverable by an assertion the note never mentioned.

    **THIS ASSERTS THE NOTE IS TRUE OF THE REQUEST THAT RAN, not that it contains
    a phrase** (reviewer, PR #67). The previous version checked for the substrings
    "REFUSED", "400" and "lands and demotes" and passed while the note told this
    caller — who sent no `type` at all — that it had retyped. A substring check
    pins that the prose changed, not that it is correct for the scenario, which
    is the same look-alike shape the note itself is about."""
    r = _correct(client, "epic_ok", {"specification": "a different scope"})
    assert r.status_code == 200 and r.json()["maturity"] == "proposed"
    note = _notes("epic_ok")[0]
    assert "Returned to `proposed`" in note
    assert "end_state_unchanged" in note, (
        "the note tells a caller what happened and not what to do instead")
    assert "retype" not in note.lower(), (
        "a caller demoted by a specification edit was told it retyped")
    assert "REFUSED" not in note, (
        "the note describes a refusal that could not have applied to this call")


def test_doing_what_the_note_says_actually_works(client):
    """**THE NOTE'S PROMISE, EXERCISED RATHER THAN READ.** Three rounds of this
    PR shipped a note whose sentences were each wrong in a different way, and
    every one of them passed a test that checked the note SAID something. This
    checks it is TRUE: take the demotion, do literally what the note instructs,
    and assert the approval comes back.

    Sent with a `type` at its current value, which is the shape a full-object
    PATCH client uses and the shape that made round 3's version of this promise
    false."""
    assert _row("epic_ok")["maturity"] == "approved"

    demoted = _correct(client, "epic_ok", {"type": "epic",
                                           "specification": "the long form v2"})
    assert demoted.json()["maturity"] == "proposed"
    note = _notes("epic_ok")[-1]
    assert "end_state_unchanged" in note, "the note did not offer the remedy"

    # Re-approve, then follow the note: the same edit, with the assertion added.
    _set_maturity("epic_ok", "approved")
    followed = _correct(client, "epic_ok", {"type": "epic",
                                            "specification": "the long form v3",
                                            "end_state_unchanged": True})
    assert followed.status_code == 200, (
        f"the note told the caller to do this and it was refused: "
        f"{followed.json()}")
    assert followed.json()["maturity"] == "approved", (
        "the note promised the approval would stand and it did not")


def test_a_retype_demotion_is_told_no_assertion_would_have_helped(client):
    """The other cause, and the one where the affordance must NOT be offered.
    A caller told to send `end_state_unchanged` after a retype would send it and
    get a 400, because the route refuses the pair. So this branch says the
    retype revoked it and that no assertion would have kept it."""
    r = _correct(client, "epic_ok", {"type": None})
    assert r.status_code == 200 and r.json()["maturity"] == "proposed"
    note = _notes("epic_ok")[0]
    assert "retype is what revoked it" in note
    assert "REFUSED" in note and "400" in note, (
        "the note does not say the assertion is refused alongside a retype")
    assert "would have kept" in note, (
        "the note does not tell the caller no assertion would have saved this")


def test_asserting_the_end_state_did_not_move_keeps_the_approval(client):
    """**The operator, 2026-08-27**, on ADR-0030's stated consequence that fixing a typo
    un-approves the epic: "I also didn't write that and I don't agree with it...
    we need to distinguish between fixing a typo and changing the desired end
    state."

    Only the editor knows which one an edit was, so the assertion is the
    caller's. The second call is the control: without it this passes against a
    build that never un-approved anything."""
    r = _correct(client, "epic_ok", {"specification": "the long form, reflowed",
                                     "end_state_unchanged": True})
    assert r.status_code == 200 and r.json()["maturity"] == "approved"
    assert _row("epic_ok")["specification"] == "the long form, reflowed"

    same = _correct(client, "epic_ok", {"specification": "a different scope"})
    assert same.status_code == 200 and same.json()["maturity"] == "proposed", (
        "the edit un-approves nothing even without the assertion, so the "
        "assertion above is not what kept the approval")


def test_the_kept_approval_is_recorded_on_the_thread_with_who_claimed_it(client):
    """**Nothing can verify the claim**, so what the mechanism offers instead is
    attribution: the actor, on the thread, beside the diff the claim was made
    about. A claim with no record is one a later reader cannot even know was
    made."""
    _correct(client, "epic_ok", {"specification": "the long form, reflowed",
                                 "end_state_unchanged": True})
    note = _notes("epic_ok")[0]
    assert "Approval kept" in note
    assert "Returned to `proposed`" not in note
    assert "does not change the desired end state" in note


def test_a_type_sent_at_its_current_value_does_not_block_the_assertion(client):
    """**THE FULL-OBJECT PATCH CASE** (reviewer, PR #67, round 3). A client that
    sends every field back, including a `type` it did not touch, is an ordinary
    client rather than a contrived one. The refusal used to key on the PRESENCE
    of `type`, so such a caller could never use the assertion at all — and the
    revocation note told it to.

    A `type` equal to the item's own changes nothing about what the item IS, and
    `_revokes_approval` returns False for it. The refusal now makes the same
    comparison, so the two cannot disagree."""
    r = _correct(client, "epic_ok", {"type": "epic",
                                     "specification": "the long form, reflowed",
                                     "end_state_unchanged": True})
    assert r.status_code == 200, r.json()
    assert r.json()["maturity"] == "approved", (
        "a no-op `type` field blocked an assertion that changes nothing")
    assert "Approval kept" in _notes("epic_ok")[0]


def test_the_refusal_names_both_types_so_a_caller_can_see_the_change(client):
    """The refusal is now made where the item is loaded, so it can say what the
    type is and what was asked for. A caller that sent a `type` believing it
    matched can see that it did not."""
    r = _correct(client, "epic_ok", {"type": None,
                                     "specification": "reworded",
                                     "end_state_unchanged": True})
    assert r.status_code == 400
    body = r.json()
    assert body["current_type"] == "epic" and body["requested_type"] is None
    assert _row("epic_ok")["maturity"] == "approved", "refused after staging"


def test_the_assertion_is_refused_alongside_a_retype_rather_than_honoured(client):
    """**A RETYPE IS NOT SOMETHING A CALLER MAY ASSERT ITS WAY PAST.** Retyping
    changes what the item IS, which is half of what an approval attaches to,
    whatever its text says. Refused rather than ignored, so a caller who thought
    the assertion covered it learns that at its own call site."""
    r = _correct(client, "impr_ok", {"type": "epic",
                                     "end_state_unchanged": True})
    assert r.status_code == 400
    assert "end_state_unchanged" in r.json()["error"]
    assert _row("impr_ok")["maturity"] == "approved", (
        "the refusal was answered after the write had already been staged")


def test_the_shared_definition_never_waives_a_retype_whatever_a_route_asks():
    """**CALLED DIRECTLY, BECAUSE NO ROUTE CAN REACH THIS BRANCH TODAY.** Both
    doors refuse `end_state_unchanged` alongside a `type` change before the write
    path runs, so a mutation that let the flag waive a retype inside
    `_revokes_approval` passes the entire suite through the routes — measured,
    not assumed. That makes the helper's own ordering a guard whose working and
    broken states produce identical evidence unless it is asserted here.

    It is worth keeping rather than deleting: `_revokes_approval` is the one
    definition three call sites share, and the next route to use it inherits
    whichever answer this branch gives."""
    approved_epic = {"maturity": "approved", "type": "epic"}
    assert libapp._revokes_approval(
        approved_epic, effective_type="improvement", spec_changing=False,
        end_state_unchanged=True) is True, (
        "the assertion waived a retype, which is the launder PR #11 closed")
    assert libapp._revokes_approval(
        approved_epic, effective_type="improvement", spec_changing=True,
        end_state_unchanged=True) is True

    # The text half IS waived, which is what the flag is for — without this the
    # assertion above would hold on a helper that ignored the flag entirely.
    assert libapp._revokes_approval(
        approved_epic, effective_type="epic", spec_changing=True,
        end_state_unchanged=True) is False
    assert libapp._revokes_approval(
        approved_epic, effective_type="epic", spec_changing=True,
        end_state_unchanged=False) is True


def test_the_assertion_does_not_reopen_the_retype_launder(client):
    """**THE LAUNDER PR #11's REVIEWER CLOSED, RETRIED WITH THE NEW FLAG AS ITS
    KEY.** Retype the approved epic away so the epic-scoped text rule stops
    applying, rewrite the specification asserting the end state held, retype
    back. If the assertion covered a retype this would end `approved` over words
    nobody agreed to; it is closed at step one, where it always was."""
    away = _correct(client, "epic_ok", {"type": None,
                                        "end_state_unchanged": True})
    assert away.status_code == 400, "the retype was waived by the assertion"
    assert _row("epic_ok")["maturity"] == "approved"

    assert _correct(client, "epic_ok", {"type": None}).status_code == 200
    assert _row("epic_ok")["maturity"] == "proposed"
    # Moved into its product's corrections queue by the retype above
    # (`library/specs/one-home-per-work-item`, PR 2); cleared below in the
    # same request that retypes it back to epic.
    assert _row("epic_ok")["parent_id"] == "cq_vivy"
    assert _correct(client, "epic_ok",
                    {"specification": "text nobody approved",
                     "end_state_unchanged": True}).status_code == 200
    assert _correct(client, "epic_ok",
                    {"type": "epic", "parent_id": None}).status_code == 200
    assert _row("epic_ok")["maturity"] == "proposed"


def test_the_assertion_cannot_resurrect_an_approval_the_item_had_lost(client):
    """It waives a revocation this edit would cause; it does not grant approval.
    An item sitting at `proposed` stays there."""
    _set_maturity("epic_ok", "proposed")
    r = _correct(client, "epic_ok", {"specification": "reworded",
                                     "end_state_unchanged": True})
    assert r.status_code == 200 and r.json()["maturity"] == "proposed"


def test_no_approval_was_kept_so_none_is_claimed_on_the_thread(client):
    """**THE RECORD IS THE ONLY SAFETY THIS MECHANISM HAS, SO IT MUST NOT BE
    ABLE TO LIE** (security-reviewer, PR #65). Nothing verifies the assertion;
    what is offered instead is an attributed note. A note saying "the approval it
    already carried still stands" written over an item that carried none is a
    false entry in the one channel a human audits this through — and it reads
    identically to a true one, which is the look-alike shape the flag exists to
    avoid.

    A client sending the flag defensively on every spec edit would trip this on
    every edit, so it is the common case rather than a corner."""
    r = _correct(client, "epic", {"specification": "new text",
                                  "end_state_unchanged": True})
    assert r.status_code == 200 and r.json()["maturity"] == "proposed"
    note = _notes("epic")[0]
    assert "Approval kept" not in note, (
        "an approval was claimed kept on an item that was never approved")
    # **THE NOTE IS STILL WRITTEN, JUST WITHOUT THE FALSE CLAIM**, and asserting
    # that is what stops the gate being "fixed" by suppressing the whole note
    # (reviewer, PR #65 — the assertion here was previously an `or` whose right
    # operand the line above had already made true, so it could not fail).
    assert "specification:" in note


def test_a_non_epics_approval_was_never_at_risk_so_nothing_is_claimed_kept(client):
    """**THE CASE A MATURITY-ONLY GATE STILL GETS WRONG** (reviewer, PR #65). A
    non-epic may be approved with no specification at all, so its approval never
    rested on that text and an edit to it revokes nothing — with or without the
    assertion. Crediting the assertion with preserving something that was never
    at risk is a false record, and it is the note rather than the maturity that
    is wrong: the maturity below is correct either way, which is why asserting
    only the maturity would pass this."""
    r = _correct(client, "impr_ok", {"specification": "a note",
                                     "end_state_unchanged": True})
    assert r.status_code == 200 and r.json()["maturity"] == "approved"
    assert "Approval kept" not in _notes("impr_ok")[0], (
        "the assertion was credited with keeping an approval that no edit to "
        "this item's text could have revoked")


def test_an_edit_outside_the_specification_claims_nothing(client):
    """The assertion sent alongside an edit that never revokes anything —
    `severity` here, which no approval ever rested on. The note is about what the
    assertion DID, so an edit outside its reach earns none.

    Named for the field it actually sends: an earlier name said "title-only" and
    this route cannot edit a title at all (reviewer, PR #65)."""
    r = _correct(client, "epic_ok", {"severity": "high",
                                     "end_state_unchanged": True})
    assert r.status_code == 200 and r.json()["maturity"] == "approved"
    assert "Approval kept" not in _notes("epic_ok")[0]


def test_a_non_boolean_assertion_is_refused_rather_than_read_as_true(client):
    """A truthy string is exactly how a claim nobody made becomes a claim that
    held an approval."""
    for value in ("true", 1, "yes", None):
        r = _correct(client, "epic_ok", {"specification": "reworded",
                                         "end_state_unchanged": value})
        assert r.status_code == 400, f"{value!r} was accepted"
        assert "end_state_unchanged" in r.json()["error"]
    assert _row("epic_ok")["maturity"] == "approved"


def test_asserting_it_false_is_the_default_rather_than_an_error(client):
    """`false` is what the flag means when absent, so sending it explicitly is a
    caller being clear rather than a caller being wrong."""
    r = _correct(client, "epic_ok", {"specification": "a different scope",
                                     "end_state_unchanged": False})
    assert r.status_code == 200 and r.json()["maturity"] == "proposed"


def test_an_approved_epic_missing_its_specification_is_refused_any_correction(client):
    """The gap check is a state invariant, so it fires on a row that is already
    in the state it forbids — which is how a row written before the gate existed
    gets caught rather than quietly corrected around."""
    r = _correct(client, "epic_approved_bare", {"severity": "high"})
    assert r.status_code == 400
    assert r.json()["missing"] == ["specification", "definition_of_done"]


# --- re-parenting ---------------------------------------------------------------


def test_an_item_may_be_nested_after_filing(client):
    """An epic usually does not exist when the work under it is first captured."""
    r = _correct(client, "bug", {"parent_id": "epic"})
    assert r.status_code == 200 and r.json()["parent_id"] == "epic"


def test_a_parent_in_another_product_is_refused(client):
    r = _correct(client, "bug", {"parent_id": "other_epic"}, token=WIDE)
    assert r.status_code == 400
    assert _row("bug")["parent_id"] is None


def test_a_parent_that_is_not_an_epic_is_refused(client):
    assert _correct(client, "bug", {"parent_id": "task"}).status_code == 400


def test_an_item_cannot_be_its_own_parent(client):
    r = _correct(client, "epic", {"parent_id": "epic"})
    assert r.status_code == 400 and "cycle" in r.json()["error"]


def test_a_parent_inside_the_items_own_subtree_is_refused(client):
    """A cycle hangs the checkout path itself, because the lease enumerates a
    subtree."""
    assert _correct(client, "child", {"type": "epic"}).status_code == 200
    r = _correct(client, "epic", {"parent_id": "child"})
    assert r.status_code == 400 and "cycle" in r.json()["error"]


def test_re_parenting_under_a_live_lease_is_refused(client):
    """Moving an item out shrinks the fence its holder is working behind."""
    _take_lease("epic")
    r = _correct(client, "child", {"parent_id": None})
    assert r.status_code == 409 and r.json()["lease"]["holder"] == "someone-else"
    assert r.json()["reason"] == "held"
    assert _row("child")["parent_id"] == "epic"


# --- a corrections queue as the destination (issue_01M12NE0RFPJY12TT8CF1169V1,
# library/specs/one-home-per-work-item) ------------------------------------


def test_an_item_may_be_moved_into_its_products_corrections_queue(client):
    r = _correct(client, "child", {"parent_id": "cq_vivy"})
    assert r.status_code == 200, r.text
    assert r.json()["parent_id"] == "cq_vivy"
    assert _row("child")["parent_id"] == "cq_vivy"


def test_another_products_queue_is_refused_as_a_parent(client):
    r = _correct(client, "bug", {"parent_id": "cq_constance"}, token=WIDE)
    assert r.status_code == 400
    assert r.json() == {"error": "parent not found: cq_constance"}
    assert _row("bug")["parent_id"] is None


def test_an_epic_cannot_be_moved_into_a_corrections_queue(client):
    r = _correct(client, "epic_bare", {"parent_id": "cq_vivy"})
    assert r.status_code == 400
    assert "epic" in r.json()["error"]


def test_an_item_in_a_queue_cannot_be_retyped_to_epic_in_a_later_request(client):
    """Two requests, not one: move the item into its queue, then retype it
    with no `parent_id` in the payload. The stored queue parent must still
    be checked against the new type (reviewer, PR #237 round 1)."""
    assert _correct(client, "task", {"parent_id": "cq_vivy"}).status_code == 200
    r = _correct(client, "task", {"type": "epic"})
    assert r.status_code == 400, r.text
    assert "epic" in r.json()["error"]
    assert _row("task")["type"] != "epic"
    assert _row("task")["parent_id"] == "cq_vivy"
    # Leaving the queue in the same request is the way through.
    r = _correct(client, "task", {"type": "epic", "parent_id": None})
    assert r.status_code == 200, r.text
    assert _row("task")["parent_id"] is None


def test_moving_into_a_queue_is_not_blocked_by_the_fence_check(client):
    """`_fence_holding`'s walk up the destination's ancestors finds nothing
    for a queue id — it is not a row in `issue_items`, so `ancestor_ids`
    stops at once and no lease is ever rooted there either. No logic change
    was needed for this; this is the move the plan asks to be covered."""
    r = _correct(client, "child", {"parent_id": "cq_vivy"})
    assert r.status_code == 200, r.text


def test_moving_out_of_a_live_lease_into_a_queue_is_refused(client):
    """The departure-side fence check applies to a queue destination exactly
    as it does to detaching to top level."""
    _take_lease("epic")
    r = _correct(client, "child", {"parent_id": "cq_vivy"})
    assert r.status_code == 409 and r.json()["lease"]["holder"] == "someone-else"
    assert r.json()["reason"] == "held"
    assert _row("child")["parent_id"] == "epic"


# --- a move changes parent_id alone (library/specs/one-home-per-work-item,
# PR 2, section 5 test 1) -----------------------------------------------------


def _seed_a_filed_work_item(item_id, *, parent_id="cq_vivy", key="CQ·9"):
    """A row shaped the way a real `POST /items` create with no parent would
    leave it, under PR 2: the queue from the start, never NULL."""
    conn = _db()
    conn.execute(
        "INSERT INTO issue_items (id, product, title, body, refs, severity, "
        "state, type, created_by, created_by_kind, needs, parent_id, "
        "topics, created_at, item_key) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (item_id, "vivy", "A filed work item", "body text", "{}", "low",
         "open", None, "agent", "agent", "agent", parent_id, "[]",
         _ts(days=1), key))
    conn.commit()
    conn.close()


def _events(item_id):
    conn = _db()
    rows = conn.execute(
        "SELECT * FROM issue_events WHERE item_id = ? ORDER BY id",
        (item_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def test_a_move_changes_parent_id_alone(client):
    """File a work item, landing in the queue as `CQ·n`; snapshot its row,
    its comments and its events. Move it to an epic with `/correct`, then
    back with the queue id; separately, move it back with `parent_id:
    null`. After each move the row must match the snapshot in every column
    but `parent_id` — `item_key` included — and its comments and events
    must match the snapshot exactly, with nothing added (the operator's
    first ruling, 2026-10-09)."""
    _seed_a_filed_work_item("filed")
    before = dict(_row("filed"))
    assert before["item_key"] == "CQ·9"
    assert _notes("filed") == []
    assert _events("filed") == []

    def _assert_only_parent_moved(expected_parent):
        row = dict(_row("filed"))
        assert row["parent_id"] == expected_parent
        for col in before:
            if col == "parent_id":
                continue
            assert row[col] == before[col], (
                f"{col} moved: {before[col]!r} -> {row[col]!r}")
        assert _notes("filed") == []
        assert _events("filed") == []

    assert _correct(client, "filed", {"parent_id": "epic"}).status_code == 200
    _assert_only_parent_moved("epic")

    assert _correct(client, "filed",
                    {"parent_id": "cq_vivy"}).status_code == 200
    _assert_only_parent_moved("cq_vivy")

    assert _correct(client, "filed", {"parent_id": "epic"}).status_code == 200
    _assert_only_parent_moved("epic")

    assert _correct(client, "filed", {"parent_id": None}).status_code == 200
    _assert_only_parent_moved("cq_vivy")


def test_a_parent_move_with_another_field_notes_only_the_other_field(client):
    """`parent_id` never joins the note, whatever else the same request
    changes — the note names only the field that earns one, and the parent
    still moves (section 5 test 12)."""
    r = _correct(client, "bug", {"parent_id": "epic", "severity": "high"})
    assert r.status_code == 200, r.text
    assert _row("bug")["parent_id"] == "epic"
    assert _row("bug")["severity"] == "high"
    [note] = _notes("bug")
    assert "severity" in note
    assert "parent_id" not in note


# --- found_during, which epic's work turned an item up without the lane change
# `parent_id` carries (issue_01M3BFN34Y8P8K39RWS6MM89ZT) -----------------------


def test_found_during_can_be_set(client):
    r = _correct(client, "bug", {"found_during": "epic"})
    assert r.status_code == 200 and r.json()["found_during"] == "epic"
    # NEITHER `parent_id` NOR THE CORRECTIONS LANE MOVES.
    assert r.json()["parent_id"] is None


def test_found_during_is_cleared_with_null(client):
    _correct(client, "bug", {"found_during": "epic"})
    r = _correct(client, "bug", {"found_during": None})
    assert r.status_code == 200 and r.json()["found_during"] is None


def test_a_found_during_that_is_not_an_epic_is_refused(client):
    r = _correct(client, "bug", {"found_during": "task"})
    assert r.status_code == 400
    assert r.json() == {"error": "unknown item id(s)", "ids": ["task"]}
    assert _row("bug")["found_during"] is None


def test_a_found_during_naming_a_missing_id_is_refused(client):
    r = _correct(client, "bug", {"found_during": "issue_nope"})
    assert r.status_code == 400
    assert r.json() == {"error": "unknown item id(s)", "ids": ["issue_nope"]}


def test_an_item_cannot_be_its_own_found_during(client):
    r = _correct(client, "epic", {"found_during": "epic"})
    assert r.status_code == 400
    assert r.json() == {"error": "unknown item id(s)", "ids": ["epic"]}


def test_a_found_during_in_another_product_is_refused_once_it_is_visible(client):
    r = _correct(client, "bug", {"found_during": "other_epic"}, token=WIDE)
    assert r.status_code == 400
    assert r.json() == {"error": "unknown item id(s)", "ids": ["other_epic"]}


def test_a_found_during_out_of_scope_reads_exactly_like_missing(client):
    """One wording for a non-epic, a missing id, the item itself, a
    cross-product epic and an out-of-scope epic — unlike `parent_id`, nothing
    here later discloses which of the five the caller hit, so a distinct
    wording for any one of them would make `found_during` an existence oracle
    one guess at a time."""
    missing = _correct(client, "bug", {"found_during": "issue_nope"})
    out_of_scope = _correct(client, "bug", {"found_during": "other_epic"})
    assert missing.status_code == out_of_scope.status_code == 400
    assert (missing.json()["error"] == out_of_scope.json()["error"]
            == "unknown item id(s)")


def test_setting_found_during_leaves_parent_id_and_lane_unchanged(client):
    r = _correct(client, "child", {"found_during": "epic_bare"})
    assert r.status_code == 200
    assert _row("child")["parent_id"] == "epic"
    assert _row("child")["found_during"] == "epic_bare"


# --- a lease never blocks its own holder -----------------------------------------
#
# The operator, 2026-09-11: "The idea is that *only* the person/agent with the checkout can
# make the change. Not that *nobody* can make a change." Re-parenting refused the
# holder too until then, so moving an item meant releasing its checkout first.


def _second_epic(item_id="epic2", parent=None):
    """A copy of the fixture epic under a new id, for a move between fences,
    at top level or under `parent`."""
    parent_sql = "NULL" if parent is None else f"'{parent}'"
    conn = _db()
    conn.executescript(f"""
        CREATE TEMP TABLE e AS SELECT * FROM issue_items WHERE id = 'epic';
        UPDATE e SET id = '{item_id}', once_key = NULL, parent_id = {parent_sql};
        INSERT INTO issue_items SELECT * FROM e;
        DROP TABLE e;
    """)
    conn.commit()
    conn.close()


def test_the_holder_may_move_an_item_out_of_its_own_epic(client):
    _take_lease("epic", holder="session-1")
    r = _correct(client, "child", {"parent_id": None, "owner": "session-1"})
    assert r.status_code == 200, r.json()
    assert _row("child")["parent_id"] == "cq_vivy"


def test_the_holder_may_move_an_item_into_an_epic_it_holds(client):
    _take_lease("epic", holder="session-1")
    r = _correct(client, "bug", {"parent_id": "epic", "owner": "session-1"})
    assert r.status_code == 200, r.json()
    assert _row("bug")["parent_id"] == "epic"


def test_holding_the_origin_does_not_get_past_someone_elses_destination(client):
    """A move touches two fences; the caller must hold each, or face no other
    holder on it."""
    _second_epic()
    _take_lease("epic", holder="session-1")
    _take_lease("epic2")
    r = _correct(client, "child", {"parent_id": "epic2", "owner": "session-1"})
    assert r.status_code == 409 and r.json()["reason"] == "held"
    assert r.json()["lease"]["holder"] == "someone-else"
    assert _row("child")["parent_id"] == "epic"


def test_the_holder_may_move_an_item_between_two_epics_it_holds(client):
    _second_epic()
    _take_lease("epic", holder="session-1")
    _take_lease("epic2", holder="session-1")
    r = _correct(client, "child", {"parent_id": "epic2", "owner": "session-1"})
    assert r.status_code == 200, r.json()
    assert _row("child")["parent_id"] == "epic2"


def test_a_move_cannot_carry_someone_elses_lease_into_the_callers_fence(client):
    """Reviewer, PR #108 round 2: the check looked up from the item and not
    down. An epic nobody holds can contain a sub-epic another session holds,
    and moving it under an epic the caller holds would nest that session's
    fence inside the caller's."""
    _second_epic()                       # the destination, held by the caller
    _second_epic("sub", parent="epic")   # beneath the epic being moved
    _take_lease("epic2", holder="session-1")
    _take_lease("sub")                   # held by someone-else
    r = _correct(client, "epic", {"parent_id": "epic2", "owner": "session-1"})
    assert r.status_code == 409, r.json()
    assert r.json()["reason"] == "held"
    assert r.json()["lease"]["root_id"] == "sub"
    assert _row("epic")["parent_id"] is None


def test_the_same_move_into_an_epic_nobody_holds_goes_through(client):
    """No fence at the destination, so nothing can end up nested."""
    _second_epic()
    _second_epic("sub", parent="epic")
    _take_lease("sub")
    r = _correct(client, "epic", {"parent_id": "epic2"})
    assert r.status_code == 200, r.json()
    assert _row("epic")["parent_id"] == "epic2"


def test_a_holder_may_nest_one_epic_it_holds_inside_another(client):
    """Two of the holder's own leases then overlap. That blocks no one else,
    so the operator's rule lets the move through (reviewer, PR #108 round 2)."""
    _second_epic()
    _take_lease("epic", holder="session-1")
    _take_lease("epic2", holder="session-1")
    r = _correct(client, "epic", {"parent_id": "epic2", "owner": "session-1"})
    assert r.status_code == 200, r.json()
    assert _row("epic")["parent_id"] == "epic2"


def test_the_destination_is_checked_as_well_as_the_origin(client):
    """Moving an item IN silently pulls it into someone else's fence."""
    _take_lease("epic")
    r = _correct(client, "bug", {"parent_id": "epic"})
    assert r.status_code == 409
    assert _row("bug")["parent_id"] is None


def test_a_dead_lease_does_not_block_a_re_parent(client):
    """Its holder is by definition not watching, so blocking on it would let one
    abandoned session freeze the shape of the tree."""
    _take_lease("epic", alive=False)
    assert _correct(client, "bug", {"parent_id": "epic"}).status_code == 200


def test_another_holders_lease_refuses_an_ordinary_correction(client):
    """The operator, 2026-09-11: no change to an item someone else has checked out. A
    severity fix under another holder's lease is refused as held, and a sweep
    can tell that from a failure by `reason`."""
    _take_lease("epic")
    r = _correct(client, "child", {"severity": "high"})
    assert r.status_code == 409
    assert r.json()["reason"] == "held"
    assert "someone-else" in r.json()["error"]
    assert _row("child")["severity"] == "low"


def test_the_holders_own_lease_lets_a_correction_through(client):
    _take_lease("epic", holder="session-1")
    r = _correct(client, "child", {"severity": "high", "owner": "session-1"})
    assert r.status_code == 200, r.json()
    assert _row("child")["severity"] == "high"


# --- the audit note --------------------------------------------------------------


def test_every_change_is_named_in_the_thread(client):
    r = _correct(client, "bug", {"severity": "high", "type": "epic"})
    assert r.status_code == 200
    note = _notes("bug")[0]
    assert "vivy-agent" in note
    assert "**severity:**" in note and "'low' → 'high'" in note
    assert "**type:**" in note


def test_two_specifications_sharing_a_prefix_do_not_read_as_identical(client):
    """A bare prefix misrepresents the change when two versions differ past the
    cut: the thread shows the same string twice while the stored text moved and
    the approval was revoked."""
    long_a = "x" * 300 + "ending A"
    long_b = "x" * 300 + "ending B"
    assert _correct(client, "epic", {"specification": long_a}).status_code == 200
    assert _correct(client, "epic", {"specification": long_b}).status_code == 200
    note = _notes("epic")[1]
    assert "sha256:" in note and "chars" in note
    old, new = note.split("→")
    assert old != new


# --- no refs door ----------------------------------------------------------------


def test_a_correction_cannot_write_refs(client):
    """A correction changes an item's content, not its references. The field
    is omitted from this route entirely rather than accepted and stripped,
    which is what makes it unforgettable for any future controlled key."""
    r = _correct(client, "bug", {"severity": "high",
                                 "refs": {"anything": "here"},
                                 "decision_id": "dec_1"})
    assert r.status_code == 200
    assert json.loads(_row("bug")["refs"]) == {}


def test_a_correction_cannot_move_an_item_between_products(client):
    """`product` is immutable everywhere, and the descendant-count disclosure
    depends on that staying true."""
    r = _correct(client, "bug", {"severity": "high", "product": "constance"})
    assert r.status_code == 200
    assert _row("bug")["product"] == "vivy"


def test_an_explicit_null_clears_a_definition_of_done(client):
    """The presence-gating treats the two prose fields identically, so both
    clears are pinned rather than one standing in for the other."""
    r = _correct(client, "epic", {"definition_of_done": None})
    assert r.status_code == 200 and r.json()["definition_of_done"] is None


def test_an_empty_specification_does_not_read_as_a_cleared_one(client):
    """`""` is a value a caller may set and `null` is a clear. Rendering both as
    None would have the thread report a field cleared when it was set to
    nothing."""
    assert _correct(client, "epic", {"specification": ""}).status_code == 200
    note = _notes("epic")[0]
    assert "'what it is' → ''" in note


def test_a_lease_outside_the_callers_scope_blocks_without_disclosing_itself(client):
    """The fence is real but its holder is not the caller's to see. Guarded
    upstream today, since no route can build a cross-product edge — which is the
    argument this check exists so as not to depend on."""
    conn = _db()
    conn.execute("UPDATE issue_items SET product='constance' WHERE id='epic'")
    conn.commit()
    conn.close()
    _take_lease("epic", holder="a-constance-agent")
    r = _correct(client, "child", {"parent_id": None})
    assert r.status_code == 409
    assert "outside this identity's scope" in r.json()["error"]
    assert "lease" not in r.json()
    assert "a-constance-agent" not in json.dumps(r.json())


def test_an_unenumerable_subtree_refuses_the_retype(client, monkeypatch):
    """Whether this epic has children is UNKNOWN, and unknown is not none."""
    def _boom(conn, root_id, **kw):
        raise lease.SubtreeTooLarge("more than 10000 nodes")
    monkeypatch.setattr(lease, "subtree_ids", _boom)
    r = _correct(client, "epic", {"type": None})
    assert r.status_code == 409 and "cannot enumerate" in r.json()["error"]
    assert _row("epic")["type"] == "epic"


# --- a losing concurrent writer is refused, not silently overwritten -----------
#
# A correction races a resolve on the same item: only one may land. Before the
# fix, correct's final UPDATE carried no predicate, so a resolve that committed
# in the window after correct's read (and every gate since, which take no lock)
# and before correct's write was silently overwritten — landing a correction on
# an item that had already gone terminal, contradicting the refusal this same
# function gives when it reads that state directly.


def test_a_resolve_that_arrives_mid_correction_waits_for_it(client, monkeypatch):
    """CQ·100. The correction takes the write lock before it reads the item,
    so a resolve that arrives while it is deciding cannot commit underneath
    it: the resolve waits, the correction lands, then the resolve runs. Held
    at `_hold_for_write`, which now sits inside the lock, for half a second,
    well under the 5 s busy timeout the resolve waits on.

    This replaces a test that held the correction BEFORE the lock and let the
    resolve commit in that window, proving the state compare-and-swap caught
    it. That window no longer exists; the compare-and-swap stays as the second
    line, and nothing can reach it from this route.
    """
    import threading
    import time

    correct_ready = threading.Event()
    results, observed = {}, {}
    real_hold = libapp._hold_for_write

    def hold_racer(conn, ident, item, owner, *, act):
        if act == "a correction":
            correct_ready.set()
            time.sleep(0.5)
            observed["resolve_done_while_held"] = "resolve" in results
        return real_hold(conn, ident, item, owner, act=act)

    monkeypatch.setattr(libapp, "_hold_for_write", hold_racer)

    def do_correct():
        results["correct"] = _correct(client, "bug", {"severity": "high"})

    def do_resolve():
        assert correct_ready.wait(timeout=20), "correct never reached the seam"
        results["resolve"] = client.post(
            "/items/bug/resolve", json={"resolution": "fixed"}, headers=_h())

    threads = [threading.Thread(target=do_correct),
               threading.Thread(target=do_resolve)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert set(results) == {"correct", "resolve"}, (
        "a request died before reaching the race seam")
    assert observed["resolve_done_while_held"] is False
    assert results["correct"].status_code == 200, results["correct"].json()
    assert results["resolve"].status_code == 200, results["resolve"].json()
    row = _row("bug")
    assert row["state"] == "fixed"
    assert row["severity"] == "high"


# --- topics, added 2026-09-08 ----------------------------------------------------
# A topic is a CLASSIFICATION — which area of a product this item belongs to —
# rather than something authored about the work, so it sits on this route beside
# `type` and `severity`. Regrouping needs no separate checkout call: with no live
# lease on the item, the correction holds it for its own write.

def test_topics_can_be_set(client):
    r = _correct(client, "bug", {"topics": ["DESIGN", "API"]})
    assert r.status_code == 200, r.text
    assert json.loads(_row("bug")["topics"]) == ["DESIGN", "API"]
    assert r.json()["topics"] == ["DESIGN", "API"]


def test_topics_are_cleared_by_null_or_empty(client):
    _correct(client, "bug", {"topics": ["DESIGN"]})
    assert _correct(client, "bug", {"topics": None}).status_code == 200
    assert json.loads(_row("bug")["topics"]) == []
    _correct(client, "bug", {"topics": ["DESIGN"]})
    assert _correct(client, "bug", {"topics": []}).status_code == 200
    assert json.loads(_row("bug")["topics"]) == []


def test_setting_the_same_topics_again_is_a_no_op(client):
    """`item["topics"]` is JSON TEXT and the validated value is a LIST. Comparing
    those directly reports a change on every call, writing an audit entry for a
    request that altered nothing."""
    def _notes():
        return _db().execute(
            "SELECT COUNT(*) FROM issue_comments WHERE item_id = 'bug'"
        ).fetchone()[0]

    assert _correct(client, "bug", {"topics": ["DESIGN"]}).status_code == 200
    after_first = _notes()
    assert after_first > 0, (
        "the first, genuine change must record a note — without this the "
        "assertion below would pass against a route that records nothing at all")
    assert _correct(client, "bug", {"topics": ["DESIGN"]}).status_code == 200
    assert _notes() == after_first, "an unchanged correction must record nothing"


def test_topics_alone_is_enough_of_a_field(client):
    """`topics` counts toward the at-least-one-field check, or regrouping would
    be refused as an empty correction."""
    assert _correct(client, "bug", {"topics": ["DESIGN"]}).status_code == 200


def test_topics_are_deduped_and_stripped_preserving_order(client):
    r = _correct(client, "bug", {"topics": ["  DESIGN  ", "API", "DESIGN"]})
    assert r.status_code == 200, r.text
    assert json.loads(_row("bug")["topics"]) == ["DESIGN", "API"]


def test_dedupe_is_case_sensitive_because_the_filter_is(client):
    """Folding case here would make DESIGN and design one topic on write while
    `value = ?` still treated them as two on read — the exact disagreement this
    field exists to remove."""
    r = _correct(client, "bug", {"topics": ["DESIGN", "design"]})
    assert r.status_code == 200, r.text
    assert json.loads(_row("bug")["topics"]) == ["DESIGN", "design"]


@pytest.mark.parametrize("bad, why", [
    ("DESIGN", "a bare string is not an array"),
    ([1], "a non-string member"),
    ([""], "an empty topic"),
    (["   "], "a whitespace-only topic"),
    (["x" * 65], "over MAX_TOPIC"),
    ([f"t{i}" for i in range(17)], "over MAX_TOPICS_PER_ITEM"),
])
def test_a_malformed_topics_payload_is_refused(client, bad, why):
    r = _correct(client, "bug", {"topics": bad})
    assert r.status_code == 400, f"{why}: {r.text}"
    assert json.loads(_row("bug")["topics"]) == [], "nothing was written"


def test_a_confusable_topic_is_refused(client):
    """Same guard a signal `kind` carries. A topic with a combining mark or a
    non-ASCII lookalike renders as an existing topic and matches nothing, so a
    filter answers 'none' while a viewer shows the item in that group."""
    for bad in ["DESIGN​", "DESIGН", "DEŚIGN"]:
        r = _correct(client, "bug", {"topics": [bad]})
        assert r.status_code == 400, f"{bad!r} was accepted"
        assert "printable ASCII" in r.json()["error"]


def test_correcting_topics_needs_no_checkout_call_and_leaves_no_fence(client):
    """With no live lease on the item, the correction holds it for its own
    write: recorded as a `written` lease, released before the response."""
    r = _correct(client, "child", {"topics": ["DESIGN"]})
    assert r.status_code == 200, r.json()
    assert json.loads(_row("child")["topics"]) == ["DESIGN"]
    conn = _db()
    rows = conn.execute("SELECT root_id, released_at, release_reason FROM "
                        "issue_leases WHERE taken_for = 'child'").fetchall()
    conn.close()
    assert [(r_["root_id"], r_["release_reason"]) for r_ in rows] == [
        ("epic", "written")]
    assert rows[0]["released_at"] is not None


def test_correcting_topics_under_another_holders_lease_is_refused(client):
    """Regrouping an item is a change to it, and the operator's rule covers every change
    to an item someone else has checked out."""
    _take_lease("epic")
    r = _correct(client, "child", {"topics": ["DESIGN"]})
    assert r.status_code == 409 and r.json()["reason"] == "held"


def test_topics_cannot_be_corrected_on_a_terminal_item(client):
    r = _correct(client, "done", {"topics": ["DESIGN"]})
    assert r.status_code == 409 and "terminal" in r.json()["error"]


def test_topics_do_not_disturb_the_approval_invariant(client):
    """Regrouping an approved epic must not return it to `proposed` — an
    approval attaches to the specification and the type, not to a label."""
    r = _correct(client, "epic_ok", {"topics": ["DESIGN"]})
    assert r.status_code == 200, r.text
    assert _row("epic_ok")["maturity"] == "approved"
