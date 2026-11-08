"""a create-time url moves to refs.source_url

Revision ID: a63c8a7f1b85
Revises: da38780cfabd
Create Date: 2026-10-26 21:44:39.792244

CQ·248 / issue_01M5XTC6P9W92YFJP9WJ6NNADY. `create_item` used to store a
create-time `refs.url` unchanged, and the row shape promoted it to
`pr_url`/`pr_repo` — the same key `/link-pr` writes for a fix PR. CQ·576
is the live occurrence: a url filed as source context served as `pr_url`
naming a merged, unrelated PR while the item itself was still unfixed.
`app.py`'s `create_item` now stores an incoming `refs.url` as
`refs.source_url` instead, and this migration catches up every row written
before that change.

**WHAT MOVES.** Every `issue_items` row whose `refs` is valid JSON, is an
object, and holds `url` with no `pr` gets its `url` renamed to
`source_url`, every other key in `refs` untouched. `/link-pr` is the only
writer that pairs `url` with `pr`; before this change, `create_item` and
`/resolve` could each write `url` with no `pr`. A row holding both `url`
and `pr` is a linked item and is left alone. A `refs` that is not valid
JSON, or not a JSON object, is left alone.

**PLAIN SQL JSON FUNCTIONS ONLY** (`json_valid`, `json_type`,
`json_extract`, `json_set`, `json_remove`), so this runs on alembic's own
connection, which loads no `vec0` extension and attaches no vector
database — unlike a running app session's.

**THE DOWNGRADE IS THE MIRROR**, moving `source_url` back to `url` on a row
holding `source_url` with neither `url` nor `pr`. A row this migration's own
upgrade did not touch — `url`-and-`pr` or a non-object `refs` — stays
untouched on the way back down too.
"""
from alembic import op


revision = 'a63c8a7f1b85'
down_revision = 'da38780cfabd'
branch_labels = None
depends_on = None

_VALID_OBJECT = "json_valid(refs) AND json_type(refs) = 'object'"


def upgrade() -> None:
    op.execute(
        "UPDATE issue_items SET refs = json_remove("
        "  json_set(refs, '$.source_url', json_extract(refs, '$.url')),"
        "  '$.url'"
        ") "
        f"WHERE {_VALID_OBJECT} "
        "AND json_extract(refs, '$.url') IS NOT NULL "
        "AND json_extract(refs, '$.pr') IS NULL")


def downgrade() -> None:
    op.execute(
        "UPDATE issue_items SET refs = json_remove("
        "  json_set(refs, '$.url', json_extract(refs, '$.source_url')),"
        "  '$.source_url'"
        ") "
        f"WHERE {_VALID_OBJECT} "
        "AND json_extract(refs, '$.source_url') IS NOT NULL "
        "AND json_extract(refs, '$.url') IS NULL "
        "AND json_extract(refs, '$.pr') IS NULL")
