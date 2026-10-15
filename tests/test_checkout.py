"""Checking out an item: the claim, the fence, and what the fence refuses.

**The fence is the reason this file is long.** `find_conflict` looks in two
directions because a lease reaches downward only, and checking one direction is
the mistake its docstring exists to prevent — so both are exercised here, with an
epic above and a sub-epic below.

`take` has four outcomes and they are distinguished because the override rate
depends on it: refresh, take, reclaim, override. A test that only proved "a lease
appears" would pass for all four.

Two things this file deliberately pins as REFUSALS rather than behaviour:
`force` (this service issues no human identity) and a subtree too large to
enumerate (a fence whose reach is unknown must not be taken).
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

import app as libapp
import call_slips
import identity
import lease
import protocol
import reference
import store

AGENT = "agent-token-vivy"
WIDE = "agent-token-wide"
NOREF = "agent-token-noref"
# Every token this file mints carries no `profile`, so this is what `_identify`
# resolves AGENT and WIDE to for `_call_slip_grant`'s purposes: unbound,
# `_persona_model`'s scope check a no-op (issue_01M35QG0VANA5P1XE3PH6A7PS4).
UNBOUND = identity.Identity(actor="vivy-agent", kind="agent")


def _ts(**delta):
    return (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
            - dt.timedelta(**delta)).isoformat(" ")


@pytest.fixture
def client(tmp_path, monkeypatch, vector_index, ledger_tables):
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
        CREATE TABLE call_slips (
            id TEXT PRIMARY KEY, item_id TEXT, product TEXT, claim_owner_label TEXT,
            issued_at TEXT, expires_at TEXT, grant_json TEXT);
        CREATE TABLE issue_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT, actor TEXT,
            kind TEXT, body TEXT, run_id TEXT, at TEXT);
    """)
    # A three-level tree, which is what makes the downward check meaningful:
    #   top_epic
    #     ├── sub_epic ── deep_task
    #     ├── task_a
    #     └── task_b
    conn.executemany(
        "INSERT INTO issue_items (id, product, title, type, state, needs, "
        "created_by_kind, parent_id, created_at, refs) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [("top_epic", "vivy", "Top epic", "epic", "open", "agent", "agent",
          None, _ts(days=3), "{}"),
         ("sub_epic", "vivy", "Sub epic", "epic", "open", "agent", "agent",
          "top_epic", _ts(days=3), "{}"),
         ("deep_task", "vivy", "Deep task", "task", "open", "agent", "agent",
          "sub_epic", _ts(days=2), "{}"),
         ("task_a", "vivy", "Task A", "task", "open", "agent", "agent",
          "top_epic", _ts(days=2), "{}"),
         ("task_b", "vivy", "Task B", "task", "open", "agent", "agent",
          "top_epic", _ts(days=2), "{}"),
         ("lone", "vivy", "Parentless", "task", "open", "agent", "agent",
          None, _ts(days=1), "{}"),
         ("op_task", "vivy", "Operator work", "task", "open", "operator",
          "agent", None, _ts(days=1), "{}"),
         ("done_task", "vivy", "Already resolved", "task", "fixed", "agent",
          "agent", None, _ts(days=1), "{}"),
         ("other_task", "constance", "Other product", "task", "open", "agent",
          "agent", None, _ts(days=1), "{}")])
    conn.commit()
    conn.close()

    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        AGENT: {"name": "vivy-agent", "products": ["vivy"]},
        WIDE: {"name": "wide-agent", "products": ["*"]},
        NOREF: {"name": "noref-agent", "products": ["vivy"],
                "withholds": ["reference.read"]},
    }))
    # The vec0 index, because the write routes reach it: an item or signal
    # created here is embedded after its commit, and a revision drops its
    # vectors inside the transaction that writes the new text.
    vector_index(db)
    ledger_tables(db)
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    monkeypatch.setattr(lease, "TUNABLES", tmp_path / "absent.json")
    return TestClient(libapp.app)


def _h(token=AGENT):
    return {"Authorization": f"Bearer {token}",
            protocol.HEADER: str(protocol.CURRENT)}


def _co(client, item, owner="session-1", token=AGENT, **extra):
    return client.post(f"/items/{item}/checkout",
                       json={"owner": owner, **extra}, headers=_h(token))


def _db():
    conn = sqlite3.connect(store.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _leases():
    conn = _db()
    rows = conn.execute("SELECT * FROM issue_leases").fetchall()
    conn.close()
    return rows


# --- what a checkout takes -----------------------------------------------------


def test_checking_out_a_leaf_leases_its_epic(client):
    r = _co(client, "task_a")
    assert r.status_code == 200
    assert r.json()["item"]["state"] == "claimed"
    assert r.json()["lease"]["root_id"] == "top_epic"
    assert r.json()["lease"]["taken_for"] == "task_a"


def test_checking_out_an_epic_leases_itself(client):
    """ARCH-428: an epic leases itself at any depth and does not reach upward, or
    checking out one sub-epic would stop every other branch of its parent."""
    assert _co(client, "sub_epic").json()["lease"]["root_id"] == "sub_epic"


def test_a_parentless_item_leases_only_itself(client):
    assert _co(client, "lone").json()["lease"]["root_id"] == "lone"


def test_the_response_carries_the_fence_and_the_policy(client):
    """A refusal has to name an expiry, and a caller has to see the terms it is
    held to rather than inferring them from one."""
    body = _co(client, "sub_epic").json()
    assert set(body["lease"]["fences"]) == {"sub_epic", "deep_task"}
    assert body["policy"]["grace_seconds"] == int(
        lease.DEFAULT_GRACE.total_seconds())


def test_claim_returns_the_old_shape_and_takes_the_lease_anyway(client):
    """**The whole point of sharing one implementation.** An actor claiming the
    old way without a lease is the hole in the fence: the librarian cannot see it
    holding anything and hands a sibling to someone else."""
    r = client.post("/items/task_a/claim", json={"owner": "session-1"},
                    headers=_h())
    assert r.status_code == 200
    assert "lease" not in r.json()
    assert r.json()["state"] == "claimed"
    assert [x["root_id"] for x in _leases()] == ["top_epic"]


# --- the fence, in both directions ---------------------------------------------


def test_a_sibling_is_refused_while_the_epic_is_held(client):
    _co(client, "task_a", owner="session-1")
    r = _co(client, "task_b", owner="session-2")
    assert r.status_code == 409
    assert r.json()["lease"]["holder"] == "session-1"
    assert "session-1" in r.json()["error"] and "until" in r.json()["error"]


def test_an_ancestor_lease_blocks_a_descendant(client):
    """UPWARD: a lease on any ancestor already fences this item, without depth
    limit — `deep_task` is two levels below the held epic."""
    _co(client, "top_epic", owner="session-1")
    r = _co(client, "deep_task", owner="session-2")
    assert r.status_code == 409
    assert r.json()["lease"]["root_id"] == "top_epic"
    assert "an epic above this item is held" in r.json()["error"]


def test_a_descendant_lease_blocks_an_ancestor(client):
    """**DOWNWARD, and this is the direction an ancestor-only check misses.**
    Someone holds `sub_epic`; taking `top_epic` would fence what they are inside,
    producing two holders whose fences overlap — the exact collision the lease
    exists to prevent."""
    _co(client, "sub_epic", owner="session-1")
    r = _co(client, "top_epic", owner="session-2")
    assert r.status_code == 409
    assert r.json()["lease"]["root_id"] == "sub_epic"
    assert "something beneath this epic is already held" in r.json()["error"]


def test_the_two_directions_are_reported_differently(client):
    """They need different responses: an ancestor conflict means there is nothing
    smaller to take, a descendant conflict means another branch may succeed.

    **Asserts the two phrases, not merely that the messages differ** (reviewer,
    PR #7). The first version compared the whole strings, which differ anyway
    because each carries its own holder, root and expiry — so collapsing the
    ancestor/descendant wording to a single phrase left it passing.
    """
    _co(client, "top_epic", owner="session-1")
    up = _co(client, "deep_task", owner="session-2").json()["error"]
    conn = _db()
    # Clear the CLAIMS as well as the leases. Releasing only the leases left
    # `top_epic` claimed from the first half, so the second checkout answered
    # "already claimed" and never reached the fence at all.
    conn.execute("UPDATE issue_leases SET released_at = ?", (_ts(minutes=1),))
    conn.execute("UPDATE issue_items SET state='open', claimed_by=NULL, "
                 "claimed_at=NULL")
    conn.commit()
    conn.close()
    _co(client, "sub_epic", owner="session-3")
    down = _co(client, "top_epic", owner="session-4").json()["error"]
    assert "an epic above this item is held" in up
    assert "something beneath this epic is already held" in down


def test_a_refused_checkout_leaves_the_item_untouched(client):
    """The lease is taken BEFORE the claim is written. The other way round, a
    caller blocked by a fence would still have flipped the item to claimed on its
    way to being told no."""
    _co(client, "task_a", owner="session-1")
    _co(client, "task_b", owner="session-2")
    conn = _db()
    row = conn.execute("SELECT state, claimed_by FROM issue_items WHERE id='task_b'"
                       ).fetchone()
    conn.close()
    assert row["state"] == "open" and row["claimed_by"] is None


def test_the_refusal_is_recorded_because_nothing_else_records_it(client):
    """A refusal moves no state, creates no lease and appends no event, so one not
    written here is gone — no later query recovers it."""
    _co(client, "task_a", owner="session-1")
    _co(client, "task_b", owner="session-2")
    conn = _db()
    ref = conn.execute("SELECT * FROM lease_refusals").fetchall()
    conn.close()
    assert len(ref) == 1
    assert ref[0]["requester"] == "session-2", "the refusal named the wrong party"
    assert ref[0]["blocking_holder"] == "session-1"
    assert ref[0]["blocks_by"] == "ancestor"


def test_a_fence_outside_the_callers_scope_is_reported_without_disclosing_it(
        client):
    """**Raised twice before it was fixed** (security-reviewer, PR #7). The
    caller is scoped to the item it named; the lease blocking it sits on a
    different row, and nothing checked that row's product. A cross-product parent
    would hand a caller in one product a real item id and an actor name in
    another.

    Unreachable over the API today — no route creates a cross-product parent —
    which is exactly the argument that ages badly, so the corruption is planted
    directly. The re-parent surface is already reserved in the contract.
    """
    conn = _db()
    conn.execute("UPDATE issue_items SET product='constance' WHERE id='top_epic'")
    conn.commit()
    conn.close()
    # A wildcard caller takes the fence on the now-foreign epic.
    _co(client, "top_epic", owner="session-1", token=WIDE)
    r = _co(client, "task_a", owner="session-2")
    assert r.status_code == 409
    assert "outside this identity's scope" in r.json()["error"]
    assert "session-1" not in r.text and "top_epic" not in r.text
    assert "lease" not in r.json()


def test_an_in_scope_fence_still_names_its_holder_and_expiry(client):
    """The narrowing must not blunt the ordinary refusal, which has to carry a
    holder and an expiry or an orchestrator can only block or retry."""
    _co(client, "task_a", owner="session-1")
    body = _co(client, "task_b", owner="session-2").json()
    assert body["lease"]["holder"] == "session-1"
    assert "until" in body["error"]


def test_an_expired_lease_is_not_a_conflict(client):
    """Its holder is provably gone, so any authorized caller may reclaim it."""
    _co(client, "task_a", owner="session-1")
    conn = _db()
    conn.execute("UPDATE issue_leases SET taken_at = ?, heartbeat_at = ?",
                 (_ts(hours=9), _ts(hours=9)))
    conn.commit()
    conn.close()
    r = _co(client, "task_b", owner="session-2")
    assert r.status_code == 200
    reasons = {x["release_reason"] for x in _leases()}
    assert "reclaimed" in reasons, "a dead lease was not recorded as reclaimed"


# --- take's four outcomes ------------------------------------------------------


def test_the_same_holder_re_checking_out_refreshes_rather_than_reclaiming(client):
    """A session re-checking out what it holds is the normal case, not a seizure —
    and reclaiming from yourself would count against the reclaim rate for
    nothing."""
    _co(client, "task_a", owner="session-1")
    first = _leases()[0]["id"]
    r = _co(client, "task_b", owner="session-1")
    assert r.status_code == 200
    rows = _leases()
    assert len(rows) == 1 and rows[0]["id"] == first
    assert rows[0]["taken_for"] == "task_b", "the refresh did not move taken_for"
    assert rows[0]["release_reason"] is None


def test_a_holder_re_checking_out_is_not_refused_by_its_own_fence(client):
    """Its own lease would register as a conflict in the walk, and refuse the
    holder its own subtree."""
    _co(client, "sub_epic", owner="session-1")
    assert _co(client, "deep_task", owner="session-1").status_code == 200


# --- a lease never refuses its own holder ---------------------------------------
#
# The operator, 2026-09-11: "The idea is that *only* the person/agent with the checkout can
# make the change. Not that *nobody* can make a change." `take`'s conflict walk
# did not compare holders, so a holder of `top_epic` was refused `sub_epic`.


def _live_holders():
    conn = _db()
    rows = conn.execute("SELECT root_id, holder FROM issue_leases "
                        "WHERE released_at IS NULL ORDER BY root_id").fetchall()
    conn.close()
    return [(r["root_id"], r["holder"]) for r in rows]


def test_a_holder_may_check_out_a_sub_epic_under_an_epic_it_holds(client):
    assert _co(client, "top_epic", owner="session-1").status_code == 200
    r = _co(client, "sub_epic", owner="session-1")
    assert r.status_code == 200, r.json()
    assert _live_holders() == [("sub_epic", "session-1"),
                               ("top_epic", "session-1")]


def test_a_holder_may_check_out_an_epic_above_a_sub_epic_it_holds(client):
    assert _co(client, "sub_epic", owner="session-1").status_code == 200
    r = _co(client, "top_epic", owner="session-1")
    assert r.status_code == 200, r.json()


def test_another_holder_is_still_refused_inside_that_epic(client):
    _co(client, "top_epic", owner="session-1")
    r = _co(client, "sub_epic", owner="session-2")
    assert r.status_code == 409
    assert "session-1" in r.json()["error"]


def test_skipping_its_own_lease_does_not_hide_another_holders(client):
    """`top_epic`'s subtree holds the caller's lease on one sub-epic and another
    holder's on a second. The first is skipped; the second still refuses."""
    conn = _db()
    conn.execute(
        "INSERT INTO issue_items (id, product, title, type, state, needs, "
        "created_by_kind, parent_id, created_at, refs) VALUES "
        "('sub_epic_2','vivy','Second sub epic','epic','open','agent','agent',"
        "'top_epic',?,'{}')", (_ts(days=3),))
    conn.commit()
    conn.close()
    assert _co(client, "sub_epic", owner="session-1").status_code == 200
    assert _co(client, "sub_epic_2", owner="session-2").status_code == 200
    r = _co(client, "top_epic", owner="session-1")
    assert r.status_code == 409
    assert "session-2" in r.json()["error"]


def test_reclaiming_a_dead_lease_of_your_own_is_a_refresh_not_a_reclaim(client):
    _co(client, "task_a", owner="session-1")
    conn = _db()
    conn.execute("UPDATE issue_leases SET taken_at = ?, heartbeat_at = ?",
                 (_ts(hours=9), _ts(hours=9)))
    conn.commit()
    conn.close()
    assert _co(client, "task_a", owner="session-1").status_code == 200
    rows = _leases()
    assert len(rows) == 1 and rows[0]["release_reason"] is None, (
        "a holder reclaimed from itself")


def test_force_breaks_every_conflict_and_not_just_the_first(client):
    """**Exercised at the domain layer because the route cannot reach it.**
    `force` is human-only and this service issues no human identity, so the
    override loop is unreachable over HTTP — and a mutation that broke only the
    FIRST conflict passed the whole route-level suite. That loop is real code
    that matters the moment a human identity exists, and its failure mode is the
    one this module exists to prevent: break one fence of two, insert anyway, and
    the overlap is left behind looking like it worked.

    Two sub-epics beneath one parent, both held, is one call meeting two fences.
    """
    _co(client, "sub_epic", owner="holder-1")
    conn = _db()
    # A second fence beneath the same parent, taken directly since the route
    # would now refuse it.
    conn.execute(
        "INSERT INTO issue_items (id, product, title, type, state, needs, "
        "created_by_kind, parent_id, created_at, refs) "
        "VALUES ('sub_two','vivy','Second sub','epic','open','agent','agent',"
        "'top_epic',?, '{}')", (_ts(days=2),))
    conn.commit()
    two = conn.execute("SELECT * FROM issue_items WHERE id='sub_two'").fetchone()
    lease.take(conn, two, holder="holder-2", actor="a")
    conn.commit()
    live = conn.execute(
        "SELECT COUNT(*) FROM issue_leases WHERE released_at IS NULL").fetchone()[0]
    assert live == 2, "the fixture needs two live fences to be meaningful"

    top = conn.execute("SELECT * FROM issue_items WHERE id='top_epic'").fetchone()
    lease.take(conn, top, holder="breaker", actor="human", force=True)
    conn.commit()
    rows = conn.execute("SELECT root_id, holder, release_reason FROM issue_leases "
                        "WHERE released_at IS NULL").fetchall()
    broken = conn.execute("SELECT COUNT(*) FROM issue_leases WHERE "
                          "release_reason = 'overridden'").fetchone()[0]
    conn.close()
    assert broken == 2, "only one of the two fences was broken"
    assert [(r["root_id"], r["holder"]) for r in rows] == [("top_epic", "breaker")]


def test_a_live_lease_cannot_be_broken_because_force_is_refused(client):
    """`force` is the override path and it is human-only; this service issues no
    human identity, so it is refused rather than ignored."""
    _co(client, "task_a", owner="session-1")
    r = _co(client, "task_b", owner="session-2", force=True)
    assert r.status_code == 403
    assert "human" in r.json()["error"]
    assert all(x["release_reason"] is None for x in _leases())


# --- the claimability gates ----------------------------------------------------


def test_operator_work_is_not_agent_claimable(client):
    """Enforced here rather than only in a listing filter: a caller with the id
    from anywhere else reaches this endpoint directly and the filter never runs."""
    r = _co(client, "op_task")
    assert r.status_code == 409
    assert "no agent can finish" in r.json()["error"]


def test_a_terminal_item_is_never_claimable(client):
    r = _co(client, "done_task")
    assert r.status_code == 409 and "already fixed" in r.json()["error"]


def test_an_out_of_scope_item_is_404(client):
    assert _co(client, "other_task").status_code == 404


def test_an_unclaimable_item_takes_no_lease(client):
    """Every gate above must refuse before the fence is touched, or a refused
    checkout would leave a lease behind."""
    for item in ("op_task", "done_task"):
        _co(client, item)
    assert _leases() == []


# --- claim takeover ------------------------------------------------------------


def test_an_expired_claim_can_be_taken_over_and_says_so(client):
    _co(client, "task_a", owner="session-1")
    conn = _db()
    conn.execute("UPDATE issue_leases SET taken_at = ?, heartbeat_at = ?",
                 (_ts(hours=9), _ts(hours=9)))
    conn.execute("UPDATE issue_items SET claimed_at = ? WHERE id='task_a'",
                 (_ts(hours=30),))
    conn.commit()
    conn.close()
    assert _co(client, "task_a", owner="session-2").status_code == 200
    conn = _db()
    note = conn.execute(
        "SELECT note FROM issue_events WHERE item_id='task_a' "
        "ORDER BY id DESC LIMIT 1").fetchone()["note"]
    conn.close()
    assert "taken over from session-1" in note


def test_a_live_claim_is_not_takeable(client):
    _co(client, "task_a", owner="session-1")
    r = _co(client, "task_a", owner="session-2")
    assert r.status_code == 409


def test_the_lease_governs_expiry_where_one_exists(client):
    """**ONE ACT MUST NOT HAVE TWO EXPIRIES.** A claim outliving its lease hands
    the item to a drain the fence then refuses; a lease outliving its claim opens
    the siblings while the item still reads claimed."""
    _co(client, "task_a", owner="session-1")
    conn = _db()
    # A claim old enough to be expired under the 24h TTL, with a live lease.
    conn.execute("UPDATE issue_items SET claimed_at = ? WHERE id='task_a'",
                 (_ts(hours=30),))
    conn.commit()
    item = conn.execute("SELECT * FROM issue_items WHERE id='task_a'").fetchone()
    expired, _ = lease.claim_status(conn, item)
    conn.close()
    assert not expired, "the TTL overrode a live lease"


# --- the subtree cap -----------------------------------------------------------


def test_a_subtree_too_large_to_enumerate_refuses_the_checkout(client,
                                                               monkeypatch):
    """**A fence whose reach is unknown must not be taken.** Proceeding would
    leave the uncounted items claimable while their epic is held — failing open,
    silently."""
    monkeypatch.setattr(lease, "MAX_SUBTREE_NODES", 1)
    r = _co(client, "top_epic")
    assert r.status_code == 409
    assert "cannot enumerate this subtree" in r.json()["error"]
    assert _leases() == [], "a lease was taken over an unmeasured fence"


def test_the_walk_terminates_on_a_cycle(client):
    """A cycle makes every ancestor walk non-terminating, and under the downward
    fence it would hang the checkout path itself."""
    conn = _db()
    conn.execute("UPDATE issue_items SET parent_id='deep_task' WHERE id='sub_epic'")
    conn.commit()
    assert "sub_epic" in lease.ancestor_ids(conn, "deep_task")
    # ASSERTS THE RESULT, not merely that the call returned (reviewer, PR #7).
    # `subtree_ids` never returns None — it returns a list or raises — so the
    # first version's `is not None` passed for any successful call whatever the
    # walk produced. The `seen` set is what terminates the cycle: the children of
    # `deep_task` include `sub_epic`, which is already seen, so the walk stops.
    assert set(lease.subtree_ids(conn, "sub_epic")) == {"deep_task"}
    conn.close()


def test_writes_are_refused_while_the_shadow_is_running(client, monkeypatch):
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    assert _co(client, "task_a").status_code == 503


# --- the call slip's grant (issue_01M12NE120JVQT4DV85N8MB7TP) ------------------
#
# Nothing behind this grant is real yet — no bundler, no persona registry, no
# model resolution, no ledger-backed run cap. What these tests pin is the
# SHAPE: the keys a client can already build against, their types, and the two
# things the grant must NEVER carry — a bearer-shaped credential, and the
# project's ledger total. A client-side reader can use this section as the
# contract.


def test_the_grant_arrives_alongside_the_existing_response(client):
    """Additive within protocol version 1 (contract §1): `item`, `lease`,
    `policy` and `adjacency` are unchanged, and the grant is new keys beside
    them, not a replacement for any of them."""
    body = _co(client, "task_a").json()
    assert body.keys() >= {"item", "lease", "policy", "adjacency", "slip_id",
                           "persona_md", "docs", "model", "budget", "limits"}


def test_the_slip_id_names_a_real_persisted_row(client):
    """Unlike the rest of the grant, `slip_id` is not a placeholder — checkout
    mints and persists a row in `call_slips` before serving it, which is what
    makes it a real credential a later `POST /tokens/redeem` can look up
    (issue_01M12NE13P3FEY3HF7732QX03F, a separate task)."""
    body = _co(client, "task_a").json()
    slip_id = body["slip_id"]
    assert isinstance(slip_id, str) and slip_id.startswith("slip_")
    conn = _db()
    row = conn.execute("SELECT * FROM call_slips WHERE id = ?",
                       (slip_id,)).fetchone()
    conn.close()
    assert row is not None
    assert row["item_id"] == "task_a"
    assert row["product"] == "vivy"
    assert row["claim_owner_label"] == "session-1"


def test_a_failed_mint_leaves_no_claim_and_no_lease_behind(client, monkeypatch):
    """The mint runs inside the same transaction as the claim and the lease
    (`_do_checkout`), so a failure there must roll back both, not just skip
    the slip. A checkout whose slip failed to insert but whose claim and
    lease survived would strand the caller holding a fence it cannot use and
    cannot retry — the item would read as claimed by itself with nothing to
    show for it. This forces the failure at the `call_slips.mint` boundary
    and pins both halves: the store rolled back (item still open, no lease
    row) AND the caller got a named, retry-safe 503 rather than an unhandled
    500 (reviewer, PR #86) — the same shape every other write-path refusal on
    this route already uses (`store.WritesDisabled`, next to this one in
    `_checkout`)."""
    def _boom(conn, **kw):
        raise RuntimeError("mint boundary forced failure")
    monkeypatch.setattr(call_slips, "mint", _boom)

    resp = _co(client, "task_a", owner="session-1")
    assert resp.status_code == 503
    body = resp.json()
    assert "mint" in body["error"].lower()
    assert "retry" in body["hint"].lower()

    conn = _db()
    item = conn.execute("SELECT state, claimed_by FROM issue_items WHERE id='task_a'"
                        ).fetchone()
    leases = conn.execute("SELECT * FROM issue_leases WHERE taken_for = 'task_a'"
                          ).fetchall()
    conn.close()
    assert item["state"] == "open" and item["claimed_by"] is None
    assert leases == []


def test_the_slip_expires_on_its_own_ttl_independent_of_the_lock(client):
    """The task's own done-condition: let a slip lapse while the lock is
    still live. Both halves in one test — a slip that expired alongside a
    dead lease would demonstrate nothing about the two clocks being
    independent."""
    body = _co(client, "task_a", owner="session-1").json()
    conn = _db()
    slip = call_slips.get(conn, body["slip_id"])
    held = conn.execute("SELECT * FROM issue_leases WHERE root_id = ?",
                        ("top_epic",)).fetchone()
    conn.close()

    just_minted = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    long_after_slip_expiry = just_minted + dt.timedelta(
        seconds=call_slips.TTL_SECONDS + 60)

    assert call_slips.is_redeemable(slip, now=just_minted)
    assert not call_slips.is_redeemable(slip, now=long_after_slip_expiry)
    # THE LOCK IS STILL LIVE at the same instant the slip is not — the lease's
    # own grace (`lease.DEFAULT_GRACE`, 4 hours) dwarfs the slip's TTL (5
    # minutes), so nothing about letting the slip lapse touches the lock.
    assert not lease.is_dead(held, now=long_after_slip_expiry)


def test_a_heartbeat_does_not_extend_the_slips_life(client):
    """The converse of the above, cheap to assert: a slip gains no life from a
    heartbeat that extends the lease. `lease.touch` moves `heartbeat_at`
    forward and that is the whole mechanism by which a lease's deadline
    moves; a slip has no such write path at all, so this pins that nothing
    reaches in and moves `expires_at` on the slip's behalf."""
    body = _co(client, "task_a", owner="session-1").json()
    conn = _db()
    before = call_slips.get(conn, body["slip_id"])
    item = conn.execute("SELECT * FROM issue_items WHERE id = ?",
                        ("task_a",)).fetchone()
    lease.touch(conn, item, holder="session-1")
    conn.commit()
    after = call_slips.get(conn, body["slip_id"])
    conn.close()
    assert before["expires_at"] == after["expires_at"]


def test_persona_md_is_content_not_an_id(client):
    """`persona_md` is ADR-0062's deliberate exception to `docs[]`'s
    by-id-only rule — it arrives as text a caller can read directly."""
    body = _co(client, "task_a").json()
    assert isinstance(body["persona_md"], str) and body["persona_md"]


def test_an_absent_persona_means_what_checkout_already_meant(client):
    """Checkout took no persona argument before this field existed. Additive
    means an absent one changes nothing about whether the checkout succeeds —
    only `persona_md`'s content reflects that none was named."""
    r = _co(client, "task_a")
    assert r.status_code == 200
    assert "default" in r.json()["persona_md"]


def test_a_named_persona_is_echoed_into_persona_md(client):
    body = _co(client, "task_a", persona="coder").json()
    assert "coder" in body["persona_md"]


def test_persona_must_be_a_non_empty_string(client):
    for bad in ("", "   ", 5, [], {}):
        r = _co(client, "task_a", owner=f"o-{bad!r}", persona=bad)
        assert r.status_code == 400
        assert "persona" in r.json()["error"]


def test_docs_is_an_empty_list_of_ids_until_a_bundler_exists(client):
    """The context-economy property is why the indirection exists at all
    (epic issue_01KZBWEYHJ1QCKBR5BTM6G6DQ9): a caller's context must stay flat
    regardless of bundle size. No bundler exists yet, so the only answer that
    keeps that promise is the empty list — inventing document content here
    would defeat the property this field exists to protect.

    **This is a forward guard, not exercised coverage today**: `docs` is
    empty, so `all(isinstance(d, str) for d in docs)` is vacuously true and
    checks nothing about "by id, never content" yet. It becomes load-bearing
    the day a bundler starts populating this list."""
    docs = _co(client, "task_a").json()["docs"]
    assert isinstance(docs, list)
    assert all(isinstance(d, str) for d in docs)


def test_model_is_a_labelled_placeholder_not_a_real_resolution(client):
    """`(persona, work item, project)` resolve to a real `model`
    (issue_01M12NE12WF9V0721WSHH34A2W) only when one of them names something
    to resolve against. `task_a` carries no `model_choice` and this checkout
    names no `persona`, so there is nothing to resolve and the labelled
    placeholder is what checkout still serves — the value every client that
    predates resolution already saw."""
    model = _co(client, "task_a").json()["model"]
    assert isinstance(model, str) and model
    assert "not-implemented" in model or "unresolved" in model, (
        "the placeholder must be legible as unresolved, not a real model id "
        "a caller could mistake for one")


# --- resolving `model` (issue_01M12NE12WF9V0721WSHH34A2W) ---------------------


def _agent_index(tmp_path, monkeypatch, docs, body="the agent's body"):
    """A knowledge index holding `docs`, each `(id, project, extra)`, shaped
    like the real one (`frontmatter_extra` included, agent-platform#517), and
    pointed at by `reference.INDEX_DB` for the rest of the test. `extra` is a
    dict serialized the way the indexer writes it, or `None` for a row with no
    usable frontmatter at all."""
    index = tmp_path / "agent-index.db"
    conn = sqlite3.connect(index)
    conn.executescript("""
        CREATE TABLE docs (
            id TEXT PRIMARY KEY, source_ref TEXT, kind TEXT, project TEXT,
            title TEXT, status TEXT, topics TEXT, audience TEXT,
            last_reviewed TEXT, updated_at TEXT, body TEXT, body_hash TEXT,
            frontmatter_extra TEXT);
    """)
    for doc_id, project, extra in docs:
        conn.execute(
            "INSERT INTO docs (id, source_ref, kind, project, title, status, "
            "topics, audience, last_reviewed, updated_at, body, body_hash, "
            "frontmatter_extra) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (doc_id, f"{doc_id}.md", "agent", project, doc_id, "current",
             "[]", "[]", "2026-08-01", "2026-08-02", body,
             "deadbeef", None if extra is None else json.dumps(extra)))
    conn.commit()
    conn.close()
    monkeypatch.setattr(reference, "INDEX_DB", index)


def test_the_items_model_choice_wins_over_the_personas_pin(
        client, tmp_path, monkeypatch):
    """§2 of the task: the work item's own choice beats what the named
    persona's agent definition pins, exactly the priority
    platform/contracts/work-item-model-override gives the Bosun's own
    `model_choice` read."""
    _agent_index(tmp_path, monkeypatch,
                [("agent-platform/agents/coder", "agent-platform",
                  {"model": "sonnet"})])
    conn = _db()
    conn.execute("UPDATE issue_items SET model_choice = 'opus' "
                "WHERE id = 'task_a'")
    conn.commit()
    conn.close()
    assert _co(client, "task_a", persona="coder").json()["model"] == "opus"


def test_the_personas_pin_is_used_when_no_item_choice_is_set(
        client, tmp_path, monkeypatch):
    _agent_index(tmp_path, monkeypatch,
                [("agent-platform/agents/coder", "agent-platform",
                  {"model": "sonnet"})])
    assert _co(client, "task_a", persona="coder").json()["model"] == "sonnet"


def test_an_ambiguous_persona_stays_on_the_placeholder(
        client, tmp_path, monkeypatch):
    """Two projects hold `reviewer` and neither is `agent-platform`
    (ARCH-426's tiebreak does not apply), so the name is ambiguous — refusing
    the whole checkout over an unresolved model would be worse than the
    placeholder a client already knows to treat as unresolved."""
    _agent_index(tmp_path, monkeypatch,
                [("svc-coder/agents/reviewer", "svc-coder", {"model": "opus"}),
                 ("svc-planner/agents/reviewer", "svc-planner",
                  {"model": "sonnet"})])
    model = _co(client, "task_a", persona="reviewer").json()["model"]
    assert model == libapp._MODEL_NOT_RESOLVED


def test_an_ambiguity_the_agent_platform_tiebreak_cannot_resolve_stays_on_the_placeholder(
        client, tmp_path, monkeypatch):
    """Two *agent-platform* docs hold `reviewer` (alongside a third, outside
    `agent-platform`, that the tiebreak already discards) — the ambiguity
    survives the tiebreak rather than being resolved by it, so this still
    has to stay on the placeholder.

    **This is the case that separates `len(matches) != 1: return None` from
    "use `matches[0]` when non-empty"**: every other ambiguity test here
    filters down to zero or one match, where both checks agree. Here the
    filtered set still has two, so a mutant that stopped checking the count
    and just took `matches[0]` would silently pick one of them — this is the
    only input in this file where that mutant answers differently from the
    real check."""
    _agent_index(tmp_path, monkeypatch,
                [("svc-coder/agents/reviewer", "svc-coder", {"model": "opus"}),
                 ("agent-platform/agents/reviewer", "agent-platform",
                  {"model": "sonnet"}),
                 ("agent-platform/legacy/agents/reviewer", "agent-platform",
                  {"model": "haiku"})])
    model = _co(client, "task_a", persona="reviewer").json()["model"]
    assert model == libapp._MODEL_NOT_RESOLVED


def test_an_ambiguous_persona_resolves_to_the_agent_platform_copy(
        client, tmp_path, monkeypatch):
    """The success side of the same tiebreak `test_an_ambiguous_persona_
    stays_on_the_placeholder` proves the failure side of: three projects
    hold `reviewer`, exactly one of them `agent-platform`, and ARCH-426's
    tiebreak — mirrored from `agentctl`'s `_library_agent` — resolves to that
    one's pin rather than leaving the name ambiguous."""
    _agent_index(tmp_path, monkeypatch,
                [("svc-coder/agents/reviewer", "svc-coder", {"model": "opus"}),
                 ("agent-platform/agents/reviewer", "agent-platform",
                  {"model": "sonnet"}),
                 ("svc-planner/agents/reviewer", "svc-planner",
                  {"model": "opus"})])
    model = _co(client, "task_a", persona="reviewer").json()["model"]
    assert model == "sonnet"


def test_an_unknown_persona_stays_on_the_placeholder(
        client, tmp_path, monkeypatch):
    """A readable index that holds no `agent` document named `no-such-agent`
    at all, so this covers the zero-match branch of `len(matches) != 1`
    rather than `reference.ReferenceIndexUnavailable`, which
    `test_an_unreadable_index_stays_on_the_placeholder` already covers.
    Replacing the check with "use `matches[0]` when non-empty" cannot pass
    this: there is no `matches[0]` to use."""
    _agent_index(tmp_path, monkeypatch,
                [("agent-platform/agents/coder", "agent-platform",
                  {"model": "sonnet"})])
    model = _co(client, "task_a", persona="no-such-agent").json()["model"]
    assert model == libapp._MODEL_NOT_RESOLVED


def test_an_unreadable_index_stays_on_the_placeholder(
        client, tmp_path, monkeypatch):
    """**A FAILURE HERE MUST NOT COST THE CALLER ITS CHECKOUT**, the same
    contract `_slip_docs` holds for a profile bundle: a corrupt index falls
    back to the placeholder rather than failing the whole checkout."""
    corrupt = tmp_path / "corrupt-index.db"
    corrupt.write_bytes(b"not a sqlite database")
    monkeypatch.setattr(reference, "INDEX_DB", corrupt)
    r = _co(client, "task_a", persona="coder")
    assert r.status_code == 200
    assert r.json()["model"] == libapp._MODEL_NOT_RESOLVED


def test_two_checkouts_with_the_same_persona_get_the_same_model(
        client, tmp_path, monkeypatch):
    """Two different items, the same persona, no item choice on either: the
    persona's pin resolves the same way both times rather than drifting
    between checkouts of the same kind of agent."""
    _agent_index(tmp_path, monkeypatch,
                [("agent-platform/agents/coder", "agent-platform",
                  {"model": "sonnet"})])
    first = _co(client, "task_a", owner="session-1", persona="coder").json()
    second = _co(client, "lone", owner="session-2", persona="coder").json()
    assert first["model"] == second["model"] == "sonnet"


def test_the_stored_grant_carries_the_resolved_model(
        client, tmp_path, monkeypatch):
    """Extends `test_the_slip_stores_exactly_the_grant_checkout_served` to the
    two real resolutions this task adds. `POST /tokens/redeem` returns
    exactly the grant stored on the slip (`test_token_redeem.py` pins that
    read), so a resolved model stored here is a resolved model redemption
    hands the Individuator — this is the half of that chain this file owns."""
    _agent_index(tmp_path, monkeypatch,
                [("agent-platform/agents/coder", "agent-platform",
                  {"model": "sonnet"})])
    conn = _db()
    conn.execute("UPDATE issue_items SET model_choice = 'opus' "
                "WHERE id = 'task_a'")
    conn.commit()
    conn.close()
    for item, persona, expected in (("task_a", "coder", "opus"),
                                    ("lone", "coder", "sonnet")):
        body = _co(client, item, owner=f"session-{item}", persona=persona).json()
        assert body["model"] == expected
        conn = _db()
        row = conn.execute("SELECT * FROM call_slips WHERE id = ?",
                          (body["slip_id"],)).fetchone()
        conn.close()
        stored = call_slips.grant_of(row)
        assert stored["model"] == expected


def test_a_checkout_that_races_a_concurrent_model_choice_write(
        client, tmp_path, monkeypatch):
    """Reproduces the security-reviewer's finding on PR #249: `_do_checkout`
    resolved `model` from `item["model_choice"]`, read once before
    `lease.take()` — an unlocked, autocommit snapshot — so a
    `PUT /items/{id}/model` landing in that window was silently dropped from
    the minted slip. The fix re-reads `model_choice` off `locked`, the row
    `lease.take()` itself holds the lock on, the same way `cap_usd` is
    already refreshed there.

    **Deterministic, the same way `test_a_checkout_that_races_a_concurrent_
    resolve_is_rolled_back` is**: `lease.take` is patched to pause exactly
    where the real race window opens — after the unlocked `item` read and
    the unlocked grant build, before the write lock is taken — while the
    model write runs to completion and commits; only then is checkout
    allowed to proceed into the lock.
    """
    import threading

    clients = tmp_path / "client-tokens.json"
    clients.write_text(json.dumps({
        "approved-client-token": {"name": "deck", "products": ["*"]}}))
    monkeypatch.setattr(identity, "CLIENT_TOKENS", clients)

    checkout_ready = threading.Event()
    write_done = threading.Event()
    real_take = lease.take

    def take_racer(*args, **kwargs):
        checkout_ready.set()
        assert write_done.wait(timeout=20), "model write never completed"
        return real_take(*args, **kwargs)

    monkeypatch.setattr(lease, "take", take_racer)

    results = {}

    def do_checkout():
        results["checkout"] = _co(client, "lone")

    def do_write():
        assert checkout_ready.wait(timeout=20), "checkout never reached the seam"
        results["write"] = client.post(
            "/items/lone/model",
            json={"model": "opus", "acting_human": "operator@example.com"},
            headers=_h("approved-client-token"))
        write_done.set()

    threads = [threading.Thread(target=do_checkout),
               threading.Thread(target=do_write)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert set(results) == {"checkout", "write"}, (
        "a request died before reaching the race seam")
    assert results["write"].status_code == 200, results["write"].json()
    assert results["checkout"].status_code == 200, results["checkout"].json()
    assert results["checkout"].json()["model"] == "opus"


def test_budget_bounds_this_run_and_carries_no_project_total(client):
    """The Bosun's epic (issue_01M1HR62S6YVMR1G6RHQ8RB5Q2) settles this: 'the
    cap on the slip bounds this run and nothing else... a slip carries a
    per-run spend cap and carries no project total.' `total_usd` is the
    ledger's own accumulating total (`store.ledger_total`, Phase 4) and must
    never appear here — that would put project standing on a credential
    issued at birth."""
    budget = _co(client, "task_a").json()["budget"]
    assert isinstance(budget, dict)
    # Allowlisted rather than a denylist of forbidden names: a denylist only
    # catches a project total that arrives under one of the names on the
    # list, and a later field (`spent_usd`, `ledger_total`) would carry the
    # same forbidden thing straight past it.
    assert set(budget) == {"cap_usd"}


def test_budget_cap_usd_is_null_for_a_project_with_no_per_run_cap(client):
    """The ordinary case: no human has ever set a ledger for `vivy`, so
    `store.live_ledger` answers None and the call slip carries no number."""
    assert _co(client, "task_a").json()["budget"]["cap_usd"] is None


def test_budget_cap_usd_carries_the_projects_per_run_cap(client):
    """Set by a human through `PUT /ledgers` (issue_01M4BDRV8C47NQN5X8T82Y2B9A),
    read from the checked-out item's own project ledger row, and served in
    dollars as every other cap field is."""
    store.upsert_ledger({
        "scope_kind": "project", "scope_id": "vivy", "granted_by": "operator",
        "warn_usd_micro": None, "soft_cap_usd_micro": None,
        "hard_cap_usd_micro": None, "per_run_cap_usd_micro": 2_500_000})
    assert _co(client, "task_a").json()["budget"]["cap_usd"] == 2.5


def test_limits_does_not_restate_what_policy_already_carries(client):
    """`policy` carries this checkout's lease terms (`grace_seconds`,
    `max_lease_seconds`) — the lock's lifecycle. `limits` is a different axis
    reserved for the call slip's own run-level constraints; serving the same
    number under both names would be the seam this task has to name rather
    than blur.

    **This is a forward guard, not exercised coverage today**: `limits` is
    reserved and empty, so `isdisjoint` holds against any `policy` whatsoever
    and checks nothing yet. It becomes load-bearing the day a run-level
    constraint is first added to `limits`."""
    body = _co(client, "task_a").json()
    assert isinstance(body["limits"], dict)
    assert set(body["limits"]).isdisjoint(body["policy"])


def test_the_grant_carries_exactly_the_slip_id_and_no_other_undecided_field(
        client):
    """A credential IS minted now — that is this task's whole point, and the
    reason this test's name and this docstring no longer say otherwise. PR
    #78 refused to serve a `slip_id` because there was no store behind it and
    no `/tokens/redeem` to redeem it at, which made a served id a fake
    credential — worse than an absent one. This PR is that store
    (`call_slips.py`), so the id is real: `_do_checkout` mints and persists a
    row before calling this builder, and `slip_id` here is that row's own
    primary key, not a placeholder. Redemption itself is still a separate
    task (issue_01M12NE13P3FEY3HF7732QX03F) and is not built here.

    This asserts against `_call_slip_grant` itself, not the response body —
    it catches a rename or addition *inside the builder*. It does not, on its
    own, catch a field spliced into `_do_checkout`'s response dict literal
    outside the builder, or a served grant value that diverges from what the
    builder returned; `test_the_checkout_response_carries_nothing_beyond_the_
    base_fields_and_the_grant` and `test_the_served_grant_matches_what_the_
    builder_returned` cover those two respectively.

    Allowlisted rather than a denylist of forbidden names: a denylist only
    catches a bearer value that arrives under one of the names on the list,
    and a later field (`grant_ref`, `ticket`) would carry the same
    bearer-shaped thing straight past it. `slip_id` is now itself an
    allowlisted exception to "no bearer-shaped value" — it is exactly that
    shape, deliberately, and the allowlist is what keeps a SECOND one
    (`grant_ref`, `ticket`) from sneaking in beside it unnoticed.

    `bundle` joined the allowlist with issue_01M21B12X6BYZ76W04JK2QWPQF and is
    not an exception to the rule above: it carries a profile NAME and a
    boolean, neither of which is bearer-shaped, and it exists so an empty
    `docs` can be told apart from a bundle that could not be read. `slip_id`
    remains the only allowlisted bearer-shaped value."""
    assert set(libapp._call_slip_grant(UNBOUND, None, "slip_placeholder")) == {
        "slip_id", "persona_md", "persona_resolved", "persona_unresolved_reason",
        "docs", "bundle", "model", "budget", "limits"}


@pytest.mark.parametrize("persona", [None, "coder"])
def test_the_checkout_response_carries_nothing_beyond_the_base_fields_and_the_grant(
        client, persona, tmp_path, monkeypatch):
    """`_do_checkout` builds its response as a literal —
    `{"item": ..., "lease": ..., "policy": ..., "adjacency": ...,
    **_call_slip_grant(persona, slip_id)}` — and this pins that literal from
    the outside: nothing can be spliced into it beyond the four base fields
    and whatever `_call_slip_grant` returns. `_call_slip_grant`'s own guard
    (`test_the_grant_carries_exactly_the_slip_id_and_no_other_undecided_field`)
    only inspects the builder's return value, so a bearer-shaped field added
    straight into `_do_checkout`'s dict literal — bypassing the builder
    entirely — would pass that guard and this file's superset check
    (`test_the_grant_arrives_alongside_the_existing_response`) untouched.
    This closes that gap by deriving the allowlist from the builder itself
    rather than hardcoding it a second time, so a legitimate rename or
    addition inside `_call_slip_grant` doesn't also require editing this
    test.

    **This assertion is deliberately strict**: it fails on *any* new
    top-level field added to the checkout response, including a legitimate
    additive one contract §1 otherwise permits. That's the point, not a
    bug to route around — a new top-level field is a checkpoint a future
    author should have to consciously pass, by extending the allowlist
    here, rather than something this guard should quietly let through
    because it was named after bearer values.

    Parametrized over `persona in (None, "coder")` for the same reason as
    `test_the_served_grant_matches_what_the_builder_returned`: a field
    spliced into `_do_checkout`'s response literal on a condition like
    `if persona: body["debug_persona"] = persona` only fires on a
    persona-carrying request, and the old single-case version (no persona)
    never sent one. **The expected side still calls
    `_call_slip_grant(None, ...)` regardless of `persona`, and that is
    deliberate, not an oversight to fix**: `_call_slip_grant`'s key set is
    the same for every persona value (only `persona_md`'s *content* varies,
    via `_persona_md`), so the allowlist this test derives from it doesn't
    need to track `persona` too. What needs to vary is the *request* — the
    actual side — since that's where a spliced-in extra key would appear.
    The `slip_id` argument passed to the expected side is a placeholder
    string, never the served one — this test checks key names only, and
    `test_the_served_grant_matches_what_the_builder_returned` is where the
    served `slip_id` value itself gets checked.

    **`claimed` passed this checkpoint on 2026-09-12** (the operator, via
    platform/decisions/0079): a checkout of a blocked item takes the lease and
    not the claim, so every checkout now says which it got. It is a boolean,
    not a bearer-shaped value."""
    monkeypatch.setattr(reference, "INDEX_DB", tmp_path / "no-such-index.db")
    extra = {} if persona is None else {"persona": persona}
    body = _co(client, "task_a", **extra).json()
    assert set(body) - {"item", "lease", "policy", "claimed", "adjacency"} == set(
        libapp._call_slip_grant(UNBOUND, None, "slip_placeholder"))


@pytest.mark.parametrize("persona", [None, "coder"])
def test_the_served_grant_matches_what_the_builder_returned(
        client, persona, tmp_path, monkeypatch):
    """Distinct from the previous two tests: this one doesn't care what keys
    are present, only whether the grant *values* actually served match what
    `_call_slip_grant` computed. A value changed or overridden after the
    builder runs — without touching the builder or adding a stray key — would
    pass both the builder-level guard and the response's key-set check, and
    only surface here.

    Parametrized over `persona in (None, "coder")` so the comparison actually
    runs the branch where a persona was named, not just the branch `_co`'s
    default takes: an edit of the shape "override a value only when a persona
    was named" inside `_do_checkout` would pass the `persona=None` case
    untouched and only fail here. The `None` case sends no `persona` key at
    all, not an explicit `persona: null` — that is the shape every caller
    uses today, and is what "additive" means for this field. The `"coder"`
    case is compared against `_call_slip_grant("coder", ...)`, not
    `_call_slip_grant(None, ...)`, so a real divergence in `persona_md` (the
    one field `_call_slip_grant` varies by persona) is what fails it, not a
    mismatched expectation.

    **`slip_id` is fed back into the expected side rather than compared
    independently**, because it is minted fresh on every checkout
    (`call_slips.mint` draws a new ULID each call) — there is no second call
    to `_call_slip_grant` that could reproduce it. Plugging the served value
    back in still exercises the real comparison for every OTHER field, and
    pins that `slip_id` round-trips unchanged from the builder's return value
    into the response; that it names a real persisted row at all is
    `test_the_slip_id_names_a_real_persisted_row`'s job, not this one's."""
    monkeypatch.setattr(reference, "INDEX_DB", tmp_path / "no-such-index.db")
    extra = {} if persona is None else {"persona": persona}
    body = _co(client, "task_a", **extra).json()
    expected = libapp._call_slip_grant(UNBOUND, persona, body["slip_id"])
    assert {k: body[k] for k in expected} == expected


@pytest.mark.parametrize("persona", [None, "coder"])
def test_the_slip_stores_exactly_the_grant_checkout_served(
        client, persona, tmp_path, monkeypatch):
    """`POST /tokens/redeem` returns the grant stored on the slip
    (issue_01M3VN8WV1K891BT866K82ZBPX), so what redemption tells the
    Individuator equals what checkout told the Bosun only if the stored copy
    is the served one. Read back from the row rather than through the redeem
    route, so this pins the store; `test_token_redeem.py` pins the read."""
    monkeypatch.setattr(reference, "INDEX_DB", tmp_path / "no-such-index.db")
    extra = {} if persona is None else {"persona": persona}
    body = _co(client, "task_a", **extra).json()
    conn = _db()
    row = conn.execute("SELECT * FROM call_slips WHERE id = ?",
                       (body["slip_id"],)).fetchone()
    conn.close()
    stored = call_slips.grant_of(row)
    assert stored is not None
    assert stored == {k: body[k] for k in stored}
    assert set(stored) == set(
        libapp._call_slip_grant(UNBOUND, persona, body["slip_id"]))


def test_claim_mints_no_slip_and_so_stores_no_grant(client):
    client.post("/items/task_a/claim", json={"owner": "session-1"}, headers=_h())
    conn = _db()
    assert conn.execute("SELECT COUNT(*) FROM call_slips").fetchone()[0] == 0
    conn.close()


def test_claim_keeps_its_old_shape_without_the_grant(client):
    """`/claim` is superseded and kept working deliberately, returning exactly
    the shape its callers already parse — the grant is `/checkout`'s alone.
    That includes `slip_id`: `/claim` never calls `call_slips.mint`, so it
    mints no credential either."""
    r = client.post("/items/task_a/claim", json={"owner": "session-1"},
                    headers=_h())
    assert r.status_code == 200
    for key in ("slip_id", "persona_md", "docs", "model", "budget", "limits",
                "lease"):
        assert key not in r.json()


def test_claim_succeeds_when_the_vectors_file_is_missing(client, monkeypatch,
                                                          tmp_path):
    """`/claim` runs through the same `_do_checkout` `/checkout` does, and pays
    the same extension load before the briefing is even reached — so it 500'd
    on a missing `LIBRARY_VECTORS_DB` file exactly as `/checkout` did, with no
    briefing in its response to show the failure was ever there (reviewer +
    security-reviewer, issue_01M35R5GA1SKRG2CMQRF189NDE round 1)."""
    import vectors
    monkeypatch.setattr(vectors, "VECTORS_DB_PATH",
                        tmp_path / "no-such-vectors.db")
    r = client.post("/items/task_a/claim", json={"owner": "session-1"},
                    headers=_h())
    assert r.status_code == 200
    assert r.json()["state"] == "claimed"


def test_claim_mints_no_call_slip_row(client):
    """The absence in the response is not enough on its own — a route that
    minted a row and then merely dropped it from the reply would still leak
    the same store growth and the same question about who may redeem it.
    This checks the store, not just the shape."""
    client.post("/items/task_a/claim", json={"owner": "session-1"}, headers=_h())
    conn = _db()
    n = conn.execute("SELECT COUNT(*) FROM call_slips").fetchone()[0]
    conn.close()
    assert n == 0


def test_claim_never_validated_persona_and_still_does_not(client):
    """`/claim` never read `persona` before this PR, so a caller that happened
    to send an invalid one had it silently ignored and succeeded. Contract §1
    permits only ADDING within protocol version 1 — validating `persona` on
    `/claim` would refuse a request the old verb used to accept, which is a
    version-2 tightening, not an addition."""
    r = client.post("/items/task_a/claim",
                    json={"owner": "session-1", "persona": 5},
                    headers=_h())
    assert r.status_code == 200


# --- a losing concurrent writer is refused, not silently overwritten -----------
#
# A checkout races a resolve on the same open item: only one may land. Before
# the fix, checkout's final UPDATE carried no predicate, so a resolve that
# committed in the window after checkout's read and before checkout's write was
# silently reopened and reclaimed regardless of which request committed first.


def test_a_checkout_that_races_a_concurrent_resolve_is_rolled_back(client,
                                                                    monkeypatch):
    """Reproduces the reviewer's finding on PR #81: checkout reads the item and
    decides claimability before taking any lock, then writes unconditionally.
    A concurrent resolve that commits in that window used to be silently
    reopened and reclaimed, with the `issue_events` row recording `from_state`
    as checkout's stale read rather than the item's real (terminal) prior
    state. The compare-and-swap added to checkout's final UPDATE closes it.

    **Deterministic rather than a coin-flip on OS scheduling.** Checkout is
    held right after its own read and claimability check — the reviewer's
    named window, and the same point `lease.take` is called from — while a
    resolve runs to completion and commits; only then is checkout allowed to
    proceed to its own write. That reproduces the exact interleaving the
    reviewer traced rather than hoping two threads happen to land in it.
    """
    import threading

    checkout_ready = threading.Event()
    resolve_done = threading.Event()
    real_take = lease.take

    def take_racer(*args, **kwargs):
        checkout_ready.set()
        assert resolve_done.wait(timeout=20), "resolve never completed"
        return real_take(*args, **kwargs)

    monkeypatch.setattr(lease, "take", take_racer)

    results = {}

    def do_checkout():
        results["checkout"] = _co(client, "lone")

    def do_resolve():
        assert checkout_ready.wait(timeout=20), "checkout never reached the seam"
        results["resolve"] = client.post(
            "/items/lone/resolve", json={"resolution": "fixed"}, headers=_h())
        resolve_done.set()

    threads = [threading.Thread(target=do_checkout),
               threading.Thread(target=do_resolve)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert set(results) == {"checkout", "resolve"}, (
        "a request died before reaching the race seam")
    assert results["resolve"].status_code == 200, results["resolve"].json()
    assert results["checkout"].status_code == 409, results["checkout"].json()
    assert results["checkout"].json()["error"] == (
        "the item is no longer open; it is now fixed")

    conn = _db()
    row = conn.execute("SELECT state, claimed_by FROM issue_items "
                       "WHERE id='lone'").fetchone()
    events = conn.execute("SELECT from_state, to_state FROM issue_events "
                          "WHERE item_id='lone'").fetchall()
    # The resolve itself records one `written` lease: it held the item for its
    # own write (the operator, 2026-09-11). What must be absent is the checkout's.
    leases = conn.execute("SELECT * FROM issue_leases WHERE root_id='lone' "
                          "AND release_reason IS NOT ?",
                          (lease.LEASE_WRITTEN,)).fetchall()
    conn.close()
    assert row["state"] == "fixed" and row["claimed_by"] is None
    assert [(e["from_state"], e["to_state"]) for e in events] == [
        ("open", "fixed")], "checkout's overwrite was recorded too"
    assert len(leases) == 0, "the losing checkout still left a lease behind"


def test_a_refusal_records_the_item_the_blocking_lease_was_taken_for(client):
    """A leaf leases its parent, so `blocking_root_id` is the epic for a sibling
    and for the very same item alike. `blocking_taken_for` tells them apart
    (issue_01M0RCF408K7TBE0N9VP224MGA)."""
    _co(client, "task_a", owner="session-1")
    _co(client, "task_b", owner="session-2")
    # The same item: session-1's lease on the epic, taken for task_b, refuses
    # session-3 asking for task_b itself once the claim is cleared.
    conn = _db()
    conn.execute("UPDATE issue_leases SET taken_for = 'task_b'")
    conn.commit()
    conn.close()
    _co(client, "task_b", owner="session-3")
    conn = _db()
    rows = conn.execute("SELECT requested_item_id, blocking_root_id, "
                        "blocking_taken_for FROM lease_refusals ORDER BY at, id"
                        ).fetchall()
    conn.close()
    assert [tuple(r) for r in rows] == [
        ("task_b", "top_epic", "task_a"),
        ("task_b", "top_epic", "task_b"),
    ]


# --- `persona_md` is the resolved definition (issue_01M4XEEPQ37JFM3SE1493WDHVG)


def test_the_placeholder_text_is_unchanged_byte_for_byte():
    """agentctl's interim roster fallback matches this exact text
    (issue_01M4XEHK9FRVRRMFGH6C80BC6T), so it is a wire contract until the
    Bosun reads `persona_resolved` instead."""
    assert libapp._persona_md("coder") == (
        "# coder\n\nPersona content is not resolved yet — no persona storage "
        "or registry exists. This is a placeholder pinning the call slip's "
        "shape ahead of it.")


def test_a_resolved_persona_serves_its_definitions_body(
        client, tmp_path, monkeypatch):
    _agent_index(tmp_path, monkeypatch,
                 [("agent-platform/agents/coder", "agent-platform",
                   {"model": "sonnet"})],
                 body="# coder\n\nYou write the change.")
    body = _co(client, "task_a", persona="coder").json()
    assert body["persona_md"] == "# coder\n\nYou write the change."
    assert body["persona_resolved"] is True
    assert body["persona_unresolved_reason"] is None
    assert body["model"] == "sonnet"


def test_the_ambiguity_tiebreak_picks_the_body_and_the_model_from_one_document(
        client, tmp_path, monkeypatch):
    """The agent-platform copy wins the tiebreak, and its body arrives beside
    its own model pin rather than another copy's."""
    index = tmp_path / "agent-index.db"
    _agent_index(tmp_path, monkeypatch,
                 [("svc-coder/agents/reviewer", "svc-coder", {"model": "opus"}),
                  ("agent-platform/agents/reviewer", "agent-platform",
                   {"model": "sonnet"})])
    conn = sqlite3.connect(index)
    conn.execute("UPDATE docs SET body = 'body of ' || project")
    conn.commit()
    conn.close()
    body = _co(client, "task_a", persona="reviewer").json()
    assert body["persona_md"] == "body of agent-platform"
    assert body["model"] == "sonnet"


@pytest.mark.parametrize("persona,docs,reason", [
    (None, [], "no_persona"),
    ("no-such-agent", [("agent-platform/agents/coder", "agent-platform", {})],
     "unresolved"),
    ("reviewer", [("svc-coder/agents/reviewer", "svc-coder", {}),
                  ("svc-planner/agents/reviewer", "svc-planner", {})],
     "unresolved"),
])
def test_an_unresolved_persona_keeps_the_placeholder_and_says_why(
        client, tmp_path, monkeypatch, persona, docs, reason):
    _agent_index(tmp_path, monkeypatch, docs)
    extra = {} if persona is None else {"persona": persona}
    body = _co(client, "task_a", **extra).json()
    assert body["persona_md"] == libapp._persona_md(persona)
    assert body["persona_resolved"] is False
    assert body["persona_unresolved_reason"] == reason


def test_an_unreadable_index_keeps_the_placeholder_and_says_so(
        client, tmp_path, monkeypatch):
    corrupt = tmp_path / "corrupt-index.db"
    corrupt.write_bytes(b"not a sqlite database")
    monkeypatch.setattr(reference, "INDEX_DB", corrupt)
    r = _co(client, "task_a", persona="coder")
    assert r.status_code == 200
    assert r.json()["persona_md"] == libapp._persona_md("coder")
    assert r.json()["persona_resolved"] is False
    assert r.json()["persona_unresolved_reason"] == "index_unreadable"


@pytest.mark.parametrize("empty", ["", "   \n", None])
def test_a_definition_with_no_body_is_not_served_as_resolved(
        client, tmp_path, monkeypatch, empty):
    _agent_index(tmp_path, monkeypatch,
                 [("agent-platform/agents/coder", "agent-platform",
                   {"model": "sonnet"})], body=empty)
    body = _co(client, "task_a", persona="coder").json()
    assert body["persona_md"] == libapp._persona_md("coder")
    assert body["persona_resolved"] is False
    assert body["persona_unresolved_reason"] == "unresolved"


def test_redemption_returns_the_resolved_definition_checkout_served(
        client, tmp_path, monkeypatch):
    """The slip stores the grant at mint, so the Individuator redeeming it
    gets the same definition the checkout response carried."""
    _agent_index(tmp_path, monkeypatch,
                 [("agent-platform/agents/coder", "agent-platform",
                   {"model": "sonnet"})],
                 body="# coder\n\nYou write the change.")
    body = _co(client, "task_a", persona="coder").json()
    conn = _db()
    stored = conn.execute("SELECT grant_json FROM call_slips WHERE id = ?",
                          (body["slip_id"],)).fetchone()
    conn.close()
    grant = json.loads(stored[0])
    assert grant["persona_md"] == "# coder\n\nYou write the change."
    assert grant["persona_resolved"] is True
    assert grant["persona_unresolved_reason"] is None


def test_a_caller_without_reference_read_gets_no_definition(
        client, tmp_path, monkeypatch):
    """`GET /reference/documents/{id}` refuses a credential that withholds
    `reference.read`, and checkout needs only `item.claim`, so the grant must
    not hand that credential the body the document route would refuse. It
    gets the answer a persona nothing carries gets."""
    _agent_index(tmp_path, monkeypatch,
                 [("agent-platform/agents/coder", "agent-platform",
                   {"model": "sonnet"})],
                 body="# coder\n\nYou write the change.")
    r = _co(client, "task_a", token=NOREF, persona="coder")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["persona_md"] == libapp._persona_md("coder")
    assert body["persona_resolved"] is False
    assert body["persona_unresolved_reason"] == "unresolved"
    assert "You write the change." not in r.text
