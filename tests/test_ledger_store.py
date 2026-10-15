"""The ledger store: an accumulating total, an optional cap set, and
append-only, voidable spend.

**Built by running the real migration rather than by hand-writing the DDL**, for
the reason `test_alembic_baseline` drives the CLI: the behaviours worth testing
here live in constraints — a partial unique index, a `GLOB` day-shape CHECK, a
unique `dedup_key` that must let NULLs coexist — and a hand-written schema in a
fixture is a second copy that drifts from the migration and then passes tests the
real database would fail.
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import store

REPO = Path(__file__).resolve().parent.parent
DAY = "2026-08-23"


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A database at head, with `store` pointed at it."""
    path = tmp_path / "ledger.db"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
             "LIBRARY_DB": str(path)},
    )
    assert result.returncode == 0, result.stderr
    monkeypatch.setattr(store, "DB_PATH", path)
    # The shadow gate is OFF by default and every write test lifts it the same
    # way. It is a deployment guard, not the behaviour under test here.
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    return path


def make_ledger(**over):
    row = {"scope_kind": "project", "scope_id": "library", "granted_by": "operator",
          "warn_usd_micro": None, "soft_cap_usd_micro": None,
          "hard_cap_usd_micro": None}
    row.update(over)
    return store.upsert_ledger(row)


@pytest.fixture
def ledger(db):
    return make_ledger(soft_cap_usd_micro=160_000_000)


@pytest.fixture
def uncapped(db):
    return make_ledger()


def spend(ledger_id, micro, **over):
    row = {"ledger_id": ledger_id, "day": DAY, "usd_micro": micro,
           "source": "agentctl", "product": "library", "reported_by": "agentctl"}
    row.update(over)
    return store.record_spend(row)


# --- the total accumulates, once ---------------------------------------------

def test_an_entry_moves_the_days_total(ledger):
    idempotent, entry, _crossed = spend(ledger["id"], 1_500_000)
    assert idempotent is False
    assert entry["usd_micro"] == 1_500_000
    conn = store.connect()
    try:
        assert store.ledger_total(conn, ledger["id"], DAY) == 1_500_000
    finally:
        conn.close()


def test_a_repeated_dedup_key_does_not_move_the_total_twice(ledger):
    """The whole reason `dedup_key` is unique in the schema.

    Assistant records in a transcript duplicate up to four times with identical
    usage, so a writer that reports each line bills a multiple. Here the second
    report must be answered with the FIRST row and leave the total where it
    was — not merely refuse, which would make streaming spend unsafe to retry.
    """
    first_idempotent, first, _first_crossed = spend(
        ledger["id"], 1_000_000, dedup_key="run-1:msg-1:req-1")
    second_idempotent, second, second_crossed = spend(
        ledger["id"], 1_000_000, dedup_key="run-1:msg-1:req-1")
    assert first_idempotent is False
    assert second_idempotent is True
    assert second["id"] == first["id"]
    assert second_crossed is None
    conn = store.connect()
    try:
        assert store.ledger_total(conn, ledger["id"], DAY) == 1_000_000
        assert conn.execute("SELECT COUNT(*) c FROM ledger_entries").fetchone()["c"] == 1
    finally:
        conn.close()


def test_a_dedup_key_matching_a_different_ledgers_entry_refuses_without_its_fields(
        db, ledger):
    """`dedup_key` is unique across the whole `ledger_entries` table, not per
    ledger (CQ·107). A caller scoped to one project reporting a key that
    collided with another project's entry must not be able to read that
    entry's id, day, amount or lane back under its own `ledger_id` — the
    pre-check is where that match is found, and it must refuse rather than
    replay it.

    **A test that fails if the ledger comparison is removed**: reverting
    `record_spend`'s pre-check to "any dedup_key match is a replay" makes
    this call return `(True, dict(other_entry), None)` instead of raising,
    and `pytest.raises` below catches that.
    """
    other = make_ledger(scope_id="vivy")
    _, other_entry, _ = spend(other["id"], 9_000_000, dedup_key="shared-key")
    with pytest.raises(store.DedupKeyTaken) as caught:
        spend(ledger["id"], 2_000_000, dedup_key="shared-key")
    assert caught.value.dedup_key == "shared-key"
    assert other_entry["id"] not in str(caught.value)
    conn = store.connect()
    try:
        assert store.ledger_total(conn, ledger["id"], DAY) == 0
        assert store.ledger_total(conn, other["id"], DAY) == 9_000_000
        assert conn.execute(
            "SELECT COUNT(*) c FROM ledger_entries").fetchone()["c"] == 1
    finally:
        conn.close()


def test_a_raced_dedup_key_matching_a_different_ledger_still_refuses(
        db, ledger, monkeypatch):
    """The `IntegrityError` race path re-reads the row that won the race, and
    it needs the same cross-ledger check the pre-check has — otherwise a
    caller could reach another project's entry just by winning the race
    instead of losing the pre-check (CQ·107).

    **Driven by blinding the pre-check's first look**, the technique
    `test_a_lost_race_still_names_the_plan_that_won` (`test_course_writes.py`)
    uses for `POST /courses`: a competing row is committed by a second
    connection first, so it is exactly the row a concurrent report would
    have left; then the first read `record_spend` would use to see it is made
    to return nothing, so the INSERT below hits the real unique constraint
    and the exception handler is what has to catch the cross-ledger match.

    **A test that fails if the ledger comparison is removed from this path**:
    reverting the race path's check back to "any winner is a replay" makes
    this call return `(True, dict(winner), None)` instead of raising.
    """
    other = make_ledger(scope_id="vivy")
    winner = sqlite3.connect(db)
    winner.execute(
        "INSERT INTO ledger_entries (id, ledger_id, day, usd_micro, at, "
        "ingested_at, source, product, reported_by, dedup_key) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("lde_winner", other["id"], DAY, 1_000_000, "2026-08-23 00:00:00",
         "2026-08-23 00:00:00", "agentctl", "vivy", "agentctl", "race-key"))
    winner.commit()
    winner.close()

    real_connect_rw = store.connect_rw
    blinded = {"done": False}

    class _Empty:
        def fetchone(self):
            return None

    class _BlindOnce:
        """Forwards every call to the real connection, except the dedup
        pre-check's SELECT, which it answers empty exactly once — the same
        shape `blind_first_look` gives `_live_course_refusal` in
        `test_course_writes.py`, applied here to a connection method rather
        than a named function, since this pre-check has none to patch."""

        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, params=()):
            if (not blinded["done"] and sql ==
                    "SELECT * FROM ledger_entries WHERE dedup_key = ?"):
                blinded["done"] = True
                return _Empty()
            return self._conn.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    monkeypatch.setattr(store, "connect_rw",
                        lambda: _BlindOnce(real_connect_rw()))
    with pytest.raises(store.DedupKeyTaken):
        spend(ledger["id"], 2_000_000, dedup_key="race-key")
    assert blinded["done"], (
        "the pre-check was never blinded; this test did not reach the race path")
    conn = store.connect()
    try:
        assert store.ledger_total(conn, ledger["id"], DAY) == 0
        assert conn.execute(
            "SELECT COUNT(*) c FROM ledger_entries WHERE dedup_key = 'race-key'"
        ).fetchone()["c"] == 1
    finally:
        conn.close()


def test_entries_without_a_dedup_key_are_never_deduped(ledger):
    """An app backend reporting one call has no transcript tuple to key on.

    NULLs do not collide under a SQLite unique index, which is what makes the
    column safe to leave nullable — two genuine spends of the same size must
    both count.
    """
    spend(ledger["id"], 2_000_000)
    spend(ledger["id"], 2_000_000)
    conn = store.connect()
    try:
        assert store.ledger_total(conn, ledger["id"], DAY) == 4_000_000
        assert conn.execute("SELECT COUNT(*) c FROM ledger_entries").fetchone()["c"] == 2
    finally:
        conn.close()


def test_the_total_always_equals_the_unvoided_entries_behind_it(ledger):
    """The invariant, stated as itself rather than as a specific arithmetic.

    **Asserts the relationship rather than an arithmetic**, so it survives a
    refactor of `record_spend` and `void_spend_entry` and still fails if the
    total and the entries ever come apart. Exercised after ordinary writes, a
    duplicate `dedup_key`, and a void — the three ways this invariant could
    break.
    """
    for i in range(6):
        spend(ledger["id"], 1_000_000, dedup_key=f"run-1:msg-{i % 2}:req-1")
    _, third, _ = spend(ledger["id"], 3_000_000)
    store.void_spend_entry(third["id"], "operator", "typo'd the amount")

    conn = store.connect()
    try:
        summed = conn.execute(
            "SELECT COALESCE(SUM(usd_micro), 0) s FROM ledger_entries "
            "WHERE ledger_id = ? AND day = ? AND voided_at IS NULL",
            (ledger["id"], DAY)).fetchone()["s"]
        total = conn.execute(
            "SELECT spent_usd_micro s FROM ledger_days "
            "WHERE ledger_id = ? AND day = ?", (ledger["id"], DAY)).fetchone()["s"]
        assert total == summed == 2_000_000
        # And the duplicates really were duplicates, so the test is not passing
        # because nothing collided.
        assert conn.execute(
            "SELECT COUNT(*) c FROM ledger_entries").fetchone()["c"] == 3
    finally:
        conn.close()


def test_each_day_has_its_own_total(ledger):
    spend(ledger["id"], 10_000_000)
    spend(ledger["id"], 90_000_000, day="2026-08-24")
    conn = store.connect()
    try:
        assert store.ledger_total(conn, ledger["id"], DAY) == 10_000_000
        assert store.ledger_total(conn, ledger["id"], "2026-08-24") == 90_000_000
    finally:
        conn.close()


# --- lane: accepted, not yet required, and only one lane is enforceable ------
#
# issue_01M35QG5NA13JE8FQVM9XET4HD.

def test_an_unknown_lane_is_refused_and_writes_nothing(ledger):
    with pytest.raises(ValueError, match="lane"):
        spend(ledger["id"], 1_000_000, lane="promo-credit")
    conn = store.connect()
    try:
        assert conn.execute("SELECT COUNT(*) c FROM ledger_entries").fetchone()["c"] == 0
        assert store.ledger_total(conn, ledger["id"], DAY) == 0
    finally:
        conn.close()


def test_a_spend_with_no_lane_is_stored_as_null_and_still_enforceable(ledger):
    idempotent, entry, _ = spend(ledger["id"], 4_000_000)
    assert idempotent is False
    assert entry["lane"] is None
    conn = store.connect()
    try:
        assert store.ledger_total(conn, ledger["id"], DAY) == 4_000_000
    finally:
        conn.close()


def test_a_gateway_entry_moves_the_enforceable_total_a_subscription_entry_does_not(ledger):
    """The write side of the split `ledger_total`'s docstring states:
    `gateway` and no-`lane` both move the enforceable total; `subscription`
    never does, however large."""
    spend(ledger["id"], 1_000_000, lane="gateway")
    idempotent, sub_entry, crossed = spend(ledger["id"], 9_000_000, lane="subscription")
    assert idempotent is False
    assert sub_entry["lane"] == "subscription"
    assert crossed is None
    conn = store.connect()
    try:
        assert store.ledger_total(conn, ledger["id"], DAY) == 1_000_000
        assert store.ledger_lane_total(conn, ledger["id"], DAY, "gateway") == 1_000_000
        assert store.ledger_lane_total(conn, ledger["id"], DAY, "subscription") == 9_000_000
        # Both entries are there — the subscription report was recorded, it
        # simply never reached `ledger_days`.
        assert conn.execute("SELECT COUNT(*) c FROM ledger_entries").fetchone()["c"] == 2
    finally:
        conn.close()


def test_ledger_lane_total_does_not_fold_an_unset_lane_into_either_side(ledger):
    """`ledger_lane_total` is an EXACT match. An entry with no `lane` is
    neither `gateway` nor `subscription` under this function — only
    `ledger_total`'s enforceable figure folds it in, and it folds it in with
    `gateway`, never with `subscription`."""
    spend(ledger["id"], 2_000_000)
    conn = store.connect()
    try:
        assert store.ledger_lane_total(conn, ledger["id"], DAY, "gateway") == 0
        assert store.ledger_lane_total(conn, ledger["id"], DAY, "subscription") == 0
        assert store.ledger_total(conn, ledger["id"], DAY) == 2_000_000
    finally:
        conn.close()


def test_voiding_a_subscription_entry_does_not_touch_the_enforceable_total(ledger):
    """The mirror of `record_spend`'s split: a subscription entry never
    joined `ledger_days`, so voiding it must not subtract from a total it
    never added to."""
    spend(ledger["id"], 1_000_000, lane="gateway")
    _, sub_entry, _ = spend(ledger["id"], 9_000_000, lane="subscription")
    conn = store.connect()
    try:
        before = store.ledger_total(conn, ledger["id"], DAY)
    finally:
        conn.close()
    store.void_spend_entry(sub_entry["id"], "operator", "test")
    conn = store.connect()
    try:
        after = store.ledger_total(conn, ledger["id"], DAY)
        assert after == before == 1_000_000
        assert store.ledger_lane_total(conn, ledger["id"], DAY, "subscription") == 0
    finally:
        conn.close()


# --- an overrun is reported, never refused and never hidden ------------------

def test_spend_past_the_soft_cap_is_recorded_and_the_total_keeps_climbing(ledger):
    """A spend that already happened cannot be refused.

    Two runs bypassing checkout spend a multiple of one cap; the Library's job
    is to make the next admission decision accurate, which means recording the
    overrun rather than rejecting the report. Nothing counts down and nothing
    clamps: the total is exactly what was spent.
    """
    idempotent, _, _ = spend(ledger["id"], 200_000_000)
    assert idempotent is False
    conn = store.connect()
    try:
        assert store.ledger_total(conn, ledger["id"], DAY) == 200_000_000
    finally:
        conn.close()


# --- an uncapped scope is not an error ----------------------------------------

def test_a_scope_with_no_ledger_reads_as_none_rather_than_raising(db):
    """Most projects are expected to stay uncapped. Admission refuses on a
    capped ledger whose total has reached a threshold, never on a missing
    one."""
    conn = store.connect()
    try:
        assert store.live_ledger(conn, "project", "vivy") is None
    finally:
        conn.close()


def test_an_uncapped_ledger_accumulates_and_is_never_capped(uncapped):
    spend(uncapped["id"], 1_000_000_000)
    conn = store.connect()
    try:
        total = store.ledger_total(conn, uncapped["id"], DAY)
    finally:
        conn.close()
    assert total == 1_000_000_000
    assert store.ledger_state(uncapped, total) == store.LEDGER_STATE_UNCAPPED


# --- ensure_live_ledger: the route's path to recording the spend of a scope
# nobody capped ---------------------------------------------------------------

def test_ensure_live_ledger_creates_an_uncapped_row_when_none_exists(db):
    row = store.ensure_live_ledger("project", "vivy")
    assert row["warn_usd_micro"] is None
    assert row["soft_cap_usd_micro"] is None
    assert row["hard_cap_usd_micro"] is None
    assert row["state"] == "live"
    conn = store.connect()
    try:
        assert store.live_ledger(conn, "project", "vivy")["id"] == row["id"]
        assert conn.execute("SELECT COUNT(*) c FROM ledgers").fetchone()["c"] == 1
    finally:
        conn.close()


def test_ensure_live_ledger_returns_the_existing_row_rather_than_a_second_one(ledger):
    """Called for a scope that already has a live ledger — a human's cap set
    through `PUT /ledgers`, here — it must return that row unchanged rather
    than raising on the partial unique index or creating a second live row."""
    again = store.ensure_live_ledger("project", "library")
    assert again["id"] == ledger["id"]
    assert again["soft_cap_usd_micro"] == ledger["soft_cap_usd_micro"]
    conn = store.connect()
    try:
        assert conn.execute("SELECT COUNT(*) c FROM ledgers").fetchone()["c"] == 1
    finally:
        conn.close()


def test_a_cap_set_after_ensure_live_ledger_accounts_the_spend_already_there(db):
    """A cap set later on a project that already has uncapped spend must
    account that spend correctly: `upsert_ledger` finds the same live row
    `ensure_live_ledger` created — same scope, same `state = 'live'` — and
    updates it in place, the same "overwrite rather than append" behaviour it
    already gives a human raising or lowering a cap mid-day
    (`test_resetting_updates_in_place_and_keeps_the_days_spend`). The day's
    total is keyed on the ledger id, not on when the cap was set, so it is
    judged against the new cap immediately rather than only from here on.
    """
    row = store.ensure_live_ledger("project", "vivy")
    spend(row["id"], 500_000_000)
    capped = make_ledger(scope_id="vivy", soft_cap_usd_micro=100_000_000)
    assert capped["id"] == row["id"]
    conn = store.connect()
    try:
        assert conn.execute("SELECT COUNT(*) c FROM ledgers").fetchone()["c"] == 1
        total = store.ledger_total(conn, row["id"], DAY)
    finally:
        conn.close()
    assert total == 500_000_000
    assert store.ledger_state(capped, total) == store.LEDGER_STATE_SOFT_CAPPED


# --- ledger_state --------------------------------------------------------------

def test_ledger_state_reports_the_highest_threshold_crossed(db):
    ledger_row = make_ledger(warn_usd_micro=10_000_000,
                             soft_cap_usd_micro=20_000_000,
                             hard_cap_usd_micro=30_000_000)
    assert store.ledger_state(ledger_row, 0) == store.LEDGER_STATE_OK
    assert store.ledger_state(ledger_row, 10_000_000) == store.LEDGER_STATE_WARNING
    assert store.ledger_state(ledger_row, 20_000_000) == store.LEDGER_STATE_SOFT_CAPPED
    assert store.ledger_state(ledger_row, 30_000_000) == store.LEDGER_STATE_HARD_CAPPED
    assert store.ledger_state(ledger_row, 999_000_000) == store.LEDGER_STATE_HARD_CAPPED


def test_ledger_state_is_uncapped_only_with_no_threshold_at_all(db):
    only_hard = make_ledger(hard_cap_usd_micro=30_000_000)
    assert store.ledger_state(only_hard, 0) == store.LEDGER_STATE_OK
    assert store.ledger_state(only_hard, 30_000_000) == store.LEDGER_STATE_HARD_CAPPED

    nothing = make_ledger(scope_id="uncapped-project")
    assert store.ledger_state(nothing, 999_000_000) == store.LEDGER_STATE_UNCAPPED


def test_a_cap_of_zero_is_a_deliberate_freeze(db):
    """`>=`, not `>`, is what makes a zero cap refuse from the first dollar —
    the same property the draw-down model's `headroom <= 0` had."""
    frozen = make_ledger(soft_cap_usd_micro=0)
    assert store.ledger_state(frozen, 0) == store.LEDGER_STATE_SOFT_CAPPED


# --- crossings fire once per threshold per day --------------------------------

def test_crossing_a_threshold_is_reported_exactly_once(db):
    ledger_row = make_ledger(warn_usd_micro=10_000_000)
    _, _, first_cross = spend(ledger_row["id"], 6_000_000)
    assert first_cross is None
    _, _, second_cross = spend(ledger_row["id"], 6_000_000)
    assert second_cross == "warn"
    _, _, third_cross = spend(ledger_row["id"], 1_000_000)
    assert third_cross is None


def test_crossing_multiple_thresholds_at_once_reports_the_highest(db):
    ledger_row = make_ledger(warn_usd_micro=10_000_000,
                             soft_cap_usd_micro=20_000_000)
    _, _, crossed = spend(ledger_row["id"], 25_000_000)
    assert crossed == "soft_cap"


# --- the grant, now a cap set --------------------------------------------------

def test_resetting_updates_in_place_and_keeps_the_days_spend(ledger):
    """Raising a cap mid-day must not forgive the morning's spend, or the cap is
    unenforceable by repeated re-setting."""
    spend(ledger["id"], 100_000_000)
    again = make_ledger(soft_cap_usd_micro=300_000_000)
    assert again["id"] == ledger["id"]
    conn = store.connect()
    try:
        assert conn.execute("SELECT COUNT(*) c FROM ledgers").fetchone()["c"] == 1
        assert store.ledger_total(conn, ledger["id"], DAY) == 100_000_000
    finally:
        conn.close()


def test_a_second_live_ledger_for_one_scope_is_impossible(ledger):
    """Enforced by the partial index, so a future writer that bypasses
    `upsert_ledger` cannot create the ambiguity either."""
    conn = store.connect_rw()
    try:
        with pytest.raises(Exception) as caught:
            conn.execute(
                "INSERT INTO ledgers (id, scope_kind, scope_id, "
                " state, granted_by, created_at, updated_at) "
                "VALUES ('ldg_second','project','library','live','operator','x','x')")
            conn.commit()
        assert "UNIQUE" in str(caught.value).upper()
    finally:
        conn.close()


def test_a_scope_may_close_more_than_one_ledger_over_its_life(ledger):
    """The failure the first version of the constraint had.

    A plain unique constraint on (scope_kind, scope_id, state) permits one live
    and one closed row, and then refuses the SECOND ledger a project ever
    closes. Only a partial index on the live state gets this right.
    """
    conn = store.connect_rw()
    try:
        conn.execute("UPDATE ledgers SET state = 'closed' WHERE id = ?",
                     (ledger["id"],))
        conn.commit()
    finally:
        conn.close()
    second = make_ledger(soft_cap_usd_micro=1_000_000)
    conn = store.connect_rw()
    try:
        conn.execute("UPDATE ledgers SET state = 'closed' WHERE id = ?",
                     (second["id"],))
        conn.commit()
    finally:
        conn.close()
    third = make_ledger(soft_cap_usd_micro=2_000_000)
    conn = store.connect()
    try:
        rows = conn.execute(
            "SELECT state FROM ledgers WHERE scope_id = 'library'").fetchall()
        assert sorted(r["state"] for r in rows) == ["closed", "closed", "live"]
        assert third["soft_cap_usd_micro"] == 2_000_000
    finally:
        conn.close()


def test_caps_must_be_non_decreasing(db):
    with pytest.raises(ValueError, match="non-decreasing"):
        make_ledger(warn_usd_micro=100, soft_cap_usd_micro=50)
    with pytest.raises(ValueError, match="non-decreasing"):
        make_ledger(soft_cap_usd_micro=100, hard_cap_usd_micro=50)
    with pytest.raises(ValueError, match="non-decreasing"):
        make_ledger(warn_usd_micro=100, hard_cap_usd_micro=50)


def test_non_decreasing_caps_with_gaps_are_fine(db):
    """Only the PRESENT caps have to be ordered — a warn-and-hard set with no
    soft cap is judged on warn <= hard directly."""
    row = make_ledger(warn_usd_micro=10, hard_cap_usd_micro=20)
    assert row["warn_usd_micro"] == 10
    assert row["soft_cap_usd_micro"] is None
    assert row["hard_cap_usd_micro"] == 20


def test_an_unset_cap_stays_null_and_all_three_null_is_legal(db):
    row = make_ledger()
    assert row["warn_usd_micro"] is None
    assert row["soft_cap_usd_micro"] is None
    assert row["hard_cap_usd_micro"] is None


def test_an_unknown_scope_kind_is_refused(db):
    with pytest.raises(ValueError, match="scope_kind"):
        store.upsert_ledger({
            "scope_kind": "run", "scope_id": "run-1", "granted_by": "operator",
            "warn_usd_micro": None, "soft_cap_usd_micro": None,
            "hard_cap_usd_micro": None})


def test_a_negative_cap_is_refused(db):
    with pytest.raises(ValueError, match="non-negative"):
        make_ledger(soft_cap_usd_micro=-1)


# --- the per-run cap is not a threshold on the project total -------------------

def test_per_run_cap_is_set_and_cleared(db):
    row = make_ledger(per_run_cap_usd_micro=5_000_000)
    assert row["per_run_cap_usd_micro"] == 5_000_000
    cleared = make_ledger(per_run_cap_usd_micro=None)
    assert cleared["per_run_cap_usd_micro"] is None


def test_per_run_cap_takes_no_part_in_the_non_decreasing_ordering(db):
    """A per-run cap bounds one run's own spend, not the project's
    accumulating total, so it is validated on its own and never compared
    against warn, soft or hard — a value far below the warn threshold, or
    far above a product's hard cap, is equally legal (issue_01M4BDRV8C47NQN5X8T82Y2B9A)."""
    low = make_ledger(warn_usd_micro=100, soft_cap_usd_micro=200,
                       hard_cap_usd_micro=300, per_run_cap_usd_micro=1)
    assert low["per_run_cap_usd_micro"] == 1
    high = make_ledger(scope_id="other", hard_cap_usd_micro=10,
                        per_run_cap_usd_micro=1_000_000)
    assert high["per_run_cap_usd_micro"] == 1_000_000


def test_a_negative_per_run_cap_is_refused(db):
    with pytest.raises(ValueError, match="non-negative"):
        make_ledger(per_run_cap_usd_micro=-1)


def test_per_run_cap_changes_no_ledger_state(db):
    """`ledger_state` reads only warn, soft and hard — a per-run cap, however
    low, must not make an otherwise-uncapped or -ok ledger read as capped."""
    uncapped = make_ledger(per_run_cap_usd_micro=1)
    assert store.ledger_state(uncapped, 0) == store.LEDGER_STATE_UNCAPPED
    assert store.ledger_state(uncapped, 999_000_000) == store.LEDGER_STATE_UNCAPPED

    capped = make_ledger(scope_id="also-capped", soft_cap_usd_micro=50_000_000,
                         per_run_cap_usd_micro=1)
    assert store.ledger_state(capped, 10_000_000) == store.LEDGER_STATE_OK


def test_per_run_cap_changes_no_crossing(db):
    """Same shape as `test_crossing_a_threshold_is_reported_exactly_once`,
    with a per-run cap set alongside the warn threshold — `_crossed_threshold`
    reads only warn, soft and hard."""
    ledger_row = make_ledger(warn_usd_micro=10_000_000, per_run_cap_usd_micro=1)
    _, _, first_cross = spend(ledger_row["id"], 6_000_000)
    assert first_cross is None
    _, _, second_cross = spend(ledger_row["id"], 6_000_000)
    assert second_cross == "warn"


# --- void ----------------------------------------------------------------------

def test_a_void_corrects_the_total(ledger):
    _, entry, _ = spend(ledger["id"], 5_000_000)
    already_voided, voided = store.void_spend_entry(entry["id"], "operator", "typo")
    assert already_voided is False
    assert voided["voided_at"] is not None
    assert voided["voided_by"] == "operator"
    assert voided["void_reason"] == "typo"
    conn = store.connect()
    try:
        assert store.ledger_total(conn, ledger["id"], DAY) == 0
    finally:
        conn.close()


def test_a_repeated_void_does_not_double_subtract(ledger):
    _, entry, _ = spend(ledger["id"], 5_000_000)
    spend(ledger["id"], 2_000_000)
    store.void_spend_entry(entry["id"], "operator", "typo")
    already_voided, voided = store.void_spend_entry(entry["id"], "operator", "typo again?")
    assert already_voided is True
    # THE ROW IS UNCHANGED, not re-stamped with the second call's reason.
    assert voided["void_reason"] == "typo"
    conn = store.connect()
    try:
        assert store.ledger_total(conn, ledger["id"], DAY) == 2_000_000
    finally:
        conn.close()


def test_voiding_an_unknown_entry_is_a_lookup_error(db):
    with pytest.raises(LookupError):
        store.void_spend_entry("lde_nonexistent", "operator", None)


def test_a_void_reason_is_optional(ledger):
    _, entry, _ = spend(ledger["id"], 1_000_000)
    _, voided = store.void_spend_entry(entry["id"], "operator", None)
    assert voided["void_reason"] is None


# --- refusals ------------------------------------------------------------------

@pytest.mark.parametrize("day", ["202X-AB-CD", "2026-8-23", "20260823",
                                 "2026-08-23T00:00:00", "", "yesterday"])
def test_a_malformed_day_is_refused(ledger, day):
    """`GLOB`, not `LIKE`: SQLite's `_` matches any character including a
    letter, so a LIKE-based check would pass `202X-AB-CD`."""
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        spend(ledger["id"], 1_000_000, day=day)


def test_an_unknown_source_is_refused(ledger):
    with pytest.raises(ValueError, match="source"):
        spend(ledger["id"], 1_000_000, source="wherever")


def test_a_boolean_is_not_an_amount(ledger):
    """`isinstance(True, int)` is True in Python, so the bool exclusion is a
    real check rather than defensive noise."""
    with pytest.raises(ValueError, match="usd_micro"):
        spend(ledger["id"], True)


@pytest.mark.parametrize("day", [
    "２０２６-０８-２３",   # fullwidth digits
    "٢٠٢٦-٠٨-٢٣",   # arabic-indic digits
    "²⁰²⁶-⁰⁸-²³",   # superscripts
])
def test_a_non_ascii_digit_day_is_refused(ledger, day):
    """`str.isdigit()` is True for all three of these, and the migration's
    `GLOB '[0-9][0-9]...'` CHECK rejects every one.

    Two definitions of "digit" in one validation path is a disagreement waiting
    to be reached from somewhere that does not fail closed — a Python check
    looser than the database's turns a clean refusal into an opaque
    IntegrityError from inside the INSERT. Pinned here because the other three
    round-1 fixes have regression tests and this one did not (reviewer, PR #50
    round 2).
    """
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        spend(ledger["id"], 1_000_000, day=day)


def test_the_python_check_and_the_table_check_agree(db):
    """The property the test above is one instance of: anything
    `_day_shape_ok` accepts, the CHECK must also accept, and vice versa.

    Asserted against the REAL table rather than against a restatement of the
    GLOB, so the two cannot drift.
    """
    conn = store.connect_rw()
    try:
        conn.execute(
            "INSERT INTO ledgers (id, scope_kind, scope_id, "
            " state, granted_by, created_at, updated_at) "
            "VALUES ('ldg_x','project','p','live','operator','x','x')")
        for candidate in ("2026-08-23", "202X-AB-CD", "２０２６-０８-２３",
                          "2026-8-23", "20260823", "", "yesterday",
                          "0000-00-00", "9999-99-99"):
            python_ok = store._day_shape_ok(candidate)
            try:
                conn.execute(
                    "INSERT INTO ledger_days (ledger_id, day, spent_usd_micro, "
                    "updated_at) VALUES ('ldg_x', ?, 0, 'x')", (candidate,))
                conn.execute("DELETE FROM ledger_days WHERE day = ?", (candidate,))
                db_ok = True
            except Exception:
                db_ok = False
            assert python_ok == db_ok, (
                f"{candidate!r}: python says {python_ok}, the table says {db_ok}")
    finally:
        conn.close()


# --- the proportionate bound ---------------------------------------------------

def test_ten_times_the_soft_cap_where_one_is_set(db):
    ledger_row = make_ledger(soft_cap_usd_micro=100_000_000)
    spend(ledger_row["id"], 1_000_000_000)   # exactly 10x: admitted
    with pytest.raises(store.DisproportionateSpend) as caught:
        spend(ledger_row["id"], 1_000_000_001, dedup_key="over")
    assert caught.value.basis == "soft_cap"
    assert caught.value.basis_micro == 100_000_000
    assert caught.value.ceiling_micro == 1_000_000_000


def test_ten_times_the_hard_cap_when_no_soft_cap_is_set(db):
    ledger_row = make_ledger(hard_cap_usd_micro=50_000_000)
    with pytest.raises(store.DisproportionateSpend) as caught:
        spend(ledger_row["id"], 500_000_001)
    assert caught.value.basis == "hard_cap"
    assert caught.value.basis_micro == 50_000_000


def test_the_soft_cap_governs_over_the_hard_cap_when_both_are_set(db):
    ledger_row = make_ledger(soft_cap_usd_micro=10_000_000,
                             hard_cap_usd_micro=50_000_000)
    with pytest.raises(store.DisproportionateSpend) as caught:
        spend(ledger_row["id"], 100_000_001)
    assert caught.value.basis == "soft_cap"
    assert caught.value.basis_micro == 10_000_000


def test_the_flat_ceiling_governs_an_uncapped_project(uncapped):
    spend(uncapped["id"], store.UNCAPPED_REPORT_CEILING_USD_MICRO)   # admitted
    with pytest.raises(store.DisproportionateSpend) as caught:
        spend(uncapped["id"], store.UNCAPPED_REPORT_CEILING_USD_MICRO + 1,
             dedup_key="over")
    assert caught.value.basis == "uncapped"
    assert caught.value.basis_micro is None
    assert caught.value.ceiling_micro == store.UNCAPPED_REPORT_CEILING_USD_MICRO


def test_the_zero_cap_freeze_still_records_and_still_refuses_checkout(db):
    """A cap of zero is a deliberate freeze. The proportionate bound falls
    through to the flat uncapped ceiling — not to "no bound at all" — so a
    real spend under the ceiling still records, and the freeze itself comes
    from `ledger_state`, not from this bound."""
    frozen = make_ledger(soft_cap_usd_micro=0)
    idempotent, _entry, crossed = spend(frozen["id"], 25_000_000)
    assert idempotent is False
    # NO CROSSING FIRES: the total was already at the zero threshold before
    # this entry (0 >= 0), so there is no below-then-at-or-above transition to
    # report. The freeze itself is enforced by `ledger_state`, independent of
    # whether a crossing signal ever fires.
    assert crossed is None
    conn = store.connect()
    try:
        total = store.ledger_total(conn, frozen["id"], DAY)
    finally:
        conn.close()
    assert total == 25_000_000
    assert store.ledger_state(frozen, total) == store.LEDGER_STATE_SOFT_CAPPED


def test_the_flat_ceiling_ignores_spend_history(uncapped):
    """A history-scaled ceiling was rejected in review (PR #77): every row it
    would read is written by `POST /ledger-entries` under `ledger.spend`,
    which the credential the bound constrains already holds, so that
    credential could walk its own ceiling up before landing a wildly wrong
    report. Thirty days of heavy real spend must not move the ceiling at all.

    History is seeded directly into `ledger_days`, not through `spend()` —
    going through `record_spend` would have each seeding call judged against
    the very ceiling this test is proving stays fixed.
    """
    import datetime as dt
    anchor = dt.date.fromisoformat(DAY)
    conn = store.connect_rw()
    try:
        for i in range(1, 31):
            day = (anchor - dt.timedelta(days=i)).isoformat()
            conn.execute(
                "INSERT INTO ledger_days (ledger_id, day, spent_usd_micro, "
                "updated_at) VALUES (?,?,?,?)",
                (uncapped["id"], day, 2_000_000_000, "2026-01-01 00:00:00"))
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(store.DisproportionateSpend) as caught:
        spend(uncapped["id"], store.UNCAPPED_REPORT_CEILING_USD_MICRO + 1,
             dedup_key="over")
    assert caught.value.basis == "uncapped"
    assert caught.value.basis_micro is None
    assert caught.value.ceiling_micro == store.UNCAPPED_REPORT_CEILING_USD_MICRO


def test_a_replay_still_answers_with_the_recorded_row_after_the_cap_drops(db):
    """The idempotent-retry flip, reproduced before it was fixed under the
    draw-down model and still true here.

    The bound must not run before the dedup short-circuit. An identical
    replay of an already-recorded report must not be refused after the cap
    was lowered — the caller told its report was rejected while that exact
    entry sits in the store unchanged.
    """
    ledger_row = make_ledger(soft_cap_usd_micro=1_000_000_000)
    first_idempotent, first, _ = spend(
        ledger_row["id"], 5_000_000_000, dedup_key="run-1:msg-1:req-1")
    assert first_idempotent is False

    make_ledger(soft_cap_usd_micro=100_000_000)   # ceiling falls from $10,000 to $1,000
    replay_idempotent, replay, _ = spend(
        ledger_row["id"], 5_000_000_000, dedup_key="run-1:msg-1:req-1")
    assert replay_idempotent is True
    assert replay["id"] == first["id"]

    # A genuinely NEW report of that size is still refused — the replay path
    # is the exception, not a hole in the bound.
    with pytest.raises(store.DisproportionateSpend):
        spend(ledger_row["id"], 5_000_000_000, dedup_key="run-1:msg-2:req-1")
