"""roster_agents and profiles: crew composition and profile selection rules
(issue_01M19QP8S0QPKM0MN7XHYWNGKY)

Revision ID: 52afccc56e4f
Revises: 4d80ea9474e4
Create Date: 2026-09-06

**Schema only — no data.** What this replaces is `ALLOWED_AUDIENCE` (a
Python set literal in agent-platform's `lib/knowledge/knowledge.py`) and
`projects/<product>/profiles/*.yaml` in the knowledge repository, and
loading the 58 current roster names and 24 current profiles into these
tables means reading files from two sibling repository checkouts that do
not exist in the environment this migration tree itself has to run in —
`alembic/env.py`'s own refusal keeps this tree to the Library's database
alone, and CI checks out only this repository. So the data load is
`load_crew_registry.py`, run once by an operator against a real checkout of
both, against a COPY of the live database first (`~/.library/library.db`)
— never a fixture, matching this repo's own
migration-testing convention, because alembic's connection does not load
sqlite-vec while an app session does.

**A NEW pair of tables this service owns outright, so `op.create_table` is
right here, the same reasoning `tool_policy`'s migration gives**: neither
table has an Orient antecedent, so there is nothing for a model to drift
away from.

**State to read and revise, not an event log** — a crew's roster and a
profile's selection rules are what `crew_registry.py`'s docstring calls
them, matching `tool_policy`'s own shape: one row per name, one row per
`(project, variant)`, upserted in place rather than appended to. No
`_events` table beside either, unlike `tool_policy_events`: nothing in this
work item's scope reads "how did this roster get this way", and adding an
audit trail nobody reads yet is scope this migration does not need.

**`include` and `claude_md_extras` are `Text` columns holding JSON**, the
same shape `issue_items.refs` and `issue_items.topics` already use in the
baseline — SQLite has no native array or object type, and `crew_registry.py`
serialises and parses both at the Python layer rather than through a second
schema definition here.
"""
from alembic import op
import sqlalchemy as sa

revision = "52afccc56e4f"
down_revision = "4d80ea9474e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "roster_agents",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("created_by", sa.String(200), nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
        sa.Column("updated_by", sa.String(200), nullable=False),
    )
    op.create_table(
        "profiles",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("project", sa.String(64), nullable=False),
        sa.Column("variant", sa.String(64), nullable=False),
        sa.Column("include", sa.Text, nullable=False),
        sa.Column("claude_md_extras", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("created_by", sa.String(200), nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
        sa.Column("updated_by", sa.String(200), nullable=False),
        sa.UniqueConstraint("project", "variant",
                             name="uq_profiles_project_variant"),
    )
    op.create_index("ix_profiles_lookup", "profiles", ["project", "variant"])


def downgrade() -> None:
    op.drop_index("ix_profiles_lookup", table_name="profiles")
    op.drop_table("profiles")
    op.drop_table("roster_agents")
