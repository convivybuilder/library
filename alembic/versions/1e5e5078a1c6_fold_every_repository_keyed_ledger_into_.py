"""fold every repository-keyed ledger into its registry project

Revision ID: 1e5e5078a1c6
Revises: ad6a0a903c09
Create Date: 2026-10-13 09:57:43.065437

A ledger's `scope_id` for `scope_kind: project` is the registry project slug,
the same vocabulary a work item's `product` uses
(`platform/contracts/library-protocol.md` §6.4). `POST /ledger-entries`
checked a product was bookable by resolving it through the registry, then
stored the caller's own spelling — so a ledger could end up keyed by a
repository rather than by the project that owns it, and a cap on one never
refused a checkout filed under the other spelling. This migration is the
data half of the fix; `app.py` and `store.py` carry the write and read side
so nothing un-normalizes it again.

**THE PROJECT REGISTRY, READ THROUGH `projects.py`'s RESOLVER, NEVER A FIXED
MAPPING.** This revision ran once already, on 2026-10-13, against the
then-live vault, under a hand-written `{repository: project}` literal
current as of that day — and it will not run again there; `alembic` records
a revision as applied and does not re-run it once the database's version
row says so. That literal is gone from this file. In its place, `upgrade()`
resolves each repository-keyed `scope_id` through `projects.resolve()` at
run time, so an install with a different registry — a different set of
projects, or the same projects under different repositories — folds
whatever ITS registry names, rather than replaying one database's history
from 2026-10-13. The registry is read only when there is at least one live,
project-scoped ledger to resolve; a fresh install's `ledgers` table holds
none, and an empty table needs no registry to fold nothing.

## What folds, and what is already correct

Grouping every live, project-scoped ledger by what `projects.resolve()`
returns for its `scope_id` gathers every repository-keyed ledger for one
project into one group, alongside that project's own pre-existing ledger
when it has one. A ledger whose `scope_id` already resolves to itself —
the ordinary case once the write-path fix above has been live since before
any ledger existed — forms a group of one and is left untouched.

## Picking a survivor, and what happens to the rest

Within a group, the row already named for the project is the survivor when
one exists. When a group has no such row — every member is named for some
other spelling and the project has never had a ledger of its own — the row
is instead renamed in place, deterministically (the lowest `scope_id`, so
the choice does not depend on table order), to the project's slug; nothing
to merge, nothing left behind. For every other, non-surviving row in a
group: every `ledger_entries` row pointing at it is repointed to the
survivor's `id` and has its `product` column rewritten to the project, its
`ledger_days` daily totals are ADDED onto the survivor's own rows for the
same day (creating one where the survivor has none yet, the same
`ON CONFLICT` upsert `store.record_spend` already uses), and the row itself
is marked `state = 'closed'` rather than deleted — `ledgers.state` already
has exactly this second value, a closed ledger is invisible to `GET
/ledgers` and to `store.live_ledger` (both filter `state = 'live'`), and
leaving the row and its own now-historical `ledger_days` in place is an
audit trail a hard delete would not be. Nothing above touches
`ledger_entries.epic_id`: it was derived from `item_id` and the entry's
`product` at write time under the OLD, repository-keyed vocabulary, and
re-deriving every historical row's epic under the new one is a second,
separate backfill this migration does not attempt.

## The three refusals, checked before anything is written

**A registry that cannot answer.** When there is at least one live,
project-scoped ledger to resolve and the registry cannot be read, this
raises naming every such ledger's `scope_id` rather than treating an
unreadable registry as "nothing needs folding" — the one case this
migration refuses to fail open on, unlike `projects.resolve()`'s own
default.

**An unresolvable scope_id.** A live, project-scoped ledger whose
`scope_id` resolves to no project at all — the registry was read and does
not know this name — raises before any statement runs, naming the
scope_id: guessing would risk silently merging or stranding a scope nobody
named here.

**Disagreeing caps.** When two ledgers folding into one project each carry
a *different*, non-null value for the same cap column (`warn_usd_micro`,
`soft_cap_usd_micro`, `hard_cap_usd_micro`, `per_run_cap_usd_micro`), which
one wins is a human's call, not this migration's to guess — it raises
naming the column and the scopes instead.

All three checks run over every row and every group before the first
write, so a refusal leaves the database exactly as it found it.

## No real downgrade, but a database with nothing to fold still downgrades

Folding two ledgers' `ledger_days` is addition with nothing recording the
two addends separately, and `ledger_entries.ledger_id`/`product` are
overwritten in place with no shadow column holding the pre-fold value — so
there is no data left to reconstruct an un-fold from, the same reasoning
`a37e9060eee8`'s downgrade gives for the decision-plane drop. Restore the
pre-migration backup if the original, repository-keyed ledgers are
genuinely needed back.

**That raise is conditional, not unconditional, for the tests and tools
that upgrade to head and then downgrade an unrelated earlier revision on
a database with nothing for this migration to fold** — every migration
test in this tree before this one does exactly that, and an unconditional
raise would fail every one of them on a migration that changed nothing for
their fixture. `downgrade()` checks for a `state = 'closed'` row or a
`granted_by` this migration's own fold stamps — the two traces upgrade()
can leave, covering both a merge and a bare rename — and raises only when
it finds one; see its own docstring.
"""
from __future__ import annotations

import datetime as _dt

from alembic import op
import sqlalchemy as sa

import projects


revision = '1e5e5078a1c6'
down_revision = 'ad6a0a903c09'
branch_labels = None
depends_on = None


_CAP_COLUMNS = ("warn_usd_micro", "soft_cap_usd_micro", "hard_cap_usd_micro",
               "per_run_cap_usd_micro")

# `granted_by` IS NOT NULL ON EVERY LEDGER ROW, so a merge or a rename that
# touches a row not itself the pre-existing survivor has to name someone —
# this is `store.LEDGER_AUTO_GRANTED`'s sibling for a system act rather than
# a human one, repeated here as a literal for the same reason the old fixed
# mapping was: a migration does not import `app.py`'s or `store.py`'s
# constants across a boundary that can drift from what actually ran. Also
# the one trace `downgrade()` can check for a rename that closed no row —
# see its own docstring.
_LEDGER_FOLD_GRANTED_BY = "system:ledger-fold-2026-10-13"


def _merged_caps(group: list) -> dict:
    """`{column: value}`, one value per `_CAP_COLUMNS` entry, agreed on by
    every row in `group` — the single non-null value they share, or `None`
    where every row in the group leaves that column unset. Raises
    `RuntimeError` naming the column and the disagreeing scopes when two
    rows in the group set it to two different non-null values."""
    merged = {}
    for col in _CAP_COLUMNS:
        values = {row[col] for row in group if row[col] is not None}
        if len(values) > 1:
            raise RuntimeError(
                f"ledgers {[row['scope_id'] for row in group]} disagree on "
                f"{col}: {sorted(values)}; refusing to guess which wins, "
                f"writing nothing")
        merged[col] = next(iter(values), None)
    return merged


def upgrade() -> None:
    conn = op.get_bind()
    if op.get_context().as_sql:
        # No real data to read in an `--sql` preview — see `8d88e5026d39`
        # and `e18a2c4f9b06` for the same split, for the same reason.
        return

    rows = conn.execute(sa.text(
        "SELECT * FROM ledgers WHERE scope_kind = 'project' "
        "AND state = 'live'")).mappings().fetchall()
    if not rows:
        # Nothing repository-keyed to fold, as on a fresh install — nothing
        # to resolve, so the registry is never read.
        return
    by_scope = {row["scope_id"]: dict(row) for row in rows}

    # REFUSAL 1, BEFORE ANYTHING ELSE: an unreadable registry must not read
    # as "nothing needs folding".
    if not projects.available():
        raise RuntimeError(
            f"ledger scope(s) {sorted(by_scope)} cannot be resolved: the "
            f"project registry is unavailable; refusing to guess, writing "
            f"nothing")

    targets: dict[str, str] = {}
    unresolved = []
    for scope_id in by_scope:
        target = projects.resolve(scope_id)
        if target is None:
            unresolved.append(scope_id)
        else:
            targets[scope_id] = target

    # REFUSAL 2, CHECKED FOR EVERY ROW BEFORE ANY GROUPING OR WRITE: a scope
    # the registry does not know is a scope this migration will not guess
    # about.
    if unresolved:
        raise RuntimeError(
            f"ledger scope(s) {sorted(unresolved)} resolve to no project in "
            f"the registry; refusing to guess, writing nothing")

    groups: dict[str, list] = {}
    for scope_id, row in by_scope.items():
        groups.setdefault(targets[scope_id], []).append(row)

    # REFUSAL 3, CHECKED FOR EVERY GROUP BEFORE THE FIRST WRITE: two ledgers
    # about to fold into one project must agree on every cap they both set.
    merged_by_target = {
        target: _merged_caps(group) for target, group in groups.items()}

    now_iso = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None).isoformat(" ")

    for target in sorted(groups):
        group = groups[target]
        if len(group) == 1 and group[0]["scope_id"] == target:
            continue  # already correctly scoped; nothing folds here

        survivor = next((row for row in group if row["scope_id"] == target), None)
        if survivor is None:
            # No project-named row exists yet — the lone source becomes the
            # survivor, renamed in place. `min` makes the choice
            # deterministic if a group ever has more than one unrooted
            # source at once. Stamped with the fold's own `granted_by` even
            # though nothing here closes a row, so `downgrade()` can still
            # tell this ran.
            survivor = min(group, key=lambda row: row["scope_id"])
            conn.execute(sa.text(
                "UPDATE ledgers SET scope_id = :target, "
                "granted_by = :granted_by, updated_at = :now WHERE id = :id"),
                {"target": target, "granted_by": _LEDGER_FOLD_GRANTED_BY,
                 "now": now_iso, "id": survivor["id"]})
            # THE SURVIVOR'S OWN PRE-EXISTING ENTRIES CARRY THE OLD SPELLING
            # TOO — they were written when `product` and this ledger's
            # `scope_id` were the same string, and the rename above just
            # moved one without the other.
            conn.execute(sa.text(
                "UPDATE ledger_entries SET product = :target "
                "WHERE ledger_id = :id"),
                {"target": target, "id": survivor["id"]})

        merged_caps = merged_by_target[target]
        if any(merged_caps[col] != survivor[col] for col in _CAP_COLUMNS):
            conn.execute(sa.text(
                "UPDATE ledgers SET warn_usd_micro = :warn, "
                "soft_cap_usd_micro = :soft, hard_cap_usd_micro = :hard, "
                "per_run_cap_usd_micro = :per_run, granted_by = :granted_by, "
                "updated_at = :now WHERE id = :id"),
                {"warn": merged_caps["warn_usd_micro"],
                 "soft": merged_caps["soft_cap_usd_micro"],
                 "hard": merged_caps["hard_cap_usd_micro"],
                 "per_run": merged_caps["per_run_cap_usd_micro"],
                 "granted_by": _LEDGER_FOLD_GRANTED_BY, "now": now_iso,
                 "id": survivor["id"]})

        for other in group:
            if other["id"] == survivor["id"]:
                continue
            conn.execute(sa.text(
                "UPDATE ledger_entries SET ledger_id = :survivor, "
                "product = :target WHERE ledger_id = :other"),
                {"survivor": survivor["id"], "target": target,
                 "other": other["id"]})
            other_days = conn.execute(sa.text(
                "SELECT day, spent_usd_micro FROM ledger_days "
                "WHERE ledger_id = :other"),
                {"other": other["id"]}).fetchall()
            for day, spent in other_days:
                conn.execute(sa.text(
                    "INSERT INTO ledger_days "
                    "(ledger_id, day, spent_usd_micro, updated_at) "
                    "VALUES (:ledger_id, :day, :spent, :now) "
                    "ON CONFLICT (ledger_id, day) DO UPDATE SET "
                    "  spent_usd_micro = spent_usd_micro + excluded.spent_usd_micro, "
                    "  updated_at = excluded.updated_at"),
                    {"ledger_id": survivor["id"], "day": day, "spent": spent,
                     "now": now_iso})
            conn.execute(sa.text(
                "UPDATE ledgers SET state = 'closed', updated_at = :now "
                "WHERE id = :id"), {"now": now_iso, "id": other["id"]})


def downgrade() -> None:
    """No real downgrade — see the module docstring for why. **But a
    database this migration found nothing to fold in must still downgrade
    cleanly**, or every existing test and tool that upgrades to head and
    then downgrades past an older revision breaks on a migration that
    changed nothing for it. So this checks for the two traces only this
    migration's own fold can leave — a `state = 'closed'` row from a merge,
    or a `granted_by` stamp from a bare rename that closed nothing — and
    only raises when it finds one; a database with nothing for this
    migration to have folded walks back through here exactly as it walked
    up, with nothing to undo."""
    conn = op.get_bind()
    if op.get_context().as_sql:
        return
    found = conn.execute(sa.text(
        "SELECT 1 FROM ledgers WHERE scope_kind = 'project' AND "
        "(state = 'closed' OR granted_by = :granted_by) LIMIT 1"),
        {"granted_by": _LEDGER_FOLD_GRANTED_BY}).fetchone()
    if found is not None:
        raise NotImplementedError(
            "no downgrade: folding two ledgers' ledger_days is addition "
            "with no record of the two addends, and "
            "ledger_entries.ledger_id/product are overwritten with no "
            "shadow column holding the pre-fold value. Restore the "
            "pre-migration backup instead.")
