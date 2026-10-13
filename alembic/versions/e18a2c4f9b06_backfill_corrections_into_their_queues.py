"""backfill every correction into its product's corrections queue

Revision ID: e18a2c4f9b06
Revises: 8d88e5026d39
Create Date: 2026-10-10

issue_01M4J613MK4QAKT4CZ54NY0BR8 (CQ·200), PR 2 of
`library/specs/one-home-per-work-item`: the switch. `8d88e5026d39` (PR 1)
added `corrections_queues` and seeded one row per product that needed one,
but wrote no `parent_id` and changed no existing row — every correction
still read `parent_id IS NULL` after it ran. This is the migration that
moves them: a work item resolves to exactly one home, an epic or its
product's corrections queue, with no column saying which, and this is the
PR that makes a NULL parent on a non-epic row stop meaning anything.

**WHAT MOVES, AND WHAT DOES NOT.** Every non-epic `issue_items` row with
`parent_id IS NULL` — open or closed alike (the operator's ruling, Slate
a-v5j93k, 2026-10-09: closed corrections get the queue too, "for
cleanliness") — gets its product's queue id. No other column changes:
`item_key` stays exactly as `d70d450ef8c7` minted it, `state` is untouched,
and this writes no row to `issue_comments` or `issue_events` — a
re-parenting the write path itself now does silently (`/correct`'s own
change in this PR), so the historical equivalent is silent too. An epic
with `parent_id IS NULL` stays top-level; `type = 'epic'` is excluded from
every statement below. `ledger_entries.epic_id = 'corrections'` — the
literal `d12fde7d50e9` introduced for exactly this case — becomes the
entry's own product's queue id, the same derivation that migration already
made for every other branch of its `CASE`.

**A DANGLING OR CROSS-PRODUCT PARENT IS LEFT AS IT IS.** This migration
only ever touches a `parent_id` that is NULL; a non-epic row whose parent
names a deleted item, a non-epic, or an item in another product is
untouched, exactly as `library/specs/one-home-per-work-item` says a dry
run should report it rather than inventing it a home. `GET
/items?home=invalid`, added alongside this migration, is the route that
surfaces it afterward.

**RE-SEEDS A QUEUE `8d88e5026d39` MISSED, BEFORE THE BACKFILL RUNS, SO THE
REFUSAL BELOW IS A BACKSTOP RATHER THAN THE ORDINARY PATH FOR AN UNLUCKY
PRODUCT.** `8d88e5026d39` seeded one queue per product with a non-epic row
or a `'corrections'` ledger entry at the moment it ran; the live vault kept
taking writes under its own code, which still stored NULL for a
parentless non-epic, for however long this PR's own review and merge took
to follow it. A product that filed its first correction in that window has
no queue row yet, and without re-running the same seed here, this
migration's own backfill would have nothing to point it at. The re-seed is
read-and-insert inside this migration's own transaction, which is what
makes it safe against the same race `8d88e5026d39`'s `UNIQUE(product)`
guards against for the live write path — there is no concurrent writer
here to race.

**REFUSES RATHER THAN LEAVE A ROW HOMELESS**, matching
`d70d450ef8c7`'s own backstop: if any non-epic row is still parentless
after the re-seed and the backfill, the upgrade raises and nothing commits.
Reaching this would mean a non-epic row carries a `product` string the
re-seed step could not resolve to any queue, including one it just
inserted for that exact string — in practice unreachable, since both steps
read and write the identical `product` column, the same way a cycle is the
only way `d70d450ef8c7`'s own loop fails to make progress.

**THE DOWNGRADE RESTORES NULL AND `'corrections'`.** Every `parent_id`
naming a row in `corrections_queues` goes back to NULL, and every
`epic_id` doing the same on `ledger_entries` goes back to the literal.
`8d88e5026d39`'s own downgrade is what drops the table itself, run after
this one's.

**THE ROW-ITERATING RE-SEED AND THE SCALAR BACKSTOP ONLY RUN AGAINST A REAL
CONNECTION**, the same split `8d88e5026d39` and `d70d450ef8c7` both make:
`alembic upgrade head --sql` renders this revision with no database to
read, so a loop over `conn.execute(...)` or a `.scalar()` call would meet
`None` rather than a result. The two `UPDATE` statements carry no such
loop, so they still render in a preview for their shape.
"""
from __future__ import annotations

import datetime as _dt

from alembic import op
import sqlalchemy as sa

import store

revision = "e18a2c4f9b06"
down_revision = "8d88e5026d39"
branch_labels = None
depends_on = None

_NON_EPIC = "(type IS NULL OR type != 'epic')"


def upgrade() -> None:
    conn = op.get_bind()
    preview = op.get_context().as_sql

    if not preview:
        needs_queue = {row[0] for row in conn.execute(sa.text(
            "SELECT DISTINCT product FROM issue_items "
            f"WHERE parent_id IS NULL AND {_NON_EPIC}"))}
        needs_queue |= {row[0] for row in conn.execute(sa.text(
            "SELECT DISTINCT product FROM ledger_entries "
            "WHERE epic_id = 'corrections'"))}
        existing = {row[0] for row in conn.execute(
            sa.text("SELECT product FROM corrections_queues"))}
        now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
        now_iso = now.isoformat(" ")
        for product in sorted(needs_queue - existing):
            conn.execute(sa.text(
                "INSERT INTO corrections_queues (id, product, created_at) "
                "VALUES (:id, :product, :created_at)"),
                {"id": f"cq_{store.new_ulid()}", "product": product,
                 "created_at": now_iso})

    conn.execute(sa.text(
        "UPDATE issue_items SET parent_id = ("
        " SELECT id FROM corrections_queues "
        " WHERE product = issue_items.product"
        ") "
        f"WHERE parent_id IS NULL AND {_NON_EPIC}"))

    if not preview:
        remaining = conn.execute(sa.text(
            "SELECT COUNT(*) FROM issue_items "
            f"WHERE parent_id IS NULL AND {_NON_EPIC}"
        )).scalar()
        if remaining:
            raise RuntimeError(
                f"corrections backfill left {remaining} non-epic "
                "issue_items row(s) with no parent_id; refusing to "
                "continue the migration")

    conn.execute(sa.text(
        "UPDATE ledger_entries SET epic_id = ("
        " SELECT id FROM corrections_queues "
        " WHERE product = ledger_entries.product"
        ") "
        "WHERE epic_id = 'corrections'"))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text(
        "UPDATE issue_items SET parent_id = NULL "
        "WHERE parent_id IN (SELECT id FROM corrections_queues)"))
    conn.execute(sa.text(
        "UPDATE ledger_entries SET epic_id = 'corrections' "
        "WHERE epic_id IN (SELECT id FROM corrections_queues)"))
