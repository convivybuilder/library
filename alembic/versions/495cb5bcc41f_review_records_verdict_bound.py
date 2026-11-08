"""review_records verdict_bound: a run-verified record's verdict, pinned to
the run that attested it

Revision ID: 495cb5bcc41f
Revises: ebb81eb799bc
Create Date: 2026-10-24

CQ·217 (issue_01M5FM2A102PKMQVKFE7HM30F0), under E39
(issue_01M52P72JPNM24XG219NQF9PFS). `_review_record_auth` now requires a
run-verified write's `verdict` and `pr` to match what the Bosun's run
register attests the run itself posted (`posted_verdict`/`posted_pr`,
self-reported in the run's own `meta.json` — `platform/decisions/0092`'s
"machine, not process" limit, same as every other fact this function
checks). A record whose `auth` became `run-verified` under that rule is
`verdict_bound`; `write_review_record` then refuses, 409, any later write on
the same key naming a different `verdict`, whatever `auth` that write itself
computes to — closing the gap a holder of only `review_record.create`
cannot close by matching facts alone, and a same-account process that also
rewrites `meta.json` cannot close either, because the stored verdict no
longer moves once bound.

**`verdict_bound` is additive and starts `False` for every existing row.**
A record `run-verified` under the OLD rule (role/repo/commit/pr alone) was
never checked against a posted verdict at all, so it carries no such
attestation to bind — `False` on every existing row (17,814 review records
on a copy of the live vault, 2026-10-25) is not a backfill guess, it is the honest
answer for a record this rule never evaluated. Only a write landing after
this migration's code deploys can set it `True`, and only by the new rule.

Carried on `review_record_versions` too, for the same reason `auth_reason`
rides along on both tables (`e4894432e1aa`): a superseded version's own
`verdict_bound` is a fact about what that version was, not about the
record's current identity.

`sa.Boolean(create_constraint=False)` — SQLite's `ADD COLUMN` cannot add a
column-level CHECK without a batch table rebuild, and this column needs
none: whether a row is bound is `_review_record_auth`'s call to make, the
same free-text, no-CHECK precedent `auth_reason` set for the reason behind
an `auth` value.
"""
from alembic import op
import sqlalchemy as sa


revision = "495cb5bcc41f"
down_revision = "ebb81eb799bc"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "review_records",
        sa.Column("verdict_bound", sa.Boolean(create_constraint=False),
                  nullable=False, server_default="0"))
    op.add_column(
        "review_record_versions",
        sa.Column("verdict_bound", sa.Boolean(create_constraint=False),
                  nullable=False, server_default="0"))


def downgrade() -> None:
    # Matching `e4894432e1aa`'s downgrade: no CHECK to unwind, and a plain
    # batch drop is enough since SQLite has no `DROP COLUMN` outside batch
    # mode.
    with op.batch_alter_table("review_record_versions") as batch_op:
        batch_op.drop_column("verdict_bound")
    with op.batch_alter_table("review_records") as batch_op:
        batch_op.drop_column("verdict_bound")
