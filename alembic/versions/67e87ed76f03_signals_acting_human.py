"""signals.acting_human: which PERSON a proxied capture was made for

Revision ID: 67e87ed76f03
Revises: ae5e7a024ffb
Create Date: 2026-08-23

`issue_01M0K2X44W12A1ACH06FZSWW84`. A capture made through a client that
verified a human records the credential in `ingested_by` and loses the person
entirely. `ingested_by` is the right field for what it says — WHICH IDENTITY
CAPTURED THE ROW — and that identity is Orient, not the human Orient
authenticated. There was nowhere for the person to go.

**Nullable, and the nullability is the design rather than a concession.** Most
captures have no human behind them at all: the Gmail poller, the CI ingress and
every agent filing a signal are machines acting on their own, and stamping them
with anything would be a fiction. `acting_human` is populated only where a
client presenting an approved-client credential named a person and
`identity.attested_human` verified the shape of that claim, which is the same
rule `signal_acks.identity` already follows for the acknowledge pair.

**A separate column rather than overloading `actor`.** `actor` is who or what
the signal is ABOUT — an email's sender, a reviewer, a customer — and is
caller-supplied prose. `acting_human` is who asked the Library to record it, and
is refused unless the credential is entitled to make that claim. Collapsing them
would make a captured customer email say the customer performed the capture.
"""
from alembic import op
import sqlalchemy as sa


revision = '67e87ed76f03'
down_revision = 'ae5e7a024ffb'
branch_labels = None
depends_on = None


# **WHY THIS IS NOT `op.batch_alter_table`, WHICH `env.py` STATES AS THE RULE
# FOR A COLUMN CHANGE** (security-reviewer, PR #48, and worth answering rather
# than leaving as an inconsistency). That rule exists for `op.alter_column`,
# which Alembic renders as `ALTER TABLE t ALTER COLUMN c ...` — a statement
# standard SQLite does not have, so it passes on the deployment machine's newer
# SQLite and fails in CI. `add_column` has no such trap: it renders as SQLite's
# own native `ALTER TABLE ... ADD COLUMN`, supported for as long as this project
# has existed, and it appends without rewriting a table holding twenty thousand
# rows.
#
# The DOWNGRADE takes the batch form, because `DROP COLUMN` is the half where
# support genuinely varies — SQLite gained it in 3.35, and the version skew
# between CI and the mini is exactly what `env.py` is warning about. A downgrade
# is rare, so recreating the table there costs nothing worth saving.
def upgrade() -> None:
    op.add_column("signals", sa.Column("acting_human", sa.String(200)))


def downgrade() -> None:
    with op.batch_alter_table("signals") as batch:
        batch.drop_column("acting_human")
