"""bundle tokens: the credential that names one agent's documents for one task
(issue_01M2VCWQXTF5EGHD1X1J3P4RTJ)

Revision ID: 3e8a5c1d7b40
Revises: 0652cc6b2cee
Create Date: 2026-09-18

`bundles.py`'s module docstring is the design record, including why this is
its own table rather than columns on `call_slips`. What this migration adds
is the store.

## `documents` holds what the token NAMES, as JSON

A list of `{"id", "body_sha256"}` objects, written once at issue and never
touched again. Redemption re-reads each named document from the Reference
Room and refuses when one is missing, no longer `current`, or no longer has
the hash the token named. A `Text` column holding JSON, the same shape
`profiles.include` already uses, because SQLite has no array type and the
list is only ever read whole.

## The scope columns are constrained together

`scope_kind` is `item` or `repo`, and the CHECK ties each to the column that
carries its subject: an `item` token has an `item_id` and no `repo`, a `repo`
token the reverse. A row with both, or neither, would be a bundle scoped to
nothing in particular, and the database refuses to hold one whatever code
writes it.

## `product` is nullable, and NULL is a real state

An `item` token records the item's product so redemption can apply the same
scope check every item-bearing route applies. A `repo` token has no item and
therefore no product; the Reference Room it reads is not product-scoped
(`reference.py`'s module docstring), so there is nothing to scope it by.

## `redeemed_by` is the redeeming request's authenticated actor

`identity.resolve`'s `ident.actor`, never a field of the request body. It is
an audit column: nothing reads it to decide anything.

## The store follows `call_slips`' shape

Expiry is a stored absolute instant, single use is a conditional `UPDATE` on
`redeemed_at`, and `item_id` carries `fk_bundle_tokens_item` to `issue_items`
as `call_slips` carries `fk_call_slips_item`. The key is declared and not
enforced: `PRAGMA foreign_keys` is off for every connection this service opens
(see `5a7e3d9286a8`). It is nullable because a `repo` token has no item, and
SQL does not check a NULL foreign key.
"""
from alembic import op
import sqlalchemy as sa


revision = "3e8a5c1d7b40"
down_revision = "0652cc6b2cee"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bundle_tokens",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("agent", sa.String(64), nullable=False),
        sa.Column("project", sa.String(64), nullable=False),
        sa.Column("variant", sa.String(64), nullable=False),
        sa.Column("scope_kind", sa.String(8), nullable=False),
        sa.Column("item_id", sa.String(40), nullable=True),
        sa.Column("repo", sa.String(64), nullable=True),
        sa.Column("product", sa.String(64), nullable=True),
        sa.Column("documents", sa.Text, nullable=False),
        sa.Column("issued_by", sa.String(200), nullable=False),
        sa.Column("issued_at", sa.DateTime, nullable=False),
        sa.Column("expires_at", sa.DateTime, nullable=False),
        sa.Column("redeemed_at", sa.DateTime, nullable=True),
        sa.Column("redeemed_by", sa.String(200), nullable=True),
        sa.CheckConstraint(
            "(scope_kind = 'item' AND item_id IS NOT NULL AND repo IS NULL) "
            "OR (scope_kind = 'repo' AND repo IS NOT NULL "
            "AND item_id IS NULL)",
            name="ck_bundle_tokens_scope"),
        sa.ForeignKeyConstraint(["item_id"], ["issue_items.id"],
                                name="fk_bundle_tokens_item",
                                ondelete="RESTRICT"),
    )


def downgrade() -> None:
    op.drop_table("bundle_tokens")
