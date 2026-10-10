"""The call slip — the credential `POST /items/{id}/checkout` mints (ADR-0062).

issue_01M12NE15SQVPS88PV58H1SS1P, Phase 5. PR #78 built the grant's shape with
no credential behind it, deliberately: a slip a client could believe is
redeemable, with no store and nothing to redeem it at, is a fake credential —
worse than an absent one. This module is the store, so checkout can mint a
real one.

**`claim_owner_label` is a caller-supplied coordination label, not an
authenticated identity — do not use it for ADR-0062's actor cross-check at
redemption.** It is populated straight from the `owner` string passed to
`mint`, the same value `_claim_owner` (app.py) calls "a COORDINATION label,
not an authorization principal": any caller holding `item.claim` can set it
to whatever it likes. The column is named `claim_owner_label` rather than
something actor-shaped (an earlier draft called it `issued_to`) precisely so
redemption does not reach for it: it is the only actor-shaped-looking column
this table persists, and ADR-0062 requires redemption to check the actor
named in the credential against an authenticated transport identity.
Reaching for this column for that check would compile, run, and check
nothing. issue_01M12NE13P3FEY3HF7732QX03F, comments 1699 and 1700, records
what the check should read instead and why that choice belongs to that task
rather than this one.

**The slip's expiry is a STORED ABSOLUTE INSTANT, set once at mint and never
extended — deliberately unlike `lease.py`.** `issue_leases` stores no expiry
at all: `lease.expires_at` derives one from `taken_at` and `heartbeat_at`
against a grace tunable, and a heartbeat pushes it out every time the holder
is seen. A slip must not work that way. One issuance admits one run, and
nothing the holder does may extend how long a leaked slip stays redeemable —
which is why this module has no `touch`.

## Deriving `TTL_SECONDS`

The window a slip must survive is mint-at-checkout to redeem-by-the
Individuator. `POST /tokens/redeem` does not exist yet
(issue_01M12NE13P3FEY3HF7732QX03F), so there is no in-system measurement of
that exact gap; the nearest real proxy is dispatch latency in the fleet as it
runs today — the time between an `agentctl spawn` command being issued and
the spawned run's own `started_at`, which is the same kind of cost: queueing
plus process startup before the run can make its first move.

**Measured against the run records under `~/Convivy/agent-runs/`, not chosen
from taste.** For every `run-*/meta.json` and `co-*/meta.json` carrying a
`parent_run` and a `label`, this matched it to the parent's own
`tool_calls.jsonl` for the `agentctl spawn ... --label <label>` line closest
before (but not after) that child's `started_at`, and took the delta. 109
matched pairs on 2026-09-03, the only day in the retained run history whose
records line up this way (2026-09-01 and 2026-09-02 have no
`tool_calls.jsonl` entries this shape can match against):

    min 0.0s   p50 1.0s   p90 3.0s   p95 3.0s   p99 6.0s   max 19.0s (secreview-478)

**Two bounds pull in opposite directions, so the choice sits between them
rather than at either.** Too short, and a slow dispatch arrives holding a
dead slip — a legitimate run fails through no fault of its own. Too long, and
a leaked slip stays redeemable for that whole window, which is the exposure
this expiry exists to bound.

`TTL_SECONDS = 300` is roughly 16x the single worst dispatch observed (19s)
and 50x the p99 (6s) — enough headroom that a real spike well past anything
seen in 109 samples still redeems, while still being minutes rather than
hours: nothing like `lease.DEFAULT_GRACE` (4 hours), because the two clocks
answer different questions. The lease bounds how long a *run* may go unseen;
this bounds how long a *credential in transit* may sit unredeemed before
being treated as leaked. If dispatch latency in the fleet grows — a busier
queue, a slower cold start — this constant is what needs revisiting, and the
measurement above is what to rerun.

**A second trigger, separate from the first: dispatch latency is a proxy, not
the thing itself.** It stands in for mint-to-redeem because that gap has no
endpoint to measure yet, so the headroom above is generous against dispatch
latency specifically and says nothing about the Individuator's actual
redemption latency once one exists. When `POST /tokens/redeem`
(issue_01M12NE13P3FEY3HF7732QX03F) lands, replace this proxy with a direct
measurement of mint-at-checkout to redeem-by-the-Individuator — the real gap
this constant is meant to bound — rather than assuming the proxy is still
representative.
"""
from __future__ import annotations

import datetime as _dt

from store import new_ulid

TTL_SECONDS = 300


class MintFailed(RuntimeError):
    """Raised by `mint` when the insert itself fails.

    A caller reaching this has the property no other write-path failure does:
    the transaction hasn't committed, so nothing is held and nothing needs
    undoing — the checkout is simply retryable. This is what lets
    `_do_checkout`'s caller answer with a typed, retry-safe refusal instead of
    an unhandled 500, matching `store.WritesDisabled` and every other named
    refusal on these routes."""


def _parse(value):
    """A stored timestamp as a naive datetime, matching `lease._parse` — times
    are naive UTC throughout this datastore, and an aware datetime raises on
    comparison against what the column hands back."""
    if isinstance(value, _dt.datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = _dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


def mint(conn, *, item_id: str, product: str, claim_owner_label: str, now=None) -> str:
    """Insert a new call slip for `item_id` and return its id.

    `expires_at` is computed and written HERE, once. Unlike `lease.expires_at`,
    which is deliberately re-derived on every read so a grace-tunable change
    is honoured immediately, a slip's expiry is a promise made at mint time —
    re-deriving it later against a policy that had since moved would let a
    slip's lifetime change after it was handed out, which is exactly the
    extension this module exists to refuse.
    """
    now = now or _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    slip_id = f"slip_{new_ulid()}"
    expires_at = now + _dt.timedelta(seconds=TTL_SECONDS)
    conn.execute(
        "INSERT INTO call_slips (id, item_id, product, claim_owner_label, "
        "issued_at, expires_at) VALUES (?,?,?,?,?,?)",
        (slip_id, item_id, product, claim_owner_label, now.isoformat(" "),
         expires_at.isoformat(" ")))
    return slip_id


def get(conn, slip_id: str):
    """The raw row for `slip_id`, or `None`."""
    return conn.execute("SELECT * FROM call_slips WHERE id = ?",
                        (slip_id,)).fetchone()


def redeem(conn, slip_id: str, *, now=None):
    """Atomically consume `slip_id` and return the fresh row, or `None` if it
    had already been redeemed (or does not exist).

    **A CONDITIONAL UPDATE, NOT READ-THEN-WRITE.** Two concurrent
    redemptions of the same slip must produce exactly one card. Reading
    `redeemed_at`, branching in Python, and writing separately would let
    both readers see `NULL` before either writes — the classic
    check-then-act race, and both would proceed. Folding the check into the
    `WHERE` clause instead means only the update that actually flips a NULL
    row can affect one; the loser's `UPDATE` matches zero rows rather than
    racing to overwrite the winner's.

    `cur.rowcount` is what decides it — 1 means this call won, 0 means a
    concurrent call already had, and the caller (`app._do_redeem`) branches
    on that count exactly as `POST /reference/documents/{id}/checkout`'s
    `BEGIN IMMEDIATE` insert branches on a `sqlite3.IntegrityError`. SQLite
    itself serializes the two `UPDATE`s (a single writer per database,
    `store.connect_rw`'s `busy_timeout` queues the second rather than
    failing it), so there is no window between the check and the write for
    a second call to slip through.
    """
    now = now or _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    cur = conn.execute(
        "UPDATE call_slips SET redeemed_at = ? WHERE id = ? AND redeemed_at IS NULL",
        (now.isoformat(" "), slip_id))
    if cur.rowcount != 1:
        return None
    return get(conn, slip_id)


def is_redeemable(row, *, now=None) -> bool:
    """Whether `row`'s stored expiry has not yet passed.

    **Expiry only — this says nothing about `redeemed_at`.** `POST
    /tokens/redeem` calls this to enforce the TTL and `redeem` separately to
    enforce single-use, and the two stay apart on purpose: an already-
    redeemed-but-unexpired slip must still read `True` here, or the TTL
    check would blur into the single-use one and a caller could no longer
    tell "too late" from "already used" without reading `redeem`'s own
    return. There is nothing to re-derive for the expiry half: `expires_at`
    was fixed at mint, so this is a straight comparison against the current
    instant rather than a walk like `lease.is_dead`'s.

    A row whose `expires_at` cannot be read is treated as not redeemable,
    matching `lease.is_dead`'s direction for the same reason: a slip nobody
    can interpret must not be treated as still good.
    """
    now = now or _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    expires_at = _parse(row["expires_at"])
    if expires_at is None:
        return False
    return now < expires_at
