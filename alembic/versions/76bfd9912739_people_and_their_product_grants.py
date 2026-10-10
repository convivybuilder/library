"""people and their product grants

Revision ID: 76bfd9912739
Revises: e4894432e1aa
Create Date: 2026-09-17 16:06:33.646711

First slice of "Person identity in the Library" (issue_01M2QWC5NZW824FV6DKNFSEJEY),
scoped by issue_01M2RSE08WRRB1RJY1ZCSMD6EG. Two new tables, no antecedent in
Orient's control database, so `op.create_table` is right here for the same
reason `52afccc56e4f`'s own docstring gives.

**`people` and `person_products` rather than one table with a JSON column.**
`profiles.include` is this repo's existing shape for "a set of things, stored
per row" — a JSON-serialised list on the owning row. That shape does not fit
here: the read route's whole security design is `_scope_clause` (`app.py:1056`),
which filters in SQL on a bare `product` column, exactly as `issue_items`,
`signals` and `review_records` already are. A JSON array would need exploding
through `json_each` and re-aliasing before that clause could touch it, which is
a second thing to keep in step with every other scoped table in this file
instead of one more row shaped like them. `person_products` carries a `product`
column for the same reason `issue_items` does, and the read route applies
`_scope_clause` to it unmodified.

**The read route never joins `people` at all, and that is a consequence of the
epic's own disclosure rule, not an oversight.** An unknown person and a person
with an empty set must answer identically, field for field. Looking `people`
up first would hand the route a display name to return for one case and
nothing for the other — a difference nothing asked for and the exact leak the
rule exists to close. So `people` exists to hold the identifier and the display
name for whatever later reads or writes need them (the admin route, the
bootstrap, the creator-default — all out of scope here per the item's own
"Out of scope" section), and the done-when's read route is answered entirely
out of `person_products`, where "no such person" and "a person with nothing
granted" are the same zero rows for the same reason they always were.

**No foreign key from `person_products.person_id` to `people.id`.** SQLite
does not enforce `FOREIGN KEY` unless a connection opts in with `PRAGMA
foreign_keys=ON`, and `store.py` never does — every existing FK constraint in
this schema (`issue_leases.root_id`, `review_records.signal_id`, ...) is
documentation of intent rather than an enforced rule, and adding one here would
claim a guarantee the service does not keep. Nothing in this slice writes
`person_products` through code the FK could have guarded anyway: the item's
"Out of scope" section reserves the write path to a later slice, and until
then a row is written by hand, the same footing `roster_agents` and `profiles`
started on before `crew_registry.py` existed.

No default column set (`created_at`/`created_by`/`updated_at`/`updated_by`)
on `person_products`, unlike `roster_agents`/`profiles`. Those name who
changed a roster entry or a profile LAST, because both are upserted in place;
a product grant is closer to `review_records`' shape — a fact recorded once,
identified by who granted it and when (`granted_by`/`granted_at`), with no
"last updated" because revoking a grant is a delete, not an update, and there
is nothing else on the row to change.
"""
from alembic import op
import sqlalchemy as sa


revision = '76bfd9912739'
down_revision = 'e4894432e1aa'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "people",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("display_name", sa.String(300), nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("created_by", sa.String(200), nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
        sa.Column("updated_by", sa.String(200), nullable=False),
    )
    op.create_table(
        "person_products",
        sa.Column("person_id", sa.String(40), nullable=False),
        sa.Column("product", sa.String(64), nullable=False),
        sa.Column("granted_at", sa.DateTime, nullable=False),
        sa.Column("granted_by", sa.String(200), nullable=False),
        sa.PrimaryKeyConstraint("person_id", "product",
                                name="pk_person_products"),
    )


def downgrade() -> None:
    op.drop_table("person_products")
    op.drop_table("people")
