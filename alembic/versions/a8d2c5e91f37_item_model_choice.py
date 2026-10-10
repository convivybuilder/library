"""item_model_choice: an operator-set model choice on a work item
(issue_01M38WTBNAS3NBGEYR8349RR52)

Revision ID: a8d2c5e91f37
Revises: 3e8a5c1d7b40
Create Date: 2026-09-23

**ADDITIVE: FIVE NULLABLE TEXT COLUMNS ON `issue_items`, NOTHING ELSE.** No
existing column, index, row or constraint is touched, and every existing row
reads NULL in all five, which means "no model choice". The columns are
`model_choice` (the model the operator chose), `model_reason`,
`model_set_by_client` (the approved client that wrote it, from the
authenticated credential), `model_acting_human` (the person that client
reported it acted for, recorded and not verified) and `model_set_at`.

**COLUMNS, NOT A CHILD TABLE.** The choice is read every time the item is, so
a table would be a join on the hottest read path, and the Bosun reads it in
the `GET /items/{id}` call it already makes. Only the current choice is
stored; each set and clear also writes a comment to the item's thread, which
is the history.

The five columns are written together or cleared together by one route
(`POST /items/{item_id}/model`); NULL in `model_choice` with the others also
NULL is the cleared state.
"""
from alembic import op
import sqlalchemy as sa

revision = "a8d2c5e91f37"
down_revision = "3e8a5c1d7b40"
branch_labels = None
depends_on = None

_COLUMNS = ("model_choice", "model_reason", "model_set_by_client",
            "model_acting_human", "model_set_at")


# `op.add_column` for the upgrade and the batch form for the downgrade, as in
# `b1a7f3e6c9d2`: `ADD COLUMN` is native SQLite, `DROP COLUMN` support varies
# with the SQLite version.
def upgrade() -> None:
    for name in _COLUMNS:
        op.add_column("issue_items", sa.Column(name, sa.Text, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("issue_items") as batch:
        for name in _COLUMNS:
            batch.drop_column(name)
