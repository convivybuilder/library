"""Ending an item, and appending to its thread.

Two things here are more than bookkeeping.

**Resolving releases the lease it holds, and refuses when it does not hold it.**
An epic otherwise stays fenced for the whole grace period after the work is done
— the same stranding `/release` prevents, by the likelier door, since an agent
that fixes something resolves it and moves on.

An earlier version of this file forced that release unconditionally, and said
here that a terminal state ends the hold regardless of who asks, so no live work
remained to protect. That reasoning is circular and the code no longer follows
it: the work is live until the resolve makes it terminal, so forcing let any
in-scope caller end another session's work and unfence its epic — the reserved
act ADR-0026 puts behind a human gate, through the route that was not checking.
A DEAD lease still forces, because reclaiming one needs no human, which is the
distinction the ADR actually draws.

**`duplicate_of` is validated, and validated in the caller's scope.** A duplicate
pointing at nothing reads as a resolved relationship and sends the next reader
after an item that was never there; checking it in scope is what stops the check
becoming an existence probe for other products.
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
def client(tmp_path, monkeypatch, ledger_tables):
    db = tmp_path / "control.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE issue_items (
            id TEXT PRIMARY KEY, product TEXT, title TEXT, body TEXT, refs TEXT,
            severity TEXT, state TEXT, claimed_by TEXT, claimed_at TEXT,
            resolution TEXT, resolved_by TEXT, resolved_at TEXT, outcome_note TEXT,
            once_key TEXT UNIQUE, created_by TEXT, created_at TEXT, run_id TEXT,
            type TEXT, created_by_kind TEXT, needs TEXT, park_reason TEXT,
            unpark_condition TEXT, parked_at TEXT, embedded_at TEXT,
            parent_id TEXT, topics TEXT, maturity TEXT, specification TEXT,
            definition_of_done TEXT, prerequisites TEXT,
            model_choice TEXT, model_reason TEXT, model_set_by_client TEXT,
            model_acting_human TEXT, model_set_at TEXT, found_during TEXT, item_key TEXT);
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
            id TEXT PRIMARY KEY, root_id TEXT, taken_for TEXT, holder TEXT,
            run_id TEXT, taken_at TEXT, heartbeat_at TEXT, released_at TEXT,
            release_reason TEXT, released_by TEXT, note TEXT);
        CREATE UNIQUE INDEX ix_live_root ON issue_leases (root_id)
            WHERE released_at IS NULL;
        CREATE TABLE lease_refusals (
            id TEXT PRIMARY KEY, at TEXT, requested_item_id TEXT, product TEXT,
            requester TEXT, actor TEXT, blocking_lease_id TEXT,
            blocking_root_id TEXT, blocking_holder TEXT, blocks_by TEXT);
        CREATE TABLE call_slips (
            id TEXT PRIMARY KEY, item_id TEXT, product TEXT, claim_owner_label TEXT,
            issued_at TEXT, expires_at TEXT);
    """)
    conn.executemany(
        "INSERT INTO issue_items (id, product, title, type, state, needs, "
        "created_by_kind, parent_id, claimed_by, created_at, refs) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [("epic_v", "vivy", "An epic", "epic", "open", "agent", "agent", None,
          None, _ts(days=3), "{}"),
         ("task_a", "vivy", "Task A", "task", "open", "agent", "agent",
          "epic_v", None, _ts(days=2), "{}"),
         ("task_b", "vivy", "Task B", "task", "open", "agent", "agent",
          "epic_v", None, _ts(days=2), "{}"),
         # A THIRD LEVEL. Two levels cannot distinguish a one-hop lookup from the
         # bidirectional walk, which is exactly how three holes in a row stayed
         # invisible to this file.
         ("sub_epic", "vivy", "Sub epic", "epic", "open", "agent", "agent",
          "epic_v", None, _ts(days=2), "{}"),
         ("deep", "vivy", "Deep task", "task", "open", "agent", "agent",
          "sub_epic", None, _ts(days=2), "{}"),
         # A SIBLING EPIC, so the tree can hold TWO simultaneous live leases
         # under one parent. A linear chain can only ever produce zero or one
         # conflict per walk, which cannot distinguish "checked the conflict
         # correctly" from "only ever finds one and assumes that is all".
         ("alt_epic", "vivy", "Sibling epic", "epic", "open", "agent", "agent",
          "epic_v", None, _ts(days=2), "{}"),
         ("lone", "vivy", "Parentless", "task", "open", "agent", "agent", None,
          None, _ts(days=1), "{}"),
         ("done", "vivy", "Already done", "task", "fixed", "agent", "agent",
          None, None, _ts(days=1), "{}"),
         ("other", "constance", "Other product", "task", "open", "agent",
          "agent", None, None, _ts(days=1), "{}")])
    conn.commit()
    conn.close()

    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        AGENT: {"name": "vivy-agent", "products": ["vivy"]},
        WIDE: {"name": "wide-agent", "products": ["*"]},
    }))
    ledger_tables(db)
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    monkeypatch.setattr(lease, "TUNABLES", tmp_path / "absent.json")
    return TestClient(libapp.app)


def _h(token=AGENT):
    return {"Authorization": f"Bearer {token}",
            protocol.HEADER: str(protocol.CURRENT)}


def _resolve(client, item, **body):
    payload = {"resolution": "fixed"}
    payload.update(body)
    return client.post(f"/items/{item}/resolve", json=payload, headers=_h())


def _db():
    conn = sqlite3.connect(store.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# --- resolving ends the hold ---------------------------------------------------


def test_resolving_releases_the_lease_it_was_holding(client):
    """**The likelier stranding**: an agent that fixes an item resolves it and
    moves on, so without this the epic stays fenced for the whole grace period
    after the work is done."""
    client.post("/items/task_a/checkout", json={"owner": "session-1"},
                headers=_h())
    # The holder names itself, exactly as `/release` requires. Without it the
    # resolver is a different party as far as the server can tell, and the fence
    # is not its to end.
    r = _resolve(client, "task_a", owner="session-1")
    assert r.status_code == 200
    assert r.json()["lease_released"]["root_id"] == "epic_v"
    conn = _db()
    row = conn.execute("SELECT release_reason FROM issue_leases").fetchone()
    conn.close()
    assert row["release_reason"] == "resolved", (
        "finished-without-releasing must stay distinguishable from a clean "
        "hand-back")


def test_resolving_work_another_session_actively_holds_is_refused(client):
    """**The reserved act, and this route was the door that was not checking**
    (security-reviewer, PR #9).

    The first version forced the release unconditionally, so any token with the
    stock `item.resolve` grant could mark another session's in-flight work
    terminal and unfence its whole epic — no human, and no `force` flag to
    refuse. My justification was circular: the work is live until this very call
    makes it terminal, so "nothing live left to protect" assumed the conclusion.

    Diverges from Orient, which forces here. ADR-0026 is a recorded decision and
    parity does not outrank it.
    """
    client.post("/items/task_a/checkout", json={"owner": "session-1"},
                headers=_h())
    r = client.post("/items/task_a/resolve",
                    json={"resolution": "wontfix", "owner": "session-2"},
                    headers=_h(WIDE))
    assert r.status_code == 409
    assert "session-1" in r.json()["error"]
    conn = _db()
    row = conn.execute("SELECT state FROM issue_items WHERE id='task_a'").fetchone()
    live = conn.execute("SELECT released_at FROM issue_leases").fetchone()
    conn.close()
    assert row["state"] == "claimed", "another session's work was made terminal"
    assert live["released_at"] is None, "a live fence was broken"


def test_naming_the_holder_still_drives_the_resolve(client):
    """**A RECORDED GAP, NOT A CLAIM THAT THIS IS RIGHT**
    (`issue_01KZMQCBXQ5KF2DTJCV3A9GMF7`).

    The gate compares two self-declared strings, so a caller that reads
    `claimed_by` off the item and asserts it walks straight past. It stops a
    caller that forgets to name itself, and nothing more.

    Worse here than at `/release`: this drives the item into a terminal state no
    later call can undo, attributed to someone who never held the work. Pinned so
    the limit is visible rather than implied, and so whoever closes it in Phase 5
    finds a test describing the hole. If this starts failing because the gap was
    closed, that is the fix landing — invert it, do not delete it.
    """
    client.post("/items/task_a/checkout", json={"owner": "session-1"},
                headers=_h())
    r = client.post("/items/task_a/resolve",
                    json={"resolution": "wontfix", "owner": "session-1"},
                    headers=_h(WIDE))
    assert r.status_code == 200
    conn = _db()
    row = conn.execute("SELECT state, resolved_by FROM issue_items "
                       "WHERE id='task_a'").fetchone()
    conn.close()
    assert row["state"] == "wontfix"
    assert row["resolved_by"] == "wide-agent", (
        "the resolver is recorded even though it never held the work")


def test_resolving_an_epic_someone_is_working_inside_is_refused(client):
    """**The hole the first guard left, and it needed no owner-guessing**
    (reviewer, PR #9). `root_id_for` maps an epic to itself while `taken_for`
    records the leaf that was checked out, so scoping the refusal to `taken_for`
    meant resolving the EPIC skipped the check entirely while a child was held.

    Strictly more permissive than the disclosed gap: an unrelated caller could
    end an epic, and the live lease on it — held for a child someone is actively
    working — survived untouched and unaware.
    """
    client.post("/items/task_a/checkout", json={"owner": "session-1"},
                headers=_h())
    r = client.post("/items/epic_v/resolve",
                    json={"resolution": "wontfix", "owner": "someone-else"},
                    headers=_h(WIDE))
    assert r.status_code == 409
    assert "session-1" in r.json()["error"]
    conn = _db()
    state = conn.execute("SELECT state FROM issue_items WHERE id='epic_v'"
                         ).fetchone()["state"]
    live = conn.execute("SELECT released_at FROM issue_leases").fetchone()
    conn.close()
    assert state == "open", "an epic was ended out from under a working session"
    assert live["released_at"] is None


def test_a_hold_two_levels_up_still_refuses(client):
    """**The gap a one-hop lookup left** (security-reviewer, PR #9). `session-1`
    holds `epic_v`, whose fence covers `sub_epic` and `deep`. Resolving `deep`
    computes its root as `sub_epic`, which carries no lease — so a single
    `live_lease_on` never looked past the immediate parent and returned 200."""
    client.post("/items/epic_v/checkout", json={"owner": "session-1"},
                headers=_h())
    r = _resolve(client, "deep", owner="someone-else")
    assert r.status_code == 409
    assert "session-1" in r.json()["error"]


def test_a_hold_two_levels_down_still_refuses(client):
    """The other direction, and a one-hop lookup missed it too. `session-1` holds
    `sub_epic` by checking out `deep`; resolving the grandparent `epic_v`
    computes its own root as itself, which carries no lease."""
    client.post("/items/deep/checkout", json={"owner": "session-1"},
                headers=_h())
    r = _resolve(client, "epic_v", owner="someone-else")
    assert r.status_code == 409
    assert "session-1" in r.json()["error"]


def test_the_holder_may_resolve_its_own_work_two_levels_down(client):
    """The permission check reads the lease's `holder` alone, so it should be
    depth-independent — but the security review verified that by tracing rather
    than by test, and reasoned-correct-but-untested is where this route has gone
    wrong four times."""
    client.post("/items/task_a/checkout", json={"owner": "session-1"},
                headers=_h())
    # `session-1` holds `epic_v`; `deep` sits two levels below it.
    assert _resolve(client, "deep", owner="session-1").status_code == 200


def test_a_dead_hold_nearby_does_not_hide_a_live_one_further_out(client):
    """`find_conflict` must walk PAST a dead lease rather than stopping at it. A
    loop that returned on the first row found would let a dead near hold mask a
    live distant one — also traced rather than tested."""
    client.post("/items/deep/checkout", json={"owner": "gone"}, headers=_h())
    conn = _db()
    conn.execute("UPDATE issue_leases SET taken_at = ?, heartbeat_at = ? "
                 "WHERE root_id = 'sub_epic'", (_ts(hours=9), _ts(hours=9)))
    conn.commit()
    conn.close()
    # A live hold further out, on the grandparent.
    client.post("/items/epic_v/checkout", json={"owner": "session-1"},
                headers=_h())
    r = _resolve(client, "deep", owner="someone-else")
    assert r.status_code == 409
    assert "session-1" in r.json()["error"], (
        "the dead nearer lease masked the live one beyond it")


def test_a_subtree_too_large_to_enumerate_refuses_the_resolve(client,
                                                              monkeypatch):
    """The same refusal checkout gives, and it had no test on this route. A fence
    whose reach cannot be measured must not be reasoned about."""
    monkeypatch.setattr(lease, "MAX_SUBTREE_NODES", 1)
    r = _resolve(client, "epic_v", owner="session-1")
    assert r.status_code == 409
    assert "cannot enumerate this subtree" in r.json()["error"]
    conn = _db()
    state = conn.execute("SELECT state FROM issue_items WHERE id='epic_v'"
                         ).fetchone()["state"]
    conn.close()
    assert state == "open", "an item was ended over an unmeasured fence"


def test_naming_one_holder_does_not_get_past_a_second_one(client):
    """**The fifth hole, and the subtlest** (reviewer, PR #9). `find_conflict`
    returns the FIRST live lease it hits. `take` gets away with that because it
    loops, breaking one per pass; a check has no loop, so reading the first hit
    as the whole answer let a caller name whichever holder it had been told about
    and sail past a second, still-live one on a sibling branch.

    Worse because the refusal names the blocking holder: one refused attempt was
    enough to learn a name to resubmit with.

    Two sibling epics under one parent, held by different sessions, is an
    ordinary shape — nested epics lease themselves, so siblings get distinct
    roots and are checked out independently.
    """
    client.post("/items/sub_epic/checkout", json={"owner": "session-1"},
                headers=_h())
    client.post("/items/alt_epic/checkout", json={"owner": "session-2"},
                headers=_h())
    # Naming either holder must not be enough, because the other is still live.
    for named in ("session-1", "session-2"):
        r = _resolve(client, "epic_v", owner=named)
        assert r.status_code == 409, f"naming {named} got past the other holder"
    conn = _db()
    state = conn.execute("SELECT state FROM issue_items WHERE id='epic_v'"
                         ).fetchone()["state"]
    live = conn.execute("SELECT COUNT(*) FROM issue_leases WHERE released_at "
                        "IS NULL").fetchone()[0]
    conn.close()
    assert state == "open" and live == 2


def test_the_sole_holder_of_a_whole_subtree_may_still_resolve_it(client):
    """The set check must not become "any conflict at all refuses". One session
    holding both branches is entitled to end the parent."""
    client.post("/items/sub_epic/checkout", json={"owner": "session-1"},
                headers=_h())
    client.post("/items/alt_epic/checkout", json={"owner": "session-1"},
                headers=_h())
    assert _resolve(client, "epic_v", owner="session-1").status_code == 200


def test_a_hold_on_an_unrelated_branch_does_not_refuse(client):
    """The walk must not become "any lease anywhere blocks everything". `lone`
    shares no ancestry with the epic tree."""
    client.post("/items/epic_v/checkout", json={"owner": "session-1"},
                headers=_h())
    assert _resolve(client, "lone", owner="someone-else").status_code == 200


def test_the_holder_may_still_resolve_the_epic_it_holds(client):
    """The refusal must not become "no epic with any lease may be resolved" — a
    session that holds an epic for a child and then finishes the epic is the
    ordinary case, not an intrusion."""
    client.post("/items/task_a/checkout", json={"owner": "session-1"},
                headers=_h())
    assert _resolve(client, "epic_v", owner="session-1").status_code == 200


def test_releasing_stays_scoped_to_taken_for_even_though_refusing_is_not(client):
    """The two concerns differ and only one takes `taken_for`: a session holding
    an epic across several children must not lose its fence to any one of them
    finishing."""
    client.post("/items/task_a/checkout", json={"owner": "session-1"},
                headers=_h())
    r = _resolve(client, "task_b", owner="session-1")
    assert r.status_code == 200
    assert r.json()["lease_released"] is None, "a sibling's resolve took the fence"
    conn = _db()
    assert conn.execute("SELECT released_at FROM issue_leases"
                        ).fetchone()["released_at"] is None
    conn.close()


def test_a_dead_lease_does_not_block_a_resolve(client):
    """Reclaiming a dead lease needs no human — that is the distinction ADR-0026
    draws, and the whole reason a lease has an expiry. A holder that went away
    must not strand the item as unresolvable."""
    client.post("/items/task_a/checkout", json={"owner": "session-1"},
                headers=_h())
    conn = _db()
    conn.execute("UPDATE issue_leases SET taken_at = ?, heartbeat_at = ?",
                 (_ts(hours=9), _ts(hours=9)))
    conn.commit()
    conn.close()
    r = _resolve(client, "task_a", owner="session-2")
    assert r.status_code == 200
    assert r.json()["lease_released"] is not None


def test_a_stranger_cannot_resolve_a_sibling_inside_someone_elses_fence(client):
    """The widened refusal reaches here too, and should: `task_b` sits inside a
    subtree `session-1` has spoken for, so ending it is ending work inside
    someone's fence.

    This test previously resolved without naming itself and expected a 200,
    which encoded the permissiveness the epic case made visible."""
    client.post("/items/task_a/checkout", json={"owner": "session-1"},
                headers=_h())
    r = _resolve(client, "task_b", owner="a-stranger")
    assert r.status_code == 409


def test_an_unfenced_item_resolves_without_complaint(client):
    """Returning None when there is nothing to release is the ordinary case for
    every row predating checkout."""
    r = _resolve(client, "lone")
    assert r.status_code == 200 and r.json()["lease_released"] is None


# --- what resolving records ----------------------------------------------------


@pytest.mark.parametrize("resolution", ["fixed", "wontfix", "duplicate"])
def test_the_resolution_becomes_the_state(client, resolution):
    """They are the same strings rather than a mapping, so "how did this end" and
    "what state is it in" cannot disagree."""
    r = _resolve(client, "lone", resolution=resolution)
    assert r.json()["state"] == resolution
    assert r.json()["resolution"] == resolution


def test_a_redirect_must_say_where(client):
    """A redirect that does not say where is a dead end wearing the shape of a
    handoff."""
    r = _resolve(client, "lone", resolution="redirected")
    assert r.status_code == 400
    assert "outcome_note" in r.json()["error"]
    assert _resolve(client, "lone", resolution="redirected",
                    outcome_note="moved to the platform queue").status_code == 200


def test_an_unknown_resolution_is_refused(client):
    r = _resolve(client, "lone", resolution="abandoned")
    assert r.status_code == 400
    assert sorted(store.ITEM_RESOLUTIONS) == r.json()["supported"]


def test_resolving_twice_is_refused(client):
    r = _resolve(client, "done")
    assert r.status_code == 409 and "already fixed" in r.json()["error"]


def test_the_resolver_is_recorded_from_the_token(client):
    _resolve(client, "lone", outcome_note="all done")
    conn = _db()
    row = conn.execute("SELECT resolved_by, outcome_note FROM issue_items "
                       "WHERE id='lone'").fetchone()
    ev = conn.execute("SELECT actor, from_state, to_state, note FROM issue_events "
                      "WHERE item_id='lone'").fetchone()
    conn.close()
    assert row["resolved_by"] == "vivy-agent"
    assert (ev["actor"], ev["from_state"], ev["to_state"]) == (
        "vivy-agent", "open", "fixed")
    assert ev["note"] == "all done"


def test_an_out_of_scope_item_is_404(client):
    assert client.post("/items/other/resolve", json={"resolution": "fixed"},
                       headers=_h()).status_code == 404


# --- duplicate_of, the one ref with a meaning the server can check --------------


def test_a_duplicate_pointing_at_nothing_is_refused(client):
    """Worse than no link: it reads as a resolved relationship and sends the next
    reader after an item that was never there."""
    r = _resolve(client, "lone", resolution="duplicate",
                 refs={"duplicate_of": "issue_nope"})
    assert r.status_code == 400
    assert "no such item" in r.json()["error"]


def test_a_duplicate_pointing_out_of_scope_reads_the_same_as_missing(client):
    """Checking in the caller's scope is what stops this becoming an existence
    probe for other products — the same rule the 404 follows."""
    absent = _resolve(client, "lone", resolution="duplicate",
                      refs={"duplicate_of": "issue_nope"}).json()
    foreign = _resolve(client, "task_a", resolution="duplicate",
                       refs={"duplicate_of": "other"}).json()
    assert absent["error"].replace("issue_nope", "X") == \
        foreign["error"].replace("other", "X")


def test_an_item_cannot_duplicate_itself(client):
    r = _resolve(client, "lone", resolution="duplicate",
                 refs={"duplicate_of": "lone"})
    assert r.status_code == 400 and "itself" in r.json()["error"]


def test_a_valid_duplicate_is_stored(client):
    r = _resolve(client, "task_a", resolution="duplicate",
                 refs={"duplicate_of": "task_b"})
    assert r.status_code == 200
    conn = _db()
    refs = json.loads(conn.execute("SELECT refs FROM issue_items WHERE id='task_a'"
                                   ).fetchone()["refs"])
    conn.close()
    assert refs["duplicate_of"] == "task_b"


def test_resolving_does_not_erase_existing_refs(client):
    """The merge is a merge: resolving must not drop what an item already knew."""
    conn = _db()
    conn.execute("UPDATE issue_items SET refs = ? WHERE id='lone'",
                 (json.dumps({"pr": "https://example.test/1"}),))
    conn.commit()
    conn.close()
    _resolve(client, "lone", refs={"note": "added"})
    conn = _db()
    refs = json.loads(conn.execute("SELECT refs FROM issue_items WHERE id='lone'"
                                   ).fetchone()["refs"])
    conn.close()
    assert refs == {"pr": "https://example.test/1", "note": "added"}


# --- comments ------------------------------------------------------------------


def test_a_comment_is_appended_with_its_author(client):
    r = client.post("/items/lone/comments", json={"body": "a note"},
                    headers=_h())
    assert r.status_code == 201
    assert r.json()["actor"] == "vivy-agent" and r.json()["kind"] == "note"


def test_a_diagnosis_on_an_unclaimed_item_is_refused(client):
    """A diagnosis is the handoff contract a fixer consumes, so a forged one on
    unclaimed work is the shape worth stopping."""
    r = client.post("/items/lone/comments",
                    json={"body": "root cause", "kind": "diagnosis"},
                    headers=_h())
    assert r.status_code == 409
    assert "claim it before posting a diagnosis" in r.json()["error"]


def test_a_diagnosis_on_a_claimed_item_is_allowed(client):
    client.post("/items/lone/checkout", json={"owner": "session-1"}, headers=_h())
    assert client.post("/items/lone/comments",
                       json={"body": "root cause", "kind": "diagnosis"},
                       headers=_h()).status_code == 201


def test_an_unknown_kind_is_refused(client):
    r = client.post("/items/lone/comments",
                    json={"body": "x", "kind": "shout"}, headers=_h())
    assert r.status_code == 400
    assert sorted(store.ITEM_COMMENT_KINDS) == r.json()["supported"]


def test_an_empty_body_is_refused(client):
    assert client.post("/items/lone/comments", json={"body": "   "},
                       headers=_h()).status_code == 400


def test_a_full_thread_is_permanently_full(client, monkeypatch):
    """Permanently rather than rate-limited, so a caller opens a new item rather
    than retrying against this thread forever."""
    monkeypatch.setattr(store, "MAX_COMMENTS_PER_ITEM", 2)
    for _ in range(2):
        assert client.post("/items/lone/comments", json={"body": "x"},
                           headers=_h()).status_code == 201
    r = client.post("/items/lone/comments", json={"body": "x"}, headers=_h())
    assert r.status_code == 409 and "thread is full" in r.json()["error"]


def test_commenting_out_of_scope_is_404(client):
    assert client.post("/items/other/comments", json={"body": "x"},
                       headers=_h()).status_code == 404


def test_writes_are_refused_while_the_shadow_is_running(client, monkeypatch):
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    assert _resolve(client, "lone").status_code == 503
    assert client.post("/items/lone/comments", json={"body": "x"},
                       headers=_h()).status_code == 503


# --- two concurrent resolutions: the compare-and-swap ---------------------------


def test_a_resolution_that_loses_a_concurrent_race_is_rolled_back(client,
                                                                  monkeypatch):
    """Two concurrent resolutions of one `open` item both pass the Python-only
    terminal check and only one may land.

    **This is the reproduction the fix exists for**: with the UPDATE
    unconditioned on state, both requests returned 200 and `issue_events` ended
    with two rows each claiming a transition out of `open`, the item left in
    whichever state committed last. The compare-and-swap refuses the loser with
    a 409 naming the state it collided with, and the loser's event is never
    written.

    Two real threads against the on-disk database, synchronised on
    `_hold_for_write` — the seam every resolve crosses AFTER reading the item
    and passing the terminal check, BEFORE taking the write lock, and exactly
    once — so both are guaranteed to have judged the item `open` before either
    writes. (The PR #26 race tests used the same two-thread barrier technique.)
    """
    import threading

    barrier = threading.Barrier(2)
    real = libapp._hold_for_write

    def racer(*args, **kwargs):
        barrier.wait(timeout=20)
        return real(*args, **kwargs)

    monkeypatch.setattr(libapp, "_hold_for_write", racer)
    results = {}

    def resolve(resolution):
        results[resolution] = _resolve(client, "lone", resolution=resolution,
                                       outcome_note="racing")

    threads = [threading.Thread(target=resolve, args=(res,))
               for res in ("fixed", "wontfix")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert set(results) == {"fixed", "wontfix"}, (
        "a request died before reaching the race seam")
    codes = {res: r.status_code for res, r in results.items()}
    assert sorted(codes.values()) == [200, 409], codes
    winner = next(res for res, code in codes.items() if code == 200)
    loser_body = next(r.json() for res, r in results.items() if res != winner)
    assert loser_body["error"] == (
        f"the item is no longer open; it is now {winner}")

    conn = _db()
    state = conn.execute("SELECT state FROM issue_items WHERE id='lone'"
                         ).fetchone()["state"]
    events = conn.execute(
        "SELECT from_state, to_state FROM issue_events WHERE item_id='lone'"
    ).fetchall()
    conn.close()
    assert state == winner, "the loser's resolution overwrote the winner's"
    assert [(e["from_state"], e["to_state"]) for e in events] == [
        ("open", winner)], "the loser's transition was recorded"
