"""review_records: one row per review identity, and its version history

Revision ID: 468a1789d29e
Revises: b1a7f3e6c9d2
Create Date: 2026-09-15

Step 1 of the Library's half of "All reviews are memory"
(issue_01M2HS6XNVPG6ZB5RNQDN2TJKN, under the approved epic
issue_01M2F52BXECXGHC1P3PRJ28AV8). This migration builds the schema only — no
write route, no read route, no backfill. `store.py` gets the column list, the
row helper, and the one canonical-content-hash function the write route
(issue_01M2HS3Q36RE5F82QXRN4Z0D0K) will call so there is exactly one
definition of what a review's content hashes to.

## The uniqueness key, and why a runless review never relies on SQLite's null
handling (settled on comment 2748 of the item above, co-library-1027,
2026-09-15, from a measurement of the 10,890 `github_review`/`pr_comment`
signals stamped in the live Archives since 2026-06-19)

**`UNIQUE(repo_identity, identity_key, role, reviewed_ref)`, and `identity_key`
is `NOT NULL`.** It is the dispatch run's id when the review carries one, and
`content:<sha256 of the canonical review content>` when it does not. SQLite
treats two NULLs in a unique index as distinct rows, which would let a
hand-made review with no run duplicate silently; making the key itself
NOT NULL removes that case rather than working around it. The repository is
part of the key because a document hash can repeat across two repositories
without meaning the same review.

For a run-bearing review this collapses on the run: the same run posting the
same (role, reviewed_ref) twice is one record, matching what the epic's
specification asks for directly. For a runless review, a byte-identical
re-post collapses the same way; a runless review with different content is a
new record, kept rather than merged, because nothing gates on a runless
record and there is nothing to lose by keeping several.

## What a second write on an existing key does — the record supersedes, it
does not just idempotently return

The measurement above is why "return the existing record" is not the whole
rule. Grouping those 10,890 signals by (run, role, reviewed commit): 50 groups
are identical re-posts, which idempotency alone collapses; 3 groups share a
verdict and differ only in prose; and 6 groups differ in VERDICT — one run
posted APPROVE, then REQUEST_CHANGES two minutes later, on selector#395
(run-68db8257, 2026-08-18) and again on sous-chef#442 (run-102e6fbc,
2026-08-14). 167 stamps carry no run at all, and 3 of those (role,
reviewed_ref) groups hold two comments each.

Today's merge gate takes the LAST stamp per agent
(`agent-platform/lib/agent-platform/review_protocol.py:309`: "the last stamp
per agent wins, so a later REQUEST_CHANGES correctly overrides an earlier
APPROVE"). A record that kept only the first write would have counted
selector#395's withdrawn APPROVE as the agent's word on that commit; a record
that kept only the last write would break "every review is kept," which the
epic's specification also asks for. Both are wrong for a changed review, so
the contract this schema supports is:

- **Identical content on an existing key returns the existing record and
  writes nothing** — no second signal, no version bump.
- **Different content on an existing key supersedes it.** The record's
  current content fields move to `review_record_versions` whole, at the
  version they were current for; the record's own row takes the new content,
  its `run_id` becomes the new write's run (matching the gate's last-word
  rule), and `version` increments. No version is lost.

`content_sha` is stored on every record, including run-bearing ones, so a
re-write compares one column (`content_sha != ?`) rather than reconstructing
the hash from six fields to learn whether anything changed. `store.py`'s
`review_content_sha` is the one function that produces it, so the write route
that implements the supersede above and this migration's own tests agree on
what "identical content" means byte for byte.

The write route owns the supersede logic; this migration states the contract
and builds the two tables it runs against.

## `auth` gets a `CHECK`, `verdict` does not

`auth` takes exactly `run-verified`, `unverified`, or `card` — the epic fixes
this set, so a fourth value stored here would be a bug rather than a new
kind of review. `verdict` does not get one: every stamped review in the live
Archives today reads `APPROVE` or `REQUEST_CHANGES` (7,938 and 2,952), but
the review-post surface that will write this table may add a value later,
and a `CHECK` here would turn that into a migration rather than a
`store.py` constant update.

## The two tables

**`review_records`** holds one row per review identity — its current
version. **`review_record_versions`** holds every version a record has
superseded, kept whole rather than as a diff, so a full history reads back
without replaying the write log. `PRIMARY KEY (record_id, version)` — the
version this row WAS, alongside the record it belonged to.

Both indexes the specification names: `(repo_identity, pr)` for "every review
on this pull request," and `(repo_identity, reviewed_ref)` for "every review
of this commit or document," across every role and run.

## What a version carries, and what it doesn't

`review_record_versions` carries every column of `review_records` except
`repo_identity`, `identity_key`, `role`, and `reviewed_ref`. Those four form
the record's uniqueness key, so they're identical across every version of one
record by construction — a version row can't disagree with the record it
belongs to on the columns that identify which record it is, and repeating
them would only restate `record_id`.

`product` and `pr` are not part of that key either, so neither carries the
same guarantee: a future supersede that changed `pr` (a review
re-associated with a different pull request) or `product` would otherwise
lose the prior association with no version row to recover it, so both ride
along on every version.

`source_comment_id` and `signal_id` ride along for the same reason `pr`
does, from the other direction: each version is its own write, with its own
GitHub comment (when the review was backfilled from one) and its own
captured signal, not a fact about the record's identity. Dropping them at
supersede time would make the superseded version's provenance — which
comment or which signal produced it — unrecoverable, which is exactly the
loss "kept whole" below promises not to make.

`review_record_versions` holds every version a record has superseded, kept
whole rather than as a diff, so a full history reads back without replaying
the write log.

## Foreign keys declared, not enforced

`review_records.signal_id` and `review_record_versions.signal_id` both
reference `signals.id`, and `review_record_versions.record_id` references
`review_records.id`, all three with `ON DELETE RESTRICT`, and none is
enforced today — SQLite defaults `PRAGMA foreign_keys` to OFF for every
connection this service opens, and `store.py` sets only `busy_timeout` (see
`b4c1f7a92e08`'s docstring, which gives the same reasoning for
`ledger_days`/`ledger_entries`). They are declared anyway: a declaration
costs nothing, documents the intended shape, and becomes live the day the
pragma is turned on. Nothing here may be written as though a delete on
`signals` or `review_records` is stopped by it today.

`paths`, `doc_ids`, and `item_ids` are JSON arrays, not foreign keys — a
review can touch several of each, and `item_prerequisites` (`b1a7f3e6c9d2`)
set the precedent that a small, bounded, per-row list lives as a column
rather than a join table.

## `auth = 'run-verified'` requires a `run_id`

`run-verified` means the record was checked against its dispatch run, so a
`run-verified` row with no `run_id` is a contradiction in the data — the
same kind of backstop `f71949b78df4_machine_certificates.py`'s `status`/
`custody` CHECKs are for their own enum-adjacent invariants, held even
though application code is expected to get it right. It goes on
`review_records` now, while the table is still empty: adding a CHECK to a
populated SQLite table later means a batch table rebuild (SQLite has no
`ALTER TABLE ... ADD CONSTRAINT`), not a one-line migration.

It is deliberately NOT on `review_record_versions`. A version's `auth` is
historical — whatever the record held at that version — and if the
record-level CHECK is ever relaxed (a fourth `auth` value, or a
`run-verified` value earned some other way), the history table shouldn't
refuse the move by continuing to enforce a rule the live table no longer
does.
"""
from alembic import op
import sqlalchemy as sa

revision = "468a1789d29e"
down_revision = "b1a7f3e6c9d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "review_records",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("repo_identity", sa.String(200), nullable=False),
        sa.Column("product", sa.String(64), nullable=False),
        sa.Column("pr", sa.Integer, nullable=True),
        sa.Column("reviewed_ref", sa.String(128), nullable=False),
        sa.Column("role", sa.String(40), nullable=False),
        sa.Column("run_id", sa.String(64), nullable=True),
        sa.Column("identity_key", sa.String(100), nullable=False),
        sa.Column("verdict", sa.String(64), nullable=False),
        sa.Column("findings", sa.Text, nullable=False, server_default="[]"),
        sa.Column("prose", sa.Text, nullable=False, server_default=""),
        sa.Column("paths", sa.Text, nullable=False, server_default="[]"),
        sa.Column("doc_ids", sa.Text, nullable=False, server_default="[]"),
        sa.Column("item_ids", sa.Text, nullable=False, server_default="[]"),
        sa.Column("auth", sa.String(20), nullable=False),
        sa.CheckConstraint("auth IN ('run-verified', 'unverified', 'card')",
                           name="ck_review_records_auth"),
        sa.CheckConstraint("auth != 'run-verified' OR run_id IS NOT NULL",
                           name="ck_review_records_run_verified_requires_run"),
        sa.Column("content_sha", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("source_comment_id", sa.String(40), nullable=True),
        sa.Column("signal_id", sa.String(40), nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
        sa.UniqueConstraint("repo_identity", "identity_key", "role",
                            "reviewed_ref", name="uq_review_records_identity"),
        sa.ForeignKeyConstraint(["signal_id"], ["signals.id"],
                                name="fk_review_records_signal",
                                ondelete="RESTRICT"),
    )
    op.create_index("ix_review_records_repo_pr", "review_records",
                     ["repo_identity", "pr"])
    op.create_index("ix_review_records_repo_ref", "review_records",
                     ["repo_identity", "reviewed_ref"])

    op.create_table(
        "review_record_versions",
        sa.Column("record_id", sa.String(40), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("product", sa.String(64), nullable=False),
        sa.Column("pr", sa.Integer, nullable=True),
        sa.Column("verdict", sa.String(64), nullable=False),
        sa.Column("findings", sa.Text, nullable=False, server_default="[]"),
        sa.Column("prose", sa.Text, nullable=False, server_default=""),
        sa.Column("paths", sa.Text, nullable=False, server_default="[]"),
        sa.Column("doc_ids", sa.Text, nullable=False, server_default="[]"),
        sa.Column("item_ids", sa.Text, nullable=False, server_default="[]"),
        sa.Column("auth", sa.String(20), nullable=False),
        sa.Column("content_sha", sa.String(64), nullable=False),
        sa.Column("run_id", sa.String(64), nullable=True),
        sa.Column("source_comment_id", sa.String(40), nullable=True),
        sa.Column("signal_id", sa.String(40), nullable=True),
        sa.Column("written_at", sa.DateTime, nullable=False),
        sa.PrimaryKeyConstraint("record_id", "version"),
        sa.ForeignKeyConstraint(["record_id"], ["review_records.id"],
                                name="fk_review_record_versions_record",
                                ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["signal_id"], ["signals.id"],
                                name="fk_review_record_versions_signal",
                                ondelete="RESTRICT"),
    )


def downgrade() -> None:
    op.drop_table("review_record_versions")
    op.drop_index("ix_review_records_repo_ref", table_name="review_records")
    op.drop_index("ix_review_records_repo_pr", table_name="review_records")
    op.drop_table("review_records")
