"""an epic is proposed, approved or declined (library/decisions/0005)

Revision ID: a4644bf94fab
Revises: b7e2d4c91a35
Create Date: 2026-10-20

Data-only, additive. `store.ITEM_MATURITIES` narrows from `(proposed,
approved, built, superseded)` to `(proposed, approved, declined)` in this
same PR; this migration is the one-time backfill that makes every live row
agree with the narrower vocabulary before any route reads it that way.

**TWO UPDATEs, EACH SCOPED TO EXACTLY THE ROWS THE WORK ITEM NAMES:**

- every `issue_items` row with `maturity = 'built'` becomes `'approved'` —
  `built` meant "the build happened", which an approved epic's own `state`
  reaching `fixed` already says on its own, so the two rows this touches
  lose no information by losing the separate bucket.
- every `type = 'epic'` row whose `state` is `open`, `claimed` or `pr-open`
  and whose `maturity` is NULL becomes `'proposed'` — an open epic with no
  maturity recorded is exactly the gap decision 0005 closes: going forward
  every epic is proposed, approved or declined, with no fourth "not yet
  assessed" reading.

**A RESOLVED EPIC WITH `maturity IS NULL` IS LEFT UNTOUCHED**, by name, in
the work item this migration builds. Its own `state` already says what
happened; inventing a maturity for it would be a claim about an approval or
a decline nobody recorded, and `/resolve`'s new not-approved refusal and
`epic_state_of`'s `None` both already read a NULL maturity correctly on a
terminal row — there is nothing downstream this backfill would unblock.

**NEITHER UPDATE TOUCHES A ROW ALREADY READING `proposed`, `approved` OR
`declined`** — `declined` cannot exist yet, since this PR is what adds the
route that writes it, and an already-`proposed` or `-approved` row is
already what the new vocabulary wants.

**NO OTHER VALUE IS TOUCHED.** `superseded`, if any live row carries it, is
left exactly as it is: the work item names only `built` and NULL-on-open-
epic as what moves, and inventing a third rule here would be scope this
migration was not asked to cover.

This is an ordinary additive migration, carrying none of the overrides
`bin/install-library` looks for: the installer's auto-migrate runs it on the
next deploy, same as any other.

**THE DOWNGRADE IS A DELIBERATE NO-OP, NOT A REVERSAL.** Both forward moves
are one-way: once a `built` row reads `approved`, or an open epic's NULL
reads `proposed`, nothing in the row says which backfill rule produced it,
so writing either back would mean guessing which rows to touch — including,
after any real use of this vault, rows an ordinary approval or decline
changed for a genuine reason that has nothing to do with this migration.
`test_there_is_no_downgrade_from_the_baseline` and the ledger migration
tests both chain a downgrade THROUGH this revision to reach an older one, so
raising here — the shape a migration with nothing honest to revert to
usually takes — would stop every one of those chains short; only the
decision-plane drop and the baseline itself are let that behave that way in
this tree. `store.ITEM_MATURITIES` is this PR's own code change and reverts
with it, independently of whatever any row reads, so a downgrade of this
revision alone still leaves the vault in a safe, readable state.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "a4644bf94fab"
down_revision = "b7e2d4c91a35"
branch_labels = None
depends_on = None

_OPEN_EPIC = ("type = 'epic' AND state IN ('open', 'claimed', 'pr-open') "
             "AND maturity IS NULL")


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text(
        "UPDATE issue_items SET maturity = 'approved' "
        "WHERE maturity = 'built'"))
    conn.execute(sa.text(
        f"UPDATE issue_items SET maturity = 'proposed' WHERE {_OPEN_EPIC}"))


def downgrade() -> None:
    # Deliberately empty — see the module docstring's "THE DOWNGRADE IS A
    # DELIBERATE NO-OP" note. There is no row-level marker distinguishing
    # this migration's two writes from an ordinary application write made
    # afterward, so nothing here is reverted.
    pass
