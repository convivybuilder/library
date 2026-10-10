"""Index signals(kind, ingested_at): the filter every list route offers and no index served

Revision ID: d3f81a6c07be
Revises: b4c1f7a92e08
Create Date: 2026-08-26

`issue_01M0Z8F9AXSSMHCWAZW6GWV3YP`, filed by the convivy-orient co against a
live page.

The baseline created `ix_signals_source` and `ix_signals_product` and stopped
there, so `kind` — a filter `GET /signals` and `GET /signals/facets` both offer,
and the one Orient's overnight page uses — has been answered by a full table
scan since. `_signal_filters` builds `kind IN (...)` in SQL, so this was never a
missing filter; it was a filter with nothing under it.

## Why it went unnoticed until it did not

**A scan of this table is cheap warm and ruinous cold, and every casual
measurement of it is warm.** Running the query by hand loads its pages, so the
second reading is milliseconds and the conclusion is "the scan is fine". Under
real page-cache pressure the same statement is physical I/O across a 255 MB
file. Measured on the running service on 2026-08-26 while a Time Machine backup
was walking the filesystem: `/signals?kind=alert&limit=50` took 4.4s, then
14.5s, then failed to return inside 30s, while `?source=slack&limit=50` — the
indexed column, same page size — stayed under 0.5s throughout.

A `sample` of the process during a slow request put **1300 of 1327 stack
samples in `seekAndRead`**, reading B-tree pages. Not lock contention, not
serialization, not the query planner making a poor choice: the plan was
`SCAN signals` over 21,031 rows and the machine was doing every one of those
reads off disk.

## Composite rather than `kind` alone

Every list route orders by `ingested_at DESC` and pages with LIMIT, so an index
on `kind` alone would seek the right rows and then still sort them in a temp
B-tree. `(kind, ingested_at)` serves the filter and most of the order together:

    before   SCAN signals                                   (rows=21031)
    after    SEARCH signals USING INDEX ... (kind=?)        (109 rows for alert)

The residual `USE TEMP B-TREE FOR LAST TERM OF ORDER BY` is the `id` tiebreak,
which is bounded by the page rather than by the table.

Measured on a copy of the live database: 75 ms to build, about 1.4 MB on a
255 MB file.

**`source` and `product` are deliberately not made composite here.** They are
indexed and healthy, and widening them is a change with a cost and no reported
symptom — the thing this migration exists to stop doing by reflex.
"""

from alembic import op

revision = "d3f81a6c07be"
down_revision = "b4c1f7a92e08"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_signals_kind_ingested_at", "signals", ["kind", "ingested_at"])


def downgrade() -> None:
    op.drop_index("ix_signals_kind_ingested_at", table_name="signals")
