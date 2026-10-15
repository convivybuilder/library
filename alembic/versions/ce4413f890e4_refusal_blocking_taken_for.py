"""refusal_blocking_taken_for: the item the blocking lease was taken for, on
every recorded refusal (issue_01M0RCF408K7TBE0N9VP224MGA)

Revision ID: ce4413f890e4
Revises: 5c10b5ba7f45
Create Date: 2026-10-14

**ONE NULLABLE TEXT COLUMN ON `lease_refusals`, AND A BACKFILL FROM THE LEASE
EACH ROW ALREADY NAMES.** A refusal row could not say whether the two crews
wanted the same item: a leaf leases its parent, so `blocking_root_id` is the
epic, and `blocks_by` tags a lease on the requested item itself `ancestor`
exactly as it tags one three levels up. The blocking lease's `taken_for` is the
item the other crew asked for, and comparing it with `requested_item_id`
answers the question directly.

The backfill copies `issue_leases.taken_for` through `blocking_lease_id`, which
every existing row carries. A row whose lease is gone stays NULL, which means
"not recorded", never "a different item".
"""
from alembic import op
import sqlalchemy as sa

revision = "ce4413f890e4"
down_revision = "5c10b5ba7f45"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("lease_refusals",
                  sa.Column("blocking_taken_for", sa.Text, nullable=True))
    op.execute(
        "UPDATE lease_refusals SET blocking_taken_for = ("
        "SELECT taken_for FROM issue_leases "
        "WHERE issue_leases.id = lease_refusals.blocking_lease_id)")


# The batch form, as in `5c10b5ba7f45`: `DROP COLUMN` support varies with the
# SQLite version.
def downgrade() -> None:
    with op.batch_alter_table("lease_refusals") as batch:
        batch.drop_column("blocking_taken_for")
