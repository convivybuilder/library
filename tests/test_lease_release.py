"""Handing a claim back, and saying you are still alive on a lease.

The lease semantics arrive as a data migration rather than a redesign (§4), so
these tests are mostly about the distinctions Orient made deliberately and that a
port is most likely to flatten.

**`taken_for` is not the same as the root.** A session holding an epic across
several of its items must release one without unfencing the rest.

**The claim and its lease go back together.** Returning the claim while keeping
the fence is the worst of both: the item returns to the queue, a drain picks it
up, and the checkout is refused by a lease its own previous holder abandoned.

**A heartbeat never resets `taken_at`.** That is what bounds the damage a
misnamed holder can do to the hard cap, and it is why `holder` can safely be a
name rather than a credential.

**`force` is human-only, and this service has no humans.** It is refused rather
than ignored, because silently dropping it would let a caller believe it had
broken a lease that is still standing.
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
        CREATE TABLE issue_leases (
            id TEXT PRIMARY KEY, root_id TEXT, taken_for TEXT, holder TEXT,
            run_id TEXT, taken_at TEXT, heartbeat_at TEXT, released_at TEXT,
            release_reason TEXT, released_by TEXT, note TEXT);
        CREATE TABLE lease_refusals (
            id TEXT PRIMARY KEY, at TEXT, requested_item_id TEXT, product TEXT,
            requester TEXT, actor TEXT, blocking_lease_id TEXT,
            blocking_root_id TEXT, blocking_holder TEXT, blocks_by TEXT);
        CREATE TABLE call_slips (
            id TEXT PRIMARY KEY, item_id TEXT, product TEXT, claim_owner_label TEXT,
            issued_at TEXT, expires_at TEXT, grant_json TEXT);
    """)
    # **`claimed_at` IS SET ON EVERY CLAIMED ROW, and its absence was hiding a
    # bug** (reviewer, PR #20). Both `claim_status` and `claim_statuses`
    # short-circuit a claimed row with no timestamp to `(True, None)` before
    # either reaches the lease lookup or the TTL arithmetic — so an equivalence
    # test over rows without one compares two functions that both returned early,
    # and the branches that actually compute an expiry went untested.
    conn.executemany(
        "INSERT INTO issue_items (id, product, title, type, state, claimed_by, "
        "claimed_at, parent_id, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        [("epic_v", "vivy", "A vivy epic", "epic", "open", None, None, None,
          _ts(days=3)),
         # Two siblings under one epic — the pair that makes `taken_for` mean
         # something. Without both, releasing one could never be distinguished
         # from releasing the fence.
         ("task_a", "vivy", "First task", "task", "claimed", "session-1",
          _ts(hours=2), "epic_v", _ts(days=2)),
         ("task_b", "vivy", "Second task", "task", "claimed", "session-1",
          _ts(hours=2), "epic_v", _ts(days=2)),
         ("lone_v", "vivy", "Parentless", "task", "claimed", "session-1",
          _ts(hours=3), None, _ts(days=1)),
         ("open_v", "vivy", "Not claimed", "task", "open", None, None, None,
          _ts(days=1)),
         ("task_c", "constance", "Another product", "task", "claimed",
          "session-1", _ts(days=2), None, _ts(days=1))])
    # ONE live lease, on the epic, taken FOR task_a.
    conn.executemany(
        "INSERT INTO issue_leases (id, root_id, taken_for, holder, run_id, "
        "taken_at, heartbeat_at) VALUES (?,?,?,?,?,?,?)",
        [("lease_1", "epic_v", "task_a", "session-1", "run-1",
          _ts(hours=1), _ts(minutes=5)),
         ("lease_lone", "lone_v", "lone_v", "session-1", None,
          _ts(hours=1), _ts(minutes=5))])
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
    # Pinned, so a tunables edit on this machine cannot move these tests.
    monkeypatch.setattr(lease, "TUNABLES", tmp_path / "absent.json")
    return TestClient(libapp.app)


def _h(token=AGENT):
    return {"Authorization": f"Bearer {token}",
            protocol.HEADER: str(protocol.CURRENT)}


def _leases(field="released_at"):
    conn = sqlite3.connect(store.DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = {r["id"]: r[field] for r in
            conn.execute("SELECT * FROM issue_leases").fetchall()}
    conn.close()
    return rows


# --- release -------------------------------------------------------------------


def test_releasing_the_item_the_lease_was_taken_for_gives_the_fence_back(client):
    r = client.post("/items/task_a/release", json={"owner": "session-1"},
                    headers=_h())
    assert r.status_code == 200
    assert r.json()["state"] == "open"
    assert r.json()["lease_released"]["root_id"] == "epic_v"
    assert _leases()["lease_1"] is not None


def test_releasing_a_sibling_leaves_the_fence_standing(client):
    """**`taken_for`, not merely the root.** The lease was taken for `task_a`;
    releasing `task_b` returns that claim and must NOT unfence the epic, because
    the holder is still working inside it."""
    r = client.post("/items/task_b/release", json={"owner": "session-1"},
                    headers=_h())
    assert r.status_code == 200
    assert r.json()["state"] == "open"
    assert r.json()["lease_released"] is None
    assert _leases()["lease_1"] is None, "the epic was unfenced by a sibling"


def test_a_parentless_item_releases_its_own_lease(client):
    """A leaf with no parent leases only itself, which makes the lease exactly
    equivalent to a claim for every row predating the epic model."""
    r = client.post("/items/lone_v/release", json={"owner": "session-1"},
                    headers=_h())
    assert r.json()["lease_released"]["root_id"] == "lone_v"
    assert _leases()["lease_lone"] is not None


def test_the_claim_and_the_lease_move_in_one_transaction(client, monkeypatch):
    """Returning the claim while keeping the fence is the worst of both, so a
    failure after the lease update must leave the claim too."""
    conn = sqlite3.connect(store.DB_PATH)
    conn.execute("""
        CREATE TRIGGER no_release BEFORE INSERT ON issue_events
        BEGIN SELECT RAISE(ABORT, 'planted'); END""")
    conn.commit()
    conn.close()
    try:
        client.post("/items/task_a/release", json={"owner": "session-1"},
                    headers=_h())
    except sqlite3.IntegrityError:
        pass
    except Exception:
        pass
    assert _leases()["lease_1"] is None, "the lease was released without the claim"
    conn = sqlite3.connect(store.DB_PATH)
    state = conn.execute("SELECT state FROM issue_items WHERE id = 'task_a'"
                         ).fetchone()[0]
    conn.execute("DROP TRIGGER no_release")
    conn.commit()
    conn.close()
    assert state == "claimed"


def test_releasing_someone_elses_claim_is_refused(client):
    """A session hands back what it holds, not what someone else does."""
    r = client.post("/items/task_a/release", json={"owner": "session-2"},
                    headers=_h())
    assert r.status_code == 409
    assert "session-1" in r.json()["error"]
    assert _leases()["lease_1"] is None


def test_releasing_an_unclaimed_item_is_refused(client):
    r = client.post("/items/open_v/release", json={"owner": "session-1"},
                    headers=_h())
    assert r.status_code == 409
    assert "not claimed" in r.json()["error"]


# --- a pr-open holder releases only the fence --------------------------------


def _link_pr(client, item_id, *, pr=9):
    """Move a claimed fixture item to `pr-open` the way the real route does:
    state flip plus the `claimed → pr-open` event, claim and lease untouched.
    The fixture rows were inserted directly, so driving the fixture through
    `/link-pr` is what gives the item the history a release must not corrupt."""
    # `owner` is the fixture lease's holder: a change to a held item must come
    # from its holder (the operator, 2026-09-11).
    r = client.post(f"/items/{item_id}/link-pr",
                    json={"pr": pr, "owner": "session-1"}, headers=_h())
    assert r.status_code == 200 and r.json()["state"] == "pr-open"


def test_a_pr_open_holder_can_release_and_stays_pr_open(client):
    """The deadlock release exists to break (issue_01M0R9P9...): a session that
    linked a PR holds a `pr-open` item it cannot hand back, and every escape
    verb refuses. Release must drop the FENCE without resetting the STATE — the
    PR is still open, so the work must not return to the queue for a drain to
    pick up and redo what is already in review."""
    _link_pr(client, "task_a")
    r = client.post("/items/task_a/release", json={"owner": "session-1"},
                    headers=_h())
    assert r.status_code == 200
    item = r.json()
    assert item["state"] == "pr-open"
    assert item["claimed_by"] == "session-1", "the release cleared the claim"
    assert item["lease_released"]["root_id"] == "epic_v"
    assert _leases()["lease_1"] is not None, "the fence did not drop"


def test_releasing_a_pr_open_item_unfences_the_epic_for_another_crew(client):
    """Dropping the fence is the point of letting a `pr-open` holder release:
    the subtree opens to the next crew even though the item itself stays
    `pr-open` and past the claim stage."""
    _link_pr(client, "task_a")
    blocked = client.post("/items/epic_v/checkout", json={"owner": "co-other"},
                          headers=_h())
    assert blocked.status_code == 409, "the live lease should fence the epic"
    r = client.post("/items/task_a/release", json={"owner": "session-1"},
                    headers=_h())
    assert r.status_code == 200
    taken = client.post("/items/epic_v/checkout", json={"owner": "co-other"},
                        headers=_h())
    assert taken.status_code == 200, taken.text
    assert taken.json()["item"]["state"] == "claimed"


def test_a_pr_open_release_records_no_false_state_event(client):
    """The release moves no state, so it writes no state event. Routing a
    `pr-open` release through the claimed path would both return the item to
    `open` and log a `claimed → open` that never happened, corrupting the one
    record of the real trajectory (`claimed → pr-open`)."""
    _link_pr(client, "task_a")
    r = client.post("/items/task_a/release", json={"owner": "session-1"},
                    headers=_h())
    assert r.status_code == 200
    conn = sqlite3.connect(store.DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT from_state, to_state FROM issue_events WHERE item_id = 'task_a' "
        "ORDER BY id").fetchall()
    conn.close()
    assert [(e["from_state"], e["to_state"]) for e in rows] == [
        (store.ITEM_STATE_CLAIMED, store.ITEM_STATE_PR_OPEN)]


def test_releasing_someone_elses_pr_open_item_is_refused(client):
    """A session hands back what it holds, not what someone else does — the same
    guard that protects a claimed item reaches a `pr-open` one."""
    _link_pr(client, "task_a")
    r = client.post("/items/task_a/release", json={"owner": "session-2"},
                    headers=_h())
    assert r.status_code == 409
    assert "session-1" in r.json()["error"]
    assert _leases()["lease_1"] is None, "a refused release still dropped the fence"


def test_an_out_of_scope_item_is_404(client):
    assert client.post("/items/task_c/release", json={"owner": "session-1"},
                       headers=_h()).status_code == 404


@pytest.mark.parametrize("value", [True, "true", 1, "yes"])
def test_any_truthy_force_is_refused_rather_than_downgraded(client, value):
    """A caller sending `"force": "true"` meant to force. A strict `is True`
    check quietly turned that into an ordinary release and told them nothing
    (security-reviewer, PR #6)."""
    r = client.post("/items/task_a/release",
                    json={"owner": "session-1", "force": value}, headers=_h())
    assert r.status_code == 403
    assert _leases()["lease_1"] is None


def test_naming_the_holder_still_releases_a_live_lease(client):
    """**A RECORDED GAP, NOT AN ASSERTION THAT THIS IS RIGHT**
    (`issue_01KZM1D4N2GRHZHCVKJR2SBDPB`).

    `owner` is self-declared, so a caller that reads the holder's name off the
    item can drop a live fence without `force` — the reserved act ADR-0026 puts
    behind a human gate. Orient behaves identically today; this port is no more
    permissive.

    Pinned so the behaviour is visible rather than incidental, and so whoever
    closes it in Phase 5 finds a test that describes the hole instead of
    discovering it. If this test starts failing because the gap was closed, that
    is the fix landing, and the test should be inverted rather than deleted.
    """
    # BOTH COLUMNS, from the real row (reviewer, PR #6). A first version selected
    # only `heartbeat_at` and passed it as `taken_at` too, which made the
    # substitute expiry collapse to beat-plus-grace unconditionally: the hard cap
    # is always the longer of the two, so that branch could never bind however
    # stale the real `taken_at` was. The guard would then have passed over a lease
    # already dead by the cap, quietly weakening the test it exists to protect.
    conn = sqlite3.connect(store.DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM issue_leases WHERE id='lease_1'").fetchone()
    conn.close()
    assert not lease.is_dead(row), (
        "the fixture lease must be alive for this to describe the gap")
    # A different session, naming the holder it could have read anywhere.
    r = client.post("/items/task_a/release", json={"owner": "session-1"},
                    headers=_h(WIDE))
    assert r.status_code == 200
    assert _leases()["lease_1"] is not None, "the live fence dropped"


def test_force_is_refused_because_this_service_has_no_humans(client):
    """**Refused, not ignored.** Orient gates force on `kind == "human"`, and
    every token here resolves to agent or system, so the gate can never pass.
    Silently dropping it would let a caller believe it had broken a lease that is
    still standing."""
    r = client.post("/items/task_a/release",
                    json={"owner": "session-2", "force": True}, headers=_h())
    assert r.status_code == 403
    assert "human" in r.json()["error"]
    assert _leases()["lease_1"] is None, "a refused force still released the lease"


# --- heartbeat -----------------------------------------------------------------


def test_a_heartbeat_moves_the_sighting_forward(client):
    before = _leases("heartbeat_at")["lease_1"]
    r = client.post("/items/task_a/heartbeat", json={"owner": "session-1"},
                    headers=_h())
    assert r.status_code == 200
    assert _leases("heartbeat_at")["lease_1"] > before


def test_a_heartbeat_never_moves_taken_at(client):
    """**The bound that makes `holder` safe as a name rather than a credential.**
    No number of heartbeats pushes a lease past its hard cap; only a re-checkout
    resets that, and a re-checkout by a non-holder is refused by the fence."""
    before = _leases("taken_at")["lease_1"]
    client.post("/items/task_a/heartbeat", json={"owner": "session-1"},
                headers=_h())
    assert _leases("taken_at")["lease_1"] == before


def test_a_heartbeat_from_a_sibling_touches_the_epics_lease(client):
    """The lease covers the subtree, so a holder working on `task_b` under a
    fence taken for `task_a` is still seen."""
    before = _leases("heartbeat_at")["lease_1"]
    r = client.post("/items/task_b/heartbeat", json={"owner": "session-1"},
                    headers=_h())
    assert r.status_code == 200
    assert _leases("heartbeat_at")["lease_1"] > before


def test_a_heartbeat_from_a_non_holder_is_404_rather_than_taking_a_lease(client):
    """A heartbeat that could CREATE a lease would be a checkout by another name,
    and one that skipped the fence."""
    r = client.post("/items/task_a/heartbeat", json={"owner": "session-2"},
                    headers=_h())
    assert r.status_code == 404
    assert "no live lease" in r.json()["error"]


def test_a_heartbeat_on_an_unfenced_item_is_404_and_writes_nothing(client):
    r = client.post("/items/open_v/heartbeat", json={"owner": "session-1"},
                    headers=_h())
    assert r.status_code == 404
    assert len(_leases()) == 2, "a heartbeat created a lease"


def test_a_dead_lease_cannot_be_heartbeated_back_to_life(client):
    """Past the grace, the lease is reclaimable; a heartbeat must not resurrect
    it, or the holder that went away could hold a subtree indefinitely."""
    conn = sqlite3.connect(store.DB_PATH)
    conn.execute("UPDATE issue_leases SET heartbeat_at = ?, taken_at = ? "
                 "WHERE id = 'lease_1'", (_ts(hours=9), _ts(hours=9)))
    conn.commit()
    conn.close()
    r = client.post("/items/task_a/heartbeat", json={"owner": "session-1"},
                    headers=_h())
    assert r.status_code == 404


def test_the_heartbeat_reports_the_policy_it_is_held_to(client):
    """The grace is a tunable that moves, so a client that hardcoded it would
    drift the moment it does."""
    body = client.post("/items/task_a/heartbeat", json={"owner": "session-1"},
                       headers=_h()).json()
    assert body["policy"]["grace_seconds"] == int(
        lease.DEFAULT_GRACE.total_seconds())
    assert body["lease"]["expires_at"].endswith("Z")


# --- the policy ----------------------------------------------------------------


def test_expiry_is_the_earlier_of_grace_and_the_hard_cap(tmp_path, monkeypatch):
    """**Isolated, because it was not** (reviewer, PR #6). This is the one lease
    test with no `client` fixture, so it never got the fixture's TUNABLES pin and
    read the real file on this machine. It passed only because that file's
    committed values happen to equal the coded defaults — and that file's own
    comment says the grace will move once the reclaim and override rates say
    something, at which point this test fails with no change in this repo."""
    monkeypatch.setattr(lease, "TUNABLES", tmp_path / "absent.json")
    taken = "2026-08-01 00:00:00"
    # A lease heartbeated recently is bounded by the cap, not the grace.
    assert lease.expires_at(taken, "2026-08-01 23:00:00") == \
        dt.datetime(2026, 8, 2, 0, 0)
    # One that went quiet early is bounded by the grace.
    assert lease.expires_at(taken, "2026-08-01 01:00:00") == \
        dt.datetime(2026, 8, 1, 5, 0)


def test_a_malformed_tunables_file_falls_back_rather_than_raising(tmp_path,
                                                                  monkeypatch):
    """Read on the checkout path, so a JSON typo must not take the fence down."""
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    monkeypatch.setattr(lease, "TUNABLES", bad)
    assert lease.grace() == lease.DEFAULT_GRACE


@pytest.mark.parametrize("value", [0, -60, "an hour", True, None])
def test_a_tunable_below_its_floor_or_malformed_falls_back(value, tmp_path,
                                                           monkeypatch):
    """**A zero or negative grace would expire every lease the instant it is
    taken**, which reads as "the lease is not working" rather than as a bad config
    value, and the whole subtree would be claimable while its holder worked."""
    cfg = tmp_path / "t.json"
    cfg.write_text(json.dumps({"grace_seconds": value}))
    monkeypatch.setattr(lease, "TUNABLES", cfg)
    assert lease.grace() >= lease.MIN_GRACE


def test_a_valid_tunable_is_honoured(tmp_path, monkeypatch):
    """The floor must not swallow a real setting."""
    cfg = tmp_path / "t.json"
    cfg.write_text(json.dumps({"grace_seconds": 7200}))
    monkeypatch.setattr(lease, "TUNABLES", cfg)
    assert lease.grace() == dt.timedelta(hours=2)


def test_an_epic_leases_itself_and_a_leaf_leases_its_parent():
    """ARCH-428: propagating upward would let checking out one sub-epic stop every
    other branch of a top-level epic. A leaf's parent is a lease root only when
    that parent is itself an epic (`library/specs/one-home-per-work-item`): a
    corrections-queue parent is not a row in `issue_items` at all, so it fails
    the lookup exactly as a parentless item's absent parent does, and the leaf
    leases only itself either way."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE issue_items (id TEXT PRIMARY KEY, type TEXT)")
    conn.executemany("INSERT INTO issue_items (id, type) VALUES (?, ?)",
                     [("top", "epic"), ("e", "epic")])
    assert lease.root_id_for(
        conn, {"id": "e", "type": "epic", "parent_id": "top"}) == "e"
    assert lease.root_id_for(
        conn, {"id": "t", "type": "task", "parent_id": "e"}) == "e"
    assert lease.root_id_for(
        conn, {"id": "t", "type": "task", "parent_id": None}) == "t"
    assert lease.root_id_for(
        conn, {"id": "t", "type": "task", "parent_id": "cq_v"}) == "t"


def test_writes_are_refused_while_the_shadow_is_running(client, monkeypatch):
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    assert client.post("/items/task_a/release", json={"owner": "session-1"},
                       headers=_h()).status_code == 503
    assert client.post("/items/task_a/heartbeat", json={"owner": "session-1"},
                       headers=_h()).status_code == 503


# --- the batched claim expiry ------------------------------------------------------


def _conn():
    conn = sqlite3.connect(store.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def test_claim_statuses_agrees_with_claim_status_row_by_row(client):
    """**Two functions computing one rule is the shape that drifts**, so the
    batched form is asserted against the single form rather than trusted. The
    batch exists to make a 200-row listing one query instead of one per claimed
    row, which is a performance fix that must not become a correctness one."""
    conn = _conn()
    try:
        rows = conn.execute(
            f"SELECT {store.ITEM_COLUMNS} FROM issue_items").fetchall()
        batched = lease.claim_statuses(conn, rows)
        for row in rows:
            one = lease.claim_status(conn, row)
            assert batched.get(row["id"], (False, None)) == one, row["id"]
    finally:
        conn.close()


def test_claim_statuses_asks_one_question_however_many_claimed_rows(client):
    """The N+1 this exists to prevent. Counted through sqlite3's own trace
    callback, which sees every statement the driver actually executes — a
    regression to one lookup per claimed row is invisible to every other test,
    and a comment claiming "one query" is not a test."""
    conn = _conn()
    asked = []
    try:
        rows = conn.execute(
            f"SELECT {store.ITEM_COLUMNS} FROM issue_items").fetchall()
        claimed = [r for r in rows if r["state"] == store.ITEM_STATE_CLAIMED]
        assert len(claimed) >= 2, "the fixture must hold more than one claim"
        conn.set_trace_callback(
            lambda sql: asked.append(sql) if "issue_leases" in sql else None)
        lease.claim_statuses(conn, rows)
    finally:
        conn.set_trace_callback(None)
        conn.close()
    assert len(asked) == 1, asked


def test_the_equivalence_actually_reaches_the_expiry_arithmetic(client):
    """**The guard on the guard.** The equivalence test above compares two
    functions, and both short-circuit a claimed row with no `claimed_at` before
    reaching the lease lookup or the TTL fallback — so on the old fixture it
    compared two early returns and proved nothing about the arithmetic. This
    asserts the fixture actually exercises both branches, so a future edit that
    drops `claimed_at` turns the equivalence test back into a tautology loudly
    rather than silently."""
    conn = _conn()
    try:
        rows = conn.execute(
            f"SELECT {store.ITEM_COLUMNS} FROM issue_items").fetchall()
        got = lease.claim_statuses(conn, rows)
    finally:
        conn.close()
    # task_a holds the lease taken FOR it, so its expiry comes from the LEASE.
    assert got["task_a"][1] is not None
    # task_b is a sibling under the same epic; the lease is not taken for it, so
    # it falls back to claimed_at + CLAIM_TTL — a different branch, real value.
    assert got["task_b"][1] is not None
    # lone_v has no lease at all and takes the same fallback.
    assert got["lone_v"][1] is not None


def test_a_checkout_response_carries_the_expiry_it_just_created(client):
    """**End to end, on the response that most needs it.** A caller checking an
    item out is told when its own hold ends; reporting null there is the shape
    the batched default let through at four write endpoints (reviewer, PR #20).
    No test anywhere asserted a correct non-default expiry before this one, which
    is exactly why the bug shipped."""
    r = client.post("/items/open_v/checkout", json={"owner": "co-new"},
                    headers=_h())
    assert r.status_code == 200, r.text
    item = r.json()["item"]
    assert item["state"] == "claimed"
    assert item["claim_expires_at"] is not None
    assert item["claim_expired"] is False


# --- a losing concurrent writer is refused, not silently overwritten -----------
#
# A release races a resolve on the same claimed item: only one may land. Before
# the fix, `lease.release_claim`'s UPDATE carried no predicate at all — not even
# matching the current state — so a resolve that committed between release's read
# and its write was silently stomped back to `open`, re-queuing work that had
# already finished. The fix takes the write lock before the read instead of
# adding a predicate the shared writer has no state to compare against
# (`machine_registry.revoke()` is the same shape). Synchronised the same way the
# other race tests are: two real threads, one held at `lease.release_claim`
# (release's seam, now reached only after the lock and a fresh read), the other
# at `lease.live_conflicts` (resolve's seam, after its own stale read) — so
# neither can write until both have arrived.


def test_a_release_that_races_a_concurrent_resolve_is_serialized(client,
                                                                 monkeypatch):
    """Reproduces the reviewer's finding on PR #81: `lease.release_claim`'s
    UPDATE carries no predicate at all, so a resolve that committed in the
    window between release's read and its write used to be silently stomped
    back to `open`, re-queuing work that had already finished — and
    `release_claim`'s own event INSERT hardcodes `from_state` as `claimed`
    regardless of what the row actually held, a false audit entry.

    **Forces resolve into that exact window rather than hoping for it.**
    `lease.release_for` is release's first write-adjacent call, reached only
    after its own read and state checks. Release is held there while a
    resolve is fired at the same item and given a short, generously bounded
    window to land its write. Before the fix, release's read took no lock, so
    an uncontended resolve completes in that window and release's later
    unconditioned write clobbers it. After the fix (`BEGIN IMMEDIATE` before
    release's read), release already holds the write lock by the time it
    reaches this pause, so resolve's write instead blocks until release
    finishes and then correctly loses its own compare-and-swap against the
    state release just wrote.
    """
    import threading
    import time

    release_paused = threading.Event()
    release_continue = threading.Event()
    real_release_for = lease.release_for

    def release_for_racer(*args, **kwargs):
        release_paused.set()
        assert release_continue.wait(timeout=20), "never told to continue"
        return real_release_for(*args, **kwargs)

    monkeypatch.setattr(lease, "release_for", release_for_racer)

    results = {}

    def do_release():
        results["release"] = client.post(
            "/items/lone_v/release", json={"owner": "session-1"}, headers=_h())

    def do_resolve():
        results["resolve"] = client.post(
            "/items/lone_v/resolve",
            json={"resolution": "fixed", "owner": "session-1"}, headers=_h())

    release_thread = threading.Thread(target=do_release)
    release_thread.start()
    assert release_paused.wait(timeout=20), "release never reached its pause"

    resolve_thread = threading.Thread(target=do_resolve)
    resolve_thread.start()
    # A short, generous window for an UNCONTENDED resolve write to complete —
    # far short of the 5s busy_timeout a genuinely lock-blocked one would hit.
    time.sleep(0.3)
    release_continue.set()

    release_thread.join(timeout=20)
    resolve_thread.join(timeout=20)

    assert set(results) == {"release", "resolve"}, (
        "a request died before reaching the race seam")
    assert results["release"].status_code == 200, results["release"].json()
    assert results["resolve"].status_code == 409, results["resolve"].json()
    assert results["resolve"].json()["error"] == (
        "the item is no longer claimed; it is now open")

    conn = sqlite3.connect(store.DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT state, claimed_by FROM issue_items "
                       "WHERE id='lone_v'").fetchone()
    events = conn.execute("SELECT from_state, to_state FROM issue_events "
                          "WHERE item_id='lone_v'").fetchall()
    conn.close()
    assert row["state"] == "open" and row["claimed_by"] is None
    assert [(e["from_state"], e["to_state"]) for e in events] == [
        ("claimed", "open")], "resolve's write landed too"
