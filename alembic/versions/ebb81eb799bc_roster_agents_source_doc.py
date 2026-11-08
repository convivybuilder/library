"""roster_agents.source_doc: which document the roster reconcile added a name
for (issue_01M5H8RPFPM6VBVJCPBND1XVBT)

Revision ID: ebb81eb799bc
Revises: a4644bf94fab
Create Date: 2026-10-22

**ADDITIVE: ONE NULLABLE TEXT COLUMN ON `roster_agents`, NOTHING ELSE**,
the same shape `5c10b5ba7f45` already gives `issue_items.short_description`.
Every existing row reads NULL, which means "not added by the reconcile" —
a name seeded by `load_crew_registry.py` or added by hand through `PUT
/roster/{name}` carries no document and keeps reading NULL forever, since
neither write path sets this column.

`agent_sync.py` is the one writer: it fills `source_doc` with the document id
(`<crew>/agents/<name>`) when it adds a name the served knowledge tree's own
agent definitions supplied, so a later scan can tell whether that document is
still there without re-deriving which rows it once added.

`ADD COLUMN` is native SQLite; `DROP COLUMN` support varies with the SQLite
version, so the downgrade uses the batch form, matching `5c10b5ba7f45` and
`a8d2c5e91f37`.
"""
from alembic import op
import sqlalchemy as sa

revision = "ebb81eb799bc"
down_revision = "a4644bf94fab"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("roster_agents",
                  sa.Column("source_doc", sa.Text, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("roster_agents") as batch:
        batch.drop_column("source_doc")
