"""merge_admission_settings: the append-only record of who turned merge-
admission enforcement on or off, and when (CQ·219, issue_01M5FM2W1JV82A3XH3P0R5TX8J)

Revision ID: 5cb0d7dc016f
Revises: 495cb5bcc41f
Create Date: 2026-10-25

**A NEW TABLE THIS SERVICE OWNS OUTRIGHT**, `f71949b78df4`'s own reasoning
for `machine_certificates`: nothing on Orient's side ever held a merge-
admission setting, so there is no prior schema here to drift away from.

**Append-only, and that is the whole design.** `POST /merge/admission`
reads the newest row (`store.merge_admission_enforced`) rather than a
separate current-state table kept in sync with an events table the way
`tool_policy`/`tool_policy_events` are — there is nothing here but the
history, so the newest row IS the current state, with no second table to
drift from the first. No route ever updates or deletes a row.

**An empty table enforces.** `enforced` has no default and no row exists
until the first `PUT /merge/admission/enforcement` call, so a fresh vault,
or one where nobody has ever flipped the switch, enforces by reading no
row at all — `store.merge_admission_enforced`'s own rule, not a value
written here.

`set_by_client` is the authenticated credential's own name (`ident.actor`),
`acting_human` the person it reported acting for (`identity.attested_
human`), and `reason` free text, optional. All three are the write's own
record, never read back to decide anything — the same split `item_model_
choice` (`a8d2c5e91f37`) draws between the capability that authorizes a
write and the human that a client attests to.

`sa.Boolean(create_constraint=False)`, matching `495cb5bcc41f`: SQLite's
`ADD COLUMN`/`CREATE TABLE` needs no CHECK here, since whether a row
enforces is a plain fact recorded at the write, not a value this schema
validates.
"""
from alembic import op
import sqlalchemy as sa

revision = "5cb0d7dc016f"
down_revision = "495cb5bcc41f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "merge_admission_settings",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("enforced", sa.Boolean(create_constraint=False), nullable=False),
        sa.Column("set_by_client", sa.Text, nullable=False),
        sa.Column("acting_human", sa.Text, nullable=False),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("set_at", sa.Text, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("merge_admission_settings")
