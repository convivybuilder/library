"""The epic lease — who is working inside a subtree, and until when.

Keel Phase 2 (ARCH-424/428), carried across from Orient's `lease_api` and
`lease_policy` as a data migration rather than a redesign, which is what §4 asks
for. The semantics already work; moving them is cheaper and safer than
re-deriving them, and every judgement below is Orient's with its reasoning
attached rather than a fresh decision.

**The epic is the lease unit.** Checking out an item under epic E takes the lease
on E, so a sibling under E is refused while it is held. Checking out E itself
takes the same lease on the same object, so "who holds E" has one answer whatever
the holder is doing, and there is no second locking concept to reconcile. An item
with no parent leases only itself.

**A lease is a different thing from a claim.** A claim says which ITEM a worker is
on and cannot express "and therefore its siblings are off limits". The lease says
which SUBTREE is spoken for. They move together on the checkout path — one call
takes both — but they are records about different objects.

**`take` AND THE FENCE ARRIVE HERE, AND THEY WERE WORTH WAITING FOR.** An earlier
slice ported only what `/release` and `/heartbeat` needed, because `/checkout`
looked blocked: it enforced the ARCH-403 feature gate by reading a decision, and
ADR-0022 kept decision records with Orient. ADR-0033 settled that — a decision may
be made in Orient, in Deck, or in a session, and its record is project state that
lives here — so the gate read the Library's own data and there was no cross-store
read. Shipping the riskiest code in the module with no route exercising it would
have been the worse order. **The gate itself is gone now**
(`issue_01M0EEPS591VQHYEBJY7NGF4C3` — `feature` is no longer a work-item type),
which is a later change than the one this paragraph explains; `take` and the
fence stay exactly as landed here, independent of the gate that motivated their
timing.

**TWO WORDS THE COURSE CODE IN THIS MODULE USES, DEFINED HERE ONCE**
(issue_01M35R7VK48D9JDYGZPP3SW7HB, under the Course-machinery epic
issue_01KZFB4KXQD82G3PDHMADXD6NA). Before this, neither appeared anywhere a
reader of Course code would land — `store.epic_progress`, `store.
waypoint_progress`, `course_position` and `waypoint_course_position` below
all point here rather than restating it.

- **An epic** is the bundle of work: an `issue_items` row of type `epic`,
  with other items parented under it (`issue_items.parent_id`).
- **A waypoint** is a point on the route that an epic, or a named set,
  reaches. Usually that set is one epic, and the waypoint needs no object
  of its own — it is the epic seen from the Course rather than from the
  work, reached when the epic's own state is terminal (`course_position`).
  A waypoint may instead be SYNTHETIC, naming several items — an epic or a
  work item, by id — that must all complete before it is reached. No
  single row represents a synthetic waypoint, so it is its own entry on the
  Course (`courses.waypoints`), reached when every member it names is
  terminal (`waypoint_course_position`).
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sqlite3
from pathlib import Path

import store
from store import new_ulid

# Release reasons, carried across. The distinctions are load-bearing: ARCH-424
# makes "a holder handed its lease back" and "another agent reclaimed a dead one"
# and "a human broke a live one" three different facts, and the last of them is
# the override signal that says whether epics are sized right.
LEASE_RELEASED = "released"
LEASE_RESOLVED = "resolved"
LEASE_RECLAIMED = "reclaimed"
LEASE_OVERRIDDEN = "overridden"
# A lease held for exactly one write and released with it (`record_write`), kept
# apart from `released` so a sweep's hundreds of them do not read as holders
# handing work back.
LEASE_WRITTEN = "written"
LEASE_RELEASE_REASONS = frozenset({LEASE_RELEASED, LEASE_RESOLVED,
                                   LEASE_RECLAIMED, LEASE_OVERRIDDEN,
                                   LEASE_WRITTEN})

TUNABLES = Path(os.environ.get(
    "ORIENT_LEASE_TUNABLES",
    str(Path.home() / "Convivy" / "convivy-orient" / "lease-tunables.json")))

# THE SHIP VALUES, recorded beside the defaults they back so the file and the
# fallback cannot drift apart silently.
#
# The grace is bounded from above by the case the operator named: an overnight worker
# dispatched at 01:00 that dies at 01:30 unfences at 05:30, before the working
# day rather than during it. A run that legitimately goes four hours without a
# single queue write is not a shape the overnight produces.
DEFAULT_GRACE = _dt.timedelta(hours=4)

# The hard cap runs from the last checkout, matching the claim TTL on purpose.
# Two coordination timeouts on the same queue disagreeing by accident is how an
# item's claim expires while its epic stays fenced, or the reverse.
DEFAULT_MAX_LEASE = _dt.timedelta(hours=24)

# The pre-lease claim timeout, kept as the fallback for rows that predate
# checkout. It matches the hard cap on purpose: two coordination timeouts on
# one queue disagreeing by accident is how a claim expires while its epic
# stays fenced.
CLAIM_TTL = _dt.timedelta(hours=24)

# Floors, so a typo in the tunables file cannot switch the fence off. A zero or
# negative grace would expire every lease the instant it is taken, which reads as
# "the lease is not working" rather than as a bad config value, and the whole
# subtree would be claimable by anyone while its holder worked.
MIN_GRACE = _dt.timedelta(minutes=1)
MIN_MAX_LEASE = _dt.timedelta(minutes=5)


def _read_tunables() -> dict:
    """The tunables file, or {} when absent or malformed.

    Failing to the defaults rather than raising is deliberate: this is read on the
    checkout path, and a JSON typo must not take the fence down with it. The
    defaults above are the recorded ship values, so the fallback is a known-good
    state rather than an arbitrary one.
    """
    try:
        raw = json.loads(TUNABLES.read_text())
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def _seconds(cfg: dict, key: str, default: _dt.timedelta,
             floor: _dt.timedelta) -> _dt.timedelta:
    """One tunable, clamped to its floor. A non-number, a missing key or a value
    below the floor all fall back rather than being honoured."""
    val = cfg.get(key)
    if not isinstance(val, (int, float)) or isinstance(val, bool):
        return default
    out = _dt.timedelta(seconds=float(val))
    return out if out >= floor else floor


def grace() -> _dt.timedelta:
    """How long after the holder was last seen a lease stays live."""
    return _seconds(_read_tunables(), "grace_seconds", DEFAULT_GRACE, MIN_GRACE)


def max_lease() -> _dt.timedelta:
    """The absolute ceiling on one lease's life, refreshed by re-checkout."""
    return _seconds(_read_tunables(), "max_lease_seconds", DEFAULT_MAX_LEASE,
                    MIN_MAX_LEASE)


def policy() -> dict:
    """Both tunables as seconds — reported so a caller can see the terms it is
    held to rather than inferring them from a refusal. The grace is a tunable
    that moves, and a client that hardcoded it would drift the moment it does."""
    return {"grace_seconds": int(grace().total_seconds()),
            "max_lease_seconds": int(max_lease().total_seconds())}


def _parse(value):
    """A stored timestamp as a naive datetime. Times are naive UTC throughout,
    the datastore-wide convention: SQLite has no native timezone, and an aware
    datetime raises on comparison against what the column hands back."""
    if isinstance(value, _dt.datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = _dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


def expires_at(taken_at, heartbeat_at) -> _dt.datetime | None:
    """When this lease dies: the EARLIER of grace-since-last-sighting and the
    hard cap since checkout.

    Returned rather than merely compared because a refusal has to name it. "Epic
    E is held by run-X" leaves an orchestrator with nothing to schedule against;
    "held until 05:30" lets it come back, and that is the difference ARCH-424 asks
    a refusal to carry.
    """
    taken, beat = _parse(taken_at), _parse(heartbeat_at)
    if taken is None or beat is None:
        return None
    return min(beat + grace(), taken + max_lease())


def is_dead(row, *, now=None) -> bool:
    """Whether this lease may be reclaimed.

    **A lease whose timestamps cannot be read is treated as DEAD**, which is the
    direction that fails toward work continuing rather than toward a subtree
    fenced forever by a row nobody can interpret. It is also unreachable in
    practice: both columns are NOT NULL.
    """
    end = expires_at(row["taken_at"], row["heartbeat_at"])
    if end is None:
        return True
    return (now or _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)) >= end


def root_id_for(conn, item) -> str:
    """Which item a checkout of `item` takes the lease on.

    An EPIC leases ITSELF, at any depth. It does not lease its own parent, and
    that is the ARCH-428 rule rather than an omission: propagating a claim upward
    would let checking out one sub-epic stop every other branch of a top-level
    epic, which is the failure that made upward reach the immediate parent only.

    A LEAF leases its immediate parent, **when that parent is itself an EPIC**,
    or itself otherwise — which covers both a parentless item and one whose
    parent names a corrections queue (`library/specs/one-home-per-work-item`):
    neither is a lease root, so a correction leases only itself exactly as a
    parentless one always has. Telling the two apart costs one lookup, which
    is cheap for the single item every caller here holds; `claim_statuses`
    resolves a whole page instead and uses `root_ids_for` below so that
    lookup runs once for the page, not once per row.
    """
    if item["type"] == store.ITEM_TYPE_EPIC:
        return item["id"]
    parent_id = item["parent_id"]
    if not parent_id:
        return item["id"]
    parent = conn.execute("SELECT type FROM issue_items WHERE id = ?",
                          (parent_id,)).fetchone()
    if parent is not None and parent["type"] == store.ITEM_TYPE_EPIC:
        return parent_id
    return item["id"]


def root_ids_for(conn, items) -> dict:
    """`{item_id: root_id}` for a batch, `root_id_for`'s own rule applied with
    one query for every parent lookup the batch needs, rather than one per
    row — what `claim_statuses` resolves a page through instead of calling
    `root_id_for` in a loop.
    """
    parent_ids = {i["parent_id"] for i in items
                  if i["type"] != store.ITEM_TYPE_EPIC and i["parent_id"]}
    epics = set()
    if parent_ids:
        marks = ",".join("?" * len(parent_ids))
        epics = {r["id"] for r in conn.execute(
            f"SELECT id FROM issue_items WHERE id IN ({marks}) AND type = ?",
            [*parent_ids, store.ITEM_TYPE_EPIC]).fetchall()}
    out = {}
    for item in items:
        if item["type"] == store.ITEM_TYPE_EPIC:
            out[item["id"]] = item["id"]
        elif item["parent_id"] and item["parent_id"] in epics:
            out[item["id"]] = item["parent_id"]
        else:
            out[item["id"]] = item["id"]
    return out


def live_lease_on(conn, root_id: str):
    """The unreleased lease row on `root_id`, dead or alive. None if there is
    none.

    "Live" here means unreleased, which is not the same as not-expired — an
    expired lease is still the row occupying the root until someone reclaims it.
    Callers that care about the difference ask `is_dead`.
    """
    return conn.execute(
        "SELECT * FROM issue_leases WHERE root_id = ? AND released_at IS NULL",
        (root_id,)).fetchone()


MAX_NESTING_DEPTH = 100
MAX_SUBTREE_NODES = 10_000


class SubtreeTooLarge(RuntimeError):
    """A subtree walk hit its node cap before exhausting the tree.

    `subtree_ids` raises this rather than returning the partial list it built. It
    computes the REACH OF A FENCE, and a fence that under-reports its own extent
    fails open: the uncounted items stay claimable while their epic is held,
    which is the collision the lease exists to prevent, arriving with no signal
    that anything went wrong. A caller meeting this refuses the checkout and
    surfaces the anomaly, and both need it visible.
    """


class LeaseConflict(Exception):
    """A live lease already fences what this checkout would take.

    Carries the blocking lease rather than a message, because the caller has to
    render a refusal that NAMES the holder and the expiry. A bare "no" leaves an
    orchestrator two options, block or retry, and both waste the run.

    `blocks_by` distinguishes the two directions a fence arrives from, which need
    different responses: `ancestor` means someone above already covers this item,
    so there is nothing smaller to take, while `descendant` means the caller
    reached for an epic somebody is working inside, and a different branch may
    well succeed.
    """

    def __init__(self, row, *, blocks_by: str):
        self.lease = row
        self.blocks_by = blocks_by
        super().__init__(f"lease {row['id']} on {row['root_id']} held by "
                         f"{row['holder']}")


def ancestor_ids(conn, item_id: str, *, max_depth: int | None = None):
    """The chain above `item_id`, nearest first, excluding itself.

    Stops on a missing parent, on a cycle, or at the depth cap, returning what it
    walked rather than raising — a caller wanting the immediate parent takes the
    first element.
    """
    # READ AT CALL TIME, not bound as a default. A default argument is evaluated
    # once at definition, so a bound expressed that way cannot be adjusted without
    # editing the signature — and cannot be exercised by a test at all.
    max_depth = MAX_NESTING_DEPTH if max_depth is None else max_depth
    out, seen = [], {item_id}
    cur = conn.execute("SELECT parent_id FROM issue_items WHERE id = ?",
                       (item_id,)).fetchone()
    while cur is not None and cur["parent_id"] and len(out) < max_depth:
        pid = cur["parent_id"]
        if pid in seen:      # a cycle in stored data — stop rather than spin
            break
        out.append(pid)
        seen.add(pid)
        cur = conn.execute("SELECT parent_id FROM issue_items WHERE id = ?",
                           (pid,)).fetchone()
    return out


def subtree_ids(conn, root_id: str, *, max_nodes: int | None = None):
    """Every descendant of `root_id`, excluding itself, breadth-first.

    This is the downward reach of the epic lease: holding a lease on epic F
    fences everything this returns, at any depth.

    The visited set terminates the walk, including on a cycle. The node cap is a
    separate runaway backstop, and hitting it raises rather than returning a
    partial answer — see `SubtreeTooLarge`. The cap sits far above any real tree,
    so raising signals something wrong rather than a limit anyone should meet.
    """
    max_nodes = MAX_SUBTREE_NODES if max_nodes is None else max_nodes
    out, seen, frontier = [], {root_id}, [root_id]
    while frontier:
        rows = conn.execute(
            f"SELECT id FROM issue_items WHERE parent_id IN "
            f"({','.join('?' * len(frontier))})", frontier).fetchall()
        frontier = [r["id"] for r in rows if r["id"] not in seen]
        seen.update(frontier)
        out.extend(frontier)
        if len(out) > max_nodes:
            raise SubtreeTooLarge(
                f"subtree of {root_id} exceeds {max_nodes} nodes; refusing to "
                f"report a partial fence")
    return out


def find_conflict(conn, root_id: str, *, now=None,
                  ignore_holder: str | None = None):
    """The live lease that would collide with taking a lease on `root_id`.

    `ignore_holder` skips that holder's own leases: a lease never refuses its
    own holder (the operator, 2026-09-11: "The idea is that *only* the person/agent with
    the checkout can make the change. Not that *nobody* can make a change.").

    **THE INVARIANT IS THAT NO TWO LIVE LEASES MAY HAVE OVERLAPPING FENCES**, and
    it takes two lookups in opposite directions because a fence reaches downward
    only. Checking one direction is the mistake this docstring exists to prevent.

    UPWARD: a lease on any ancestor already fences `root_id`, because a lease on
    epic F covers F's whole subtree without depth limit. The root itself is
    checked first — a lease there is the common case, and it is an
    ancestor-class conflict for the reader's purposes.

    DOWNWARD: a lease on any DESCENDANT holds part of what this checkout would
    fence. An ancestor-only check would let a caller take epic G while someone
    else holds sub-epic F beneath it, producing two holders whose fences overlap
    — the exact collision the lease exists to prevent, arriving through the one
    direction nobody looked.

    An EXPIRED lease is not a conflict: its holder is provably gone, so any
    authorized caller may reclaim it, which `take` does.

    `subtree_ids` may raise `SubtreeTooLarge`, and this deliberately does not
    catch it: a partial answer would report "no conflict" for a fence whose full
    extent was never enumerated, which fails OPEN.
    """
    conflicts = live_conflicts(conn, root_id, now=now,
                               ignore_holder=ignore_holder)
    return conflicts[0] if conflicts else None


def live_conflicts(conn, root_id: str, *, now=None,
                   ignore_holder: str | None = None) -> list:
    """EVERY live lease overlapping this fence, nearest first, leaving out
    `ignore_holder`'s own when it is given.

    **`find_conflict` returns one, and a caller that needs to know whether ANY
    other party holds part of this subtree must not read that one as the whole
    answer** (reviewer, PR #9). `take` gets away with the single value because it
    loops, breaking one conflict per pass until none remain. A caller that only
    CHECKS has no such loop, and reading the first hit as the complete picture is
    how resolve came to let a caller name whichever holder it had been told about
    and sail past a second, still-live one on a sibling branch.

    Two sibling epics under one parent, held by different sessions, is an
    ordinary shape rather than a corner: nested epics lease themselves, so
    siblings get distinct roots and are checked out independently.

    One walk with two consumers, so the two cannot drift about what overlapping
    means.
    """
    def counts(held) -> bool:
        return (not is_dead(held, now=now)
                and (ignore_holder is None or held["holder"] != ignore_holder))

    out = []
    for rid in [root_id, *ancestor_ids(conn, root_id)]:
        held = live_lease_on(conn, rid)
        if held is not None and counts(held):
            out.append(LeaseConflict(held, blocks_by="ancestor"))
    kids = subtree_ids(conn, root_id)
    if kids:
        rows = conn.execute(
            f"SELECT * FROM issue_leases WHERE released_at IS NULL AND root_id "
            f"IN ({','.join('?' * len(kids))})", kids).fetchall()
        for held in rows:
            if counts(held):
                out.append(LeaseConflict(held, blocks_by="descendant"))
    return out


def _release_row(conn, row, *, reason: str, by: str, note: str | None = None):
    now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    conn.execute(
        "UPDATE issue_leases SET released_at = ?, release_reason = ?, "
        "released_by = ?, note = COALESCE(?, note) WHERE id = ?",
        (now.isoformat(" "), reason, by, note, row["id"]))


def held_by(row, holder: str | None) -> bool:
    """Whether `holder` holds the lease `row`.

    A comparison, not a disclosure. It lives here so a caller that only compares
    holders does not read the holder field itself, which is the token
    `test_every_lease_disclosure_checks_the_roots_product` looks for when it
    finds the functions that disclose a lease to a caller.
    """
    return holder is not None and row["holder"] == holder


def record_write(conn, item, *, holder: str, actor: str, act: str) -> None:
    """Record a lease held for exactly one write: taken and released at once.

    `app._hold_for_write` calls this when no live lease covers `item`, so the
    change it guards still happened under a checkout, as the operator's rule of
    2026-09-11 requires. The row is released in the statement that inserts it,
    so it fences nothing and no live-lease query ever sees it. It rides the
    caller's transaction: committed with the write, or rolled back with it when
    a later check refuses.
    """
    now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None).isoformat(" ")
    conn.execute(
        "INSERT INTO issue_leases (id, root_id, taken_for, holder, taken_at, "
        "heartbeat_at, released_at, release_reason, released_by, note) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (f"lease_{new_ulid()}", root_id_for(conn, item), item["id"], holder, now,
         now, now, LEASE_WRITTEN, actor, act))


def take(conn, item, *, holder: str, actor: str, run_id: str | None = None,
         force: bool = False):
    """Take, refresh or reclaim the lease covering `item`, for `holder`.

    Four outcomes, distinguished because the override rate depends on it:

    - **Refresh** — the same holder already holds this root. Bump the timestamps
      and return; a session re-checking-out what it holds is the normal case
      rather than a seizure.
    - **Take** — nothing conflicts, so insert.
    - **Reclaim** — the row on this root is DEAD, no sighting inside the grace or
      past the hard cap. Any authorized caller may take it, recorded as
      `reclaimed` so a routine reclaim does not dilute the override rate.
    - **Override** — `force` breaks a LIVE lease. The caller is responsible for
      having established that it may; the route restricts this to a human, and
      this service has no humans, so nothing here can reach it today.

    Raises `LeaseConflict` when a live lease blocks and `force` is unset.
    """
    root_id = root_id_for(conn, item)
    now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)

    # **THE WRITE LOCK IS TAKEN BEFORE THE CONFLICT SCAN, NOT AT THE INSERT**
    # (security-reviewer, PR #7). Without this the scan is a sequence of plain
    # SELECTs holding no lock, and two ordinary concurrent checkouts break the
    # invariant this module exists to hold:
    #
    #   A checks out `top_epic`  — scan finds nothing live in its subtree.
    #   B checks out `deep_task` — root `sub_epic`; scan walks up to `top_epic`
    #                              and finds nothing, because A has not committed
    #                              and a SELECT takes no lock.
    #   Both insert. The partial unique index only rejects a second row on the
    #   SAME root, and these differ — so both succeed, and `top_epic` and
    #   `sub_epic` are held at once by different holders with overlapping fences.
    #
    # `BEGIN IMMEDIATE` acquires the write lock at the first statement rather
    # than at the first write, so the second caller waits and then re-runs its
    # scan against committed state. It is the check-then-act that needs the lock,
    # and protecting only the insert protects the wrong half.
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")

    def _refresh(row):
        conn.execute(
            "UPDATE issue_leases SET heartbeat_at = ?, taken_at = ?, "
            "taken_for = ?, run_id = COALESCE(?, run_id) WHERE id = ?",
            # `taken_at` moves too: a LIVE holder re-checking out resets the hard
            # cap. That is the one path that may, which is what bounds a misnamed
            # heartbeat to the cap.
            (now.isoformat(" "), now.isoformat(" "), item["id"], run_id,
             row["id"]))
        return conn.execute("SELECT * FROM issue_leases WHERE id = ?",
                            (row["id"],)).fetchone()

    held = live_lease_on(conn, root_id)
    if held is not None and held["holder"] == holder and not is_dead(held, now=now):
        # THE HOLDER RE-CHECKING OUT ITS OWN LIVE LEASE. Returns here rather than
        # falling through, because the insert below would add a second live row
        # on this root, which the partial unique index refuses. The walk itself
        # skips the holder's own leases.
        #
        # An AGED-OUT lease of our own falls through deliberately. It is not a
        # conflict to anyone, so the walk finds whatever took an ancestor while we
        # were gone and refuses on that — the check a blind refresh would skip.
        return _refresh(held)

    # BREAK EVERY CONFLICT, NOT THE FIRST. `find_conflict` returns one lease and a
    # subtree can hold several: taking epic G while two sub-epics beneath it are
    # held is one call meeting two fences. Breaking one and inserting anyway
    # would leave exactly the overlap this module exists to prevent, and it would
    # look like it worked. Each release removes one conflict, so this terminates.
    #
    # **THE HOLDER'S OWN LEASES ARE NOT CONFLICTS** (the operator, 2026-09-11: "*only*
    # the person/agent with the checkout can make the change. Not that *nobody*
    # can make a change"). A holder of `top_epic` checking out `sub_epic` beneath
    # it was refused by its own lease until then. Its two leases now overlap,
    # which blocks nobody else: every other holder still meets both of them.
    while (conflict := find_conflict(conn, root_id, now=now,
                                     ignore_holder=holder)) is not None:
        if not force:
            raise conflict
        _release_row(conn, conflict.lease, reason=LEASE_OVERRIDDEN, by=actor,
                     note=f"live lease broken by {actor} to check out {item['id']}")

    held = live_lease_on(conn, root_id)
    if held is not None:
        if held["holder"] == holder:
            # Our own aged-out lease with the fence now clear. Refresh rather
            # than reclaim: reclaiming from yourself would count against the
            # reclaim rate for nothing.
            return _refresh(held)
        _release_row(conn, held, reason=LEASE_RECLAIMED, by=actor,
                     note=f"holder {held['holder']} last seen "
                          f"{held['heartbeat_at']}")

    lease_id = f"lease_{new_ulid()}"
    try:
        conn.execute(
            "INSERT INTO issue_leases (id, root_id, taken_for, holder, run_id, "
            "taken_at, heartbeat_at) VALUES (?,?,?,?,?,?,?)",
            (lease_id, root_id, item["id"], holder, run_id, now.isoformat(" "),
             now.isoformat(" ")))
    except sqlite3.IntegrityError:
        # LOST THE INSERT RACE against a concurrent checkout of the same root.
        # With the write lock held from the scan this is now the narrow case the
        # index exists for rather than the common one, and it stays handled.
        #
        # NO SAVEPOINT, where Orient wraps this in one, and the divergence is
        # deliberate rather than an omission (reviewer, PR #7). Orient needs
        # `begin_nested` so a lost race does not roll back work the caller did
        # earlier in the same session. SQLite's default `ON CONFLICT ABORT` rolls
        # back only the failing statement, so any release or reclaim written
        # earlier in this same `take` survives for the caller's commit — the
        # guarantee Orient's savepoint buys, supplied here by the engine.
        #
        # Refuse in the same terms the loser would have got a millisecond
        # earlier, naming the holder that won.
        winner = live_lease_on(conn, root_id)
        if winner is None:
            raise
        raise LeaseConflict(winner, blocks_by="ancestor") from None
    return conn.execute("SELECT * FROM issue_leases WHERE id = ?",
                        (lease_id,)).fetchone()


def record_refusal(conn, item, conflict, *, requester: str, actor: str) -> None:
    """Write down a checkout the fence turned away.

    **THIS IS THE ONLY MOMENT THE FACT EXISTS.** A refusal moves no state, creates
    no lease and appends no event, so a refusal not written here is gone and no
    later query recovers it. That is why this instrumentation could not be a query
    over what the store already keeps.

    `requester` is who was turned away, passed in rather than read off the
    blocking lease — that lease knows who HELD the subtree, the other party, and
    recording the holder in both fields would make every refusal look
    self-inflicted.

    Called from the route rather than inside `take`, because `take` raises the
    same exception for a caller about to retry with `force`. A human override is a
    different fact from a collision the fence caught, and counting them together
    would inflate the number being watched.
    """
    held = conflict.lease
    conn.execute(
        "INSERT INTO lease_refusals (id, at, requested_item_id, product, "
        "requester, actor, blocking_lease_id, blocking_root_id, "
        "blocking_holder, blocks_by) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (f"ref_{new_ulid()}",
         _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None).isoformat(" "),
         item["id"], item["product"], requester, actor, held["id"],
         held["root_id"], held["holder"], conflict.blocks_by))


def claim_status(conn, item, *, now=None):
    """`(expired, expires_at)` for `item`'s claim, from one lease lookup.

    **ONE ACT MUST NOT HAVE TWO EXPIRIES.** Checking out an item claims it and
    leases its subtree in the same call, so a claim outliving its lease, or a
    lease outliving its claim, is one hold half-expired. Both are real failures: a
    claim expiring first hands the item to a drain the fence then refuses, and a
    lease expiring first opens the siblings while the item itself still reads
    claimed.

    So where a lease exists it is the authority and the 24-hour TTL is the
    fallback for rows predating checkout. Only a lease taken FOR this item counts
    — one taken for a sibling fences this item without claiming it, and that
    refusal comes from the fence at checkout rather than from a claim it never
    had.
    """
    if item["state"] != store.ITEM_STATE_CLAIMED:
        return False, None
    if not item["claimed_at"]:
        # Claimed with no timestamp is corrupt, and stranding it is the worse
        # failure.
        return True, None
    held = live_lease_on(conn, root_id_for(conn, item))
    if held is not None and held["taken_for"] == item["id"]:
        return is_dead(held, now=now), expires_at(held["taken_at"],
                                                  held["heartbeat_at"])
    claimed = _parse(item["claimed_at"])
    if claimed is None:
        return True, None
    ends = claimed + CLAIM_TTL
    return (now or _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)) >= ends, ends


def claimable_clause(*, now=None) -> tuple[str, list]:
    """The SQL half of `claim_status`, for the `state=claimable` listing.

    **Kept beside the Python version deliberately**, the way Orient keeps
    `claimable_clause` beside `claim_is_expired`: the two answer one question in
    two languages, and separating them across modules is how they drift into
    disagreeing. A drain lists `claimable` and then checks out what it finds; if
    the list says takeable and the endpoint says held, every pass burns a refusal.

    **A listing filter, not the fence.** An item whose own claim has lapsed is
    listed even when a sibling's lease still covers its subtree, and the checkout
    refuses it there. That is deliberate and it is Orient's division too: a filter
    on one query is a convenience, and the endpoint is the rule.

    **Where a lease exists it is the authority**, and the 24-hour TTL is the
    fallback for rows predating checkout — the same ordering `claim_status`
    states, because one act must not have two expiries.

    Timestamps are compared as text. Every writer here stores
    `datetime.isoformat(" ")` and every row migrated out of Orient was written by
    SQLAlchemy in that same shape, so the comparison is the one Orient's own query
    makes; ISO-8601 with a space separator orders lexicographically, which is what
    makes that safe rather than merely convenient.
    """
    now = now or _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    live = ("SELECT 1 FROM issue_leases l WHERE l.taken_for = issue_items.id "
            "AND l.released_at IS NULL")
    sql = (f"(issue_items.claimed_at IS NULL"
           f" OR EXISTS ({live} AND (l.heartbeat_at <= ? OR l.taken_at <= ?))"
           f" OR (NOT EXISTS ({live}) AND issue_items.claimed_at <= ?))")
    return sql, [(now - grace()).isoformat(" "),
                 (now - max_lease()).isoformat(" "),
                 (now - CLAIM_TTL).isoformat(" ")]


def claim_statuses(conn, rows, *, now=None) -> dict:
    """`{item_id: (expired, expires_at)}` for a whole page, in ONE query.

    **The N+1 this exists to prevent is measured rather than theoretical.**
    Orient's serialiser issued two identical SELECTs per claimed row on a 200-row
    listing before its own PR #158 collapsed them to one; one per row is still one
    per row. A page's claimed items usually share very few epics, so the lease
    rows they need come back in a single `IN (...)` and the arithmetic happens in
    Python.

    Same answers as `claim_status` row by row, and the tests assert that rather
    than trusting it — two functions computing one rule is exactly the shape that
    drifts.
    """
    claimed = [r for r in rows if r["state"] == store.ITEM_STATE_CLAIMED]
    if not claimed:
        return {}
    roots_by_item = root_ids_for(conn, claimed)
    roots = sorted(set(roots_by_item.values()))
    marks = ",".join("?" * len(roots))
    held = {row["root_id"]: row for row in conn.execute(
        f"SELECT * FROM issue_leases WHERE root_id IN ({marks}) "
        f"AND released_at IS NULL", roots).fetchall()}
    out = {}
    for item in claimed:
        if not item["claimed_at"]:
            # Claimed with no timestamp is corrupt, and stranding it is the worse
            # failure. `claim_status` says the same.
            out[item["id"]] = (True, None)
            continue
        lease_row = held.get(roots_by_item[item["id"]])
        if lease_row is not None and lease_row["taken_for"] == item["id"]:
            out[item["id"]] = (is_dead(lease_row, now=now),
                               expires_at(lease_row["taken_at"],
                                          lease_row["heartbeat_at"]))
            continue
        claimed_at = _parse(item["claimed_at"])
        if claimed_at is None:
            out[item["id"]] = (True, None)
            continue
        ends = claimed_at + CLAIM_TTL
        stamp = now or _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
        out[item["id"]] = (stamp >= ends, ends)
    return out


def live_lease_rows(conn, root_ids) -> dict:
    """Unreleased lease rows for a batch of roots, dead or alive, keyed by
    `root_id`. One query for the whole batch, the same shape `claim_statuses`
    uses for a page of claimed items — but keyed on the roots directly rather
    than filtered down to rows already in state `claimed` first.

    **NEEDED BECAUSE A LEASE CAN COVER A ROOT WHOSE OWN ROW NEVER TURNS
    `claimed`.** An epic leases itself (`root_id_for`), so a checked-out child
    leaves the epic `open` while the lease still fences it, and a blocked
    epic's own checkout takes the lease without the claim (`app.py`
    `/checkout`, platform/decisions/0079). `claim_statuses` answers neither
    case: it only ever looks at rows already in state `claimed`.
    """
    root_ids = sorted(set(root_ids))
    if not root_ids:
        return {}
    marks = ",".join("?" * len(root_ids))
    return {row["root_id"]: row for row in conn.execute(
        f"SELECT * FROM issue_leases WHERE root_id IN ({marks}) "
        f"AND released_at IS NULL", root_ids).fetchall()}


def course_position(entries, items: dict, entry_leases: dict, *, now=None) -> dict:
    """A Course's progress over its own entries: counts, and — in Course
    order — which entry ids are reached, in flight, and remaining.

    **PURE, over data a caller already loaded.** `entries` is the Course's
    own ordered id list, `items` maps an id to its resolved row (a missing
    key means the id could not be resolved, in scope or otherwise), and
    `entry_leases` maps an entry id to its own unreleased lease row, from
    `live_lease_rows`. No query here, and no query added by calling it — a
    caller batches both lookups once, up front, the same way it already
    batches item and prerequisite resolution.

    REACHED: the entry's own state is a terminal resolution
    (`store.ITEM_TERMINAL`) — an epic with zero children can be complete, so
    this never looks at descendants.

    IN FLIGHT (not reached, and either): the entry's own state is not
    `open` (claimed or pr-open, whether or not that claim has since
    expired); or an unreleased, non-dead lease is rooted on it. Several
    entries may be in flight at once.

    REMAINING: every other entry — open, unclaimed, uncovered.

    MISSING is counted but placed in none of the three lists: an id nobody
    can resolve is not nothing and is not yet reached.

    Every entry lands in exactly one of reached, in flight, remaining, or
    missing. Nothing here ranks what can start next — the order `entries`
    arrives in is the Course's plan, not a schedule.
    """
    reached_ids, in_flight_ids, remaining_ids = [], [], []
    missing = 0
    for eid in entries:
        item = items.get(eid)
        if item is None:
            missing += 1
            continue
        if item["state"] in store.ITEM_TERMINAL:
            reached_ids.append(eid)
            continue
        if item["state"] != store.ITEM_STATE_OPEN:
            in_flight_ids.append(eid)
            continue
        lease_row = entry_leases.get(eid)
        if lease_row is not None and not is_dead(lease_row, now=now):
            in_flight_ids.append(eid)
        else:
            remaining_ids.append(eid)
    return {
        "entries": len(entries),
        "reached": len(reached_ids),
        "reached_ids": reached_ids,
        "in_flight": len(in_flight_ids),
        "in_flight_ids": in_flight_ids,
        "remaining": len(remaining_ids),
        "remaining_ids": remaining_ids,
        "missing": missing,
    }


def waypoint_course_position(waypoints: list[dict]) -> dict:
    """A Course's synthetic waypoints, placed into the same three groups
    `course_position` places its epic entries into — "epic" and "waypoint"
    are defined once, in this module's own docstring above.

    **PURE, over `store.waypoint_progress`'s own output, one dict per
    waypoint plus its `id`.** No query here, same rule as `course_position`.

    REACHED: `reached` is already true — every member terminal.

    REMAINING: not reached, no member missing, no member terminal, and every
    resolved member's state is the literal `open` — nothing has touched any
    of them yet. **No lease walk**, unlike `course_position`'s epic entries:
    a member here can be any item, not only an epic, so there is no single
    rule for which id a checkout would have leased, and the specification
    this computes asks only for "members and their states" — own state is
    what that means.

    IN FLIGHT: everything else not reached and not remaining — some member
    has progressed, or one could not be resolved at all. A missing member
    can never be `open`, so it always lands a waypoint here rather than in
    remaining, which is the "reported, not dropped" rule this function's
    share of it.
    """
    reached_ids, in_flight_ids, remaining_ids = [], [], []
    for w in waypoints:
        wid = w["id"]
        if w["reached"]:
            reached_ids.append(wid)
        elif (w["missing"] == 0 and w["terminal"] == 0
              and set(w["by_state"]) <= {store.ITEM_STATE_OPEN}):
            remaining_ids.append(wid)
        else:
            in_flight_ids.append(wid)
    return {
        "total": len(waypoints),
        "reached": len(reached_ids),
        "reached_ids": reached_ids,
        "in_flight": len(in_flight_ids),
        "in_flight_ids": in_flight_ids,
        "remaining": len(remaining_ids),
        "remaining_ids": remaining_ids,
    }


def release_for(conn, item, *, holder: str, actor: str,
                reason: str = LEASE_RELEASED, force: bool = False):
    """Release the lease taken FOR `item`, if one is held. None otherwise.

    **Scoped to `taken_for`, not merely to the root**, and the distinction is what
    lets a session hold an epic across several of its items. Releasing item A when
    the lease was taken for A gives the epic back; releasing A when the lease was
    taken for its sibling B leaves the fence where it is, because the holder is
    still working inside it.

    Also scoped to the holder: a session hands back what it holds. `force`
    overrides that for a human clearing a stuck fence, and for the resolve path
    once its own caller has established the lease is dead or is the caller's —
    which it now does. It used to force unconditionally on the strength of
    "a terminal state ends the lease regardless of who is asking", and that is
    circular: the work is live until the resolve makes it terminal.

    Returning None when there is nothing to release is deliberate — an item with
    no lease is the ordinary case for every row predating checkout, and raising
    would make release fail on exactly the items it always worked on.

    **AN UNCLOSED RESIDUAL, RECORDED RATHER THAN IMPLIED** (security-reviewer,
    PR #6; filed as `issue_01KZM1D4N2GRHZHCVKJR2SBDPB`). `holder` is a
    self-declared name, so a caller that reads the current holder off the item,
    the lease listing or this endpoint's own 409 body can name it and drop a LIVE
    lease without `force`. ADR-0026 calls breaking a live lease the reserved act
    and puts the gate on the act rather than the endpoint, so this door reaches
    something the `force` refusal is meant to close.

    It is not a divergence: Orient does exactly this today, passing the item's own
    `claimed_by` as `holder` so its guard compares the string the caller just
    matched. This port is as permissive as the service it replaces and no more.

    It is also not the residual ADR-0026 already bounded. That one is the misnamed
    HEARTBEAT, bounded by the hard cap because a heartbeat moves last-seen and
    never taken-at. Release has no such bound: the fence over the whole subtree
    drops immediately, while the real holder is mid-task.

    No check available today closes it, which is why it is recorded instead of
    patched. Every session presents the same token, so "session-1 releasing its
    own claim" — the primary exit path — is indistinguishable from someone else
    naming session-1. Phase 5's per-session tokens make the holder verifiable and
    are the structural fix.

    **THIS MUST BE RESOLVED BEFORE `LIBRARY_WRITES_ENABLED=1`, not merely before
    Phase 5**, and the distinction is the finding rather than pedantry
    (security-reviewer, PR #6). The only thing between this code and production
    exposure is that flag, which belongs to the vault migration and was designed
    around a different risk — the "guarded only upstream, one refactor from
    moving" shape. Whoever flips it to complete the write cutover would otherwise
    carry this gap in without anything forcing them to re-litigate it. The
    sibling `fences` gap in this same module already states its tie to the
    cutover; this one had said only "Phase 5", so the two carried different
    guarantees for no reason.

    Closing it costs something real either way, which is why the disposition is
    the operator's rather than mine: refusing to drop a live lease here means a clean exit
    strands the fence until grace, delaying a DIFFERENT session's pickup of the
    subtree by up to four hours, on the fleet's main coordination primitive.
    Raised as `dec_01KZM23Y3PSKBRXF69H7Q3C6G4`.
    """
    held = live_lease_on(conn, root_id_for(conn, item))
    if held is None or held["taken_for"] != item["id"]:
        return None
    if not force and held["holder"] != holder:
        return None
    now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    conn.execute(
        "UPDATE issue_leases SET released_at = ?, release_reason = ?, "
        "released_by = ? WHERE id = ?",
        (now.isoformat(" "), reason, actor, held["id"]))
    return conn.execute("SELECT * FROM issue_leases WHERE id = ?",
                        (held["id"],)).fetchone()


def touch(conn, item, holder: str):
    """Record a sighting of `holder` on the lease covering `item`, if it holds
    one.

    Every lease-bearing action a holder takes is a heartbeat, which keeps the
    grace period honest without asking workers to run a keepalive loop. A caller
    holding nothing gets None and no error: acting on an unfenced item is legal,
    and turning it into a failure would make the lease a gate on work rather than
    an arbitrator of it.

    **`holder` IS A NAME, NOT A CREDENTIAL, AND THE HARD CAP IS WHAT MAKES THAT
    SAFE** (security-reviewer on Orient's PR #158). Every session presents the
    same token, so the server cannot tell a daytime co from the overnight drain —
    which is the whole reason callers name themselves. A caller that reads a
    holder's name off a lease listing can therefore heartbeat a lease it does not
    hold.

    That is bounded rather than unbounded, deliberately. This refreshes
    `heartbeat_at` and NEVER `taken_at`, so no number of heartbeats pushes a lease
    past its hard cap; only a re-checkout resets that, and a re-checkout by a
    non-holder is refused by the fence. The worst a misnamed heartbeat can do is
    hold a subtree to the cap, which is exactly the case the cap exists for.

    Binding this to the token identity instead would not fix it and would break
    the feature: every session would share one identity, so any session could
    heartbeat any other's lease AND no session could distinguish its own.
    """
    held = live_lease_on(conn, root_id_for(conn, item))
    if held is None or held["holder"] != holder or is_dead(held):
        return None
    now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    conn.execute("UPDATE issue_leases SET heartbeat_at = ? WHERE id = ?",
                 (now.isoformat(" "), held["id"]))
    return conn.execute("SELECT * FROM issue_leases WHERE id = ?",
                        (held["id"],)).fetchone()


def lease_dict(row, *, conn=None) -> dict:
    """Serialise a lease.

    **`fences` NOW ARRIVES, closing the gap PR #6 named.** That slice had no
    subtree walk, so a heartbeat returned no `fences` where Orient's does — a
    divergence recorded there rather than left for a client to discover after the
    cutover. `subtree_ids` came with the fence, so the field costs nothing now.

    Passing `conn` is what asks for it, matching Orient: enumerating a subtree is
    a walk, and a listing rendering many rows should not pay for it per row.

    A subtree too large to enumerate returns `fences: None` rather than raising.
    Here the extent is descriptive — the caller is being told what it holds — and
    failing a heartbeat over it would strand a live holder. `find_conflict` makes
    the opposite call on the same walk, and deliberately: there the extent decides
    whether a fence is safe to take, so a partial answer fails open.
    """
    out = {
        "id": row["id"],
        "root_id": row["root_id"],
        "root_url": f"/engineering/issues/{row['root_id']}",
        "taken_for": row["taken_for"],
        "holder": row["holder"],
        "run_id": row["run_id"],
        "taken_at": store._iso(row["taken_at"]),
        "heartbeat_at": store._iso(row["heartbeat_at"]),
        # DERIVED rather than stored: the grace period is a tunable that may
        # change at any time, so a stored expiry would be a snapshot of a policy
        # that has since moved.
        "expires_at": store._iso(expires_at(row["taken_at"],
                                            row["heartbeat_at"])),
        "dead": is_dead(row),
        "released_at": store._iso(row["released_at"]),
        "release_reason": row["release_reason"],
        "released_by": row["released_by"],
        "note": row["note"],
    }
    if conn is not None:
        try:
            out["fences"] = [row["root_id"], *subtree_ids(conn, row["root_id"])]
        except SubtreeTooLarge:
            out["fences"] = None
    return out


def release_claim(conn, item_id: str, *, actor: str, note: str | None,
                  prev_owner: str | None):
    """Hand a claim back: claimed → open, with the state event.

    Written here beside the lease release so the two stay one transaction at the
    call site. A claim returned while its fence stays up is the worst of both —
    the item returns to the queue, a drain picks it up, and the checkout is
    refused by a lease its own previous holder walked away from.
    """
    now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    conn.execute(
        "UPDATE issue_items SET state = ?, claimed_by = NULL, claimed_at = NULL "
        "WHERE id = ?", (store.ITEM_STATE_OPEN, item_id))
    conn.execute(
        "INSERT INTO issue_events (item_id, actor, from_state, to_state, at, note) "
        "VALUES (?,?,?,?,?,?)",
        (item_id, actor, store.ITEM_STATE_CLAIMED, store.ITEM_STATE_OPEN,
         now.isoformat(" "), note))


def sqlite_error() -> type:
    """Exposed so a route can catch a write failure without importing sqlite3."""
    return sqlite3.Error


def refusal_dict(row, *, blocker_visible: bool = False) -> dict:
    """One recorded collision, in protocol shape.

    Every column is served. A refusal row exists only to be read by somebody
    working out who collided with whom, so withholding a field here would leave
    the reader with the same "N refusals happened" the aggregate already gives.

    `blocks_by` says WHICH DIRECTION the fence fired — whether an ancestor held
    the branch above the requested item, or a descendant held something below it.
    The two mean different things about how work was sliced, which is why
    `librarian_metrics` breaks its count down by the same field.
    """
    out = {
        "id": row["id"],
        "at": row["at"],
        "requested_item_id": row["requested_item_id"],
        "product": row["product"],
        "requester": row["requester"],
        "actor": row["actor"],
        "blocks_by": row["blocks_by"],
    }
    # **WHO BLOCKED IT IS WITHHELD WHEN THE BLOCKING SUBTREE IS OUT OF SCOPE.**
    # `blocks_by` and the requested item stay: the caller already knows its own
    # request was refused and in which direction, which is the fact it needs to
    # act. The blocking lease's holder, root and lease id are the other crew's,
    # and the caller has proved scope only over the item it named.
    #
    # `blocked: true` says a blocker exists without naming it, so a withheld row
    # reads as "something held this" rather than as "nothing did" — the same
    # distinction `missing: true` draws on a Course entry.
    if blocker_visible:
        out["blocking_lease_id"] = row["blocking_lease_id"]
        out["blocking_root_id"] = row["blocking_root_id"]
        out["blocking_holder"] = row["blocking_holder"]
    else:
        out["blocked"] = True
    return out
