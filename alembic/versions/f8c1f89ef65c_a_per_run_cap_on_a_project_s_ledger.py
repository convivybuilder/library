"""a per-run cap on a project's ledger, handed to each admitted run

Revision ID: f8c1f89ef65c
Revises: 80ab55fe0dba
Create Date: 2026-10-07

issue_01M4BDRV8C47NQN5X8T82Y2B9A, a child of the ledger epic
(issue_01M0B8YCFN4C1CYNANVG87CANG). The operator approved on 2026-10-07: a human,
through Orient, may set an optional per-run dollar cap on a project, beside
its warning, soft and hard caps and under the same rule that only a human
sets it. `agentctl` (the Bosun's E1·4) reads the checkout response's
`budget.cap_usd` and enforces it in-process, including the kill; the Library
enforces nothing about it and this column moves no threshold.

**ADDITIVE: one nullable column, nothing else.** No existing column, index,
row shape or constraint on `ledgers` changes, so this needs no batch mode on
the way up — `ADD COLUMN` is native SQLite, matching `80ab55fe0dba`'s
reasoning for the same shape of change.

**Not among `warn_usd_micro`, `soft_cap_usd_micro`, `hard_cap_usd_micro`.**
Those three are thresholds against the project's accumulating total, checked
in that order and enforced non-decreasing. A per-run cap bounds one run's own
spend, not the total, so it is validated on its own and takes no part in that
ordering (`store.upsert_ledger`'s docstring carries the fuller reasoning).

Existing rows get NULL, matching every other nullable cap column: a project
no human has given a per-run cap gives its runs no number, and nothing is
ended for its own cost.
"""
from alembic import op
import sqlalchemy as sa


revision = 'f8c1f89ef65c'
down_revision = '80ab55fe0dba'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ledgers",
                  sa.Column("per_run_cap_usd_micro", sa.BigInteger, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("ledgers") as batch:
        batch.drop_column("per_run_cap_usd_micro")
