"""project_registry_edits: the audit trail for PATCH /registry/crews/{slug}
(CQ·249, §6.8 of knowledge#1062)

Revision ID: 42d8d9e44390
Revises: a63c8a7f1b85
Create Date: 2026-10-27 15:30:00.000000

**Schema only — no data.** `project_registry.patch_crew` is the one writer,
inserting a row in the same `BEGIN IMMEDIATE` transaction as the field
edit it records, and only when the merged row actually changed something.

**One row per field-changing patch, not one row per changed field.**
`changes` holds every field this patch changed as a JSON object mapping
each field to `{"before": ..., "after": ...}`, so the audit trail reads
one patch as one entry — the same reasoning that gives `project_registry`
itself one row per slug rather than one row per field.

**A NEW table this service owns outright**, the same reasoning
`project_registry`'s own migration (`da38780cfabd`) gives: nothing in
Orient has a `project_registry_edits` table to drift away from.

**PLAIN SQL, NO SQLITE-VEC** — `op.create_table` with plain columns only,
so this runs on alembic's own connection, which does not load the `vec0`
extension.
"""
from alembic import op
import sqlalchemy as sa

revision = "42d8d9e44390"
down_revision = "a63c8a7f1b85"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_registry_edits",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("slug", sa.Text, nullable=False),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("at", sa.Text, nullable=False),
        sa.Column("changes", sa.Text, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("project_registry_edits")
