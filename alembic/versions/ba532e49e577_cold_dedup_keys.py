"""cold_dedup_keys: the registry that keeps a dedup_key unique once its row
moves cold (memory-retention epic clause 9, issue_01M35R5QKX7QDCM822K3J18SWX)

Revision ID: ba532e49e577
Revises: d12fde7d50e9
Create Date: 2026-09-30

Build plan: projects/library/specs/archives-cold-tier.md §4. Moving a signal
out of `main.signals` removes its key from the only `UNIQUE (dedup_key)`
constraint `insert_signal` relies on, so a repost of an already-moved
signal's `dedup_key` would otherwise mint a second, live row with a key
`main.signals` no longer enforces uniqueness over. `cold_tier.py`'s mover
fills this table in the same transaction as the hot delete it follows;
`insert_signal` checks it on a `dedup_key` miss in hot, before falling
through to a fresh insert; `discard_sweep.py` removes a discarded kind's
cold rows' entries alongside the rows themselves, plus an unscoped
end-of-pass step for the window a crash between the two can leave.

**A NEW table this service owns outright, the same reasoning
`9c1c4f2b8a3e` gives**: nothing moved a signal cold before this, so there is
no prior schema here to drift away from.

**`dedup_key` is the primary key rather than a separate id column** — this
table has no life of its own beyond "which signal does this key now belong
to", so a synthetic id would be a second way to identify a row that already
has one. `signal_id` is indexed for the restore path (`cold_tier.py
--restore` removes a batch's entries by the ids it is restoring) and for the
unscoped cleanup step (`DELETE ... WHERE signal_id NOT IN (SELECT id FROM
cold.signals)`), neither of which is a lookup by `dedup_key`.
"""
from alembic import op
import sqlalchemy as sa

revision = "ba532e49e577"
down_revision = "d12fde7d50e9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cold_dedup_keys",
        sa.Column("dedup_key", sa.String(300), primary_key=True),
        sa.Column("signal_id", sa.String(40), nullable=False),
    )
    op.create_index("ix_cold_dedup_keys_signal_id", "cold_dedup_keys",
                     ["signal_id"])


def downgrade() -> None:
    op.drop_index("ix_cold_dedup_keys_signal_id", table_name="cold_dedup_keys")
    op.drop_table("cold_dedup_keys")
