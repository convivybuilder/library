"""tool_policy: the per-(agent, crew, model, target) authorization surface (ADR-0048)

Revision ID: c68fdb59edfc
Revises: 0b1e7c4a9d52
Create Date: 2026-08-19

**A NEW TABLE THIS SERVICE OWNS OUTRIGHT, so `op.create_table` is the right tool
here** where the baseline used raw DDL — `store`'s docstring explains why the
baseline copied Orient's carried-over schema statement-for-statement rather than
modelling it: during the shadow those rows belonged to Orient, and a model here
would be a second definition free to drift. `tool_policy` has no Orient
antecedent; the Library is this table's only author from row one, so there is
nothing for a model to drift away from.

**State to read and revise, not an event to approve** (ADR-0048). One row per
(agent, crew, model, target) combination, upserted in place — a grant that
changes its mind overwrites the row rather than appending a new one, which is
the Reading Room's own provisioning shape and not the decision-plane's
raise/approve/consume one. `tool_policy_events` is the append-only history
beside it, matching every other write surface in this file
(`issue_events`, `course_events`, `decision_events`): the current-state table
answers "what applies now" in one lookup, and the events table answers "how did
it get this way" for whoever needs to ask.

**`tool_policy_events.id` doubles as the cheap version number ADR-0048 asks
for.** `MAX(id)` (or 0 on an empty table) is a monotonic counter that bumps on
every write and costs nothing beyond the primary key SQLite already maintains
— no separate counter row to keep in sync with the events it is meant to
count.
"""
from alembic import op
import sqlalchemy as sa

revision = "c68fdb59edfc"
down_revision = "0b1e7c4a9d52"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tool_policy",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("agent", sa.String(64), nullable=False),
        sa.Column("crew", sa.String(64), nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("target", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(10), nullable=False),
        sa.Column("created_by", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_by", sa.String(200), nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
        sa.UniqueConstraint("agent", "crew", "model", "target",
                             name="uq_tool_policy_combination"),
    )
    op.create_index("ix_tool_policy_lookup", "tool_policy",
                     ["agent", "crew", "model"])
    op.create_table(
        "tool_policy_events",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("actor", sa.String(200), nullable=False),
        sa.Column("agent", sa.String(64), nullable=False),
        sa.Column("crew", sa.String(64), nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("target", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(10), nullable=False),
        sa.Column("at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_tool_policy_events_at", "tool_policy_events", ["at"])


def downgrade() -> None:
    op.drop_index("ix_tool_policy_events_at", table_name="tool_policy_events")
    op.drop_table("tool_policy_events")
    op.drop_index("ix_tool_policy_lookup", table_name="tool_policy")
    op.drop_table("tool_policy")
