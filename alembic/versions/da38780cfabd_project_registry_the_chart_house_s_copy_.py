"""project_registry: the Chart House's copy of the project registry
(CQ·241, platform/decisions/0098's 2026-10-25 amendment)

Revision ID: da38780cfabd
Revises: 5cb0d7dc016f
Create Date: 2026-10-25 16:07:22.333368

**Schema only — no data.** `load_project_registry.py` is the operator step
that loads the registry file this replaces, run once against a copy of the
live vault and then the live vault itself, the same two-step
`load_crew_registry.py` already takes and for the same reason: this
migration tree runs against the Library's database alone, and the registry
file lives in a sibling checkout this tree has no business reading.

**One row per top-level key in the file, project or metadata alike** — the
amendment's own shape: "an install has one crew registry, held by the
Library in its Chart House." `slug` holds a project's own slug or a
`_`-prefixed metadata key (`_scopes`, `_meta`, and so on) exactly as the
file's own top level does, and `entry` holds that key's value, serialised
as JSON — the same `Text`-column-holds-JSON shape `profiles.include`
already uses in this tree, because SQLite has no native object type and
`project_registry.py` serialises and parses at the Python layer rather than
through a second schema definition here.

**A NEW table this service owns outright, so `op.create_table` is right
here, the same reasoning `roster_agents` and `profiles`' own migration
gives**: nothing in Orient has a `project_registry` table to drift away
from.

**State to read and revise, not an event log** — `load_project_registry.py`
loads an empty table, or replaces the whole contents under `--replace`, so
there is one row per key and no `_events` table beside it, matching `roster_agents` and
`profiles`' own choice for the same reason: nothing in this item's scope
reads "how did this registry get this way."
"""
from alembic import op
import sqlalchemy as sa

revision = "da38780cfabd"
down_revision = "5cb0d7dc016f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_registry",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("slug", sa.String(64), nullable=False, unique=True),
        sa.Column("entry", sa.Text, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
        sa.Column("updated_by", sa.String(200), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("project_registry")
