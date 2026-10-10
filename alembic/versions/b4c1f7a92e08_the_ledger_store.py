"""The ledger store: a daily grant per scope, a per-day balance, and append-only spend

Revision ID: b4c1f7a92e08
Revises: 67e87ed76f03
Create Date: 2026-08-23

`issue_01M0B8YCFN4C1CYNANVG87CANG`, the Library's half of Phase 4. The Chart
House is defined as holding "work items: issues and epics, plus the ledgers a
project draws on", and this is the ledgers. Until now the last of checkout's
hard refusals had nothing to read: a claim already refuses on scope, on
capability and on an unapproved linked decision, and could not refuse on a
drawn-down ledger because there were none.

**Three tables rather than one, because a grant, a balance and a spend event
have three different guarantees.** A grant is arbitrated — two runs drawing on
one budget is contention, and resolving it is this vault's defining job. A
balance is derived and must be cheap to read on the admission path. An entry is
a record of something that happened and nothing arbitrates over it.

## Why the balance is materialized rather than summed

`agentctl` streams spend DURING a run rather than at exit, deliberately: a
ledger updated only when runs end lags by the length of the longest run, which
is exactly the window the next admission decision needs it current in. That
makes entries arrive continuously, so a checkout that summed them would pay a
growing scan at the moment admission control has to be fast. `ledger_days`
carries the running total, written inside the entry's own transaction under the
same compare-and-set discipline the leases already use, so it cannot drift from
the entries it summarizes.

## Why the day key is a stored LOCAL date rather than derived from `at`

The fleet's existing budget already buckets by local day, and its reason is
recorded rather than incidental: a UTC key rolls the day at 17:00 MST and folds
an evening's interactive work into the next day's pool, which produced a false
soft-cap crossing on 2026-06-14. `telemetry.platform_day()` is
`datetime.now().astimezone().strftime("%Y-%m-%d")`, and timestamps stay UTC
while only the BUCKET is local.

So the Library must not derive the day from `at` with a UTC cast, or the two
halves of the Keel will disagree about which day a spend falls in for seven
hours out of every twenty-four — and they are the enforcing half and the
recording half of the same cap. The writer supplies the local day key and the
Library stores it as given.

**Storing it rather than deriving it is also what makes per-user local time
reachable.** A derived key would be the SERVER's day, which stops having one
right answer the moment two operators sit in different zones (the operator, 2026-08-23:
the system should run on the user's local time). A stored key is the caller's
day, and generalizes without a schema change.

**Which means the day is caller-controlled, and a gate must never trust a field
its subject controls.** Unvalidated, a caller could book spend to whichever day
still has headroom — a past day whose allowance no longer matters — and the
admission refusal would never fire. That is the same shape as forging a review
verdict, one table over.

The CHECK below bounds the FORM. Bounding the VALUE is the write surface's job
and is stated here so it is not rediscovered: **the route accepts a day within
one day either side of the server's own local date** — wide enough for any
real timezone offset and for clock skew, narrow enough that no meaningful
headroom can be reached by choosing a different date. A day outside that window
is refused rather than clamped, because silently rebucketing someone's spend is
a worse answer than telling them the clock disagrees.

## Why money is an integer

`usd_micro` is millionths of a dollar. Provider costs run to fractions of a
cent, and a ledger is the one place in this service where a number is compared
against a limit to produce a refusal. Storing the exact integer removes any
question about what the boundary value was, at the cost of one conversion where
a caller reports dollars.

## Why `item_id` is here at all, since the epic did not ask for it

The fifth Keel completion criterion is that the operator can measure results of
agent/task/model combinations, and "task" is the axis nothing currently stores.
Measured on 2026-08-23: the fleet's run ledger under `agent-runs/scorecard/`
carries agent, model, run_id, cost and `pr_number` across 2,311 rows and no
work-item identifier; joining it to the Library on `(repo, pr_number)` matches
163 of the 228 work items that carry a PR. A bridge that resolves most of the
time is what a dashboard under-reports through, so the entry carries the item
directly and the question becomes one query against one store.

**No foreign key on it.** Spend legitimately arrives with no work item — an app
backend reporting its own runtime spend has none — so the column is nullable and
an index gives the join.

## What the foreign keys below do and do not do today

`ledger_days` and `ledger_entries` declare a foreign key onto `ledgers` with
`ON DELETE RESTRICT`, and **none of it is enforced at present.** SQLite defaults
`PRAGMA foreign_keys` to OFF for every connection and this service sets only
`busy_timeout` (`store.py:369`), so the existing `fk_issue_items_parent_id`
RESTRICT is equally declarative. Checked rather than assumed.

They are declared anyway, because a declaration costs nothing, documents the
intended shape, and becomes live the day the pragma is turned on. What must not
happen is code written against them as though they hold — a ledger row deleted
today takes no entries with it and is stopped by nothing.

Turning the pragma on is its own change: it alters the behaviour of every write
path in the service at once and could begin refusing writes that currently
succeed, so it wants its own testing rather than a line in a ledger migration.

## `person` and `team` are here and are not populated by anything

ADR-0021 requires the axis, on the reasoning that with one organizational
credential the Library's records are the only place per-user attribution can
come from, because the provider's invoice sees one key. The operator's call on
2026-08-23 is that the person axis may not be needed in this loop at all, and
for a single-operator deployment that call is right: it carries no information
today.

**The columns are here because they are two nullable columns and the mechanism
is not.** What ADR-0021 argues is expensive later is the RECORD, not the schema:
attribution cannot be reconstructed from a bill that never carried it. Two
nullable columns preserve that for the day a second person exists, while
building none of the resolution machinery — which is the part that does not
work today anyway, since `agentctl` holds no Library credential at all and
`identity.attested_human` is reachable only by an approved client.

If the axis is to be genuinely abandoned rather than deferred, that is a change
to ADR-0021 and belongs in a decision record rather than in a schema that
quietly omits it.

## The uniqueness on `dedup_key` is the point of the column

Assistant records in a transcript duplicate up to four times with byte-identical
usage, so a reader that sums every line over-reports by a multiple — in a budget
enforcer, that means killing healthy runs at a fraction of their cap. The
discipline is currently written down in this epic, in the Keel architecture and
in `budget.py`'s own comments: three statements of a rule and no enforcement of
it. A unique constraint makes the over-count structurally impossible, and makes
streaming spend safe to retry, which the write path needs regardless — a report
that cannot be retried without double-charging is one that has to be delivered
exactly once over a network.

The writer sets it to the tuple `cost_join.py` already dedupes on,
`(run_id, message_id, request_id)`.
"""
from alembic import op
import sqlalchemy as sa


revision = "b4c1f7a92e08"
down_revision = "67e87ed76f03"
branch_labels = None
depends_on = None


# THE SHAPE OF A LOCAL DAY KEY, `YYYY-MM-DD`.
#
# A GLOB rather than a LIKE: SQLite's `LIKE` has no character-class syntax, so
# `_` matches any single character including a letter, and `'202X-AB-CD'` would
# pass. `GLOB` is case-sensitive and takes `[0-9]`, which is the check actually
# intended here.
#
# This bounds the FORM only. It cannot bound the VALUE — a well-formed key for
# an arbitrary past date passes, and refusing that is the write surface's job
# (see the module docstring). A CHECK that tried would have to call `date('now')`,
# which is the server's UTC day and therefore the wrong clock, and would also make
# the constraint non-deterministic, which SQLite forbids in a CHECK.
_DAY_SHAPE = "day GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'"


def upgrade() -> None:
    # THE GRANT. Stable configuration: what this scope may spend per day. One
    # row per scope, not per day — the day-by-day numbers live in `ledger_days`,
    # so changing an allowance is one update rather than a backfill.
    #
    # `granted_by` records the human who set it. The operator's call on 2026-08-23 is
    # that a grant is a direct human change made through Orient, so this holds
    # the attested human's name rather than the credential's, matching what
    # `tool_policy.actor` and `signals.acting_human` already do.
    op.create_table(
        "ledgers",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("scope_kind", sa.String(20), nullable=False),
        sa.Column("scope_id", sa.String(64), nullable=False),
        sa.Column("daily_granted_usd_micro", sa.BigInteger, nullable=False),
        sa.Column("state", sa.String(20), nullable=False, server_default="live"),
        sa.Column("granted_by", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_ledgers_scope", "ledgers", ["scope_kind", "scope_id"])
    # ONE LIVE LEDGER PER SCOPE. Two live grants for one project is an ambiguity
    # admission control cannot resolve — it would have to pick one, and either
    # choice is wrong half the time. Enforced here rather than in the route,
    # because the route is not the only future writer.
    #
    # **A PARTIAL INDEX, NOT A UNIQUE CONSTRAINT ON (scope_kind, scope_id,
    # state).** That was the first version of this and it is wrong in a way that
    # only shows up on the second close: a plain three-column constraint permits
    # exactly one row per (scope, state) pair, so it would allow one live and
    # one closed ledger and then refuse the SECOND ledger a project ever closes.
    # The rule being enforced is about the live state alone, so the index says
    # so. SQLite has supported partial indexes since 3.8.
    op.create_index("uq_ledgers_one_live_per_scope", "ledgers",
                    ["scope_kind", "scope_id"], unique=True,
                    sqlite_where=sa.text("state = 'live'"))

    # THE BALANCE. One row per ledger per local day, created lazily by the day's
    # first entry. This is what the admission path reads, and it is the only
    # table on that path.
    op.create_table(
        "ledger_days",
        sa.Column("ledger_id", sa.String(40), nullable=False),
        sa.Column("day", sa.String(10), nullable=False),
        sa.Column("spent_usd_micro", sa.BigInteger, nullable=False,
                  server_default="0"),
        sa.Column("updated_at", sa.DateTime, nullable=False),
        sa.PrimaryKeyConstraint("ledger_id", "day"),
        sa.CheckConstraint(_DAY_SHAPE, name="ck_ledger_days_day_shape"),
        sa.ForeignKeyConstraint(["ledger_id"], ["ledgers.id"],
                                name="fk_ledger_days_ledger",
                                ondelete="RESTRICT"),
    )

    # THE SPEND. Append-only. Nothing arbitrates over these rows; they are the
    # evidence behind the balance above.
    op.create_table(
        "ledger_entries",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("ledger_id", sa.String(40), nullable=False),
        # STORED, not derived from `at` — see the module docstring. The writer
        # supplies the local day key so the Library and agentctl agree on which
        # day a spend falls in.
        sa.Column("day", sa.String(10), nullable=False),
        sa.Column("usd_micro", sa.BigInteger, nullable=False),
        # WHEN THE SPEND HAPPENED, not when it was reported. Streaming means
        # those differ, and a retry makes them differ a lot.
        sa.Column("at", sa.DateTime, nullable=False),
        sa.Column("ingested_at", sa.DateTime, nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("product", sa.String(64), nullable=False),
        sa.Column("run_id", sa.String(100)),
        sa.Column("agent", sa.String(100)),
        sa.Column("model", sa.String(100)),
        sa.Column("item_id", sa.String(40)),
        sa.Column("person", sa.String(200)),
        sa.Column("team", sa.String(100)),
        sa.Column("reported_by", sa.String(200), nullable=False),
        sa.Column("dedup_key", sa.String(300)),
        sa.ForeignKeyConstraint(["ledger_id"], ["ledgers.id"],
                                name="fk_ledger_entries_ledger",
                                ondelete="RESTRICT"),
        # NULLS DO NOT COLLIDE under a SQLite unique index, which is what makes
        # this safe to leave nullable: a writer with no transcript tuple to key
        # on (an app backend reporting a single call) simply writes no key and
        # is never deduped against anything, while the streaming writer that
        # HAS one is deduped exactly. Matches `signals.dedup_key`.
        sa.UniqueConstraint("dedup_key", name="uq_ledger_entries_dedup_key"),
        sa.CheckConstraint(_DAY_SHAPE, name="ck_ledger_entries_day_shape"),
    )
    op.create_index("ix_ledger_entries_ledger_day", "ledger_entries",
                    ["ledger_id", "day"])
    op.create_index("ix_ledger_entries_item_id", "ledger_entries", ["item_id"])
    op.create_index("ix_ledger_entries_run_id", "ledger_entries", ["run_id"])
    op.create_index("ix_ledger_entries_product", "ledger_entries", ["product"])


def downgrade() -> None:
    op.drop_table("ledger_entries")
    op.drop_table("ledger_days")
    op.drop_table("ledgers")
