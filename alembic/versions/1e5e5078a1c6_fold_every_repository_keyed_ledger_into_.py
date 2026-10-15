"""fold every repository-keyed ledger into its registry project

Revision ID: 1e5e5078a1c6
Revises: ad6a0a903c09
Create Date: 2026-10-13 09:57:43.065437

A ledger's `scope_id` for `scope_kind: project` is the registry project slug,
the same vocabulary a work item's `product` uses
(`platform/contracts/library-protocol.md` §6.4). `POST /ledger-entries`
checked a product was bookable by resolving it through the registry, then
stored the caller's own spelling — so every one of the 12 live ledgers on
2026-10-13 is keyed by a repository rather than by the project that owns it,
and a cap on one never refuses a checkout filed under the other spelling.
This migration is the data half of the fix; `app.py` and `store.py` carry the
write and read side so nothing un-normalizes it again.

**THE FIXED MAPPING BELOW IS EXACTLY THE 12 LIVE KEYS, READ 2026-10-13 — NOT
THE REGISTRY.** A migration must not depend on a file outside the database:
`agent-platform/config/projects.json` can be edited, moved or made
unreadable independently of this tree, and a migration that read it would
behave differently depending on when it happened to run. The mapping is a
literal, so what this revision does is exactly what a diff of it shows,
forever.

## What folds, and what is already correct

Grouping the 12 keys by the project `projects.resolve()` returns for each
leaves four groups with more than one member or with no pre-existing
project-named ledger — `bosun` (from `agent-platform` alone, with no
`bosun` ledger to begin with), `library` (`knowledge` into the existing
`library` ledger), `selector` (`selector-backend` into the existing
`selector` ledger) and `vivy` (`vivy-landing` and
`vivy-ownerrez-sandbox-tools` into the existing `vivy` ledger). The other
four live keys (`convivy-deck`, `convivy-orient`, `shipwright`, `sous-chef`)
already equal their own project slug and this migration leaves them alone.
That is 8 projects after the fold, not 9 — the ticket's own "12 ledgers into
9" arithmetic does not match its own enumerated mapping, which is what this
migration builds from; the discrepancy is called out rather than silently
carried into a comment that would state it as settled.

## Picking a survivor, and what happens to the rest

Within a group, the row already named exactly for the project is the
survivor when one exists (`library`, `selector`, `vivy`); `bosun` has no
such row, so its one source (`agent-platform`) is renamed in place — nothing
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

## The two refusals, checked before anything is written

**An unresolvable scope_id.** A live project-scope ledger whose `scope_id`
is neither a key of the fixed mapping nor already one of its values (a
project this migration knows about under some OTHER spelling) raises before
any statement runs — guessing would risk silently merging or stranding a
scope nobody named here, and this migration would rather stop and be told
to be taught the new spelling.

**Disagreeing caps.** When two ledgers folding into one project each carry
a *different*, non-null value for the same cap column (`warn_usd_micro`,
`soft_cap_usd_micro`, `hard_cap_usd_micro`, `per_run_cap_usd_micro`), which
one wins is a human's call, not this migration's to guess — it raises
naming the column and the scopes instead. On 2026-10-13 all 12 live ledgers
are uncapped, so this is a correctness guard for a case the live data does
not exercise, not a response to one that has happened.

Both checks run over every group before the first write, so a refusal
leaves the database exactly as it found it.

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
a database with none of the 12 repository keys to begin with** — every
migration test in this tree before this one does exactly that, and an
unconditional raise would fail every one of them on a migration that
changed nothing for their fixture. `downgrade()` checks for the five
values only this migration's own fold can produce and raises only when it
finds one; see its own docstring.
"""
from __future__ import annotations

import datetime as _dt

from alembic import op
import sqlalchemy as sa


revision = '1e5e5078a1c6'
down_revision = 'ad6a0a903c09'
branch_labels = None
depends_on = None


# THE 12 LIVE KEYS, READ 2026-10-13 — see the module docstring for why this is
# a literal rather than a registry read. Every value here is itself a key a
# future run of this same mapping would leave untouched (`_already_known`
# below), which is what lets `convivy-deck`, `convivy-orient`, `shipwright`
# and `sous-chef` — each its own value — pass the "already a project slug"
# half of the refusal check with no special-casing.
_REPO_TO_PROJECT = {
    "agent-platform": "bosun",
    "convivy-deck": "convivy-deck",
    "convivy-orient": "convivy-orient",
    "knowledge": "library",
    "library": "library",
    "selector": "selector",
    "selector-backend": "selector",
    "shipwright": "shipwright",
    "sous-chef": "sous-chef",
    "vivy": "vivy",
    "vivy-landing": "vivy",
    "vivy-ownerrez-sandbox-tools": "vivy",
}

_KNOWN_PROJECTS = frozenset(_REPO_TO_PROJECT.values())

_CAP_COLUMNS = ("warn_usd_micro", "soft_cap_usd_micro", "hard_cap_usd_micro",
               "per_run_cap_usd_micro")

# `granted_by` IS NOT NULL ON EVERY LEDGER ROW, so a merge that moves a cap
# onto the survivor from a row that is not itself the survivor has to name
# someone — this is `store.LEDGER_AUTO_GRANTED`'s sibling for a system act
# rather than a human one, repeated here as a literal for the same reason
# the mapping above is: a migration does not import `app.py`'s or `store.py`'s
# constants across a boundary that can drift from what actually ran.
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
    by_scope = {row["scope_id"]: dict(row) for row in rows}

    # REFUSAL 1, CHECKED FOR EVERY ROW BEFORE ANY GROUPING OR WRITE: a scope
    # this migration cannot place is a scope it will not guess about.
    unresolved = sorted(
        scope_id for scope_id in by_scope
        if scope_id not in _REPO_TO_PROJECT and scope_id not in _KNOWN_PROJECTS)
    if unresolved:
        raise RuntimeError(
            f"ledger scope(s) {unresolved} are neither one of the fixed "
            f"repository keys this migration knows how to fold nor already "
            f"a known project slug; refusing to guess, writing nothing")

    groups: dict[str, list] = {}
    for scope_id, row in by_scope.items():
        target = _REPO_TO_PROJECT.get(scope_id, scope_id)
        groups.setdefault(target, []).append(row)

    # REFUSAL 2, CHECKED FOR EVERY GROUP BEFORE THE FIRST WRITE: two ledgers
    # about to fold into one project must agree on every cap they both set.
    merged_by_target = {}
    for target, group in groups.items():
        merged_by_target[target] = _merged_caps(group)

    now_iso = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None).isoformat(" ")

    for target in sorted(groups):
        group = groups[target]
        if len(group) == 1 and group[0]["scope_id"] == target:
            continue  # already correctly scoped; nothing folds here

        survivor = next((row for row in group if row["scope_id"] == target), None)
        if survivor is None:
            # Exactly one source maps here and no project-named row exists
            # yet (`agent-platform` -> `bosun`, on 2026-10-13's live data) —
            # the lone source becomes the survivor, renamed in place.
            # `min` makes the choice deterministic if a future mapping ever
            # grows this to more than one un-rooted source at once.
            survivor = min(group, key=lambda row: row["scope_id"])
            conn.execute(sa.text(
                "UPDATE ledgers SET scope_id = :target, updated_at = :now "
                "WHERE id = :id"),
                {"target": target, "now": now_iso, "id": survivor["id"]})
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


# THE FIVE VALUES ONLY THIS MIGRATION'S OWN FOLD CAN PRODUCE: the four
# merge sources (closed rather than deleted — see `upgrade`) keep their
# own scope_id, and `bosun` exists at all only because `agent-platform` was
# renamed to it. Nothing else in this tree ever sets `ledgers.state` to
# `'closed'` (checked: `grep`'d for the literal across `store.py` and
# `app.py`), so finding any of these five is finding this migration's own
# work, not a false positive from something unrelated.
_FOLD_MARKERS = ("knowledge", "selector-backend", "vivy-landing",
                 "vivy-ownerrez-sandbox-tools", "bosun")


def downgrade() -> None:
    """No real downgrade — see the module docstring for why. **But a
    database this migration found nothing to fold in must still downgrade
    cleanly**, or every existing test and tool that upgrades to head and
    then downgrades past an older revision breaks on a migration that
    changed nothing for it. So this checks for the five markers only this
    migration's own fold can leave behind, and only raises when it finds
    one — a database with none of the 12 repository keys to begin with
    walks back through here exactly as it walked up, with nothing to undo."""
    conn = op.get_bind()
    if op.get_context().as_sql:
        return
    markers = ",".join(f"'{s}'" for s in _FOLD_MARKERS)
    found = conn.execute(sa.text(
        f"SELECT 1 FROM ledgers WHERE scope_kind = 'project' AND "
        f"(state = 'closed' OR scope_id IN ({markers})) LIMIT 1")).fetchone()
    if found is not None:
        raise NotImplementedError(
            "no downgrade: folding two ledgers' ledger_days is addition "
            "with no record of the two addends, and "
            "ledger_entries.ledger_id/product are overwritten with no "
            "shadow column holding the pre-fold value. Restore the "
            "pre-migration backup instead.")
