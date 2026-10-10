"""found_during on work items (issue_01M3BFN34Y8P8K39RWS6MM89ZT)

Revision ID: ee33c883acc4
Revises: d70d450ef8c7
Create Date: 2026-10-01 00:35:19.450171

**ADDITIVE: one nullable text column and its index, nothing else.** No
existing column, index, row shape or constraint on `issue_items` changes, so
this needs no batch mode on the way up — `ADD COLUMN` is native SQLite,
matching `a8d2c5e91f37`'s reasoning for the same shape of change.

`found_during` records which epic's work turned a work item up, distinct
from `parent_id`: giving an item a `parent_id` moves it out of the
corrections lane, and `found_during` is exactly the way to attribute an
item to an epic's work WITHOUT that lane change. `app._validate_found_during`
is what keeps the column honest — a non-epic, the item's own id, a missing
id, a cross-product epic or an epic the writer cannot see are all refused at
write time — so nothing here enforces the invariant beyond "nullable text".

The index backs `GET /items?found_during=<epic id>`, the listing filter a
closing epic's cleanup crew reads from, the same reason `ix_ledger_entries_
epic_id` exists beside that column.
"""
from alembic import op
import sqlalchemy as sa


revision = 'ee33c883acc4'
down_revision = 'd70d450ef8c7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("issue_items", sa.Column("found_during", sa.Text, nullable=True))
    op.create_index("ix_issue_items_found_during", "issue_items", ["found_during"])


def downgrade() -> None:
    op.drop_index("ix_issue_items_found_during", table_name="issue_items")
    with op.batch_alter_table("issue_items") as batch:
        batch.drop_column("found_during")
