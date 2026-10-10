"""black_box_registrations: the model and gateway a black-box crew declares
at registration (issue_01M19QQNTWD4RD9MSZZRKHYM7N)

Revision ID: 9c1c4f2b8a3e
Revises: 52afccc56e4f
Create Date: 2026-09-10

**Only the two fields `tool_policy` has no column for.** The declared tool
permissions ARE `tool_policy` rows, written through the same
`store.upsert_policy` `PUT /policy` already uses — this migration is not a
second policy store, only the model/gateway binding beside it.

**A NEW table this service owns outright, the same reasoning
`c68fdb59edfc` and `f71949b78df4` give**: no black box ever registered
anywhere before this route, so there is no prior schema for a model here to
drift away from.

**One row per crew, upserted in place, not appended** — a crew that
re-registers with a new model or gateway overwrites its own row, the same
shape `crew_registry.profile_put` uses for a rebind and `tool_policy`'s own
migration uses for a grant that changes its mind. No `_events` table beside
it: nothing in this work item's scope reads "how did this binding change
over time", and `tool_policy_events` already carries the audit trail for
the one part of this route that is state a caller revises and history a
caller can be asked to explain.
"""
from alembic import op
import sqlalchemy as sa

revision = "9c1c4f2b8a3e"
down_revision = "52afccc56e4f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "black_box_registrations",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("crew", sa.String(64), nullable=False, unique=True),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("gateway", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("created_by", sa.String(200), nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
        sa.Column("updated_by", sa.String(200), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("black_box_registrations")
