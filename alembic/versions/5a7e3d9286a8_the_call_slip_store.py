"""the call slip store

Revision ID: 5a7e3d9286a8
Revises: 6295bace84c6
Create Date: 2026-09-03

issue_01M12NE15SQVPS88PV58H1SS1P, Phase 5 (ADR-0062). Checkout has served the
call slip's grant since PR #78, with no credential minted behind it —
deliberately, because a slip a client could believe is redeemable, with no
store and nothing to redeem it at, is a fake credential. This is the store.

## The expiry is stored, not derived

`issue_leases` stores no expiry at all: `lease.expires_at` derives one from
`taken_at` and `heartbeat_at` against a grace tunable, and a heartbeat pushes
it out every time the holder is seen. A call slip must not work that way —
one issuance admits one run, and nothing the holder does may extend how long a
leaked slip stays redeemable — so `expires_at` here is a plain column, set
once at mint by `call_slips.mint` and never written again.

## Why `product` is a column and not only reachable through `item_id`

Same reasoning `ledger_entries` already carries for its own `product` column
(`b4c1f7a92e08`): the redemption path (`POST /tokens/redeem`,
issue_01M12NE13P3FEY3HF7732QX03F) will need it to check the caller's scope
without a join, and it is always known at mint time because checkout has
already resolved the item's product to get this far.

## `claim_owner_label` rather than `holder` or `issued_to`

Matches the lease's `holder` in spirit but not in name: a slip is issued to
whoever checked out (`owner` in `_do_checkout`), which is not necessarily the
identity that later redeems it — the Materializer redeems on the agent's
behalf. The column is deliberately not actor-shaped: an earlier name,
`issued_to`, read like the answer to ADR-0062's actor cross-check at
redemption, and it isn't one — see `call_slips.py`'s module docstring for
what the value actually is and what the check should read instead.

## The foreign key is declared and not enforced, matching every table here

SQLite's `PRAGMA foreign_keys` is off for every connection this service opens
(`store.py`), so `fk_call_slips_item` is exactly as declarative as
`fk_issue_leases_root_id` and `fk_ledger_entries_ledger` already are — see
`b4c1f7a92e08`'s docstring for the reasoning, unchanged here.
"""
from alembic import op
import sqlalchemy as sa


revision = "5a7e3d9286a8"
down_revision = "6295bace84c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "call_slips",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("item_id", sa.String(40), nullable=False),
        sa.Column("product", sa.String(64), nullable=False),
        sa.Column("claim_owner_label", sa.String(200), nullable=False),
        sa.Column("issued_at", sa.DateTime, nullable=False),
        sa.Column("expires_at", sa.DateTime, nullable=False),
        sa.ForeignKeyConstraint(["item_id"], ["issue_items.id"],
                                name="fk_call_slips_item",
                                ondelete="RESTRICT"),
    )
    op.create_index("ix_call_slips_item_id", "call_slips", ["item_id"])


def downgrade() -> None:
    op.drop_table("call_slips")
