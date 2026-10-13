"""a call slip keeps the grant it was issued with, for redemption to return

Revision ID: 962c9c87df11
Revises: e18a2c4f9b06
Create Date: 2026-10-10

issue_01M3VN8WV1K891BT866K82ZBPX, a child of the Phase 5 token-protocol epic
(issue_01KZBWEYHJ1QCKBR5BTM6G6DQ9). Redeeming a call slip returns everything
an Individuator needs to render the agent: the persona, the documents by id
and hash, the model, the budget and the limits. Checkout already serves that
grant to the Bosun, but builds it from the persona and profile named on the
checkout request, which nothing stores. So `POST /tokens/redeem` can only
return the grant the slip was issued with if the slip row keeps it.

**ADDITIVE: one nullable column, nothing else.** `ADD COLUMN` is native
SQLite, matching `f8c1f89ef65c`'s reasoning for the same shape of change.
`call_slips.mint` writes it once, in the same INSERT that writes the slip, and
nothing writes it again, the same rule the slip's `expires_at` follows.

**Named `grant_json`, not `grant`.** `GRANT` is an SQL keyword, and a column
named after one has to be quoted at every use.

Existing rows get NULL. Every slip minted before this revision expired within
`call_slips.TTL_SECONDS` of its mint, so no redeemable slip is left without a
grant; redemption answers `grant: null` for one anyway, rather than inventing
a grant it was not issued with.
"""
from alembic import op
import sqlalchemy as sa


revision = '962c9c87df11'
down_revision = 'e18a2c4f9b06'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("call_slips",
                  sa.Column("grant_json", sa.Text, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("call_slips") as batch:
        batch.drop_column("grant_json")
