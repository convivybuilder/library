"""corrections_queues: one row per product

Revision ID: 8d88e5026d39
Revises: cb1fe64e0559
Create Date: 2026-10-10

issue_01M12NE0RFPJY12TT8CF1169V1 (E18·2), PR 1 of
`library/specs/one-home-per-work-item`: the additive half. A work item is
to resolve to exactly one home, an epic or its product's corrections
queue, with no column saying which — a reader follows `parent_id` into
whichever table it names. This is the table a correction's `parent_id`
will eventually name; nothing in this PR points a `parent_id` at it yet.

**ADDITIVE ONLY.** No existing column, row or write changes. A correction
is still stored with `parent_id IS NULL`, exactly as before this migration
runs — the backfill that points existing corrections at their product's
queue is PR 2's migration, gated on its own soak window and deploy order,
not this one's.

**One row per product with any non-epic work item or any ledger entry
still carrying the literal `epic_id = 'corrections'`.** The seed does not
filter on `parent_id`, so a product whose non-epic items all sit under
epics still gets a queue, empty until a correction is filed there. `product` is `UNIQUE`,
backing the one-queue-per-product rule `_validate_parent`'s same-product
check depends on with a second, schema-level guarantee — a future write
path that forgot the check could still not insert a second queue for one
product.

**This table carries no `ForeignKeyConstraint` of its own, and
`issue_items.parent_id` is not altered to add one pointing here.**
`issue_items.parent_id` already carries `fk_issue_items_parent_id`,
declared against `issue_items.id` alone — unenforced, like every FK in
this tree (`PRAGMA foreign_keys` is off for every connection `store.py`
opens; see `b4c1f7a92e08`'s docstring for why that is deliberate), but
still a statement about what the column means. Once PR 2's backfill
points a correction's `parent_id` at a row in THIS table instead, that
statement goes stale for every correction — a known, accepted gap the
build plan names (`library/specs/one-home-per-work-item`, "The catch")
and nothing enforces today, and not one this migration's own table can
fix by declaring a constraint of its own.

**THE SEED LOOP ONLY RUNS AGAINST A REAL CONNECTION**, the same split
`d70d450ef8c7`'s backfill makes: `alembic upgrade head --sql` renders this
revision with no database to read, so `conn.execute(...)` returns `None`
there rather than a result to iterate — `op.get_context().as_sql` is how a
migration tells the two apart, and the preview skips the seed loop
entirely rather than raising `TypeError` on a `None` result.
"""
from __future__ import annotations

import datetime as _dt

from alembic import op
import sqlalchemy as sa

import store

revision = "8d88e5026d39"
down_revision = "cb1fe64e0559"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "corrections_queues",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("product", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )
    conn = op.get_bind()
    if op.get_context().as_sql:
        # No real data to read in an `--sql` preview; see the docstring above.
        return
    # `type IS NULL` too: library PR 236 clears the type on open non-epic
    # rows, and `NULL != 'epic'` is not true, so without it a product whose
    # corrections are all untyped would get no queue when 236 runs first.
    products = {row[0] for row in conn.execute(sa.text(
        "SELECT DISTINCT product FROM issue_items "
        "WHERE type IS NULL OR type != 'epic'"))}
    products |= {row[0] for row in conn.execute(sa.text(
        "SELECT DISTINCT product FROM ledger_entries "
        "WHERE epic_id = 'corrections'"))}
    now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None).isoformat(" ")
    for product in sorted(products):
        conn.execute(sa.text(
            "INSERT INTO corrections_queues (id, product, created_at) "
            "VALUES (:id, :product, :created_at)"),
            {"id": f"cq_{store.new_ulid()}", "product": product,
             "created_at": now})


def downgrade() -> None:
    op.drop_table("corrections_queues")
