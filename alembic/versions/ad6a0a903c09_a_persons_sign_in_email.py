"""a person's sign-in email: the address Cloudflare Access signs them in
with, optional, and unique without regard to case

Revision ID: ad6a0a903c09
Revises: fc50f714c340
Create Date: 2026-10-12

Fifth slice of "Person identity in the Library" (issue_01M2QWC5NZW824FV6DKNFSEJEY),
scoped by issue_01M4QQZ7VTANJ5ZGF630DCB0Y7: Orient holds only a viewer's
sign-in email and asks the Library which projects that viewer may see, so a
person needs a column the Library can look that email up by.

**ADDITIVE: one nullable column and one unique index, nothing else.**
`ADD COLUMN` is native SQLite, `962c9c87df11`'s reasoning for the same shape
of change. Every person created before this revision, and the bootstrap's
own first person, gets `NULL` — the epic's own words are "a person is
created without one" — and every existing route that reads or writes
`people` keeps working unchanged, since nothing before this slice selects or
writes this column.

**The uniqueness is "without regard to case", and the index enforces it
being an ORDINARY unique index because the value stored is ALWAYS
lower-cased first.** `app.py`'s write route trims and lower-cases before
ever reaching SQL — the same division of labour `76bfd9912739` draws for
`display_name` and `change_person_products` draws for a product name — so
two spellings of one address can never both reach this column, and a plain
`CREATE UNIQUE INDEX` is enough; no `COLLATE NOCASE` is needed to do work the
write path already did. A NULL is not compared to another NULL by SQLite's
own uniqueness rule, so any number of people may hold no email at once.

**One new table, `person_email_changes`, shaped like `person_product_changes`
for the same reason: a change to a person's email is recorded with its
administrator and the time, read back rather than overwritten, the way a
change to the project set already is** (`0652cc6b2cee`). `email` holds the
value the row made current, `NULL` for a clear, so the history shows what
happened without a second column naming the action. No foreign key to
`people.id`, for `76bfd9912739`'s reason: `store.py` never enables `PRAGMA
foreign_keys`, so a constraint here would claim a guarantee the service does
not keep.

Purely additive — one new column, one new index, one new table, no backfill,
no data migration, so the installer may apply it unattended.
"""
from alembic import op
import sqlalchemy as sa


revision = 'ad6a0a903c09'
down_revision = 'fc50f714c340'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("people",
                  sa.Column("sign_in_email", sa.String(254), nullable=True))
    op.create_index("ix_people_sign_in_email", "people", ["sign_in_email"],
                     unique=True)
    op.create_table(
        "person_email_changes",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("person_id", sa.String(40), nullable=False),
        sa.Column("email", sa.String(254), nullable=True),
        sa.Column("changed_at", sa.DateTime, nullable=False),
        sa.Column("changed_by", sa.String(200), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("person_email_changes")
    op.drop_index("ix_people_sign_in_email", table_name="people")
    with op.batch_alter_table("people") as batch:
        batch.drop_column("sign_in_email")
