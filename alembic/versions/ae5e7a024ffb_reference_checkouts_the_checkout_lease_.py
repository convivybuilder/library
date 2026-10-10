"""reference_checkouts: the checkout lease for a Reference Room document

Revision ID: ae5e7a024ffb
Revises: a37e9060eee8
Create Date: 2026-08-20 08:53:09.035736

`issue_01M0DVAH9KAACHH0RRWQZ3KZTE`. A new table this service owns outright, so
`op.create_table` is the right tool here, matching `tool_policy`
(`c68fdb59edfc`) rather than the baseline's raw DDL — there is no Orient
antecedent for this row to drift away from.

**A document's checkout is `issue_leases`' one-live-lease invariant, applied to
a git-backed vault instead of a database row** (ADR-0022 — the Reference
Room's content stays files in `knowledge`, so this table holds the LOCK, not
the document). `doc_id` is the KB document id from `~/.knowledge-index/
index.db` — a separate database this one has no foreign key into, so it is
stored as a plain string rather than constrained. `ix_reference_checkouts_live_doc`
is the same partial-unique shape as `ix_issue_leases_live_root`: at most one
unreleased row per `doc_id`, enforced by the database rather than by a
check-then-insert race in application code.
"""
from alembic import op
import sqlalchemy as sa


revision = 'ae5e7a024ffb'
down_revision = 'a37e9060eee8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reference_checkouts",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("doc_id", sa.String(300), nullable=False),
        sa.Column("holder", sa.String(200), nullable=False),
        sa.Column("run_id", sa.String(100)),
        sa.Column("taken_at", sa.DateTime, nullable=False),
        sa.Column("heartbeat_at", sa.DateTime, nullable=False),
        sa.Column("released_at", sa.DateTime),
        sa.Column("release_reason", sa.String(20)),
        sa.Column("released_by", sa.String(200)),
    )
    op.create_index("ix_reference_checkouts_doc_id", "reference_checkouts",
                     ["doc_id"])
    op.create_index("ix_reference_checkouts_holder", "reference_checkouts",
                     ["holder"])
    # RAW DDL FOR THE PARTIAL INDEX, matching the baseline's own
    # `ix_issue_leases_live_root` — `op.create_index`'s `sqlite_where` kwarg
    # would work too, but the baseline already establishes raw SQL as this
    # repo's way of spelling a `WHERE released_at IS NULL` index, and a second
    # spelling beside it is a thing to notice rather than a convenience.
    op.execute(
        "CREATE UNIQUE INDEX ix_reference_checkouts_live_doc "
        "ON reference_checkouts (doc_id) WHERE released_at IS NULL")


def downgrade() -> None:
    op.execute("DROP INDEX ix_reference_checkouts_live_doc")
    op.drop_index("ix_reference_checkouts_holder",
                   table_name="reference_checkouts")
    op.drop_index("ix_reference_checkouts_doc_id",
                   table_name="reference_checkouts")
    op.drop_table("reference_checkouts")
