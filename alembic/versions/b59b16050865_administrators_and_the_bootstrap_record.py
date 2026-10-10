"""administrators and the bootstrap record

Revision ID: b59b16050865
Revises: 76bfd9912739
Create Date: 2026-09-18 13:55:12.482091

Second slice of "Person identity in the Library" (issue_01M2QWC5NZW824FV6DKNFSEJEY),
scoped by issue_01M2RSEKRDQS0YQDKVEAMNHGN2: the local `bootstrap_library.py`
command, which makes the first administrator and mints the first approved-client
credential. It is one of the readers `76bfd9912739` created `people` for. Two new
tables, no antecedent in Orient's control database, so `op.create_table` is right
here for the same reason that migration's own docstring gives.

**`administrators` is its own table rather than a column on `people` or a row
in `person_products`.** A product grant says "this person may see this product"
and an administrator says "this person may run the Library's own configuration".
Those are different facts with different lifetimes, different readers and
different revocation stakes, and a table that carries both is untangled later
at the cost of every row already written against the merged shape. The two
tables sharing a `person_id` and a `granted_by`/`granted_at` pair is the only
thing they have in common, and that is a coincidence of provenance, not a
shared meaning.

It is a table and not an `is_administrator` column on `people` for three
reasons. `people`'s `updated_at`/`updated_by` name who changed a person LAST, so
a boolean flip would overwrite the record of who made someone an administrator
with whoever last edited their display name. The read route in `app.py` never
touches `people`, and a column there would make authority something a read of
that table could reach by accident. And SQLite's `ALTER TABLE` cannot add a
`NOT NULL` column to a table that has rows without a default, so a column would
need a backfill this table does not.

Shaped like `person_products` for the same reason that table has no
`updated_*` pair: an administrator is a fact recorded once, identified by who
granted it and when, and revoking it is a delete. There is deliberately no
"role" column. One kind of administrator exists; a second kind is a later
migration that knows what it is distinguishing, not a column guessed at now.
No foreign key to `people.id`, for `76bfd9912739`'s reason: `store.py` never
enables `PRAGMA foreign_keys`, so the constraint would claim a guarantee the
service does not keep.

**`bootstrap_record` exists so that the one-time guard is a recorded event and
not a count.** A bootstrap gated on "no administrator exists" re-arms whenever
`administrators` or `people` is empty, which is true on a database whose person
records someone emptied, and true of a table nobody ever populated. The command
must refuse in exactly the first case, so the thing it consults is a row
written by the bootstrap itself, in a table nothing else writes or deletes, and
it does not read `people` or `administrators` at all. That independence is the
whole design: emptying either of those two tables, or revoking the only
administrator, leaves this row where it was.

**A single row, enforced by the schema.** `id` is `PRIMARY KEY CHECK (id = 1)`,
so a second insert collides in SQLite itself whatever the calling code does. The
command's guard is therefore the constraint and not a `SELECT` followed by an
`INSERT`: two bootstraps racing each other cannot both succeed, and a future
change to the command cannot forget the check.

`credential_sha256` is the SHA-256 of the credential minted in the same act,
never the credential, matching `credential_bindings`. The credential file
`identity.CLIENT_TOKENS` names is keyed by the raw token (`identity.resolve`
looks the bearer token up as the dict key), so the secret lives there and this
column only lets a reader confirm which credential a bootstrap produced.
`person_id` has no foreign key and no dependency on `people`, so the row
still says who was made administrator after that person's record is gone.

**The boundary, stated here as well as in the command.** This row lives in the
database it guards. Anyone with local write access to that database can
`DELETE FROM bootstrap_record` and run the command again, and that is accepted:
local write access already owns everything, including writing the credential
file by hand, which is how an approved-client credential was made before this
command existed. The row defends against a different thing, which is the
command re-firing because a table it never consults was emptied.
"""
from alembic import op
import sqlalchemy as sa


revision = 'b59b16050865'
down_revision = '76bfd9912739'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "administrators",
        sa.Column("person_id", sa.String(40), primary_key=True),
        sa.Column("granted_at", sa.DateTime, nullable=False),
        sa.Column("granted_by", sa.String(200), nullable=False),
    )
    op.create_table(
        "bootstrap_record",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=False),
        sa.Column("person_id", sa.String(40), nullable=False),
        sa.Column("credential_sha256", sa.String(64), nullable=False),
        sa.Column("performed_at", sa.DateTime, nullable=False),
        sa.Column("performed_by", sa.String(200), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_bootstrap_record_singleton"),
    )


def downgrade() -> None:
    op.drop_table("bootstrap_record")
    op.drop_table("administrators")
