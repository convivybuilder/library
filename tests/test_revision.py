"""Checking a held item back in.

**The lease is the authorization**, which makes checkout the precondition of
editing exactly as ARCH-424 makes it the precondition of working — and it is
required at SUBMIT time, not merely at checkout, because a revision arriving
after the lease lapsed comes from a holder who no longer holds it.

Say the asymmetry with re-parenting out loud, because a reader who learns one
rule will guess the other wrong: re-parenting is refused while a live lease
covers an item, and revising is permitted only while one does. A lease protects
the tree's shape from outsiders and authorizes the holder's edits to the item's
own content, two jobs wearing one name.

Revised text drops its vectors in the same transaction as the write and is
re-embedded after the commit, which is contract §5's ordering. Those tests assert
on the STORED VECTORS rather than on the route's wording, because the wording is
the part a route can get right while the index goes stale.

The adjacency briefing is recomputed on the revised text and appended to the
revision's own note. What a correct briefing reports for this fixture is an EMPTY
list with `available: true`, since nothing here resembles anything else — and
holding that distinction is the point, because `available: false` means the
briefing was not computed and a reader must never read it as "nothing resembles
this".
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

import app as libapp
import conftest
import identity
import lease
import protocol
import store

AGENT = "agent-token-vivy"
HOLDER = "co-vivy-0912"


def _ts(**delta):
    return (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
            - dt.timedelta(**delta)).isoformat(" ")


@pytest.fixture
def client(tmp_path, monkeypatch, vector_index):
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
        CREATE TABLE decision_items (
            id TEXT PRIMARY KEY, product TEXT, state TEXT, refs TEXT);
    """)
    # (id, product, title, body, type, state, maturity, parent_id, spec, dod)
    rows = [
        ("held", "vivy", "A held item", "the original body", "bug", "claimed",
         None, None, None, None),
        ("unheld", "vivy", "Nobody holds this", "body", "bug", "open", None,
         None, None, None),
        ("epic", "vivy", "An approved epic", "body", "epic", "open", "approved",
         None, "the long form", "the finish line"),
        ("under_epic", "vivy", "Inside the epic", "body", "bug", "open", None,
         "epic", None, None),
        ("impr_ok", "vivy", "An approved improvement", "body", "improvement",
         "open", "approved", None, "a scope", None),
        ("done", "vivy", "Done", "body", "bug", "fixed", None, None, None, None),
        ("other", "constance", "Other product", "body", "bug", "open", None,
         None, None, None),
    ]
    conn.executemany(
        "INSERT INTO issue_items (id, product, title, body, type, state, "
        "maturity, parent_id, specification, definition_of_done, severity, "
        "needs, created_by_kind, created_at, refs, topics) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,'medium','agent','agent',?,'{}','[]')",
        [(*r, _ts(days=2)) for r in rows])
    # The live Course the approved epic stands on; a demotion has to take it off.
    conn.execute(
        "INSERT INTO courses (id, product, title, entries, status, version, "
        "created_by, created_at) VALUES ('course_vivy', 'vivy', 'The route', ?, "
        "'live', 1, 'test', ?)", (json.dumps(["epic"]), _ts(days=3)))
    conn.commit()
    conn.close()

    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        AGENT: {"name": "vivy-agent", "products": ["vivy"]},
    }))
    # The vec0 index, because the write routes reach it: an item or signal
    # created here is embedded after its commit, and a revision drops its
    # vectors inside the transaction that writes the new text.
    vector_index(db)
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    monkeypatch.setattr(lease, "TUNABLES", tmp_path / "absent.json")
    return TestClient(libapp.app)


def _h():
    return {"Authorization": f"Bearer {AGENT}",
            protocol.HEADER: str(protocol.CURRENT)}


def _db():
    conn = sqlite3.connect(store.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _take_lease(root_id, *, holder=HOLDER, alive=True):
    conn = _db()
    stamp = _ts(seconds=5) if alive else _ts(days=3)
    conn.execute(
        "INSERT INTO issue_leases (id, root_id, taken_for, holder, taken_at, "
        "heartbeat_at) VALUES (?,?,?,?,?,?)",
        (f"lease_{root_id}", root_id, root_id, holder, stamp, stamp))
    conn.commit()
    conn.close()


def _revise(client, item_id, body, *, owner=HOLDER):
    payload = dict(body)
    if owner is not None:
        payload["owner"] = owner
    return client.post(f"/items/{item_id}/revision", json=payload, headers=_h())


def _row(item_id):
    conn = _db()
    row = conn.execute("SELECT * FROM issue_items WHERE id = ?",
                       (item_id,)).fetchone()
    conn.close()
    return row


def _course_entries():
    conn = _db()
    row = conn.execute("SELECT entries FROM courses").fetchone()
    conn.close()
    return json.loads(row["entries"])


def _set_maturity(item_id, value):
    """Straight to the row: no revision writes `maturity` any more."""
    conn = _db()
    conn.execute("UPDATE issue_items SET maturity = ? WHERE id = ?",
                 (value, item_id))
    conn.commit()
    conn.close()


def _notes(item_id):
    conn = _db()
    rows = [r["body"] for r in conn.execute(
        "SELECT body FROM issue_comments WHERE item_id = ? ORDER BY id",
        (item_id,)).fetchall()]
    conn.close()
    return rows


def _heartbeat(root_id):
    conn = _db()
    hb = conn.execute("SELECT heartbeat_at FROM issue_leases WHERE root_id = ?",
                      (root_id,)).fetchone()["heartbeat_at"]
    conn.close()
    return hb


# --- the lease is the authorization ---------------------------------------------


def test_revising_an_item_the_caller_holds(client):
    _take_lease("held")
    r = _revise(client, "held", {"title": "A better title"})
    assert r.status_code == 200
    assert r.json()["item"]["title"] == "A better title"
    assert r.json()["lease"]["holder"] == HOLDER


def test_an_unheld_item_cannot_be_revised(client):
    """A revision is authorized by the lease, so with none there is nothing
    authorizing this one."""
    r = _revise(client, "unheld", {"title": "no"})
    assert r.status_code == 409 and "no live lease" in r.json()["error"]
    assert _row("unheld")["title"] == "Nobody holds this"


def test_a_lapsed_lease_reads_exactly_as_no_lease(client):
    """The grant expired and the subtree is claimable by someone else. A long
    session that never heartbeats holds a dead lease and looks alive to itself."""
    _take_lease("held", alive=False)
    r = _revise(client, "held", {"title": "no"})
    assert r.status_code == 409 and "no live lease" in r.json()["error"]


def test_someone_else_holding_it_is_refused_and_the_holder_is_named(client):
    """A bare no leaves an orchestrator with block or retry, and both waste the
    run."""
    _take_lease("held", holder="another-session")
    r = _revise(client, "held", {"title": "no"})
    assert r.status_code == 409
    assert "another-session" in r.json()["error"]
    assert r.json()["lease"]["holder"] == "another-session"
    assert _row("held")["title"] == "A held item"


def test_the_lease_on_the_epic_authorizes_revising_a_child(client):
    """The lease is on the item's epic, so it fences the subtree."""
    _take_lease("epic")
    assert _revise(client, "under_epic", {"body": "revised"}).status_code == 200


def test_a_terminal_item_is_a_record_rather_than_a_draft(client):
    _take_lease("done")
    r = _revise(client, "done", {"title": "no"})
    assert r.status_code == 409 and "terminal" in r.json()["error"]


def test_an_item_in_another_product_is_not_found(client):
    _take_lease("other")
    assert _revise(client, "other", {"title": "no"}).status_code == 404


# --- every revision is a sighting of its holder ----------------------------------


def test_a_revision_heartbeats_the_lease(client):
    """Every lease-bearing action a holder takes is a heartbeat, which keeps the
    grace period honest without asking workers to run a keepalive loop."""
    _take_lease("held")
    before = _heartbeat("held")
    assert _revise(client, "held", {"title": "moved"}).status_code == 200
    assert _heartbeat("held") > before


def test_an_idempotent_resubmission_is_still_a_sighting(client):
    """It is a no-op for the item and NOT one for the lease: getting here proves
    the caller still holds it."""
    _take_lease("held")
    before = _heartbeat("held")
    r = _revise(client, "held", {"title": "A held item"})
    assert r.status_code == 200
    assert _notes("held") == []
    assert _heartbeat("held") > before
    assert r.json()["adjacency"]["available"] is False
    assert "nothing changed" in r.json()["adjacency"]["reason"]


# --- what this route will not touch ----------------------------------------------


@pytest.mark.parametrize("field,value", [
    ("product", "constance"), ("parent_id", "epic"), ("type", "epic"),
    ("state", "fixed"), ("needs", "operator"), ("severity", "high"),
    ("refs", {"decision_id": "dec_1"}),
])
def test_a_forbidden_field_is_refused_by_name(client, field, value):
    """Silently dropping it would report success over a change that did not
    happen, and each of these has a route that DOES record it."""
    _take_lease("held")
    r = _revise(client, "held", {"title": "ok", field: value})
    assert r.status_code == 400
    assert r.json()["refused"] == [field]
    assert field in r.json()["error"]
    assert _row("held")["title"] == "A held item"


def test_at_least_one_revisable_field_is_required(client):
    _take_lease("held")
    r = _revise(client, "held", {})
    assert r.status_code == 400 and "at least one of" in r.json()["error"]


def test_a_title_may_not_be_cleared(client):
    """An item with no title is unreadable in every list that shows one."""
    _take_lease("held")
    for bad in (None, "", "   "):
        assert _revise(client, "held", {"title": bad}).status_code == 400


def test_a_body_may_be_cleared(client):
    """A description that turned out to be wrong is better empty than wrong."""
    _take_lease("held")
    r = _revise(client, "held", {"body": None})
    assert r.status_code == 200 and r.json()["item"]["body"] is None


@pytest.mark.parametrize("body", [
    {"title": 7}, {"body": []}, {"specification": 7},
    {"definition_of_done": 3},
])
def test_a_malformed_field_is_refused(client, body):
    _take_lease("held")
    assert _revise(client, "held", body).status_code == 400


def test_writes_are_refused_while_the_shadow_is_running(client, monkeypatch):
    _take_lease("held")
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    assert _revise(client, "held", {"title": "x"}).status_code == 503


# --- the approval invariant, shared with /correct ---------------------------------


def test_revising_an_approved_epics_text_returns_it_to_proposed(client):
    """The same gate `/correct` applies, through the same helper. A door enforcing
    it and another not is worth less than a validation."""
    _take_lease("epic")
    r = _revise(client, "epic", {"specification": "a different scope"})
    assert r.status_code == 200 and r.json()["item"]["maturity"] == "proposed"
    assert "Returned to `proposed`" in _notes("epic")[0]


@pytest.mark.parametrize("value", [None, "approved", "proposed", "shipped"])
def test_maturity_is_refused_here_too_and_names_the_approval_route(client, value):
    """**BOTH DOORS OR NEITHER**: `/correct` was the obvious hole and this route
    wrote the same field. The refusal is on presence, ahead of every other
    check, so an unheld item gets the same answer and nothing is staged."""
    _take_lease("under_epic")
    r = _revise(client, "under_epic", {"maturity": value, "title": "renamed"})
    assert r.status_code == 400
    assert r.json()["route"] == "POST /items/{item_id}/approve"
    assert _row("under_epic")["maturity"] is None
    assert _row("under_epic")["title"] == "Inside the epic"


def test_a_revision_demotion_takes_the_epic_off_its_course(client):
    _take_lease("epic")
    r = _revise(client, "epic", {"specification": "a different scope"})
    assert r.status_code == 200 and r.json()["item"]["maturity"] == "proposed"
    assert _course_entries() == []
    assert "Left the Course" in _notes("epic")[0]
    conn = _db()
    event = conn.execute("SELECT * FROM course_events").fetchone()
    conn.close()
    assert event["action"] == store.COURSE_ACTION_DEMOTED
    assert "epic left the Course" in event["note"]


def test_a_revision_asserting_the_end_state_leaves_the_course_alone(client):
    _take_lease("epic")
    r = _revise(client, "epic", {"specification": "reflowed",
                                 "end_state_unchanged": True})
    assert r.status_code == 200 and r.json()["item"]["maturity"] == "approved"
    assert _course_entries() == ["epic"]


def test_the_revocation_note_says_what_the_caller_could_have_done_here_too(client):
    """Both doors or neither — this is the door a holder actually edits an epic
    through, so it is the one a caller reads the note on most."""
    _take_lease("epic")
    r = _revise(client, "epic", {"specification": "a different scope"})
    assert r.status_code == 200 and r.json()["item"]["maturity"] == "proposed"
    note = _notes("epic")[-1]
    assert "Returned to `proposed`" in note
    assert "end_state_unchanged" in note, (
        "the note tells a caller what happened and not what to do instead")
    # **AND SAYS NOTHING ABOUT `type`, because this route cannot retype**
    # (security-reviewer, PR #67). The first version copied `/correct`'s sentence
    # verbatim and warned a holder off an interaction this door has no field for.
    assert "`type`" not in note, (
        "the note warns about a type change on a route that refuses `type` by "
        "name, so it describes a scenario this door cannot produce")


def test_asserting_the_end_state_did_not_move_keeps_the_approval_here_too(client):
    """**BOTH DOORS OR NEITHER.** A rule enforced at `/correct` and waived at
    `/revision` is two rules that happen to match today, and this is the route a
    holder actually edits an epic's text through. Same helper, so the assertion
    reaches the same invariant.

    The second call is the control: without it this passes against a build that
    never un-approved anything."""
    _take_lease("epic")
    r = _revise(client, "epic", {"specification": "a scope, reflowed",
                                 "end_state_unchanged": True})
    assert r.status_code == 200 and r.json()["item"]["maturity"] == "approved"
    note = _notes("epic")[0]
    assert "Approval kept" in note and "Returned to `proposed`" not in note

    same = _revise(client, "epic", {"specification": "a different scope"})
    assert same.status_code == 200
    assert same.json()["item"]["maturity"] == "proposed", (
        "the edit un-approves nothing even without the assertion, so the "
        "assertion above is not what kept the approval")


def test_no_approval_was_kept_so_none_is_claimed_on_the_thread_here_too(client):
    """**BOTH DOORS OR NEITHER, AND THAT APPLIES TO THE NEGATIVE CASES TOO**
    (reviewer, PR #65). The round-1 fix landed identically at both doors and only
    `/correct` grew regressions for it, which is the same "two rules that happen
    to match today" this file already refuses one paragraph up. The two call
    sites being textually identical is a reason to expect them to agree, not
    evidence that they do.

    Demoted first, so the edit lands on an item carrying no approval."""
    _take_lease("epic")
    _set_maturity("epic", "proposed")
    r = _revise(client, "epic", {"specification": "new text",
                                 "end_state_unchanged": True})
    assert r.status_code == 200 and r.json()["item"]["maturity"] == "proposed"
    # **THE LAST NOTE, because the demotion above wrote one first** — `_notes`
    # is ascending, so index 0 here is the setup call rather than the call under
    # test, and the assertion would pass against a note that never mentions the
    # assertion at all.
    note = _notes("epic")[-1]
    assert "Approval kept" not in note, (
        "an approval was claimed kept on an item that was carrying none")
    assert "specification:" in note, "the note itself stopped being written"


def test_a_non_epics_approval_was_never_at_risk_here_either(client):
    """A non-epic may be approved with no specification at all, so an edit to
    that text revokes nothing with or without the assertion, and crediting the
    assertion with saving it is a false record. The maturity below is correct
    either way, which is why asserting only the maturity would pass this."""
    _take_lease("impr_ok")
    r = _revise(client, "impr_ok", {"specification": "a note",
                                    "end_state_unchanged": True})
    assert r.status_code == 200 and r.json()["item"]["maturity"] == "approved"
    assert "Approval kept" not in _notes("impr_ok")[-1]


def test_a_non_boolean_assertion_is_refused_here_too(client):
    _take_lease("epic")
    r = _revise(client, "epic", {"specification": "reworded",
                                 "end_state_unchanged": "true"})
    assert r.status_code == 400
    assert "end_state_unchanged" in r.json()["error"]


def test_a_non_epic_keeps_its_approval_when_its_text_moves(client):
    """Its approval never rested on that text, and this route cannot retype, so
    the retype half of the revocation is unreachable here."""
    _take_lease("impr_ok")
    r = _revise(client, "impr_ok", {"specification": "a note"})
    assert r.status_code == 200 and r.json()["item"]["maturity"] == "approved"


def test_revising_a_title_does_not_touch_an_approval(client):
    """An approval attaches to the specification, not to every word on the row."""
    _take_lease("epic")
    r = _revise(client, "epic", {"title": "A renamed epic"})
    assert r.status_code == 200 and r.json()["item"]["maturity"] == "approved"


# --- the audit note ---------------------------------------------------------------


def test_the_note_names_the_actor_and_the_holder(client):
    _take_lease("held")
    assert _revise(client, "held", {"title": "renamed"}).status_code == 200
    note = _notes("held")[0]
    assert "vivy-agent" in note and HOLDER in note
    assert "**title:**" in note and "'A held item' → 'renamed'" in note


def test_two_bodies_sharing_a_prefix_do_not_read_as_identical(client):
    """`body` runs to MAX_ITEM_TEXT, so it is digested rather than quoted whole
    for the same reason the two prose fields are."""
    _take_lease("held")
    long_a, long_b = "y" * 300 + "ending A", "y" * 300 + "ending B"
    assert _revise(client, "held", {"body": long_a}).status_code == 200
    assert _revise(client, "held", {"body": long_b}).status_code == 200
    note = _notes("held")[1]
    assert "sha256:" in note and "chars" in note
    old, new = note.split("→")[0], note.split("→")[1]
    assert old != new


def test_one_comment_per_revision(client):
    """A second row per revision doubles the rate an item's thread grows, and the
    cap is enforced only in /comments — so the first casualty of exhausting it is
    a HUMAN refused a comment because system notes spent the budget."""
    _take_lease("held")
    assert _revise(client, "held", {"title": "one"}).status_code == 200
    assert _revise(client, "held", {"title": "two"}).status_code == 200
    assert len(_notes("held")) == 2


# --- the briefing this service cannot compute yet ---------------------------------


def test_the_briefing_is_recomputed_on_the_revised_text(client):
    """Contract §5. Nothing in this fixture resembles anything else, so what a
    correct briefing reports is an EMPTY list with `available: true` — which is
    the distinction the whole shape exists for: a reader must not be able to
    mistake "not computed" for "nothing resembles this"."""
    _take_lease("held")
    near = _revise(client, "held", {"title": "renamed"}).json()["adjacency"]
    assert near["available"] is True
    assert near["neighbours"] == []
    assert near["policy"]["threshold"] > 0
    assert set(near["guidance"]) == {"same-product", "cross-product"}
    assert "authoritative" in near["untrusted"]


def test_a_missing_vectors_file_degrades_the_revision_rather_than_500ing(
        client, monkeypatch, tmp_path):
    """`_do_revision` and `_revision_briefing` both opened
    `vectors.connect_rw()` unconditionally, so a missing `LIBRARY_VECTORS_DB`
    file 500'd the whole revision rather than degrading the briefing alone
    (reviewer + security-reviewer, issue_01M35R5GA1SKRG2CMQRF189NDE round 1).
    The revision itself still lands — nothing about writing the new title
    depends on the vector index — and the reason named is `vectors.
    available()`'s own."""
    import vectors
    monkeypatch.setattr(vectors, "VECTORS_DB_PATH",
                        tmp_path / "no-such-vectors.db")
    _take_lease("held")
    r = _revise(client, "held", {"title": "renamed while vectors are down"})
    assert r.status_code == 200
    assert _row("held")["title"] == "renamed while vectors are down"
    near = r.json()["adjacency"]
    assert near["available"] is False
    assert near["reason"] == "the vector index file is missing or could not be opened"


def test_the_missing_vectors_reason_names_no_path_in_response_or_note(
        client, monkeypatch, tmp_path):
    """The reason above reaches an authenticated caller's JSON AND, through
    `_briefing_note`, a permanent `issue_comments` row — a durable record any
    future holder of the item can read, not a transient response. Neither
    may carry the server's absolute filesystem path or the raw exception
    text (security-reviewer, issue_01M35R5GA1SKRG2CMQRF189NDE round 2): the
    old wording interpolated `_attach_vectors_db`'s own message, which for a
    missing file names `VECTORS_DB_PATH` verbatim.
    """
    import vectors
    missing = tmp_path / "no-such-vectors.db"
    monkeypatch.setattr(vectors, "VECTORS_DB_PATH", missing)
    _take_lease("held")
    r = _revise(client, "held", {"title": "renamed while vectors are down"})
    assert r.status_code == 200
    assert str(missing) not in r.text
    assert str(tmp_path) not in r.text
    assert "FileNotFoundError" not in r.text
    notes = _notes("held")
    assert notes, "revision must have left a briefing note"
    for note in notes:
        assert str(missing) not in note
        assert str(tmp_path) not in note
        assert "FileNotFoundError" not in note


def test_the_briefing_lands_on_the_revision_note_rather_than_a_second_one(client):
    """One comment per revision. A row per briefing doubles the rate an item's
    thread grows, and the cap is enforced only in `/comments`, so the first
    casualty of exhausting it is a human refused a comment."""
    _take_lease("held")
    assert _revise(client, "held", {"title": "renamed"}).status_code == 200
    notes = _notes("held")
    assert len(notes) == 1
    assert "**title:**" in notes[0]
    assert "Adjacency at check-in: no near duplicates" in notes[0]


def test_moving_the_text_replaces_the_vectors_with_the_new_ones(client):
    """Contract §5: the vectors are dropped in the same transaction as the write
    and re-embedded after the commit.

    Asserted on the STORED VECTORS rather than on the response's wording, because
    the wording is what a route can get right while the index stays stale. The
    fake embedder maps words to dimensions, so the new vector matches the new text
    and would not match the old one."""
    _take_lease("held")
    # EMBEDDED FIRST, so there is something to replace. Without this the item has
    # no vectors at all and a route that never invalidates still ends up with the
    # right ones — the test would pass against the code it exists to reject.
    libapp.vectors.embed_item_by_id(libapp.vectors.VAULT_CHART_HOUSE, "held")
    before = conftest.vectors_for(store.DB_PATH, "held")
    assert conftest.unpack(before[0]) == pytest.approx(
        conftest.fake_vector("A held item\n\nthe original body"))
    assert _revise(client, "held", {"body": "wholly new text"}).status_code == 200
    after = conftest.vectors_for(store.DB_PATH, "held")
    assert len(after) == 1
    assert conftest.unpack(after[0]) == pytest.approx(
        conftest.fake_vector("A held item\n\nwholly new text"))
    row = _db().execute("SELECT embedded_at FROM issue_items WHERE id='held'"
                        ).fetchone()
    assert row["embedded_at"] is not None


def test_a_second_revision_leaves_no_vector_of_the_text_it_replaced(client):
    """The failure this is really about: an index idempotent on `embedded_at`
    keeps the vectors of text the item no longer holds, and nothing announces it.
    So revise TWICE — the second pass is the one that would find a stale row."""
    _take_lease("held")
    _revise(client, "held", {"body": "first version of the words"})
    _revise(client, "held", {"body": "entirely different second words"})
    stored = conftest.vectors_for(store.DB_PATH, "held")
    assert len(stored) == 1                   # one item, one chunk, one vector
    assert conftest.unpack(stored[0]) == pytest.approx(
        conftest.fake_vector("A held item\n\nentirely different second words"))


def test_a_revision_that_moves_no_text_keeps_the_vectors_it_had(client):
    """Only `title` and `body` are embedded, so a specification-only revision
    must not throw away a correct index and pay to rebuild it."""
    _take_lease("epic")
    libapp.vectors.embed_item_by_id(libapp.vectors.VAULT_CHART_HOUSE, "epic")
    before = conftest.vectors_for(store.DB_PATH, "epic")
    stamp = _db().execute("SELECT embedded_at FROM issue_items WHERE id='epic'"
                          ).fetchone()["embedded_at"]
    assert _revise(client, "epic", {"specification": "new"}).status_code == 200
    assert conftest.vectors_for(store.DB_PATH, "epic") == before
    assert _db().execute("SELECT embedded_at FROM issue_items WHERE id='epic'"
                         ).fetchone()["embedded_at"] == stamp


def test_a_holder_outside_the_callers_scope_is_not_named(client):
    """The caller proved scope over the item it named, not over the row the lease
    sits on. Guarded upstream today, since no route can build a cross-product
    edge — which is the argument this check exists so as not to depend on."""
    conn = _db()
    conn.execute("UPDATE issue_items SET product='constance' WHERE id='epic'")
    conn.commit()
    conn.close()
    _take_lease("epic", holder="a-constance-session")
    r = _revise(client, "under_epic", {"title": "renamed"})
    assert r.status_code == 409
    assert "outside this identity's scope" in r.json()["error"]
    assert "lease" not in r.json()
    assert "a-constance-session" not in json.dumps(r.json())


# --- a losing concurrent writer is refused, not silently overwritten -----------
#
# A revision races a resolve on the same item: only one may land. Before the
# fix, revision's final UPDATE carried no predicate, so a resolve that committed
# in the window after revision's read (and every gate since, which take no
# lock) and before revision's write was silently overwritten — landing a
# revision on an item that had already gone terminal, contradicting the
# refusal this same function gives when it reads that state directly.


def test_a_revision_that_races_a_concurrent_resolve_is_rolled_back(client,
                                                                    monkeypatch):
    """Deterministic rather than a coin-flip on OS scheduling. Revision is held
    at `_approval_invariant` — the seam every revision reaches unconditionally,
    right after the lease check and before any mutation — while a resolve runs
    to completion and commits; only then is revision allowed to proceed to its
    own write.

    The resolve names the same holder the lease was taken for, so it clears
    revision's own lease gate rather than tripping it — the race is on the
    `issue_items` row, not on who may hold the lease.
    """
    import threading

    _take_lease("held")
    revision_ready = threading.Event()
    resolve_done = threading.Event()
    real_invariant = libapp._approval_invariant

    def invariant_racer(*args, **kwargs):
        revision_ready.set()
        assert resolve_done.wait(timeout=20), "resolve never completed"
        return real_invariant(*args, **kwargs)

    monkeypatch.setattr(libapp, "_approval_invariant", invariant_racer)

    results = {}

    def do_revise():
        results["revise"] = _revise(client, "held", {"title": "a new title"})

    def do_resolve():
        assert revision_ready.wait(timeout=20), "revision never reached the seam"
        results["resolve"] = client.post(
            "/items/held/resolve",
            json={"resolution": "fixed", "owner": HOLDER}, headers=_h())
        resolve_done.set()

    threads = [threading.Thread(target=do_revise),
               threading.Thread(target=do_resolve)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert set(results) == {"revise", "resolve"}, (
        "a request died before reaching the race seam")
    assert results["resolve"].status_code == 200, results["resolve"].json()
    assert results["revise"].status_code == 409, results["revise"].json()
    assert results["revise"].json()["error"] == (
        "the item is no longer claimed; it is now fixed")

    row = _row("held")
    assert row["state"] == "fixed"
    assert row["title"] == "A held item"
    assert _notes("held") == []


# --- short_description (issue_01M0XR0QSMKBSGBYD49CPTJ37E) ----------------------


def test_a_short_description_can_be_set_and_cleared(client):
    _take_lease("held")
    r = _revise(client, "held", {"short_description": "Held label"})
    assert r.status_code == 200
    assert r.json()["item"]["short_description"] == "Held label"
    assert _row("held")["short_description"] == "Held label"
    assert "**short_description:**" in _notes("held")[0]
    r = _revise(client, "held", {"short_description": None})
    assert r.status_code == 200
    assert _row("held")["short_description"] is None


def test_a_short_description_over_the_cap_is_refused_and_stores_nothing(client):
    _take_lease("held")
    r = _revise(client, "held", {
        "short_description": "s" * (store.MAX_SHORT_DESCRIPTION + 1)})
    assert r.status_code == 400
    assert r.json()["limit"] == store.MAX_SHORT_DESCRIPTION
    assert _row("held")["short_description"] is None


def test_a_short_description_revision_keeps_the_approval_and_the_vectors(client):
    """The label is not embedded and is not part of what an operator approves,
    so changing it moves neither."""
    _take_lease("epic")
    libapp.vectors.embed_item_by_id(libapp.vectors.VAULT_CHART_HOUSE, "epic")
    before = conftest.vectors_for(store.DB_PATH, "epic")
    r = _revise(client, "epic", {"short_description": "Epic label"})
    assert r.status_code == 200
    assert r.json()["item"]["maturity"] == "approved"
    assert conftest.vectors_for(store.DB_PATH, "epic") == before
