"""discard_sweep — for each signal of a kind the retention decision discards,
removes its `signals` row, in whichever tier it lives, its `library_vectors`
rows and any blob no remaining record references, all together.

Source of truth: library/decisions/0001-what-the-archives-keep-embed-and-
move-cold (memory-retention epic clause 5, issue_01M35R5CX2WDTBE5E6WQY8BTZH).
`vectors.DISCARDED_SIGNAL_KINDS` is the one place that decision's Discarded
section lives in code, empty today, so on today's data this removes nothing.
Nothing here restates a kind list of its own — every kind this sweep acts on,
or reports, comes from `vectors.py`.

**Reads and writes both tiers** (memory-retention epic clause 9,
archives-cold-tier.md §1 row 10): a signal moved cold before its kind was
ever discarded is still found and removed, from `cold.signals`, with its
`cold_dedup_keys` entry removed in a second, separate transaction — see
`_sweep_kind`'s own docstring for why that split exists and in which order.
`sweep()`'s own unscoped end-of-pass step prunes a registry entry a crash
between those two transactions can leave dangling.

**A referenced signal is kept whatever its own rule says.** Before a
discarded-kind row is removed, every OTHER signal's `refs` is checked for its
id, the same `json_each` scan `GET /signals?unreferenced_by=` already runs
(`app.py`'s `_signal_list_where`). A hit keeps the row, its vectors and its
blob exactly as they are.

**A kind this sweep finds with no rule in the decision is reported, not
silently kept.** The decision's own Defaults section: "The discard sweep...
name[s] every kind [it finds] without a rule here, so that kind gets an entry
in the next revision." A signal kind outside all three of
`vectors.EMBEDDED_SIGNAL_KINDS`, `vectors.KEPT_NOT_EMBEDDED_SIGNAL_KINDS` and
`vectors.DISCARDED_SIGNAL_KINDS` prints with its count on every run, dry or
real, and this sweep never touches it — an unruled kind defaults to kept.

**Batched for the same reason `vectors.embed_missing` batches its writes**
(see that function's docstring): a sweep wide enough to matter must not hold
the single write lock as long as `store.BUSY_TIMEOUT_MS`. There is no model
call in the middle here, unlike that sweep — each batch is a handful of
`DELETE`s against indexed or rowid-keyed columns, several orders of magnitude
cheaper than a chunk-embedding batch, so `SWEEP_BATCH_SIZE` is chosen with a
wide, unmeasured margin rather than timed against a real table the way
`vectors.MAX_CHUNKS_PER_TXN` was. That margin now also covers the blob
quarantine renames a batch makes — they run under the same lock, inside the
same transaction (see "The quarantine rename itself runs inside" below), and
there is at most one rename per distinct blob ref in a batch of at most
`SWEEP_BATCH_SIZE` rows, so the count stays bounded the same way the row
deletes already are.

**`library_vectors.item_id` is an auxiliary vec0 column with no index, so a
one-row-at-a-time `DELETE ... WHERE item_id = ?` scans the whole table once
per row** — measured at roughly four minutes for 487 signals against a
112,000-row table (platform incident, 2026-09-12). Every batch here instead
looks up every row's `rowid` in ONE scan (`item_id IN (...)`) and deletes by
`rowid`, the fix that incident established.

**Each batch's candidate query is paged by `(ingested_at, id)`, not `id`
alone** (reviewer, PR #180 round 2) — `ix_signals_kind_ingested_at` covers
`(kind, ingested_at)`, the same index `d3f81a6c07be` built for every other
`kind`-filtered list route, and paging on the columns that index actually
orders by lets SQLite seek straight to each batch's start instead of pulling
every remaining row of the kind into a temp B-tree to sort by `id` first —
confirmed with `EXPLAIN QUERY PLAN` against the alembic-migrated schema:
`SEARCH signals USING INDEX ix_signals_kind_ingested_at (kind=? AND
ingested_at>?)` plus a temp B-tree for only `id`, the tiebreak within one
`ingested_at` instant, never for the whole kind. A cursor of `(ingested_at,
id)` from the last row of each batch is what keeps a kept (referenced) row
from being reselected on the next page — the predicate is strictly after
that pair in the listing's own order, so a row already seen, kept or
removed, is never seen again within the same kind's sweep.

**Blob delete race — why a blob is quarantined, never unlinked outright**
(security-reviewer, PR #180). `app.py`'s `create_signal` calls
`blobs.write_blob` BEFORE it inserts the row that names the result, and
`write_blob`'s content-addressed short-circuit returns the existing ref
without touching the file's mtime when the bytes already exist. So a
concurrent, byte-identical submission can call `write_blob` (finds the file,
short-circuits) AFTER this sweep's reference check has already decided
nothing names that ref, and then commit its own `INSERT` AFTER this sweep
would otherwise have unlinked the file — a plain grace-period check on the
file's mtime cannot see that gap, because the short-circuit never gives the
file a fresh mtime to check.

Quarantining instead closes it without depending on mtime for the DECISION
to keep or drop the file, only for the DELAY before finalizing: this sweep
`os.rename`s an unreferenced blob out of its content-addressed path into a
quarantine name in the same directory (`blobs.quarantine_blob`), so a
`write_blob` landing after that rename finds no file at `ref` and simply
writes a fresh copy there, never colliding with the quarantined one. After
`blobs.QUARANTINE_GRACE_SECONDS` — chosen well above the longest a real
request can plausibly spend between `write_blob` returning and its `INSERT`
committing, including the whole of `store.BUSY_TIMEOUT_MS` that `INSERT` may
itself wait on (see that constant's own comment in `blobs.py`) —
`_resolve_quarantine` re-checks `signals` for the ref: if something now
names it, the quarantined file is restored (or, if a fresh copy already
landed at `ref` in the meantime, simply dropped, since the fresh copy is
byte-identical and already in place); if nothing does, it is finally
unlinked. A pass that crashes between quarantining a blob and reaching that
re-check leaves the file sitting in quarantine; the next pass finds it by
scanning the blob store's own shard directories (`blobs.quarantined_refs`)
rather than by any record this sweep itself keeps across runs, so no
quarantined file is lost or left stranded past its grace period.

**The quarantine rename itself runs inside the same `BEGIN IMMEDIATE`
transaction as the row deletes it follows, before `conn.commit()`, not
after** (security-reviewer, PR #180 round 2) — an earlier version of this
sweep quarantined each batch's blobs only once the row deletes had already
committed, so a kill or an `os.rename` `OSError` landing in that gap left a
blob permanently orphaned: its `signals` row was already gone, and nothing
else in this codebase scans the blob store for content with zero
references. Doing the rename before the commit closes the gap rather than
narrowing it: if `blobs.quarantine_blob` raises for any ref in the batch,
`_sweep_kind` rolls the whole transaction back and re-raises, so the rows
this batch deleted are never durable and the next pass's candidate query
finds them again. Any blob this batch already renamed before the failure
stays renamed — `os.rename` is a filesystem operation, not part of the SQL
transaction, so a rollback cannot undo it — but its signal row is back too,
so `_resolve_quarantine` finds that ref still referenced and restores it
rather than finalizing it. A crash after `conn.commit()` returns is
ordinary: both the row deletes and every rename in the batch are durable
together, and `_resolve_quarantine` finalizes or restores each quarantined
ref on its own schedule exactly as it always has.

    LIBRARY_DB=<path> LIBRARY_VECTORS_DB=<path> LIBRARY_BLOB_DIR=<path> \\
        .venv/bin/python discard_sweep.py --dry-run
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time

import blobs
import store
import vectors

# See the module docstring's "Batched" note. 500 rows is `vectors.
# embed_missing`'s own default batch size, reused here as a familiar,
# conservative starting point rather than a value measured against this
# sweep's own (much cheaper) per-row cost.
SWEEP_BATCH_SIZE = 500

# How many times `_execute_retrying_busy` tries a write (a batch's `BEGIN`,
# or the prune's `DELETE`) before the pass fails. SQLite's busy handler gives up once the sleeps it requested add up to
# the timeout, not once that much wall time has passed. Under fsync load, a
# `BEGIN IMMEDIATE` against a lock held throughout was refused after 11.0 s to
# 12.0 s on a 5 s timeout, with no other writer (20 samples, measured locally).
# Each attempt gets a fresh timeout, and the lock can be free by the next one.
BEGIN_ATTEMPTS = 3


def _execute_retrying_busy(conn, sql: str, params=()) -> sqlite3.Cursor:
    """`conn.execute`, retried on a `SQLITE_BUSY` refusal. A refused statement
    changes nothing, so a retry repeats no work; every other error raises at
    once."""
    for attempt in range(1, BEGIN_ATTEMPTS + 1):
        try:
            return conn.execute(sql, params)
        except sqlite3.OperationalError as e:
            busy = e.sqlite_errorcode & 0xFF == sqlite3.SQLITE_BUSY
            if not busy or attempt == BEGIN_ATTEMPTS:
                raise


def _begin_immediate(conn) -> None:
    """`BEGIN IMMEDIATE`, retried on a `SQLITE_BUSY` refusal. A refused `BEGIN`
    opens no transaction, so a retry repeats nothing the batch has already
    done. Takes the write lock on every attached file, `cold` included."""
    _execute_retrying_busy(conn, "BEGIN IMMEDIATE")


def _ruled_kinds() -> frozenset[str]:
    return (vectors.EMBEDDED_SIGNAL_KINDS | vectors.KEPT_NOT_EMBEDDED_SIGNAL_KINDS
            | vectors.DISCARDED_SIGNAL_KINDS)


def unruled_kind_counts(conn) -> dict[str, int]:
    """Every kind present in `signals` that the retention decision gives no
    rule for, with its count, over BOTH tiers — a kind's rows do not stop
    counting toward this report once some of them move cold. Read-only.
    `cold` must already be attached on `conn` (`sweep` attaches it once,
    for every function in this module that needs it)."""
    ruled = _ruled_kinds()
    source = store.signals_source()
    rows = conn.execute(
        f"SELECT kind, COUNT(*) AS n FROM {source} AS signals "
        "GROUP BY kind").fetchall()
    return {r["kind"]: r["n"] for r in rows if r["kind"] not in ruled}


def _referenced_ids(conn, candidate_ids: list[str],
                     removed_ids: set[str]) -> set[str]:
    """Which of `candidate_ids` some OTHER, still-remaining signal's `refs`
    names — `removed_ids` are signals this sweep has already decided to
    remove (committed already on a real run, still present but not counted
    on a dry run), so a reference from one of THOSE does not count.

    **`CASE WHEN json_valid(...) THEN ... ELSE '[]' END` as `json_each`'s
    OWN ARGUMENT, not a `WHERE json_valid(...) AND ...` guard** (reviewer,
    PR #180) — the same rule `app.py`'s `_signal_filters` states for its
    `unreferenced_by` clause's own `json_each`: that function raises on
    unparseable JSON rather than skipping the row, and nothing guarantees a
    planner applies a `WHERE` predicate before it evaluates a table-valued
    function named in the `FROM` clause. One malformed `refs` anywhere in
    `signals` would have aborted this whole query — not merely skipped its
    own row — under the `AND`-guarded shape this replaces. Substituting
    `'[]'` for an invalid `refs` makes `json_each` iterate zero rows for
    that signal instead of ever being handed the bad JSON: the guard is now
    structural rather than a hope about evaluation order.

    **Reads BOTH tiers** — a cold `proposal_decision` must still retire a
    hot `proposal`, and the reverse. `cold` must already be attached on
    `conn`.
    """
    if not candidate_ids:
        return set()
    placeholders = ",".join("?" * len(candidate_ids))
    source = store.signals_source()
    rows = conn.execute(
        f"SELECT signals.id AS referencer, json_each.value AS referenced "
        f"FROM {source} AS signals, json_each(CASE WHEN json_valid(signals.refs) "
        "THEN signals.refs ELSE '[]' END) "
        "WHERE signals.id != json_each.value "
        f"AND json_each.value IN ({placeholders})",
        candidate_ids).fetchall()
    return {r["referenced"] for r in rows if r["referencer"] not in removed_ids}


def _blob_ref_of(meta_json) -> str | None:
    return store._json_or(meta_json, {}).get("blob_ref")


def _blob_still_referenced(conn, ref: str, removed_ids: set[str]) -> bool:
    """Whether some signal OTHER than one already removed still names `ref`
    in `meta.blob_ref` — the same "malformed JSON aborts the whole
    statement" hazard `_referenced_ids` guards against, guarded the same
    way: `CASE WHEN json_valid(meta) THEN json_extract(meta, ...) END`
    keeps an unparseable `meta` from ever reaching `json_extract`, which
    raises on malformed JSON exactly as `json_each` does.

    **Reads BOTH tiers** — one blob can back a hot row and a cold row at
    once (archives-cold-tier.md §3), so a blob a cold row still references
    must not be quarantined. `cold` must already be attached on `conn`.
    """
    source = store.signals_source()
    rows = conn.execute(
        f"SELECT id FROM {source} AS signals WHERE (CASE WHEN "
        "json_valid(meta) THEN json_extract(meta, '$.blob_ref') END) = ?",
        (ref,)).fetchall()
    return any(r["id"] not in removed_ids for r in rows)


def _vector_rowids(conn, ids: list[str]) -> list[int]:
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT rowid FROM {vectors.VEC_TABLE} WHERE vault = ? AND "
        f"item_id IN ({placeholders})",
        (vectors.VAULT_ARCHIVES, *ids)).fetchall()
    return [r[0] for r in rows]


class KindReport:
    """One discarded kind's counts from one sweep pass."""

    def __init__(self, kind: str):
        self.kind = kind
        self.removed = 0
        self.kept_referenced = 0
        self.blobs_removed = 0
        self.blobs_kept = 0

    def line(self, *, dry_run: bool) -> str:
        verb = "would remove" if dry_run else "removed"
        return (f"discard_sweep: {self.kind}: {verb} {self.removed} "
                f"signal(s), kept {self.kept_referenced} (referenced), "
                f"{verb} {self.blobs_removed} blob(s), kept "
                f"{self.blobs_kept} blob(s) (referenced)")


def _sweep_kind(conn, kind: str, *, dry_run: bool,
                 removed_ids: set[str]) -> KindReport:
    """One kind, oldest `ingested_at` first (ties broken by `id`), in batches
    of `SWEEP_BATCH_SIZE`, over BOTH tiers — a kind marked discarded after
    some of its rows have already moved cold must still remove those too.
    See the module docstring's "paged by `(ingested_at, id)`" note for why
    the cursor is that pair rather than `id` alone. Self-attaches `cold`
    (idempotent) so a direct call in a test needs no setup beyond its own.

    **Each batch's read (the candidates, and the reference check against
    them), its vector delete, its cold-tier delete and its blob quarantining
    run inside ONE `BEGIN IMMEDIATE` transaction on a real run; the hot
    delete and the registry cleanup run in a SEPARATE one, immediately
    after** (archives-cold-tier.md §1 row 10) — `cold.signals` and `main`
    are two files, and WAL does not commit a transaction spanning both of
    them atomically, so a single transaction here would give the same false
    guarantee the mover's own single-transaction form would (plan §3). The
    cold delete commits FIRST: a crash between the two transactions leaves a
    `cold_dedup_keys` entry naming a row now missing from cold, which
    `insert_signal` treats as an ordinary miss (falls through to a fresh
    insert) and `sweep`'s own unscoped end-of-pass step cleans up. The
    reverse order would instead leave the registry cleared while the cold
    row still exists, which is the state that would let a repost mint a
    live duplicate of a row still sitting in cold.

    Within transaction A, quarantining runs before `conn.commit()`, not
    after (the module docstring's "quarantine rename itself runs inside"
    note has the full reasoning); if it raises, transaction A is rolled back
    and the error re-raised rather than swallowed, so a batch that fails
    partway never reports counts for work that did not durably happen, and
    transaction B never opens. A dry run holds no transaction at all; its
    connection is read-only and cannot open one.
    """
    store.attach_cold(conn, readonly=dry_run)
    report = KindReport(kind)
    cursor_at = ""
    cursor_id = ""
    source = store.signals_source("kind = ?")
    while True:
        if not dry_run:
            _begin_immediate(conn)
        rows = conn.execute(
            f"SELECT id, meta, ingested_at FROM {source} AS signals WHERE "
            "(ingested_at > ? OR (ingested_at = ? AND id > ?)) "
            "ORDER BY ingested_at, id LIMIT ?",
            (kind, kind, cursor_at, cursor_at, cursor_id,
             SWEEP_BATCH_SIZE)).fetchall()
        if not rows:
            if not dry_run:
                conn.commit()
            break
        cursor_at, cursor_id = rows[-1]["ingested_at"], rows[-1]["id"]
        candidate_ids = [r["id"] for r in rows]
        referenced = _referenced_ids(conn, candidate_ids, removed_ids)
        to_remove = [r for r in rows if r["id"] not in referenced]
        report.kept_referenced += len(rows) - len(to_remove)

        blob_refs: list[str] = []
        remove_ids: list[str] = []
        if to_remove:
            remove_ids = [r["id"] for r in to_remove]
            blob_refs = sorted({ref for r in to_remove
                                 if (ref := _blob_ref_of(r["meta"]))})
            report.removed += len(remove_ids)
            removed_ids.update(remove_ids)

            if not dry_run:
                vector_rowids = _vector_rowids(conn, remove_ids)
                if vector_rowids:
                    vplaceholders = ",".join("?" * len(vector_rowids))
                    conn.execute(
                        f"DELETE FROM {vectors.VEC_TABLE} WHERE rowid IN "
                        f"({vplaceholders})", vector_rowids)
                placeholders = ",".join("?" * len(remove_ids))
                # A no-op for an id this kind's rows never reached cold —
                # `DELETE ... WHERE id IN (...)` removes nothing for an id
                # not present on this side, the same way the registry
                # delete below is a no-op for an id that was never cold.
                conn.execute(
                    f"DELETE FROM cold.signals WHERE id IN ({placeholders})",
                    remove_ids)

        # Blob files are QUARANTINED, never unlinked outright, and the
        # rename runs HERE — inside this same `BEGIN IMMEDIATE` transaction,
        # after the deletes above, immediately following the per-ref
        # remaining-reference check just below, and BEFORE `conn.commit()`
        # (security-reviewer, PR #180 round 2;
        # the module docstring's "quarantine rename itself runs inside"
        # note has the full reasoning for why this replaced quarantining
        # after the commit). `_resolve_quarantine`, run once at the end of
        # `sweep`, is what actually unlinks a quarantined blob — only after
        # `blobs.QUARANTINE_GRACE_SECONDS` has passed AND a re-check against
        # `signals` still finds nothing naming it.
        if not dry_run:
            try:
                for ref in blob_refs:
                    if _blob_still_referenced(conn, ref, removed_ids):
                        report.blobs_kept += 1
                    else:
                        report.blobs_removed += 1
                        blobs.quarantine_blob(ref)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        else:
            for ref in blob_refs:
                if _blob_still_referenced(conn, ref, removed_ids):
                    report.blobs_kept += 1
                else:
                    report.blobs_removed += 1

        # Transaction B: the hot delete (a no-op for an id that moved cold
        # before this kind was discarded) and the registry cleanup (a no-op
        # for an id that never did), after transaction A has durably removed
        # the cold copy of whichever of these ids had one. Both statements
        # write `main` only, but `BEGIN IMMEDIATE` takes the write lock on
        # every attached file, `cold` included, for all of B. Narrowing B
        # is outside this change.
        if not dry_run and remove_ids:
            placeholders = ",".join("?" * len(remove_ids))
            _begin_immediate(conn)
            conn.execute(
                f"DELETE FROM main.signals WHERE id IN ({placeholders})",
                remove_ids)
            conn.execute(
                f"DELETE FROM cold_dedup_keys WHERE signal_id IN "
                f"({placeholders})", remove_ids)
            conn.commit()

        if len(rows) < SWEEP_BATCH_SIZE:
            break
    return report


class QuarantineReport:
    """One sweep's pass over every currently quarantined blob — not scoped
    to one kind, since a quarantined ref may have come from any kind's
    batch in this run or a prior, interrupted one (the module docstring's
    "Blob delete race" note, clause c)."""

    def __init__(self):
        self.finalized = 0
        self.restored = 0
        self.pending = 0

    def line(self, *, dry_run: bool) -> str:
        verb = "would finalize" if dry_run else "finalized"
        restore_verb = "would restore" if dry_run else "restored"
        return (f"discard_sweep: quarantine: {verb} {self.finalized} "
                f"blob(s), {restore_verb} {self.restored} blob(s) "
                f"(referenced again), left {self.pending} blob(s) still in "
                f"their grace period")


def _resolve_quarantine(conn, *, dry_run: bool,
                         grace_seconds: float | None = None) -> QuarantineReport:
    """The second phase of the two-phase delete: every ref currently
    quarantined (by this pass's own batches above, or left over from a pass
    that crashed between quarantining a blob and reaching this point),
    past `grace_seconds` (read from `blobs.QUARANTINE_GRACE_SECONDS` at
    call time when not given explicitly, so a test can override the module
    constant without this function's signature capturing the old value at
    import time).

    A ref still short of its grace period is left alone entirely — neither
    restored nor finalized — so the next pass, whenever it runs, finds it
    again and re-checks then. A dry run performs the identical re-check and
    reports what it would do, but calls none of `blobs.restore_blob` /
    `blobs.discard_quarantined`, so it writes nothing, exactly as a dry
    run's signal/vector phase above does.
    """
    if grace_seconds is None:
        grace_seconds = blobs.QUARANTINE_GRACE_SECONDS
    report = QuarantineReport()
    now = time.time()
    for ref, quarantined_at in blobs.quarantined_refs():
        if now - quarantined_at < grace_seconds:
            report.pending += 1
            continue
        if _blob_still_referenced(conn, ref, set()):
            report.restored += 1
            if not dry_run:
                blobs.restore_blob(ref)
        else:
            report.finalized += 1
            if not dry_run:
                blobs.discard_quarantined(ref)
    return report


def _prune_stale_cold_dedup_keys(conn, *, dry_run: bool) -> int:
    """`cold_dedup_keys` entries naming a row no longer in `cold.signals` —
    the window a crash between `_sweep_kind`'s two transactions can leave
    (its own docstring's "two files, so two transactions" note), left
    behind because a deleted cold row leaves no KIND to sweep by, so the
    per-kind loop above can never find it again. Unscoped and run once at
    the end of every pass, whatever kinds are discarded — the same shape
    `_resolve_quarantine`'s own end-of-pass step takes for the same reason.
    Returns the count removed (or that would be, on a dry run).
    """
    if dry_run:
        return conn.execute(
            "SELECT COUNT(*) FROM cold_dedup_keys WHERE signal_id NOT IN "
            "(SELECT id FROM cold.signals)").fetchone()[0]
    # No explicit `BEGIN IMMEDIATE`: it would also take the write lock on
    # `cold`, which the subquery only reads, and this statement writes `main`
    # alone. Python opens a deferred transaction before the DELETE, so the
    # only write lock requested is `main`'s. A refused DELETE changes nothing,
    # so retrying the statement inside that open transaction is safe.
    cur = _execute_retrying_busy(
        conn,
        "DELETE FROM cold_dedup_keys WHERE signal_id NOT IN "
        "(SELECT id FROM cold.signals)")
    conn.commit()
    return cur.rowcount


def sweep(conn, *, dry_run: bool
          ) -> tuple[list[KindReport], dict[str, int], QuarantineReport, int]:
    """One pass: every discarded kind, then the unruled-kind report, then
    the quarantine re-check, then the unscoped registry prune. Returns
    `(kind_reports, unruled_counts, quarantine_report, pruned_dedup_keys)`."""
    store.attach_cold(conn, readonly=dry_run)
    removed_ids: set[str] = set()
    reports = [_sweep_kind(conn, kind, dry_run=dry_run, removed_ids=removed_ids)
               for kind in sorted(vectors.DISCARDED_SIGNAL_KINDS)]
    unruled = unruled_kind_counts(conn)
    quarantine_report = _resolve_quarantine(conn, dry_run=dry_run)
    pruned = _prune_stale_cold_dedup_keys(conn, dry_run=dry_run)
    return reports, unruled, quarantine_report, pruned


def run(*, dry_run: bool) -> int:
    conn = vectors.connect(store.DB_PATH) if dry_run else vectors.connect_rw()
    try:
        reports, unruled, quarantine_report, pruned = sweep(conn, dry_run=dry_run)
        for report in reports:
            print(report.line(dry_run=dry_run))
        print(quarantine_report.line(dry_run=dry_run))
        verb = "would prune" if dry_run else "pruned"
        print(f"discard_sweep: registry: {verb} {pruned} stale "
              f"cold_dedup_keys entry(ies)")
        for kind in sorted(unruled):
            print(f"discard_sweep: {kind}: {unruled[kind]} signal(s) with no "
                  f"rule in the retention decision — kept", file=sys.stderr)
        return 0
    finally:
        conn.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be removed without writing")
    args = parser.parse_args(argv)
    return run(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
