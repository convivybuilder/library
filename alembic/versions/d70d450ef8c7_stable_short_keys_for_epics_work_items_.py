"""stable short keys for epics, work items and corrections
(issue_01M2P5CBY5JY8SK5W344CV82AE)

Revision ID: d70d450ef8c7
Revises: ba532e49e577
Create Date: 2026-10-01 00:36:23.216119

**ADDITIVE: one nullable column on `issue_items`, plus a new table.**
`item_key` is the short, stable, human-typed label this issue adds — `E9`
for an epic, `E9·4` for a sub-item of it, `CQ·63` for a correction (an item
with no parent that is not itself an epic) — and `issue_item_key_counters` is
what keeps a number from ever being reissued: a `(scope_kind, scope_id) ->
next_value` row per product (an epic's own numbering, and a correction's) and
per epic (the numbering of its own sub-items), read and advanced together,
inside the same write transaction that mints the next key
(`store.mint_item_key`), rather than derived from `MAX()` or list order — a
deleted or resolved item's number has to stay retired, which neither of
those can promise.

**THE KEY IS MINTED ONCE, AT CREATE, AND NOTHING AFTER THAT EVER CHANGES
IT** — not `/correct`'s re-parenting, not a retype. That is the simplest of
the rules the issue allows, and the one adopted here: a key once printed
stays attached to the item it named, whatever that item's position or type
becomes later. `issue_item_key_counters` existing per epic rather than per
Course position is what makes that hold — an epic's own key and a Course's
order already live on two different axes (the issue's own argument for why
position cannot be the key), and nothing here ties a sub-item's numbering to
anything about its epic besides the epic's id.

**BACKFILLED HERE, in `created_at` order (ties by `id`) — the same order the
store mints the next one in**, so a vault with history gets the keys it
would have minted all along: the oldest epic in a product is `E1`, its
oldest sub-item `E1·1`, the oldest parentless non-epic item `CQ·1`.

**AN EPIC'S KEY DEPENDS ONLY ON ITS PRODUCT, NEVER ON ITS OWN PARENT** —
`store.mint_item_key` checks `item_type == ITEM_TYPE_EPIC` before it ever
looks at `parent_id`, so a sub-epic nested under another epic gets its own
`E<n>`, not a key built from its parent's. The backfill makes the same
choice, and it is what keeps this correct at any nesting depth in one
statement: every epic's key comes from a single per-product count, with no
row's key depending on any other row's, so it cannot matter how many epics
are stacked above it.

Three passes: epics (every one, any depth) and corrections first, each an
independent per-product sequence read in one statement apiece; then
sub-items, which read their own epic parent's key — already written by the
first pass, whatever that epic's own depth, before this one ever runs.
**The sub-item pass runs in a loop, level by level, rather than assuming one
pass is enough**: an item is only picked up once its own parent already has
a key, so a sub-item of a sub-item (a shape `_validate_parent` refuses on
the live write path today, but that a row from before that rule existed is
not guaranteed to honor) is picked up the next time through, once its
parent's pass has run. The loop stops the first time a pass assigns
nothing, and the backfill then asserts no row was left keyless — a failure
there means historical data the three passes above cannot resolve (for
instance a cycle), and the upgrade refuses rather than ship it silently.

**THE LOOP AND THE ASSERTION ONLY RUN AGAINST A REAL CONNECTION.** `alembic
upgrade head --sql` renders this revision without a database to read, so
`conn.execute(...)` returns no result there (`op.get_context().as_sql` is how
a migration tells the two apart) — the preview still emits one pass of the
sub-item statement so its shape is visible, but the data-dependent loop and
the backstop it feeds are meaningless against no data and are skipped rather
than raising `AttributeError` on a `None` result.

**THE COUNTERS ARE SEEDED PAST THE BACKFILL, not left for the first live
write to discover.** A product or epic with no existing rows gets no counter
row at all — `store.mint_item_key` starts a fresh scope at 1 the same way a
brand-new install would.
"""
from alembic import op
import sqlalchemy as sa


revision = 'd70d450ef8c7'
down_revision = 'ba532e49e577'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("issue_items", sa.Column("item_key", sa.Text, nullable=True))
    # RAW DDL FOR THE PARTIAL INDEX, matching the baseline's
    # `ix_issue_leases_live_root` and `ae5e7a024ffb`'s own choice of the same
    # spelling over `op.create_index`'s `sqlite_where` kwarg — one way to say
    # a partial index in this repo, not two. Scoped to `product` because the
    # key alone is not unique across products, the same way `E9` in `vivy`
    # and `E9` in `constance` are two different epics.
    op.execute(
        "CREATE UNIQUE INDEX ix_issue_items_product_item_key "
        "ON issue_items (product, item_key) WHERE item_key IS NOT NULL")

    op.create_table(
        "issue_item_key_counters",
        sa.Column("scope_kind", sa.String(20), primary_key=True),
        sa.Column("scope_id", sa.String(64), primary_key=True),
        sa.Column("next_value", sa.Integer, nullable=False),
    )

    # Epics: `E<n>`, one per-product sequence — EVERY epic, whatever its own
    # depth in the tree (a sub-epic included), because the key tracks only
    # the product column, which every row already carries. No row's key
    # depends on any other row's here, so one statement is correct at any
    # nesting depth.
    op.execute(
        "UPDATE issue_items SET item_key = 'E' || ranked.rn "
        "FROM (SELECT id, ROW_NUMBER() OVER ("
        " PARTITION BY product ORDER BY created_at, id) AS rn "
        " FROM issue_items WHERE type = 'epic'"
        ") AS ranked WHERE issue_items.id = ranked.id")
    # Corrections: `CQ·<n>`, one per-product sequence, independent of the
    # epics' — a product's first epic and its first correction are both
    # numbered 1.
    op.execute(
        "UPDATE issue_items SET item_key = 'CQ·' || ranked.rn "
        "FROM (SELECT id, ROW_NUMBER() OVER ("
        " PARTITION BY product ORDER BY created_at, id) AS rn "
        " FROM issue_items WHERE parent_id IS NULL AND type != 'epic'"
        ") AS ranked WHERE issue_items.id = ranked.id")

    # Sub-items: `<epic's key>·<n>`, one per-epic sequence. Every epic
    # already has its key from the pass above, so an ordinary tree resolves
    # in one trip through this loop — but it RUNS LEVEL BY LEVEL rather than
    # assuming that: each pass only picks up a row whose own parent already
    # has a key, so a sub-item of a sub-item — a shape the live write path
    # refuses today, but an older row is not guaranteed to honor — is picked
    # up the next time through, once its parent's own pass has run. The loop
    # stops the first time a pass assigns nothing.
    sub_item_pass = sa.text(
        "UPDATE issue_items SET item_key = "
        " (SELECT p.item_key FROM issue_items p WHERE p.id = issue_items.parent_id)"
        " || '·' || ranked.rn "
        "FROM (SELECT id, ROW_NUMBER() OVER ("
        " PARTITION BY parent_id ORDER BY created_at, id) AS rn "
        " FROM issue_items WHERE parent_id IS NOT NULL AND type != 'epic'"
        " AND item_key IS NULL AND parent_id IN "
        " (SELECT id FROM issue_items WHERE item_key IS NOT NULL)"
        ") AS ranked WHERE issue_items.id = ranked.id")
    conn = op.get_bind()
    if op.get_context().as_sql:
        # No real data to loop against in an `--sql` preview — one pass is
        # enough to show the statement's shape; see the docstring above.
        conn.execute(sub_item_pass)
    else:
        while True:
            result = conn.execute(sub_item_pass)
            if result.rowcount <= 0:
                break

        # **THE BACKSTOP.** If anything above could not resolve a row — the
        # only way left is historical data the three passes' assumptions
        # don't cover, since every live write mints its key inside the same
        # transaction as the row itself — fail the upgrade loudly rather
        # than ship a silently keyless row that `store.item_dict` would
        # serve as `"key": None` forever.
        remaining = conn.execute(sa.text(
            "SELECT COUNT(*) FROM issue_items WHERE item_key IS NULL"
        )).scalar()
        if remaining:
            raise RuntimeError(
                f"stable_short_keys backfill left {remaining} issue_items "
                "row(s) with no item_key; refusing to continue the migration")

    # Seed every scope the backfill just touched past its own count, so the
    # next live create continues the sequence rather than restarting it.
    op.execute(
        "INSERT INTO issue_item_key_counters (scope_kind, scope_id, next_value) "
        "SELECT 'epic', product, COUNT(*) + 1 FROM issue_items "
        "WHERE type = 'epic' GROUP BY product")
    op.execute(
        "INSERT INTO issue_item_key_counters (scope_kind, scope_id, next_value) "
        "SELECT 'correction', product, COUNT(*) + 1 FROM issue_items "
        "WHERE parent_id IS NULL AND type != 'epic' GROUP BY product")
    op.execute(
        "INSERT INTO issue_item_key_counters (scope_kind, scope_id, next_value) "
        "SELECT 'item', parent_id, COUNT(*) + 1 FROM issue_items "
        "WHERE parent_id IS NOT NULL AND type != 'epic' GROUP BY parent_id")


def downgrade() -> None:
    op.drop_table("issue_item_key_counters")
    op.execute("DROP INDEX ix_issue_items_product_item_key")
    with op.batch_alter_table("issue_items") as batch:
        batch.drop_column("item_key")
