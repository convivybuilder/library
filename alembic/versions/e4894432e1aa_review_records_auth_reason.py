"""review_records auth_reason: why the write route decided the `auth` it did

Revision ID: e4894432e1aa
Revises: 468a1789d29e
Create Date: 2026-09-15 11:44:23.190155

Step 2 of the Library's half of "All reviews are memory"
(issue_01M2HS6XNVPG6ZB5RNQDN2TJKN) is the write route
(issue_01M2HS3Q36RE5F82QXRN4Z0D0K), and it decides `auth` by checking a
review's dispatch run against the Bosun's facts — never by trusting the
caller. `auth` alone says WHAT the record's authentication state is;
`auth_reason` says WHY, one of a handful of named checks (`no run`, `run not
found`, `private run`, `role mismatch`, `repo mismatch`, `review_sha
mismatch`) or `None` for a clean `run-verified` record with nothing to
explain. Free text rather than a CHECK-constrained enum, matching `verdict`'s
own reasoning in the prior migration: the set of reasons is the write route's
to grow as the auth logic grows, and a CHECK here would turn that growth into
a migration.

Nullable on both tables, and absent from `review_content_sha`'s hash. The
reason travels with the auth decision but is not itself content a re-post
supersedes on — two writes that land on the same `auth` for different
reasons are not a change worth keeping a version for, and `review_content_sha`
already governs what counts as "the same review" for every other column
here.
"""
from alembic import op
import sqlalchemy as sa


revision = 'e4894432e1aa'
down_revision = '468a1789d29e'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("review_records", sa.Column("auth_reason", sa.Text, nullable=True))
    op.add_column("review_record_versions",
                  sa.Column("auth_reason", sa.Text, nullable=True))


def downgrade() -> None:
    # SQLite has no `ALTER TABLE ... DROP COLUMN` before batch mode, and both
    # tables are still empty in every deploy this migration reaches — no data
    # to preserve, so a plain batch drop rather than the COALESCE-guarded
    # recreate `b4c1f7a92e08`'s downgrade needs for a populated table.
    with op.batch_alter_table("review_record_versions") as batch_op:
        batch_op.drop_column("auth_reason")
    with op.batch_alter_table("review_records") as batch_op:
        batch_op.drop_column("auth_reason")
