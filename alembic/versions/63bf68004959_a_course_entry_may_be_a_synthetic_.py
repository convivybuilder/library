"""a Course entry may be a synthetic waypoint

Revision ID: 63bf68004959
Revises: 962c9c87df11
Create Date: 2026-10-11 15:38:05.478828

issue_01M12NE0VHY8828VJPFX8ARZ24 (E5·3), under the Course-machinery epic
(issue_01KZFB4KXQD82G3PDHMADXD6NA). A waypoint may name several things that
must all complete before it is reached, and no single row represents one —
it needs a place of its own.

**`courses.entries` CANNOT CARRY IT.** The protocol pins that array to
epic id strings (platform/contracts/library-protocol §6.1, "a Course
contains only epics"), so a synthetic waypoint lives beside it rather than
inside it — the new, additive place the work item calls for.

**ADDITIVE: two nullable-by-default JSON columns, nothing else.** No
existing column, index, row shape or constraint changes, so this needs no
batch mode on the way up, matching `d12fde7d50e9`'s reasoning for the same
shape of change.

- `courses.waypoints` holds the Course's own synthetic waypoints — a JSON
  array of `{"id", "members"}`, each `members` a list of item ids. `NOT
  NULL DEFAULT '[]'`, so `store.course_dict` reads every row the same way
  whether or not it predates this column, the same convention `entries`
  itself already set.
- `course_events.waypoints` is the snapshot column `COURSE_ACTION_WAYPOINT_
  ADDED` writes to, mirroring `entries`' own snapshot-on-the-event
  convention. Nullable, like `entries` on that table: most actions never
  touch the waypoint list and NULL is what "did not move here" means.
"""
from alembic import op
import sqlalchemy as sa


revision = '63bf68004959'
down_revision = '962c9c87df11'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "courses",
        sa.Column("waypoints", sa.JSON(), nullable=False,
                   server_default="[]"))
    op.add_column(
        "course_events",
        sa.Column("waypoints", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("course_events") as batch:
        batch.drop_column("waypoints")
    with op.batch_alter_table("courses") as batch:
        batch.drop_column("waypoints")
