"""a lane on every ledger entry, accepted and not yet required

Revision ID: 80ab55fe0dba
Revises: 2c89868910a8
Create Date: 2026-10-07 01:14:52.206272

`issue_01M35QG5NA13JE8FQVM9XET4HD`, the prerequisite the Bosun's Phase 4 epic
(`issue_01KZBWEYGQ5JXE96V1D0DYX5RA`) waits on. Spend reaches the ledger from
two lanes — metered gateway spend, and subscription usage whose dollar
figure is an estimate — and only the first is money a budget can be
enforced against. This column is what lets a later read, and a later
enforcement figure, tell the two apart.

**ADDITIVE: one nullable text column, nothing else.** No existing column,
index, row shape or constraint on `ledger_entries` changes, so this needs no
batch mode on the way up — `ADD COLUMN` is native SQLite, matching
`d12fde7d50e9`'s reasoning for the same shape of change.

**THIS IS THE FIRST OF TWO STEPS, and this migration is only the first.**
`POST /ledger-entries` accepts `lane` and does not yet require it — a write
with no `lane` is still recorded, with this column left NULL. Requiring it
is a later contract change, made once the Bosun's `agentctl` sends it.

**No backfill, unlike `d12fde7d50e9`'s `epic_id`.** That migration could
derive an epic from a fact already on the row (`item_id`); there is no fact
on an existing `ledger_entries` row this vault could derive a lane from —
guessing one would assert something about a past spend nobody recorded.
Every row this vault holds at the moment this ships gets NULL, the same
value a future write with no `lane` gets.
"""
from alembic import op
import sqlalchemy as sa


revision = '80ab55fe0dba'
down_revision = '2c89868910a8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ledger_entries", sa.Column("lane", sa.String(20), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("ledger_entries") as batch:
        batch.drop_column("lane")
