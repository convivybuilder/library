"""crew on a work item's claim and an epic lease's holder

Revision ID: a6d1c1b1974e
Revises: ce4413f890e4
Create Date: 2026-10-15 04:30:42.909011

**ADDITIVE: two nullable text columns, nothing else.** No existing column,
index, row shape or constraint on `issue_items` or `issue_leases` changes, so
this needs no batch mode on the way up — `ADD COLUMN` is native SQLite,
matching `ee33c883acc4`'s reasoning for the same shape of change.

`issue_items.claimed_by_crew` and `issue_leases.holder_crew` record which
crew's credential took a claim or a lease, beside the existing self-declared
`claimed_by`/`holder` name (CQ·10, the operator's ruling of 2026-10-15 in comment 5378:
"The Library records which crew took each held item, and refuses a release or
resolve from a credential whose crew is not the holding crew"). `identity.py`
is what lets a credential carry a crew at all; `app.py` is what reads these
two columns to refuse a release, resolve, reopen, checkout or claim across
crews.

**Every row written before this migration, and every row written by a
credential with no crew, carries `NULL` here — and that is not a gap to
backfill.** No live credential names a crew yet (minting one is a separate,
later act, through `mint_credential.py --crew`), so nothing is refused the
moment this lands: `app.py`'s crew check is a no-op wherever this column
reads `NULL`, which today is every row. This is an ordinary additive
migration the deploy watcher runs on its own, with none of the gating the
Phase 3 drop stage needs.
"""
from alembic import op
import sqlalchemy as sa


revision = 'a6d1c1b1974e'
down_revision = 'ce4413f890e4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("issue_items", sa.Column("claimed_by_crew", sa.Text, nullable=True))
    op.add_column("issue_leases", sa.Column("holder_crew", sa.Text, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("issue_leases") as batch:
        batch.drop_column("holder_crew")
    with op.batch_alter_table("issue_items") as batch:
        batch.drop_column("claimed_by_crew")
