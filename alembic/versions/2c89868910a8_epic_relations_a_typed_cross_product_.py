"""epic_relations: a typed cross-product relation the lease walk never
traverses (issue_01M1JSPH7DBHG0BZFPJPNNZGSC)

Revision ID: 2c89868910a8
Revises: ee33c883acc4
Create Date: 2026-10-01 03:01:09.471431

`_validate_parent` refuses a `parent_id` across products, unconditionally,
because `lease.ancestor_ids` and `lease.subtree_ids` are plain `parent_id`
walks with no product filter — every edge is same-product only because the
parent check makes it so. "This item is part of that epic's work" still
needs a carrier when the epic sits in a different product, so this is a new,
separate table rather than a second kind of edge on `parent_id`: nothing in
`lease.py` reads `issue_epic_relations`, so a lease on the epic cannot fence
a row only this table points at, and the epic-retype descendant count
(`app.py`'s `_do_correct`, over `lease.subtree_ids`) cannot either.

**`id` is a plain autoincrement, matching `issue_comments` and
`issue_events`** — this table has no identity to carry past this row the way
`issue_items.id` does, so a synthetic ULID would be a second way to name a
row that already has one.

**`UNIQUE (item_id, epic_id)`** makes the write idempotent: a caller
retrying a relation it already made gets the same row back rather than a
duplicate or a constraint error surfacing as a 500.

**Both foreign keys cascade.** A relation naming a row that no longer exists
is not a fact about anything; `issue_items` rows are never hard-deleted in
practice, but a row that is restores the invariant rather than orphaning the
relation.

**Indexed on `epic_id`**, for the epic's own listing — "who relates to
this epic" — which is the read this table exists to serve.
`UNIQUE (item_id, epic_id)` already covers `item_id` lookups.
"""
from alembic import op
import sqlalchemy as sa


revision = '2c89868910a8'
down_revision = 'ee33c883acc4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "issue_epic_relations",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("item_id", sa.String(40), nullable=False),
        sa.Column("epic_id", sa.String(40), nullable=False),
        sa.Column("created_by", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.ForeignKeyConstraint(["item_id"], ["issue_items.id"],
                                name="fk_issue_epic_relations_item_id",
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["epic_id"], ["issue_items.id"],
                                name="fk_issue_epic_relations_epic_id",
                                ondelete="CASCADE"),
        sa.UniqueConstraint("item_id", "epic_id",
                            name="uq_issue_epic_relations_item_epic"),
    )
    op.create_index("ix_issue_epic_relations_epic_id", "issue_epic_relations",
                    ["epic_id"])


def downgrade() -> None:
    op.drop_index("ix_issue_epic_relations_epic_id",
                  table_name="issue_epic_relations")
    op.drop_table("issue_epic_relations")
