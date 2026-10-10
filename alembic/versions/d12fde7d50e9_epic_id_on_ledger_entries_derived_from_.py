"""epic_id on ledger entries, derived from the work item

Revision ID: d12fde7d50e9
Revises: 7a43066dc44d
Create Date: 2026-09-29 13:37:12.056085

`issue_01M12NE18ZCR6SZR7M78SMN4E4`, under the LLM-spend epic
(`issue_01M0B8TDRCH7SCGNSXHRGSV4TF`). The epic dimension a spend event sits
under is a gap the ledger-store epic's spec named and left open; the
orchestrator's ruling is DERIVED FROM THE WORK ITEM, never accepted from the
caller — `POST /ledger-entries` already refuses any attribution key it did
not itself derive (PR #164), and `epic_id` is no exception.

**ADDITIVE: one nullable text column and its index, nothing else.** No
existing column, index, row shape or constraint on `ledger_entries` changes,
so this needs no batch mode on the way up — `ADD COLUMN` is native SQLite,
matching `a8d2c5e91f37`'s reasoning for the same shape of change.

## The rule, backfilled here in SQL and enforced by `store.record_spend`
going forward

- `item_id` is NULL -> `epic_id` NULL. Runtime spend with no work item (an
  app backend reporting its own calls) has no epic, and NULL is the true
  answer for it.
- `item_id` names an item whose own `type` is `epic` -> `epic_id` is that
  item's own id.
- `item_id` names an item with a `parent_id` -> `epic_id` is the parent's id.
- `item_id` names an item with no parent that is not itself an epic ->
  `epic_id` is the literal `'corrections'` — `store.py`'s own vocabulary
  comment is exactly this: "a work item is either an epic's sub-item or a
  correction, there is no third category."
- `item_id` names no item this vault holds, OR names one in a different
  product than the entry's own -> `epic_id` NULL. A dangling reference
  derives nothing; it is not refused, matching `record_spend`'s "spend
  already incurred is never refused." An item outside the entry's product is
  treated the same as a missing one, never distinguished from it, so a
  caller cannot use `item_id` to learn that an out-of-scope id exists
  (security review, PR #167 round 1) — a legitimate parent is always
  same-product with its child, so nothing legitimate is lost by the
  constraint.

The backfill below is the same CASE expression as a plain SQL statement,
applied once to every row `ledger_entries` already carries, so a project's
history and its next spend report agree on which epic a given `item_id`
resolves to.
"""
from alembic import op
import sqlalchemy as sa


revision = 'd12fde7d50e9'
down_revision = '7a43066dc44d'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ledger_entries", sa.Column("epic_id", sa.String(40), nullable=True))
    op.create_index("ix_ledger_entries_epic_id", "ledger_entries", ["epic_id"])
    # THE BACKFILL. Only rows with an `item_id` this vault still holds, IN THE
    # ENTRY'S OWN PRODUCT, get a non-NULL `epic_id`; everything else (no
    # `item_id`, a dangling one, or one belonging to a different product) is
    # already NULL from `ADD COLUMN` and needs no statement.
    op.execute(
        "UPDATE ledger_entries SET epic_id = ("
        " SELECT CASE"
        "   WHEN i.type = 'epic' THEN i.id"
        "   WHEN i.parent_id IS NOT NULL THEN i.parent_id"
        "   ELSE 'corrections'"
        " END"
        " FROM issue_items i"
        " WHERE i.id = ledger_entries.item_id"
        " AND i.product = ledger_entries.product"
        ") "
        "WHERE item_id IN ("
        " SELECT id FROM issue_items i"
        " WHERE i.product = ledger_entries.product)")


def downgrade() -> None:
    op.drop_index("ix_ledger_entries_epic_id", table_name="ledger_entries")
    with op.batch_alter_table("ledger_entries") as batch:
        batch.drop_column("epic_id")
