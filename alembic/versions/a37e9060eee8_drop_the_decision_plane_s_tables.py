"""drop the decision plane's tables

Revision ID: a37e9060eee8
Revises: c68fdb59edfc
Create Date: 2026-08-19

**Run only after `retire_decisions.py --verify` passes**, the cutover
sequence's own shape applied to a much smaller migration: the 120 rows in
`decision_items` (and their `decision_comments`/`decision_events` history)
have to exist as signals before the tables holding the originals are
dropped, or a citation by `dec_<id>` stops resolving with nothing left to
recover it from.

`issue_01M0ASMCB4F68PF3NJG7BV7K6C` — the operator, 2026-08-18, "Let's get rid of the
feature entirely." No soak period, unlike Phase 3 Stage C's 220 MB live
vault: this is 120 rows nothing has written to since the plane's last route
was removed, not a table serving traffic underneath a migration.
"""
from alembic import op

revision = "a37e9060eee8"
down_revision = "c68fdb59edfc"
branch_labels = None
depends_on = None

# Dependency order for the drop: children before the parent they reference,
# so the foreign keys never point at a table that is already gone.
TABLES = ("decision_comments", "decision_events", "decision_items")


def upgrade() -> None:
    for table in TABLES:
        op.drop_table(table)


def downgrade() -> None:
    """**There is no downgrade.** Recreating the tables recreates empty ones
    — the rows are gone from here the moment `upgrade` runs, migrated into
    the Archives as signals by `retire_decisions.py` before this revision is
    ever applied. Restore from the pre-migration backup if the plane's
    tables are genuinely needed back, the same way the baseline's own
    downgrade points at a backup rather than pretending to reverse itself.
    """
    raise NotImplementedError(
        "no downgrade: decision_items/decision_comments/decision_events "
        "would come back empty. Restore the pre-migration backup instead.")
