"""Index test-result rows on refs.commit: GET /signals/pr-state's CI join scans the whole kind partition otherwise

Revision ID: dd34ff7c65c3
Revises: f8c1f89ef65c
Create Date: 2026-10-07

issue_01M2PERFVRG9NGS7DK0XXNZCTQ, round 2 (reviewer and security-reviewer,
PR #226). `_pr_ci_for_page`'s sha-ranked join pushes `kind = 'test-result'
AND json_extract(refs, '$.commit') IN (...)` into `signals_source`'s
`extra_predicate`, which seeks `ix_signals_kind_ingested_at (kind,
ingested_at)` and then filters the `IN (...)` list against every row that
seek returns — the whole `test-result` partition, hot and cold, growing
about 2,000 rows a day with no window applied to this query
(security-reviewer, PR #226 round 1).

**A partial expression index, not a composite one**, because the column
this join needs to seek on is inside `refs`, a JSON blob, not a declared
column — `CREATE INDEX ... ON signals (json_extract(refs, '$.commit'))` is
the only form SQLite can use to seek that value directly. `WHERE kind =
'test-result'` keeps the index out of every write to a `pr_state` or any
other kind's row, and out of a `test-result` row's own write, until that
row is actually looked up by commit.

**The `WHERE` clause is the literal string `kind = 'test-result'`, not a
bound parameter, and `_pr_ci_for_page`'s own query is written to match it
exactly for that reason.** SQLite only uses a partial index when it can
prove, at prepare time, that the query's `WHERE` clause implies the
index's — a `kind = ?` bound parameter is not known until execution, so a
query written that way would silently fall back to a full partition scan
even with this index built. `"test-result"` is a literal this function
chooses itself, never a request value, so inlining it costs nothing a bound
parameter was buying.

**No `ANALYZE` here (round 3, reviewer and security-reviewer, PR #226
round 2).** The live database has no `sqlite_stat1` table at all — this
would have been the first `ANALYZE` it ever ran — and `ANALYZE signals`
refreshes statistics for every existing index on `signals`
(`ix_signals_ingested_at`, `ix_signals_stream`, `ix_signals_embedded_at`,
`ix_signals_privacy_class`, `ix_signals_source`, `ix_signals_product`,
`ix_signals_kind_ingested_at`), not only this one, which could change the
plan of every other query against `signals` this PR does not test.
`_pr_ci_for_page`'s sha-ranked query instead names this index with
`INDEXED BY` (`store.signals_source`'s `hot_indexed_by`), which both forces
the choice without needing statistics to win it on cost and turns a
database that has not run this migration into a loud
`sqlite3.OperationalError: no such index`, confirmed against a fixture one
revision short of this one, rather than a silent fall-back scan. `INDEXED
BY` is not available to the per-repo `EXISTS` query in the same function —
there is no index on `refs.run_url` for it to name — so that query's cost
is unchanged by this migration either way; see `_pr_ci_for_page`'s own
docstring for what it actually costs, measured the same way.

**Added to `main` only.** The cold tier's schema is not migrated by this
alembic tree at all — `init_cold.py` replays `main.signals`'s own
`sqlite_master` once, at install time, onto a fresh cold file
(library/specs/archives-cold-tier.md §2) — so there is no `schema="cold"`
form of this migration to write. A cold file created before this migration
runs does not gain the index retroactively, which is exactly why
`store.signals_source`'s `hot_indexed_by` is only ever pushed into the hot
arm of the hot/cold union: `INDEXED BY` naming a missing index is an
error on the cold arm too, not a fallback. A fresh `init_cold.py` run
after `alembic upgrade head` picks up the index the same way it already
picks up `ix_signals_kind_ingested_at`.
"""
from alembic import op

revision = "dd34ff7c65c3"
down_revision = "f8c1f89ef65c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX ix_signals_test_result_commit ON signals "
        "(json_extract(refs, '$.commit')) WHERE kind = 'test-result'")


def downgrade() -> None:
    op.execute("DROP INDEX ix_signals_test_result_commit")
