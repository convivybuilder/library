"""github-activity-capture signals use the repository's basename

Revision ID: 5b667713b87c
Revises: 42d8d9e44390
Create Date: 2026-10-31 02:00:00.000000

CQ·252 / issue_01M67TF7Y14VS7JZ98Y08EKDY4. The steward ruled on 2026-10-30
that the Bosun's github-activity-capture writes a bare repository name as a
signal's `product` and `stream`, and changed the writer to match. This
migration catches up the rows written before that change, which Orient's
CQ·111 waits on.

**WHAT MOVES.** Every `signals` row with `ingested_by =
'github-activity-capture'` and a `/` in its `product`. `product` becomes the
text after its first `/`, and `stream`'s leading `<product>` becomes that
basename: `convivy/knowledge` becomes `knowledge`, and `convivy/widget#13`
becomes `widget#13`. Every such row's stream begins with its product (read
on the live vault on 2026-10-31: 4,463 rows, none otherwise). Another
writer's owner/repo row is left alone, by the steward's ruling.

**`dedup_key` IS LEFT UNCHANGED.** It embeds owner/repo
(`gh:convivy/agent-platform:pr917:merged`), and the capture deduplicates
its overlap re-reads on it, so rewriting it would let a re-read insert a
second copy of every event.

**THE DOWNGRADE IS THE MIRROR.** It restores each row's owner/repo from its
own `dedup_key` (`gh:<owner>/<repo>:…`), for that writer's rows whose
`product` has no `/`. That includes any bare row the capture wrote after
its own change deployed, which is what a rollback to the owner/repo writer
needs.

**PLAIN SQL ONLY**, so this runs on alembic's own connection, which loads
no `vec0` extension. Every column touched lives in the hot `signals` table;
no row in scope has moved to the cold tier.
"""
from alembic import op


revision = '5b667713b87c'
down_revision = '42d8d9e44390'
branch_labels = None
depends_on = None

_CAPTURE = "ingested_by = 'github-activity-capture'"
_BASENAME = "substr(product, instr(product, '/') + 1)"
# The owner/repo between `gh:` and the next `:` of the row's dedup_key.
_OWNER_REPO = "substr(dedup_key, 4, instr(substr(dedup_key, 4), ':') - 1)"


def upgrade() -> None:
    # SQLite evaluates every SET expression against the row as it was, so
    # both read the owner/repo `product`.
    op.execute(
        f"UPDATE signals SET "
        f"stream = {_BASENAME} || substr(stream, length(product) + 1), "
        f"product = {_BASENAME} "
        f"WHERE {_CAPTURE} AND product LIKE '%/%'")


def downgrade() -> None:
    op.execute(
        f"UPDATE signals SET "
        f"stream = {_OWNER_REPO} || substr(stream, length(product) + 1), "
        f"product = {_OWNER_REPO} "
        f"WHERE {_CAPTURE} AND product NOT LIKE '%/%' "
        f"AND dedup_key LIKE 'gh:%/%:%'")
