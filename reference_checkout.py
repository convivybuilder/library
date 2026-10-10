"""Checkout/check-in for a Reference Room document — the lock, not the write.

`issue_01M0DVAH9KAACHH0RRWQZ3KZTE`. The Reference Room's content stays files in
the `knowledge` repository (ADR-0022); this module never touches them. What it
owns is the same discipline `lease.py` gives a work item's subtree, applied to
one KB document instead: a document can be checked out, refusing a second
concurrent checkout, held, and checked back in. The Library does not commit or
push — the operator, 2026-08-18: "the mechanics of the action are a question for the
Bosun's Bead and Course, not the Library's."

**`doc_id` NAMES A ROW IN A DIFFERENT DATABASE** (`~/.knowledge-index/
index.db`, read by `reference.py`), so there is no foreign key to it — the two
are separate deployables on separate schedules, the index rebuilt from the
`knowledge` repo on its own cadence. A checkout can therefore outlive the
document it names, or be taken against an id the index has not indexed yet;
callers that need "does this document exist" ask `reference.py` first, the
same way `get_reference_document` already does.

**EXPIRY REUSES `lease.py`'S OWN POLICY RATHER THAN DEFINING A SECOND ONE.**
`grace()`, `max_lease()`, `expires_at()` and `is_dead()` are generic over any
row carrying `taken_at`/`heartbeat_at` — they hold no `issue_items` reference
— so a document checkout dies by the identical rule an item lease does, off the
same tunables file. Two coordination timeouts on the same vault disagreeing by
accident is exactly what `lease.py`'s own module docstring warns against.
"""
from __future__ import annotations

import datetime as _dt
import sqlite3

import lease
from store import new_ulid


class CheckoutConflict(Exception):
    """A live, unexpired checkout already holds this document.

    Carries the row rather than a message, matching `lease.LeaseConflict`: the
    caller renders a refusal naming the holder and the expiry, not a bare "no".
    """

    def __init__(self, row):
        self.checkout = row
        super().__init__(
            f"checkout {row['id']} on {row['doc_id']} held by {row['holder']}")


def live_checkout_on(conn, doc_id: str):
    """The unreleased checkout row on `doc_id`, dead or alive. None if there is
    none.

    "Live" means unreleased, not unexpired — matching `lease.live_lease_on`,
    for the same reason: an expired checkout is still the row occupying the
    document until something reclaims it, and a caller that cares about the
    difference asks `lease.is_dead`.
    """
    return conn.execute(
        "SELECT * FROM reference_checkouts WHERE doc_id = ? "
        "AND released_at IS NULL", (doc_id,)).fetchone()


def _stamp_release(conn, row_id: str, *, reason: str, by: str, now_iso: str) -> None:
    conn.execute(
        "UPDATE reference_checkouts SET released_at = ?, release_reason = ?, "
        "released_by = ? WHERE id = ?", (now_iso, reason, by, row_id))


def take(conn, doc_id: str, *, holder: str, actor: str, run_id: str | None = None):
    """Check out `doc_id`. Raises `CheckoutConflict` when a live, unexpired
    checkout blocks.

    **A DEAD CHECKOUT IS RECLAIMED RATHER THAN BLOCKING**, matching
    `lease.take`'s own handling of a dead item lease — a document whose
    holder went quiet past `lease.grace()` (or past `lease.max_lease()` since
    it was taken) is not a document anyone is still working on, and treating
    it as a live block would fence a document forever against a holder that
    is never coming back to release it. The reclaim is stamped `by=actor`,
    the verified caller, never `holder` — `holder` is a caller-declared label
    (`_claim_owner`'s own contract), and recording it as the actor who broke a
    dead lock would let anyone's self-chosen name land in an audit column
    meant to say who actually did it.

    **THE WRITE LOCK IS TAKEN BEFORE THE SCAN, NOT AT THE INSERT**
    (security-reviewer, PR #45), matching `lease.take`'s own reasoning: a
    scan under no lock is a sequence of plain `SELECT`s, and two concurrent
    callers reclaiming the same dead `doc_id` would both see it as dead, both
    release it, and one of the two inserts would then hit the partial unique
    index as an uncaught `IntegrityError` — a 500 on a caller that did nothing
    wrong, where a clean 409 naming the winner is the whole point of this
    module. `BEGIN IMMEDIATE` serializes the second caller behind the first,
    so it re-scans against what the first actually committed.
    """
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    now_iso = now.isoformat(" ")
    held = live_checkout_on(conn, doc_id)
    if held is not None:
        if not lease.is_dead(held, now=now):
            raise CheckoutConflict(held)
        _stamp_release(conn, held["id"], reason="reclaimed", by=actor,
                       now_iso=now_iso)
    checkout_id = new_ulid()
    try:
        conn.execute(
            "INSERT INTO reference_checkouts (id, doc_id, holder, run_id, "
            "taken_at, heartbeat_at) VALUES (?,?,?,?,?,?)",
            (checkout_id, doc_id, holder, run_id, now_iso, now_iso))
    except sqlite3.IntegrityError:
        # LOST THE INSERT RACE, matching `lease.take`'s own handling: with the
        # write lock held since the scan this is the narrow case the index
        # exists for, not the common one. Refuse in the same terms the loser
        # would have got a moment earlier, naming whoever actually won.
        winner = live_checkout_on(conn, doc_id)
        if winner is None:
            raise
        raise CheckoutConflict(winner) from None
    return conn.execute("SELECT * FROM reference_checkouts WHERE id = ?",
                        (checkout_id,)).fetchone()


def release(conn, doc_id: str, *, holder: str, actor: str):
    """Check `doc_id` back in. `None` when there is nothing live, or the live
    checkout belongs to a different holder.

    **Scoped to the holder, matching `lease.release_for`'s own belt-and-
    suspenders shape** — the route already checked `held["holder"] == holder`
    for its refusal message before calling this, and the check is repeated
    here so this function has no path that releases a checkout its argument
    did not actually match, whatever a future caller does or forgets to check
    first.

    **STAMPED WITH `actor`, NOT `holder`** (security-reviewer, PR #45, round
    2) — the same reasoning `take`'s reclaim now uses: `holder` is a
    caller-declared label, and `lease.release_for` records every release,
    ordinary or not, against the verified actor rather than that label.
    """
    held = live_checkout_on(conn, doc_id)
    if held is None or held["holder"] != holder:
        return None
    now_iso = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None).isoformat(" ")
    _stamp_release(conn, held["id"], reason="released", by=actor, now_iso=now_iso)
    return conn.execute("SELECT * FROM reference_checkouts WHERE id = ?",
                        (held["id"],)).fetchone()


def checkout_dict(row) -> dict:
    """One checkout in protocol shape. `expires_at` is computed rather than
    stored, matching `lease.lease_dict` — it is a function of `taken_at` and
    `heartbeat_at` against the CURRENT tunables, and a stored value would go
    stale the moment the tunables file changes."""
    end = lease.expires_at(row["taken_at"], row["heartbeat_at"])
    return {
        "id": row["id"],
        "doc_id": row["doc_id"],
        "holder": row["holder"],
        "run_id": row["run_id"],
        "taken_at": row["taken_at"],
        "heartbeat_at": row["heartbeat_at"],
        "expires_at": end.isoformat(" ") if end else None,
        "released_at": row["released_at"],
        "release_reason": row["release_reason"],
        "released_by": row["released_by"],
    }


def touch(conn, doc_id: str, *, holder: str):
    """Record a sighting of `holder` on the checkout covering `doc_id`, if it
    holds one. `None` when there is no live checkout, when it belongs to
    someone else, or when it is already dead.

    **A heartbeat NEVER creates a checkout**, matching `lease.touch` and for the
    same reason `/items/{id}/heartbeat` gives: one that could create would be a
    checkout by another name, and one that skipped the conflict check.

    **Refreshes `heartbeat_at` and never `taken_at`, which is what bounds the
    damage a misnamed heartbeat can do.** `holder` is a caller-declared label
    rather than a credential — every session presents the same token, so a
    caller that reads a holder's name off a checkout can heartbeat a checkout it
    does not hold. `lease.touch` carries the full reasoning; the consequence is
    identical here. Since `expires_at` is computed from `taken_at` against the
    hard cap as well as from `heartbeat_at`, no number of heartbeats pushes a
    checkout past that cap, and only a re-checkout resets it.

    Dead is refused rather than revived. A checkout past its grace has already
    become reclaimable by `take`, and letting a heartbeat resurrect it would
    reopen the window a reclaim just closed — the holder that went away long
    enough to be reclaimable has to check out again.
    """
    held = live_checkout_on(conn, doc_id)
    if held is None or held["holder"] != holder or lease.is_dead(held):
        return None
    now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    conn.execute("UPDATE reference_checkouts SET heartbeat_at = ? WHERE id = ?",
                 (now.isoformat(" "), held["id"]))
    return conn.execute("SELECT * FROM reference_checkouts WHERE id = ?",
                        (held["id"],)).fetchone()
