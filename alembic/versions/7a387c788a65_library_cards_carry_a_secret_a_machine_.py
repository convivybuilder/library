"""library_cards carry a secret, a machine and a crew; call_slips carry a crew

Revision ID: 7a387c788a65
Revises: 5b667713b87c
Create Date: 2026-11-05 23:12:23.588289

library/specs/library-card-credential-and-revocation-plan §6. A card is
recognised by its secret's hash and bound to the machine that redeemed it,
and the slip it came from carries the crew that checked its item out, so a
card can later be recognised and scoped without a second table to join
against.

**`library_cards.secret_sha256` is the lookup key for a presented card,
indexed and unique**, the same shape `credential_bindings.credential_sha256`
already uses: the raw secret is never stored, only its hash, and `UNIQUE`
means a hash collision between two issued cards fails loudly at insert
rather than letting one card authenticate as another.

**`library_cards.bound_machine`, `.crew` and `.profile` are written at
issuance, from the handshake and the slip; this migration adds the columns,
and the code that reads them back is a later change.**

**`library_cards.revoked_cause`, `.revoked_by` and `.revocation_detail` ship
with this migration rather than a second one days later,** because the
table already exists and this change is the one the live vault's five-
minute deploy window is already paying for. Nothing in this change writes to
them; the row they would describe doesn't exist until revocation is built.

**`library_cards.state` gets an index because a hard-cap crossing scans
every active card**, a query shaped `WHERE state = 'active'`, over a table
the API never lists, selects, or paginates by any other column.

**Every column here is nullable because the row it belongs to already
exists**, matching `f2f766662793`'s own reasoning: a card issued before this
migration ran, or a slip minted before it, has nothing to write to a column
it has never heard of, and a `NOT NULL` would make such a row unrepresentable
rather than merely incomplete.

`op.add_column` renders SQLite's native `ADD COLUMN`, as `f2f766662793` and
`67e87ed76f03` do; `op.batch_alter_table` is for `op.alter_column`, which
standard SQLite has no statement for. The downgrade takes the batch form,
matching `67e87ed76f03`'s reasoning: `DROP COLUMN` support varies by SQLite
version, and a downgrade is rare enough that recreating the table costs
nothing worth saving.
"""
from alembic import op
import sqlalchemy as sa


revision = '7a387c788a65'
down_revision = '5b667713b87c'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("call_slips", sa.Column("crew", sa.String(64), nullable=True))

    op.add_column(
        "library_cards",
        sa.Column("secret_sha256", sa.String(64), nullable=True))
    op.add_column(
        "library_cards",
        sa.Column("bound_machine", sa.String(200), nullable=True))
    op.add_column(
        "library_cards", sa.Column("crew", sa.String(64), nullable=True))
    op.add_column(
        "library_cards", sa.Column("profile", sa.String(200), nullable=True))
    op.add_column(
        "library_cards",
        sa.Column("revoked_cause", sa.String(20), nullable=True))
    op.add_column(
        "library_cards",
        sa.Column("revoked_by", sa.String(200), nullable=True))
    op.add_column(
        "library_cards",
        sa.Column("revocation_detail", sa.Text, nullable=True))

    op.create_index("ix_library_cards_secret_sha256", "library_cards",
                     ["secret_sha256"], unique=True)
    op.create_index("ix_library_cards_state", "library_cards", ["state"])


def downgrade() -> None:
    op.drop_index("ix_library_cards_state", table_name="library_cards")
    op.drop_index("ix_library_cards_secret_sha256", table_name="library_cards")

    with op.batch_alter_table("library_cards") as batch:
        batch.drop_column("revocation_detail")
        batch.drop_column("revoked_by")
        batch.drop_column("revoked_cause")
        batch.drop_column("profile")
        batch.drop_column("crew")
        batch.drop_column("bound_machine")
        batch.drop_column("secret_sha256")

    with op.batch_alter_table("call_slips") as batch:
        batch.drop_column("crew")
