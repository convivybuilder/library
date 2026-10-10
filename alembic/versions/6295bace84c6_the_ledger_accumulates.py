"""the ledger accumulates: the grant becomes a cap set, an entry becomes voidable

Revision ID: 6295bace84c6
Revises: f71949b78df4
Create Date: 2026-09-02

`issue_01M0B8YCFN4C1CYNANVG87CANG`. The model inverts (the operator, 2026-08-27, epic
comment 1081): a ledger records what has been spent, not what remains. Spend
accumulates and nothing is debited toward zero. Enforcement moves from one
balance to three thresholds — a warning that refuses nothing, a soft cap that
refuses new admissions, and a hard cap — compared against the same accumulated
total rather than against a shrinking headroom.

## Why the grant becomes the SOFT cap, and not the warning or the hard cap

`daily_granted_usd_micro` was exactly the number at which `b4c1f7a92e08`'s
admission refusal fired: headroom reaching zero. A soft cap is defined the same
way — the threshold at which new admission is refused while runs already in
flight continue. The old column and the new soft cap are the same fact under
two models, so migrating the value forward is not a choice among three
plausible homes; it is the one column whose stored MEANING survived the
inversion unchanged. The warning threshold never refused anything under the old
model either, so mapping the grant there would silently turn every drawn-down
product live again. The hard cap requires an act this epic does not perform on
its own — revocation, Phase 5 — so a grant that used to gate admission on its
own has no business landing on a threshold this deploy cannot yet enforce.

## Why `ledger_days` is untouched

It already stored an accumulating total; only the DERIVED headroom counted
down. `store.ledger_headroom` read `daily_granted_usd_micro` minus
`ledger_days.spent_usd_micro` — the subtraction lived in Python, not in the
schema — so the column this migration removes is the only one that encoded the
old model in the database at all.

## Why the three new caps are nullable rather than given a sentinel

Null means that threshold is not set, and all three null is a ledger row that
refuses nothing — a scope with a cap SET carries the row anyway (`granted_by`,
`created_at`), so "no threshold" has to be representable without also meaning
"no row exists". A sentinel (0, or `MAX_LEDGER_USD_MICRO`) would collide with a
genuine deliberate freeze at zero or a genuine cap at the ceiling, so it is
either wrong on the boundary or requires the same three-state logic nullable
already gives for free.

## Why a voided entry is marked rather than deleted

`ledger_entries` is append-only: nothing before this migration ever removed a
row, because the spend log is the evidence a materialized balance is audited
against. A void has to correct that balance — a mistaken report otherwise
stands until a human re-grants, which an accumulating total gives no re-grant
to correct with — but deleting the row would make the log stop being a record
of everything reported and start being a record of everything that turned out
to be right, which is a different and weaker guarantee. Marking a reversal on
the row keeps both: `store.void_spend_entry` subtracts the voided amount from
`ledger_days` in the same transaction, and the row stays behind as the evidence
that a report was made and then corrected, by whom, and why.
"""
from alembic import op
import sqlalchemy as sa


revision = "6295bace84c6"
down_revision = "f71949b78df4"
branch_labels = None
depends_on = None


# **`copy_from` ON EVERY `batch_alter_table` BELOW, and it is not optional
# decoration.** SQLite's batch mode recreates the table it is altering, and
# recreating it means knowing its current columns, indexes and constraints —
# ordinarily read by reflecting the live connection, which is exactly what
# `alembic upgrade --sql` never opens (`env.py`'s `run_migrations_offline`).
# Without an explicit `copy_from`, the offline preview fails outright with
# "batch mode ... requires a live database connection", caught by
# `test_the_offline_preview_runs` the moment this migration's `upgrade()`
# became the first one in this tree whose UPGRADE path (not only its
# downgrade) uses batch mode.
#
# **AND EVERY INDEX AND CONSTRAINT THE TABLE CARRIES AT THAT POINT HAS TO BE
# NAMED ON IT TOO, not only the columns.** Given no `copy_from`, batch mode
# reflects the live table and carries its indexes forward automatically —
# verified against a real database before this comment was written. Given an
# explicit `copy_from`, alembic recreates the table from exactly what that
# `Table` object says and nothing else; the first version of this migration
# supplied columns alone and silently dropped `ix_ledgers_scope` and the
# partial unique index enforcing one live ledger per scope, caught by
# `test_a_second_live_ledger_for_one_scope_is_impossible` going from failing
# to passing to no longer catching anything. Each function below describes
# the table as it stands at the exact moment its batch block runs —
# mid-migration, not the schema at either end of it — with every index,
# foreign key and check constraint `b4c1f7a92e08` gave the table, so the
# recreate loses none of them.
def _ledgers_before_dropping_the_grant() -> sa.Table:
    t = sa.Table(
        "ledgers", sa.MetaData(),
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("scope_kind", sa.String(20), nullable=False),
        sa.Column("scope_id", sa.String(64), nullable=False),
        sa.Column("daily_granted_usd_micro", sa.BigInteger, nullable=False),
        sa.Column("state", sa.String(20), nullable=False, server_default="live"),
        sa.Column("granted_by", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
        sa.Column("warn_usd_micro", sa.BigInteger),
        sa.Column("soft_cap_usd_micro", sa.BigInteger),
        sa.Column("hard_cap_usd_micro", sa.BigInteger),
    )
    sa.Index("ix_ledgers_scope", t.c.scope_kind, t.c.scope_id)
    sa.Index("uq_ledgers_one_live_per_scope", t.c.scope_kind, t.c.scope_id,
            unique=True, sqlite_where=sa.text("state = 'live'"))
    return t


def _ledgers_before_restoring_the_grant() -> sa.Table:
    t = sa.Table(
        "ledgers", sa.MetaData(),
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("scope_kind", sa.String(20), nullable=False),
        sa.Column("scope_id", sa.String(64), nullable=False),
        sa.Column("state", sa.String(20), nullable=False, server_default="live"),
        sa.Column("granted_by", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
        sa.Column("warn_usd_micro", sa.BigInteger),
        sa.Column("soft_cap_usd_micro", sa.BigInteger),
        sa.Column("hard_cap_usd_micro", sa.BigInteger),
        # NULLABLE HERE, matching the `op.add_column` just above this batch
        # block in `downgrade()` — the NOT NULL is what this same batch adds.
        sa.Column("daily_granted_usd_micro", sa.BigInteger),
    )
    sa.Index("ix_ledgers_scope", t.c.scope_kind, t.c.scope_id)
    sa.Index("uq_ledgers_one_live_per_scope", t.c.scope_kind, t.c.scope_id,
            unique=True, sqlite_where=sa.text("state = 'live'"))
    return t


def _ledger_entries_before_dropping_void() -> sa.Table:
    t = sa.Table(
        "ledger_entries", sa.MetaData(),
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("ledger_id", sa.String(40), nullable=False),
        sa.Column("day", sa.String(10), nullable=False),
        sa.Column("usd_micro", sa.BigInteger, nullable=False),
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
        sa.Column("voided_at", sa.DateTime),
        sa.Column("voided_by", sa.String(200)),
        sa.Column("void_reason", sa.String(500)),
        sa.ForeignKeyConstraint(["ledger_id"], ["ledgers.id"],
                                name="fk_ledger_entries_ledger",
                                ondelete="RESTRICT"),
        sa.UniqueConstraint("dedup_key", name="uq_ledger_entries_dedup_key"),
        sa.CheckConstraint(
            "day GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'",
            name="ck_ledger_entries_day_shape"),
    )
    sa.Index("ix_ledger_entries_ledger_day", t.c.ledger_id, t.c.day)
    sa.Index("ix_ledger_entries_item_id", t.c.item_id)
    sa.Index("ix_ledger_entries_run_id", t.c.run_id)
    sa.Index("ix_ledger_entries_product", t.c.product)
    return t


def upgrade() -> None:
    op.add_column("ledgers", sa.Column("warn_usd_micro", sa.BigInteger, nullable=True))
    op.add_column("ledgers", sa.Column("soft_cap_usd_micro", sa.BigInteger, nullable=True))
    op.add_column("ledgers", sa.Column("hard_cap_usd_micro", sa.BigInteger, nullable=True))
    # THE GRANT BECOMES THE SOFT CAP — see the module docstring for why this is
    # the one column whose meaning survives the inversion, rather than a choice
    # among the three.
    op.execute("UPDATE ledgers SET soft_cap_usd_micro = daily_granted_usd_micro")
    with op.batch_alter_table(
            "ledgers", copy_from=_ledgers_before_dropping_the_grant()) as batch:
        batch.drop_column("daily_granted_usd_micro")

    op.add_column("ledger_entries", sa.Column("voided_at", sa.DateTime, nullable=True))
    op.add_column("ledger_entries",
                  sa.Column("voided_by", sa.String(200), nullable=True))
    op.add_column("ledger_entries",
                  sa.Column("void_reason", sa.String(500), nullable=True))


def downgrade() -> None:
    op.add_column("ledgers",
                  sa.Column("daily_granted_usd_micro", sa.BigInteger, nullable=True))
    # `soft_cap_usd_micro` IS THE FIRST CHOICE, matching `upgrade()`'s
    # forward mapping, but a ledger this deploy made legal — warn-only,
    # hard-cap-only, or all three caps null — has no soft cap to fall back to,
    # and the column below this comment is about to be made NOT NULL. Fall
    # to the hard cap next, and only then to a literal ceiling: this
    # downgrade is a reversal of the model, not of one ledger's history, and
    # the model it reverses TO refuses once a grant is exhausted, so an
    # uncapped row has to become a grant large enough never to be exhausted —
    # the opposite of zero, which would freeze, on downgrade, every project a
    # human had deliberately left uncapped. 1_000_000_000_000 is
    # `store.MAX_LEDGER_USD_MICRO` — a migration does not import the app
    # module, so the value is repeated here as a literal.
    op.execute(
        "UPDATE ledgers SET daily_granted_usd_micro = "
        "COALESCE(soft_cap_usd_micro, hard_cap_usd_micro, 1000000000000)")
    with op.batch_alter_table(
            "ledgers", copy_from=_ledgers_before_restoring_the_grant()) as batch:
        batch.alter_column("daily_granted_usd_micro", nullable=False)
        batch.drop_column("warn_usd_micro")
        batch.drop_column("soft_cap_usd_micro")
        batch.drop_column("hard_cap_usd_micro")

    with op.batch_alter_table(
            "ledger_entries",
            copy_from=_ledger_entries_before_dropping_void()) as batch:
        batch.drop_column("voided_at")
        batch.drop_column("voided_by")
        batch.drop_column("void_reason")
