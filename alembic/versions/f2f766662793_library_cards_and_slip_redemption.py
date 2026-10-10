"""library cards and slip redemption

Revision ID: f2f766662793
Revises: 5a7e3d9286a8
Create Date: 2026-09-03 20:13:40.847909

issue_01M12NE13P3FEY3HF7732QX03F, Phase 5 (ADR-0062). `POST /tokens/redeem`
exchanges a call slip for a library card — this is the store for both halves
of that exchange: the column that lets a slip be consumed exactly once, and
the table the redeemed card lives in.

## `call_slips.redeemed_at` — a plain nullable column, set once

Matches `expires_at`'s own shape: written once, at redemption, and never
touched again. Single-use is enforced by `call_slips.redeem`'s conditional
`UPDATE ... WHERE id = ? AND redeemed_at IS NULL`, not by application code
reading this column and branching — two concurrent redemptions both read
`NULL` before either writes if that were the shape. The column is what the
`WHERE` clause tests; it does no work by itself.

`op.add_column` rather than `op.batch_alter_table` (`env.py`'s comment above
`run_migrations_online`, and `67e87ed76f03` for the precedent): a nullable
`ADD COLUMN` is SQLite's own native statement, not the `ALTER TABLE ... ALTER
COLUMN` form standard SQLite lacks. The downgrade takes the batch form for the
same reason `67e87ed76f03` does — `DROP COLUMN` support varies by SQLite
version, and a downgrade is rare enough that recreating the table costs
nothing worth saving.

## `library_cards` — a new table this service owns outright

One row per redeemed slip. `slip_id` is `UNIQUE`, which backs the single-use
guarantee with a second, independent mechanism at the schema level: even a
future write path that forgets the conditional `UPDATE` cannot insert two
cards for one slip, because SQLite itself refuses the second `INSERT`.

**`actor` records the identity that redeemed the slip — the caller
`identity.resolve` produced on the redemption request — and deliberately not
`call_slips.claim_owner_label`.** `call_slips.py`'s module docstring and
issue_01M12NE13P3FEY3HF7732QX03F's comments 1699 and 1700 both name
`claim_owner_label` as a caller-supplied coordination label unfit for an
authorization decision; this is the same reasoning applied to what a card
binds to, not only to the eventual actor cross-check. It is a placeholder for
"the agent this card is bound to" in the ADR-0062 sense — the Materializer
redeems on the agent's behalf, so today this names the Materializer's own
credential, and the two become the same identity only once a per-agent
credential exists on this path.

**`protocol_version` is `NOT NULL`**: ADR-0062 and contract §1 both require
redemption to pin the version for the card's lifetime, so a card minted with
no version recorded would be a card the contract does not recognise.

**`state` carries a `CHECK` constraint, matching `machine_certificates`'
`status` column** (`f71949b78df4`) for the reason recorded there: the
application enforces the direction (`machine_identity.py` reads FOR
`active`), and the constraint is the backstop that holds even if that check
is ever misconfigured. `revoke` (`POST /tokens/revoke`,
issue_01M12NE155ABV0K2K50P6CBWKJ) is a separate task and writes no code here
— the column exists so that task has a state to move rather than a table to
alter.

The two foreign keys are declared and not enforced, matching every table in
this tree (`PRAGMA foreign_keys` is off for every connection `store.py`
opens) — see `b4c1f7a92e08`'s docstring for why that is deliberate rather
than an oversight.
"""
from alembic import op
import sqlalchemy as sa


revision = 'f2f766662793'
down_revision = '5a7e3d9286a8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("call_slips", sa.Column("redeemed_at", sa.DateTime, nullable=True))

    op.create_table(
        "library_cards",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("actor", sa.String(200), nullable=False),
        sa.Column("product", sa.String(64), nullable=False),
        sa.Column("item_id", sa.String(40), nullable=False),
        sa.Column("slip_id", sa.String(40), nullable=False, unique=True),
        sa.Column("protocol_version", sa.Integer, nullable=False),
        sa.Column("state", sa.String(10), nullable=False,
                   server_default="active"),
        sa.CheckConstraint("state IN ('active', 'revoked')",
                           name="ck_library_cards_state"),
        sa.Column("issued_at", sa.DateTime, nullable=False),
        sa.Column("revoked_at", sa.DateTime, nullable=True),
        sa.ForeignKeyConstraint(["slip_id"], ["call_slips.id"],
                                name="fk_library_cards_slip",
                                ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["item_id"], ["issue_items.id"],
                                name="fk_library_cards_item",
                                ondelete="RESTRICT"),
    )
    op.create_index("ix_library_cards_actor", "library_cards", ["actor"])


def downgrade() -> None:
    op.drop_index("ix_library_cards_actor", table_name="library_cards")
    op.drop_table("library_cards")
    with op.batch_alter_table("call_slips") as batch:
        batch.drop_column("redeemed_at")
