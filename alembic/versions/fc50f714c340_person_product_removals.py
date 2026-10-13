"""person product removals: the current-state mirror of `person_products`
for a default project a person no longer sees

Revision ID: fc50f714c340
Revises: 63bf68004959
Create Date: 2026-10-12 01:19:10.428018

Fourth slice of "Person identity in the Library" (issue_01M2QWC5NZW824FV6DKNFSEJEY),
scoped by issue_01M2RSFKJBEHD5Y76X0B6XAYZG: a person's set defaults to the
projects the registry's `created_by` names them as having created, and an
administrator's removal of a default project has to be recorded somewhere,
because `person_products` records only explicit grants.

**One new table, shaped like `person_products` on purpose.** A default
project was never granted, so revoking it has no row in `person_products` to
delete — there never was one. `person_product_removals` is that table's
mirror for the opposite direction: one row per product this person's
default currently excludes, identified by who excluded it and when,
exactly as `person_products` names who granted a product and when.
Restoring a removed default deletes the row; `store.change_person_products`
never writes a `person_products` row for a default project, so there is
nothing else to touch.

**No new column on `person_product_changes`, and no new `action` value.**
That table already distinguishes only `'grant'` (this call made a product
visible) and `'revoke'` (this call made it invisible) — true of a default's
removal and restoration exactly as it is true of an explicit grant's
revocation and re-grant, so the existing two actions cover both tables
without widening the CHECK constraint. Which table a given row's action
touched is derivable from whether the product was that person's default at
the time, which the registry (not this schema) records.

No foreign key to `people.id` or to `person_products`, for `76bfd9912739`'s
reason: `store.py` never enables `PRAGMA foreign_keys`, so a constraint here
would claim a guarantee the service does not keep.

Purely additive — one new table, no backfill, no data migration.
"""
from alembic import op
import sqlalchemy as sa


revision = 'fc50f714c340'
down_revision = '63bf68004959'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "person_product_removals",
        sa.Column("person_id", sa.String(40), nullable=False),
        sa.Column("product", sa.String(64), nullable=False),
        sa.Column("removed_at", sa.DateTime, nullable=False),
        sa.Column("removed_by", sa.String(200), nullable=False),
        sa.PrimaryKeyConstraint("person_id", "product",
                                name="pk_person_product_removals"),
    )


def downgrade() -> None:
    op.drop_table("person_product_removals")
