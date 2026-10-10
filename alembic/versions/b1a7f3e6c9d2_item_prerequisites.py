"""item_prerequisites: a work item names what it is waiting on

Revision ID: b1a7f3e6c9d2
Revises: 9c1c4f2b8a3e
Create Date: 2026-09-10

The operator decided 2026-09-10 (Deck session) that work items get prerequisites and
Prerequisites replace parking: a parked item hides its blocker inside a state and a
prose unpark condition nothing can check automatically, where a
prerequisite names another item or a testable condition and `blocked` reads
its true state back rather than trusting a human remembered to unpark. This
migration is the storage half only — `parked`, `/park`, `/unpark` and
`ITEM_HELD` are untouched; migrating an existing parked item to a
prerequisite is later work this migration does not do.

**ONE NULLABLE COLUMN, HOLDING A JSON LIST — NOT A NEW TABLE.** An item's
prerequisites are read every time the item itself is, on every listing page
and on every checkout, so a second table would be a join on the hottest read
path this service has, for a field that is small, bounded
(`store.MAX_PREREQUISITES_PER_ITEM`) and never queried by its own contents
outside `json_each`. `topics` set the precedent this follows.

NULL and `[]` both mean "no prerequisites" — the same convention `refs` and
`topics` already use — so nothing here treats an old row differently from a
freshly cleared one.
"""
from alembic import op
import sqlalchemy as sa

revision = "b1a7f3e6c9d2"
down_revision = "9c1c4f2b8a3e"
branch_labels = None
depends_on = None


# **`op.add_column` RATHER THAN `op.batch_alter_table`, matching
# `67e87ed76f03`'s reasoning.** `env.py`'s batch rule guards `op.alter_column`,
# which SQLite has no native statement for; `ADD COLUMN` is SQLite's own
# syntax and needs no table rebuild. The downgrade takes the batch form
# because `DROP COLUMN` is the half whose support varies with the SQLite
# version — see `env.py`'s note on CI's SQLite trailing the deploy machine's.
def upgrade() -> None:
    op.add_column("issue_items",
                  sa.Column("prerequisites", sa.Text, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("issue_items") as batch:
        batch.drop_column("prerequisites")
