"""work items carry no type: clear bug/nit/improvement/task off every OPEN row

Revision ID: cb1fe64e0559
Revises: dd34ff7c65c3
Create Date: 2026-10-09

The operator, 2026-09-24 (issue_01M0EEPS591VQHYEBJY7NGF4C3): "why do we have
bug, nit, and improvement work items? I don't want them. I also don't want
task", "I just want work items". `epic` is the only value `store.ITEM_TYPES`
still holds; the application half of this change (store.py, app.py) makes the
four refused on create and correct. This migration is the storage half: it
drops `type`'s `NOT NULL DEFAULT 'bug'` so a non-epic row can be untyped at
all, then clears `type` on every row this applies to going forward.

**OPEN ROWS ONLY, MATCHING THE DEFINITION OF DONE.** "Closed work items keep
the type they closed with, because the Library refuses edits to closed
items" — `issue_correct` enforces that refusal today (`item is {state};
corrections are not allowed on terminal items`), and a migration bypassing
it with a blanket `UPDATE` would rewrite history the service itself will not
touch. `ITEM_ACTIVE` (`open`, `claimed`, `pr-open`) is "OPEN" here; `fixed`,
`wontfix`, `duplicate` and `redirected` are `ITEM_TERMINAL` and keep whatever
they carry. Both sets are repeated as literals rather than imported — a
migration does not import the app module, matching `6295bace84c6`'s reasoning
for its own literal ceiling.

**The installer applies it like any additive migration.** Clearing a type
column on a row nothing live depends on needs no hold on its timing, unlike
the Phase 3 vault drop. This docstring deliberately does not name the
installer's hold flag: the installer stops on any mention of it other than a
plain assignment to True, a docstring included.

**`copy_from` IS NOT OPTIONAL HERE** — see `6295bace84c6`'s comment on its own
upgrade's batch block for the full argument. In short: SQLite's batch mode
recreates the table, and recreating it needs to know its current columns and
indexes; with no live connection to reflect from (`alembic upgrade head
--sql`, which `test_the_offline_preview_runs` drives), an unqualified
`batch_alter_table` on the upgrade path fails outright. `_issue_items_before_
untyping` is `issue_items` exactly as `0b1e7c4a9d52` plus every migration
since left it, read off a database built fresh from `alembic upgrade head`
before this revision existed.
"""
from alembic import op
import sqlalchemy as sa

revision = "cb1fe64e0559"
down_revision = "dd34ff7c65c3"
branch_labels = None
depends_on = None

# Repeated as literals rather than imported from `store` — see the module
# docstring. These are `store.ITEM_TERMINAL` (`store.ITEM_RESOLUTIONS`) as of
# this revision.
_TERMINAL_STATES = ("fixed", "wontfix", "duplicate", "redirected")


def _issue_items_before_untyping() -> sa.Table:
    t = sa.Table(
        "issue_items", sa.MetaData(),
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("product", sa.String(64), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("refs", sa.JSON, nullable=False),
        sa.Column("severity", sa.String(20), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("claimed_by", sa.String(200)),
        sa.Column("claimed_at", sa.DateTime),
        sa.Column("resolution", sa.String(20)),
        sa.Column("resolved_by", sa.String(200)),
        sa.Column("resolved_at", sa.DateTime),
        sa.Column("outcome_note", sa.Text),
        sa.Column("once_key", sa.String(300), unique=True),
        sa.Column("created_by", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("run_id", sa.String(100)),
        sa.Column("type", sa.String(20), nullable=False, server_default="bug"),
        sa.Column("created_by_kind", sa.String(10)),
        sa.Column("needs", sa.String(20)),
        sa.Column("park_reason", sa.Text),
        sa.Column("unpark_condition", sa.Text),
        sa.Column("parked_at", sa.DateTime),
        sa.Column("embedded_at", sa.DateTime),
        sa.Column("parent_id", sa.String(40)),
        sa.Column("topics", sa.JSON),
        sa.Column("maturity", sa.String(16)),
        sa.Column("specification", sa.Text),
        sa.Column("definition_of_done", sa.Text),
        sa.Column("prerequisites", sa.Text),
        sa.Column("model_choice", sa.Text),
        sa.Column("model_reason", sa.Text),
        sa.Column("model_set_by_client", sa.Text),
        sa.Column("model_acting_human", sa.Text),
        sa.Column("model_set_at", sa.Text),
        sa.Column("item_key", sa.Text),
        sa.Column("found_during", sa.Text),
        sa.ForeignKeyConstraint(["parent_id"], ["issue_items.id"],
                                name="fk_issue_items_parent_id",
                                ondelete="RESTRICT"),
    )
    sa.Index("ix_issue_items_product", t.c.product)
    sa.Index("ix_issue_items_state", t.c.state)
    sa.Index("ix_issue_items_created_at", t.c.created_at)
    sa.Index("ix_issue_items_embedded_at", t.c.embedded_at)
    sa.Index("ix_issue_items_severity", t.c.severity)
    sa.Index("ix_issue_items_needs", t.c.needs)
    sa.Index("ix_issue_items_type", t.c.type)
    sa.Index("ix_issue_items_parent_id", t.c.parent_id)
    sa.Index("ix_issue_items_maturity", t.c.maturity)
    sa.Index("ix_issue_items_product_item_key", t.c.product, t.c.item_key,
            unique=True, sqlite_where=sa.text("item_key IS NOT NULL"))
    sa.Index("ix_issue_items_found_during", t.c.found_during)
    return t


def upgrade() -> None:
    with op.batch_alter_table(
            "issue_items",
            copy_from=_issue_items_before_untyping()) as batch:
        batch.alter_column("type", nullable=True, server_default=None)
    terminal = ",".join(f"'{s}'" for s in _TERMINAL_STATES)
    op.execute(
        f"UPDATE issue_items SET type = NULL "
        f"WHERE type != 'epic' AND state NOT IN ({terminal})")


def downgrade() -> None:
    # LOSSY, matching `6295bace84c6`'s downgrade for the same reason: the
    # value a cleared row carried before this migration ran is gone, and
    # `NOT NULL` cannot be restored over a live NULL. `'bug'` is what the
    # column defaulted new rows to before this revision, so it is also the
    # fallback here — the same choice `ITEM_DEFAULT_TYPE` made for every row
    # that never named a type at all.
    op.execute("UPDATE issue_items SET type = 'bug' WHERE type IS NULL")
    with op.batch_alter_table("issue_items") as batch:
        batch.alter_column("type", nullable=False, server_default="bug")
