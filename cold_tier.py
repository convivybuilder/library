"""cold_tier — relocates a signal to the cold tier once it passes its
kind's hot window (library/decisions/0001-what-the-archives-keep-embed-and-
move-cold, memory-retention epic clause 9, issue_01M35R5QKX7QDCM822K3J18SWX).

Source of truth: `vectors.HOT_WINDOW_DAYS`, copied from the retention
decision. An unruled kind — one `HOT_WINDOW_DAYS` has no entry for — moves
at `vectors.DEFAULT_HOT_WINDOW_DAYS` (the decision's Defaults section: an
unnamed kind is "cold" outright). A kind the decision discards
(`vectors.DISCARDED_SIGNAL_KINDS`) is left to `discard_sweep.py`, never
moved here.

**The move is copy-then-delete, in two single-file transactions per batch**
(build plan §3): `main.signals` is WAL, and SQLite does not commit a WAL
transaction atomically across attached files, so a single cross-file
transaction cannot give the "never in neither tier" guarantee a crash must
preserve. Step 1 inserts the batch into `cold.signals` and commits, taking
only the cold file's write lock. Step 2, `BEGIN IMMEDIATE` on `main`,
inserts the batch's `dedup_key`s into `cold_dedup_keys` (reading them from
`main.signals` before removing the rows that hold them) and then deletes
those rows, committing both together — the key leaves hot and enters the
registry in one commit, so `insert_signal`'s dedup check never has a window
in which a moved signal's key is unprotected. Step 2 only ever deletes a
row whose cold copy is already committed, so an interruption anywhere
leaves the row in hot alone, or in both files — never in neither — and the
next pass's `id NOT IN (SELECT id FROM cold.signals)` on step 1 skips a row
already copied, so a resumed pass does not re-copy it.

**Step 2's `BEGIN IMMEDIATE` is retried on `SQLITE_BUSY`** (`_begin_immediate`,
issue_01M504BG7VGE2AFXSYAHH9PDFX) — `main` is a live database `app.py`'s own
routes are concurrently writing while a pass runs, and a `BEGIN IMMEDIATE`
that loses the race for the write lock once, under load, is ordinary
writer-vs-writer contention the busy handler already waited out, not a case
worth crashing the whole pass over.

**No blob moves, and no vector moves.** Vectors stay in `vectors.db`
(clause 7 already moved them off the hot file); blobs are already outside
both database files in the content-addressed store. A moved row keeps its
`meta.blob_ref` unchanged, and this module never opens `LIBRARY_VECTORS_DB`
or `LIBRARY_BLOB_DIR` at all.

**`--restore` is the reverse, also two single-file transactions per
batch:** step 1, `BEGIN IMMEDIATE` on `main`, inserts the batch's cold rows
into `main.signals` (skipping ids already there) and deletes their
`cold_dedup_keys` entries, committing both together — the hot row and its
`dedup_key` uniqueness return in one step. Step 2, on the cold file, deletes
only the batch's rows whose hot copy is now committed.

    LIBRARY_DB=<path> LIBRARY_COLD_DB=<path> .venv/bin/python cold_tier.py --dry-run
"""
from __future__ import annotations

import argparse
import datetime as _dt
import sqlite3
import sys

import store
import vectors

# **UNMEASURED — see archives-cold-tier.md's "Guesses and things not yet
# measured" table.** The target is step 2's hold staying near 250 ms against
# `store.BUSY_TIMEOUT_MS` (5,000 ms); this number is a conservative starting
# guess, not a timed one, and the plan is explicit that measuring it is a
# deploy-time job, against a COPY of the live database at the worst-case
# batch (500 rows of a kind like `transcript-turn`, whose bodies reach
# `store.MAX_TEXT`), never a developer's laptop or a scratch database. Raise
# or lower this once that measurement exists.
MOVE_BATCH_BYTE_BUDGET = 20_000_000

# The row cap a batch never exceeds regardless of the byte budget above —
# the same number `vectors.embed_missing`'s own default batch and
# `discard_sweep.SWEEP_BATCH_SIZE` use, kept as a familiar, conservative
# starting point.
MOVE_BATCH_ROWS = 500

# How many times `_execute_retrying_busy` tries a write (a batch's
# `BEGIN IMMEDIATE`) before the pass fails — the same constant and the same
# measurement `discard_sweep.BEGIN_ATTEMPTS` is built on: under fsync load, a
# `BEGIN IMMEDIATE` against a lock held throughout was refused after 11.0s to
# 12.0s on a 5s timeout, with no other writer (20 samples, measured locally,
# `discard_sweep.py`). SQLite's busy handler gives up once the sleeps it
# requested add up to the timeout, not once that much wall time has passed,
# so a single attempt can run well past `store.BUSY_TIMEOUT_MS` under load.
# Each attempt gets a fresh timeout, and the lock can be free by the next one.
BEGIN_ATTEMPTS = 3


def _execute_retrying_busy(conn, sql: str, params=()) -> sqlite3.Cursor:
    """`conn.execute`, retried on a `SQLITE_BUSY` refusal, which the `& 0xFF`
    mask reads as every `SQLITE_BUSY_*` extended code too. A refused statement
    changes nothing, so a retry repeats no work; every other error raises at
    once.

    **Duplicated from `discard_sweep.py` rather than shared, deliberately**
    (the same reason `_ruled_kinds` above is its own copy) — the two modules
    have no third one to share it through without introducing a dependency
    neither otherwise needs."""
    for attempt in range(1, BEGIN_ATTEMPTS + 1):
        try:
            return conn.execute(sql, params)
        except sqlite3.OperationalError as e:
            # `getattr`, not a bare attribute read: a real SQLite failure
            # always carries `sqlite_errorcode`, but an injected,
            # hand-constructed `OperationalError` (`tests/
            # test_archives_cold_tier_reads.py`'s crash-safety fixture)
            # does not, and that must raise at once rather than crash on a
            # missing attribute while deciding whether to retry.
            code = getattr(e, "sqlite_errorcode", None)
            busy = code is not None and code & 0xFF == sqlite3.SQLITE_BUSY
            if not busy or attempt == BEGIN_ATTEMPTS:
                raise


def _begin_immediate(conn) -> None:
    """`BEGIN IMMEDIATE`, retried on a `SQLITE_BUSY` refusal. A refused
    `BEGIN` opens no transaction, so a retry repeats nothing the batch has
    already done. Takes the write lock on every attached file, `cold`
    included.

    **This is the fix for issue_01M504BG7VGE2AFXSYAHH9PDFX.** A loaded CI
    runner's `move` pass raised `database is locked` under a concurrent
    writer on `main`, without recording which statement raised. Under
    amplified contention, with the busy timeout shrunk to make it visible,
    every lock error the move side raised came from this `BEGIN IMMEDIATE`
    in `_move_batch`'s step 2, as a plain `SQLITE_BUSY` after the timeout
    ran out: never from step 1's `INSERT ... SELECT`, and never
    `SQLITE_BUSY_SNAPSHOT` or `SQLITE_BUSY_RECOVERY` (convivy/library#291).
    That CI hit this same statement is inferred from that, not observed. That is
    ordinary writer-vs-writer contention the busy handler already tried and
    failed to clear in one attempt, not a case SQLite skips the handler for,
    so a second attempt — on a fresh timeout, once the other writer's
    transaction has had a chance to finish — is the fix `discard_sweep.py`
    already established for the identical shape."""
    _execute_retrying_busy(conn, "BEGIN IMMEDIATE")


def _cutoff(window_days: int) -> str:
    """`ingested_at` older than this has passed `window_days`, compared as
    the same naive-UTC `isoformat(" ")` string `insert_signal` writes
    (build plan §3)."""
    now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    return (now - _dt.timedelta(days=window_days)).isoformat(" ")


def _ruled_kinds() -> frozenset[str]:
    """Every kind the retention decision has an opinion about, movable or
    not — the same union `discard_sweep._ruled_kinds` computes, kept as its
    own copy here because the two modules have no third one to share it
    through without introducing a dependency neither otherwise needs."""
    return (frozenset(vectors.HOT_WINDOW_DAYS)
            | vectors.DISCARDED_SIGNAL_KINDS)


def _unruled_kinds(conn) -> frozenset[str]:
    """Every kind present in `signals` the decision gives no rule for — an
    unruled kind still moves, at `vectors.DEFAULT_HOT_WINDOW_DAYS`, unlike
    `discard_sweep`'s own unruled kinds, which it never touches."""
    ruled = _ruled_kinds()
    rows = conn.execute("SELECT DISTINCT kind FROM main.signals").fetchall()
    return frozenset(r[0] for r in rows if r[0] not in ruled)


def _kinds_and_windows(conn) -> dict[str, int]:
    """kind -> hot window in days, for every kind this pass considers
    moving: every ruled, movable kind (a `None` window — never moves — is
    dropped), plus every unruled kind found live, at the default window."""
    movable = {kind: days for kind, days in vectors.HOT_WINDOW_DAYS.items()
              if days is not None}
    for kind in _unruled_kinds(conn):
        movable[kind] = vectors.DEFAULT_HOT_WINDOW_DAYS
    return movable


class KindReport:
    """One kind's moved count from one pass."""

    def __init__(self, kind: str):
        self.kind = kind
        self.moved = 0

    def line(self, *, dry_run: bool) -> str:
        verb = "would move" if dry_run else "moved"
        return f"cold_tier: {self.kind}: {verb} {self.moved} signal(s) cold"


def _move_batch(conn, ids: list[str]) -> None:
    """Copy-then-delete one batch of ids, in two single-file transactions —
    see the module docstring for the full reasoning. Writes nothing when
    `ids` is empty."""
    if not ids:
        return
    placeholders = ",".join("?" * len(ids))
    cols = ",".join(store.TWO_TIER_SIGNAL_COLUMNS)

    # Step 1: cold insert, taking only the cold file's write lock. `id NOT
    # IN (SELECT id FROM cold.signals)` skips a row a prior, interrupted
    # pass already copied, so a resumed pass never double-inserts it.
    conn.execute(
        f"INSERT INTO cold.signals ({cols}) "
        f"SELECT {cols} FROM main.signals "
        f"WHERE id IN ({placeholders}) "
        f"AND id NOT IN (SELECT id FROM cold.signals)", ids)
    conn.commit()

    # Step 2: BEGIN IMMEDIATE on main — the registry insert (reading
    # dedup_key from the row BEFORE the delete removes it) and the hot
    # delete, in the same transaction so the key leaves hot and enters the
    # registry in one commit. Both clauses repeat "AND id IN (SELECT id
    # FROM cold.signals)" so only a row whose cold copy is durably
    # committed is ever removed from hot. Retried on SQLITE_BUSY — see
    # `_begin_immediate`.
    _begin_immediate(conn)
    conn.execute(
        f"INSERT INTO cold_dedup_keys (dedup_key, signal_id) "
        f"SELECT dedup_key, id FROM main.signals "
        f"WHERE id IN ({placeholders}) "
        f"AND id IN (SELECT id FROM cold.signals) "
        f"AND dedup_key IS NOT NULL", ids)
    conn.execute(
        f"DELETE FROM main.signals WHERE id IN ({placeholders}) "
        f"AND id IN (SELECT id FROM cold.signals)", ids)
    conn.commit()


def _move_kind(conn, kind: str, window_days: int, *, dry_run: bool) -> KindReport:
    """One kind, oldest `ingested_at` first, in batches capped at
    `MOVE_BATCH_ROWS` rows and `MOVE_BATCH_BYTE_BUDGET` bytes
    (`length(body) + length(meta)`), paged by `(ingested_at, id)` over
    `ix_signals_kind_ingested_at` — the same index and cursor shape
    `discard_sweep._sweep_kind` uses, for the same reason.

    **A row is never split across batches.** Rows accumulate into a batch
    until the next one would cross the byte budget, then stop; the cursor
    advances only to the LAST ROW ACTUALLY INCLUDED, so a row the budget
    held back for this pass is found again, first, on the next one — never
    skipped. The loop keeps querying until a page comes back empty, rather
    than stopping once a page is short, for the same reason: a short page
    whose byte budget truncated it still has candidates beyond the included
    rows.
    """
    report = KindReport(kind)
    cutoff = _cutoff(window_days)
    cursor_at, cursor_id = "", ""
    while True:
        rows = conn.execute(
            "SELECT id, length(body) + length(meta) AS sz, ingested_at "
            "FROM main.signals WHERE kind = ? AND embedded_at IS NOT NULL "
            "AND ingested_at < ? AND "
            "(ingested_at > ? OR (ingested_at = ? AND id > ?)) "
            "ORDER BY ingested_at, id LIMIT ?",
            (kind, cutoff, cursor_at, cursor_at, cursor_id, MOVE_BATCH_ROWS)
        ).fetchall()
        if not rows:
            break
        batch = []
        total = 0
        for r in rows:
            if batch and total + r["sz"] > MOVE_BATCH_BYTE_BUDGET:
                break
            batch.append(r)
            total += r["sz"]
        ids = [r["id"] for r in batch]
        report.moved += len(ids)
        if not dry_run:
            _move_batch(conn, ids)
        cursor_at, cursor_id = batch[-1]["ingested_at"], batch[-1]["id"]
    return report


def move(conn, *, dry_run: bool) -> list[KindReport]:
    """One pass: every ruled, movable kind plus every unruled kind found
    live, each at its own window, ordered by kind name so the report is
    reproducible."""
    kinds_and_windows = _kinds_and_windows(conn)
    return [_move_kind(conn, kind, window_days, dry_run=dry_run)
            for kind, window_days in sorted(kinds_and_windows.items())]


def _restore_batch(conn, ids: list[str]) -> None:
    """Copy-then-delete one batch back to hot, in two single-file
    transactions — the reverse of `_move_batch`. Writes nothing when `ids`
    is empty."""
    if not ids:
        return
    placeholders = ",".join("?" * len(ids))
    cols = ",".join(store.TWO_TIER_SIGNAL_COLUMNS)

    # Step 1: BEGIN IMMEDIATE on main — the hot insert (skipping ids
    # already there) and the registry delete, in one transaction so the hot
    # row and its dedup_key uniqueness return together. Retried on
    # SQLITE_BUSY, for the same reason `_move_batch`'s own BEGIN IMMEDIATE
    # is — see `_begin_immediate`.
    _begin_immediate(conn)
    conn.execute(
        f"INSERT INTO main.signals ({cols}) "
        f"SELECT {cols} FROM cold.signals "
        f"WHERE id IN ({placeholders}) "
        f"AND id NOT IN (SELECT id FROM main.signals)", ids)
    conn.execute(
        f"DELETE FROM cold_dedup_keys WHERE signal_id IN ({placeholders})",
        ids)
    conn.commit()

    # Step 2: cold delete, only rows whose hot copy is now committed.
    conn.execute(
        f"DELETE FROM cold.signals WHERE id IN ({placeholders}) "
        f"AND id IN (SELECT id FROM main.signals)", ids)
    conn.commit()


def restore(conn, *, dry_run: bool) -> int:
    """Every cold row, back to hot, in batches of `MOVE_BATCH_ROWS` paged
    by `(ingested_at, id)` over `cold.signals` — not kind-scoped, unlike
    `move`, since a rollback restores everything regardless of which kind
    put it there. Returns the count restored (or that would be)."""
    total = 0
    cursor_at, cursor_id = "", ""
    while True:
        rows = conn.execute(
            "SELECT id, ingested_at FROM cold.signals WHERE "
            "(ingested_at > ? OR (ingested_at = ? AND id > ?)) "
            "ORDER BY ingested_at, id LIMIT ?",
            (cursor_at, cursor_at, cursor_id, MOVE_BATCH_ROWS)).fetchall()
        if not rows:
            break
        cursor_at, cursor_id = rows[-1]["ingested_at"], rows[-1]["id"]
        ids = [r["id"] for r in rows]
        total += len(ids)
        if not dry_run:
            _restore_batch(conn, ids)
    return total


def run(*, dry_run: bool, do_restore: bool) -> int:
    conn = store.connect() if dry_run else store.connect_rw()
    try:
        store.attach_cold(conn, readonly=dry_run)
        if do_restore:
            total = restore(conn, dry_run=dry_run)
            verb = "would restore" if dry_run else "restored"
            print(f"cold_tier: {verb} {total} signal(s) to hot")
        else:
            for report in move(conn, dry_run=dry_run):
                print(report.line(dry_run=dry_run))
        return 0
    finally:
        conn.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would move without writing")
    parser.add_argument("--restore", action="store_true",
                        help="move every cold row back to hot, reversing "
                             "every pass this has ever made")
    args = parser.parse_args(argv)
    return run(dry_run=args.dry_run, do_restore=args.restore)


if __name__ == "__main__":
    sys.exit(main())
