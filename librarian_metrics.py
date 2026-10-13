"""ADR-0019's instrumentation — the numbers that decide whether the librarian
ever needs a model.

Ported from `convivy-orient/librarian_metrics.py` as a data migration (§4):
the definitions carry across unchanged, re-expressed as raw SQL over the same
two tables (`issue_items`, `lease_refusals`) rather than SQLAlchemy over the
ORM models the shadow does not share.

**TWO OF THE FOUR ARE NOT MEASURABLE YET, AND THIS MODULE SAYS SO RATHER THAN
REPORTING A ZERO.** A metric that reads 0 because its input does not exist
looks exactly like a metric reading 0 because the system is healthy, and
ADR-0019 exists to stop that class of invisible failure. See `_bundle_metrics`.
"""
from __future__ import annotations

import datetime as _dt

import store

# The default reporting window. Long enough that a low-volume product shows a
# non-trivial denominator, short enough that a rate reflects how the system
# works now rather than how it worked a quarter ago.
DEFAULT_WINDOW_DAYS = 30

# How long an item needs before its cohort's duplicate rate means anything. A
# duplicate is discovered when somebody files the second one and later
# resolves it, so a cohort filed yesterday has had no chance to accumulate
# any — its rate is near zero by construction, and reading that as health is
# the exact misreading this module exists to prevent.
MATURITY_DAYS = 7

# Was `ISSUE_ACTIVE` in Orient's `control_db`, kept as its own name here
# rather than folded into the call sites below even though it is now
# identical to `store.ITEM_ACTIVE` — `awaiting-decision`, the one state that
# used to make the two definitions differ, retired with the decision plane
# (issue_01M0ASMCB4F68PF3NJG7BV7K6C). This module answers "still open,
# whatever it's waiting on", which happens to be the same states today as
# "a work route may still move this item," but the two questions remain
# distinct even though their answers currently coincide.
STILL_ACTIVE = store.ITEM_ACTIVE


def duplicate_filing_rate(conn, *, window_days: int = DEFAULT_WINDOW_DAYS,
                          product: str | None = None,
                          now: _dt.datetime | None = None) -> dict:
    """The fraction of work items filed in the window that were later resolved
    as duplicates of another — adjacency failing at the job it exists for.

    **COHORTED BY FILING DATE, NOT BY RESOLUTION DATE**, and the difference
    matters. Counting resolutions in a window against filings in the same
    window divides one population by a different one, so a burst of cleanup
    makes the rate spike without anything having been filed badly. Cohorting
    asks a single question of a single set: of the things filed then, how many
    turned out to be duplicates.

    The cost is that a cohort matures. Recent filings have not had time to be
    discovered, so their rate is low by construction; the response carries
    `mature` and the unresolved count so a reader can see how much of the
    cohort is still undecided rather than inferring health from a small
    number.
    """
    now = now or _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    since = now - _dt.timedelta(days=window_days)
    where, params = "WHERE created_at >= ?", [since.isoformat(" ")]
    if product:
        where += " AND product = ?"
        params.append(product)
    filed = conn.execute(
        f"SELECT COUNT(*) FROM issue_items {where}", params).fetchone()[0]
    dupes = conn.execute(
        f"SELECT COUNT(*) FROM issue_items {where} AND resolution = ?",
        [*params, "duplicate"]).fetchone()[0]
    marks = ",".join("?" * len(STILL_ACTIVE))
    unresolved = conn.execute(
        f"SELECT COUNT(*) FROM issue_items {where} AND state IN ({marks})",
        [*params, *sorted(STILL_ACTIVE)]).fetchone()[0]
    return {
        "available": True,
        "window_days": window_days,
        "filed": filed,
        "resolved_duplicate": dupes,
        # None rather than 0.0 on an empty cohort: no filings means no rate,
        # and 0.0 would read as "nothing was duplicated" from a window where
        # nothing happened at all.
        "rate": round(dupes / filed, 4) if filed else None,
        "still_unresolved": unresolved,
        "mature": (now - since).days >= MATURITY_DAYS and filed > 0,
        "note": (f"cohorted by filing date; items filed in the last "
                 f"{MATURITY_DAYS} days have had little chance to be found "
                 f"duplicate, so a short window under-reports. {unresolved} "
                 f"of {filed} are still unresolved."),
    }


def refusals(conn, *, window_days: int = DEFAULT_WINDOW_DAYS,
            product: str | None = None,
            now: _dt.datetime | None = None) -> dict:
    """Collisions the epic lease CAUGHT, which ADR-0019 asks for beside the
    duplicate rate: "a refused sibling claim is a collision the fence caught,
    where a duplicate filing is one it did not."

    Read the two together or neither means much. Refusals alone could mean the
    fence is working or that epics are sized so coarsely that everything
    collides; duplicates alone measure only the failures, so the mechanism
    gets judged entirely on what it missed.

    **A ZERO HERE IS AMBIGUOUS UNTIL THE EPIC MODEL IS USED**, and the
    response says so. Nothing can be refused for a fence reason while there
    are no epics: a correction leases only itself, so its collisions
    surface as `already claimed` on the ordinary claim path rather than as a
    fence refusal.
    """
    now = now or _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    since = now - _dt.timedelta(days=window_days)
    where, params = "WHERE at >= ?", [since.isoformat(" ")]
    if product:
        where += " AND product = ?"
        params.append(product)
    rows = conn.execute(
        f"SELECT blocks_by, requester, blocking_root_id FROM lease_refusals "
        f"{where}", params).fetchall()
    by_direction: dict[str, int] = {}
    for r in rows:
        by_direction[r["blocks_by"]] = by_direction.get(r["blocks_by"], 0) + 1
    # SCOPED LIKE EVERYTHING ELSE IN THIS RESPONSE. It qualifies a zero, which
    # made it feel like context rather than data — and an unscoped total tells
    # a single-product token how many epics exist elsewhere.
    epic_where, epic_params = "WHERE type = ?", [store.ITEM_TYPE_EPIC]
    if product:
        epic_where += " AND product = ?"
        epic_params.append(product)
    epics = conn.execute(
        f"SELECT COUNT(*) FROM issue_items {epic_where}",
        epic_params).fetchone()[0]
    return {
        "available": True,
        "window_days": window_days,
        "refusals": len(rows),
        # Kept apart because they say different things about epic sizing: an
        # ancestor block means someone above holds the whole branch, a
        # descendant block means the caller reached for an epic somebody is
        # working inside.
        "by_direction": by_direction,
        "distinct_requesters": len({r["requester"] for r in rows}),
        "distinct_subtrees": len({r["blocking_root_id"] for r in rows}),
        "epics_in_store": epics,
        "note": ("a fence refusal requires an epic to fence: with 0 epics a "
                 "correction leases only itself, so collisions appear "
                 "as 'already claimed' rather than here. Read a zero against "
                 "epics_in_store before reading it as 'no collisions'."
                 if not epics else
                 "collisions the fence caught; read beside the duplicate "
                 "rate, which counts the ones it did not"),
    }


def _bundle_metrics() -> dict:
    """Bundle miss rate and bundle waste — NOT MEASURABLE YET, with the
    evidence.

    **Reported rather than omitted, and reported as unavailable rather than
    as zero.** ADR-0019 makes these two a trigger for building a
    model-driven librarian. A trigger that silently reads 0 because nothing
    feeds it is the owner problem in a different costume — which is the
    failure that ADR names and this module exists to avoid.
    """
    return {
        "bundle_miss_rate": {
            "available": False,
            "reason": ("the issued bundle holds agent and skill names while "
                       "runtime retrievals are KB document ids — measured "
                       "2026-08-05 across 12,515 runs, 75 distinct names "
                       "against 191 distinct ids with an intersection of "
                       "zero, so a set difference reports 100% by "
                       "construction"),
            "measurable_when": ("a bundle contains documents — Phase 5's "
                                "token protocol"),
        },
        "bundle_waste": {
            "available": False,
            "reason": ("nothing records a read: individuated agents and "
                       "skills are consumed off disk by Claude Code, which "
                       "emits no signal this platform captures, so an "
                       "unread skill is indistinguishable from a heavily "
                       "used one"),
            "measurable_when": ("the Library issues and redeems the bundle "
                                "(Phase 5), or a read signal is captured "
                                "per individuated path"),
        },
    }


def snapshot(conn, *, window_days: int = DEFAULT_WINDOW_DAYS,
            product: str | None = None, now: _dt.datetime | None = None) -> dict:
    """All of ADR-0019's measurements at once, each carrying its own
    availability.

    Returned as one object because the ADR's trigger reads them together —
    "build the model-driven librarian when miss rate or duplicate rate stays
    materially above target after the rule set has been tuned" — and a reader
    who has to assemble them from separate calls is a reader who will use
    whichever one is handy.
    """
    return {
        "duplicate_filing_rate": duplicate_filing_rate(
            conn, window_days=window_days, product=product, now=now),
        "lease_refusals": refusals(
            conn, window_days=window_days, product=product, now=now),
        **_bundle_metrics(),
        "adr": "platform/decisions/0019-the-librarian-is-deterministic-until-proven-otherwise",
    }
