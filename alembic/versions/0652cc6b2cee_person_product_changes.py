"""person product changes: the append-only record beside `person_products`

Revision ID: 0652cc6b2cee
Revises: b59b16050865
Create Date: 2026-09-18 15:20:41.318702

Third slice of "Person identity in the Library" (issue_01M2QWC5NZW824FV6DKNFSEJEY),
scoped by issue_01M2RSF4MMRT7PBHJJPQ2BKEC7: the write route that changes a
person's project set, on an identity the Library verified. One new table, no
antecedent in Orient's control database, so `op.create_table` is right here for
the same reason `76bfd9912739`'s own docstring gives.

**`person_products` cannot be its own history, and that is why this table
exists.** That table carries `granted_at`/`granted_by` for each CURRENT grant,
and revoking a grant is a DELETE. A revocation therefore erases the row that
said who granted the access and when, and leaves nothing that says who took it
away. The route's requirement is that every change records its administrator and
time, and that an earlier record survives a later change. A current-state table
can meet the first half for a grant and neither half for a revocation, and a
grant-then-grant test would pass against it, because the second grant overwrites
nothing the test looks at.

**One row per grant or revocation actually made, appended and never updated.**
`action` is `'grant'` or `'revoke'`, and a request that changes nothing (a grant
of a product already held, a revoke of one not held) writes no row, so the table
holds changes and not requests. A request that changes several products writes
several rows sharing one `changed_at`/`changed_by`. Reading a person's history
is `WHERE person_id = ? ORDER BY id`; the integer `id` orders rows whose `changed_at`
is equal, which timestamps alone cannot.

**A history may legitimately begin with a revocation, so do not read that as a
missing row.** `76bfd9912739` records that `person_products` rows were written by
hand before any write path existed, and this table starts empty at this
migration. Revoking a grant that predates either one appends a `revoke` with no
`grant` before it. The invariant this table actually keeps is that every change
the write route makes is recorded, not that every row in `person_products` has a
recorded origin.

**It is a table beside `person_products` rather than columns on it, for the
reason `administrators` is separate from it** (`b59b16050865`). The current-state
table answers "what may this person see now" in one indexed lookup that the read
route in `app.py` runs on every call, and it must stay that narrow. This table
answers "how did it get this way", which a different reader asks rarely and for a
different purpose. `tool_policy_events`, `issue_events` and `course_events` are
the same split.

**Append-only by the code path, and not by a trigger.** Nothing in this
repository uses a SQLite trigger, and the sibling event tables rely on the same
convention: the one writer, `store.change_person_products`, only INSERTs here.
The boundary is the one `b59b16050865` states for `bootstrap_record`: anyone with
local write access to the database can edit any row in it, and this table does
not pretend otherwise.

`changed_by` is `identity.Identity.actor` for the credential that authenticated
the call, which for an approved-client credential minted for a person is that
person's id. It is never the client-supplied `acting_human` field. No foreign key
to `people.id`, for `76bfd9912739`'s reason: `store.py` never enables `PRAGMA
foreign_keys`, so the constraint would claim a guarantee the service does not
keep. There is deliberately no index beyond the primary key, because nothing
reads this table yet; the reader that arrives with a query shape adds the index
that shape wants.
"""
from alembic import op
import sqlalchemy as sa


revision = '0652cc6b2cee'
down_revision = 'b59b16050865'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "person_product_changes",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("person_id", sa.String(40), nullable=False),
        sa.Column("product", sa.String(64), nullable=False),
        sa.Column("action", sa.String(6), nullable=False),
        sa.Column("changed_at", sa.DateTime, nullable=False),
        sa.Column("changed_by", sa.String(200), nullable=False),
        sa.CheckConstraint("action IN ('grant', 'revoke')",
                           name="ck_person_product_changes_action"),
    )


def downgrade() -> None:
    op.drop_table("person_product_changes")
