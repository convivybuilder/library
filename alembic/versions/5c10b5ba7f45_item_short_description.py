"""item_short_description: the label a route or a rail draws when a title
will not fit (issue_01M0XR0QSMKBSGBYD49CPTJ37E)

Revision ID: 5c10b5ba7f45
Revises: 1e5e5078a1c6
Create Date: 2026-10-14

**ADDITIVE: ONE NULLABLE TEXT COLUMN ON `issue_items`, NOTHING ELSE.** No
existing column, index, row or constraint is touched, and every existing row
reads NULL, which means "no short description". A client draws its own
fallback from the title for those rows, so a partially populated corpus is a
working state. Nothing backfills it.

The length bound (`store.MAX_SHORT_DESCRIPTION`) is enforced at the write
routes, the same as `title`'s, so there is no CHECK constraint here to keep in
step with it.
"""
from alembic import op
import sqlalchemy as sa

revision = "5c10b5ba7f45"
down_revision = "1e5e5078a1c6"
branch_labels = None
depends_on = None


# `op.add_column` for the upgrade and the batch form for the downgrade, as in
# `a8d2c5e91f37`: `ADD COLUMN` is native SQLite, `DROP COLUMN` support varies
# with the SQLite version.
def upgrade() -> None:
    op.add_column("issue_items",
                  sa.Column("short_description", sa.Text, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("issue_items") as batch:
        batch.drop_column("short_description")
