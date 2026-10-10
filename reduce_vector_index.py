"""The one-time reduction of the Archives' vector index: remove the
`library_vectors` rows that the retention decision says should never have
been embedded (library/decisions/0001-what-the-archives-keep-embed-and-move-cold).

`vectors.EMBEDDED_SIGNAL_KINDS` (convivy/library#177) is the allow-list; a
signal kind absent from it is not embedded, per that decision's own Defaults
section. #177 stopped ingest from vectorising those kinds going forward, but
the rows written for them before it deployed are still in the index. This
script removes them.

**RUN IT ONLY WHEN `EMBEDDED_SIGNAL_KINDS` ON THE DEPLOYED TREE MATCHES THE
DECISION.** It deletes the vectors of every Archives kind outside that set,
and nothing puts them back: the row's `embedded_at` stays set, so the sweep
never reselects it. The operator ruled on 2026-10-05 that `transcript-turn` stays
embedded; until that kind is in the set, a run deletes its vectors too.

**Imports `EMBEDDED_SIGNAL_KINDS` rather than restating it** — the allow-list
lives in one place, `vectors.py`, and this reads it rather than keeping a
second copy that could drift from what ingest itself now enforces.

**Reads signals over both tiers, hot and cold** (`store.signals_source()`).
The cold-tier mover leaves a moved signal's vectors in place, so a join to
`main.signals` alone skips them. That leaves every not-embedded vector whose
signal had already moved cold in the index.

**Only `library_vectors` is touched, never `signal_vectors`.** The latter is
legacy: `vectors.nearest`/`insert_vectors` read and write `library_vectors`
alone for both vaults (`vault` is a metadata column on that one table), and
nothing in this service still queries `signal_vectors` — it is carried by
`init_vectors.py`/`migrate.py`/`split_vectors.py` for compatibility with a
shape Orient created, not because search depends on it.

**Never writes to `signals`, `issue_items`, or any table outside the vector
index's own attached file.** A signal's `embedded_at` stamp is left exactly
as it is: `embed_missing`'s sweep selects `WHERE embedded_at IS NULL`
(`vectors.py`), and every row this script targets already carries a non-NULL
stamp — it was written when the row was first embedded, before #177 existed
to stop that. Deleting the vector rows underneath a stamp that stays set is
what keeps the sweep from re-embedding them; nothing here needs to touch the
stamp itself, only the vectors.

**Deletes by rowid, in bounded batches, each its own short write
transaction** — the same discipline `vectors.embed_missing` uses for the
identical reason: `store.connect_rw` sets a 5,000 ms busy timeout, and a
single transaction over the whole reduction would hold the write lock for
however long that takes, queuing every concurrent ingest and revision behind
it. A batch here is delete-only, with no model call, so it is cheap compared
to `MAX_CHUNKS_PER_TXN`'s 4,000-chunk budget; `--batch-size` still exists to
tune it without a redeploy.

**Resumable by construction, with no separate progress record.** Each
batch's `SELECT` is bounded by a `rowid` cursor that only ever advances past
rows the batch already examined, and a deleted row can never be selected
again because it is gone. An interrupted run simply leaves the cursor
implicit in which rows remain; the next run starts its own cursor at 0 and
finds exactly the rows still there. A completed run's next invocation
matches zero rows on its very first batch and deletes nothing.

**`--dry-run` writes nothing and reports the same measurement, projected.**
Real deletion opens `vectors.connect_rw()`, which is refused
(`store.WritesDisabled`) unless `LIBRARY_WRITES_ENABLED=1` — the same door
every other write path in this service is refused at. The dry run opens
`vectors.connect(store.DB_PATH)` instead, a read-only handle, so it cannot
write even by accident.

**Safe to run against the live, serving database with the service up.**
Nothing here holds a lock anywhere near the busy timeout, and nothing here
needs exclusive access the way `split_vectors.py`'s trailing `VACUUM` does —
there is no `VACUUM` in this script at all, deliberately: a `VACUUM` needs
exclusive access to the whole file and this reduction is designed to run
without taking the service down. The freed space is reclaimed later, if at
all, by a separate, explicit `VACUUM` run with the service stopped.

    LIBRARY_DB=<path> LIBRARY_VECTORS_DB=<path> LIBRARY_COLD_DB=<path> \\
        .venv/bin/python reduce_vector_index.py --dry-run

    LIBRARY_DB=<path> LIBRARY_VECTORS_DB=<path> LIBRARY_COLD_DB=<path> \\
        LIBRARY_WRITES_ENABLED=1 .venv/bin/python reduce_vector_index.py
"""
from __future__ import annotations

import argparse
import sqlite3
import sys

import store
import vectors

# Delete-only, no model call — cheap next to `vectors.MAX_CHUNKS_PER_TXN`'s
# 4,000-chunk budget, which is sized for a batch that also runs the embedder.
# Tunable with `--batch-size` rather than fixed, since the right number for a
# delete-only batch has not been measured against a production-sized index
# the way that constant was.
BATCH_SIZE = 2000


def _kind_report(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    """Signal count and Archives vector-row count, by kind — every kind
    present in `signals`, not only the ones `EMBEDDED_SIGNAL_KINDS` names.

    Two queries rather than one `LEFT JOIN FROM signals`, deliberately: a
    join starting from `signals` would drive the scan from the larger side
    and probe `library_vectors` once per signal with no index to do it by.
    Starting the vector count from `library_vectors` and joining each row to
    `signals` by primary key instead makes that half an indexed lookup per
    vector row.
    """
    vector_counts = {
        r["kind"]: r["n"] for r in conn.execute(
            f"SELECT s.kind AS kind, COUNT(*) AS n "
            f"FROM vecdb.{vectors.VEC_TABLE} lv "
            f"JOIN {store.signals_source()} AS s ON s.id = lv.item_id "
            f"WHERE lv.vault = ? GROUP BY s.kind",
            (vectors.VAULT_ARCHIVES,)).fetchall()}
    signal_counts = {
        r["kind"]: r["n"] for r in conn.execute(
            f"SELECT kind, COUNT(*) AS n FROM {store.signals_source()} AS s "
            f"GROUP BY kind"
        ).fetchall()}
    kinds = sorted(set(vector_counts) | set(signal_counts))
    return {k: {"signals": signal_counts.get(k, 0),
                "vectors": vector_counts.get(k, 0)} for k in kinds}


def _project_after(before: dict[str, dict[str, int]]) -> dict[str, dict[str, int]]:
    """What `_kind_report` would measure after the reduction, computed rather
    than run — the `--dry-run` report. A kind this script never touches
    (embedded) keeps its count; every other kind projects to zero vectors,
    since every row this script targets is exactly "vault is the Archives,
    kind is not in the allow-list"."""
    return {k: {"signals": v["signals"],
                "vectors": v["vectors"] if k in vectors.EMBEDDED_SIGNAL_KINDS else 0}
            for k, v in before.items()}


def _print_report(before: dict[str, dict[str, int]],
                   after: dict[str, dict[str, int]], *, after_label: str) -> None:
    total_before = sum(v["vectors"] for v in before.values())
    total_after = sum(v["vectors"] for v in after.values())

    def share(n: int, total: int) -> str:
        return f"{n / total:.1%}" if total else "—"

    print(f"  {'kind':28} {'signals':>8} {'vectors':>9} {'share':>7}   "
          f"{after_label + ' vectors':>9} {after_label + ' share':>7}")
    kinds = sorted(set(before) | set(after))
    for kind in kinds:
        b = before.get(kind, {"signals": 0, "vectors": 0})
        a = after.get(kind, {"signals": 0, "vectors": 0})
        flag = "" if kind in vectors.EMBEDDED_SIGNAL_KINDS else "  (not embedded)"
        print(f"  {kind:28} {b['signals']:>8} {b['vectors']:>9} "
              f"{share(b['vectors'], total_before):>7}   "
              f"{a['vectors']:>9} {share(a['vectors'], total_after):>7}{flag}")
    print(f"  {'TOTAL':28} {'':>8} {total_before:>9} {'100.0%':>7}   "
          f"{total_after:>9} {'100.0%':>7}")


def reduce_index(conn: sqlite3.Connection, batch_size: int = BATCH_SIZE, *,
                  max_batches: int | None = None) -> int:
    """Delete every `library_vectors` row whose vault is the Archives and
    whose signal's kind `EMBEDDED_SIGNAL_KINDS` does not name. Returns the
    number of rows deleted. Requires `cold` already attached on `conn`
    (`store.attach_cold`), as `_kind_report` does.

    The `rowid` cursor only ever advances past rows this call has already
    examined — deletion-candidates AND kept rows alike — so no row already
    looked at is rescanned by a later batch, and the whole run is one pass
    over the Archives' share of the index regardless of how many batches
    that takes.

    `max_batches` stops after that many committed batches rather than
    draining the table — a seam for a test that wants to interrupt a run
    partway and then finish it with a second call. `main` never passes it,
    so an ordinary run always drains.

    **NO `BEGIN IMMEDIATE`, unlike `embed_missing`'s otherwise-identical
    batching loop.** `embed_missing` takes the lock before it reads because
    it both reads and writes `main` (the stamp) inside the same transaction
    — `write_if_current`'s check-then-write. This function never writes
    `main` at all, and `BEGIN IMMEDIATE` acquires its reserved lock on the
    connection's main database regardless of which attached schema the
    write actually lands in (confirmed empirically: a writer holding an
    open transaction on `main` alone made a `BEGIN IMMEDIATE` here fail with
    `database is locked` even though the only statement that followed wrote
    `vecdb`). A plain `DELETE`, with no explicit `BEGIN`, opens pysqlite's
    ordinary implicit DEFERRED transaction instead — its reserved lock is
    acquired against whichever file the DELETE actually touches, `vecdb`,
    at the moment the DELETE runs, so a writer busy on `main` alone never
    blocks this.
    """
    placeholders = ",".join("?" * len(vectors.EMBEDDED_SIGNAL_KINDS))
    select_sql = (
        f"SELECT lv.rowid AS rowid "
        f"FROM vecdb.{vectors.VEC_TABLE} lv "
        f"JOIN {store.signals_source()} AS s ON s.id = lv.item_id "
        f"WHERE lv.vault = ? AND lv.rowid > ? AND s.kind NOT IN ({placeholders}) "
        f"ORDER BY lv.rowid LIMIT ?")
    cursor = 0
    deleted = 0
    batches = 0
    while max_batches is None or batches < max_batches:
        rowids = [r["rowid"] for r in conn.execute(
            select_sql,
            (vectors.VAULT_ARCHIVES, cursor, *vectors.EMBEDDED_SIGNAL_KINDS,
             batch_size)).fetchall()]
        if not rowids:
            break
        cursor = rowids[-1]
        marks = ",".join("?" * len(rowids))
        conn.execute(
            f"DELETE FROM vecdb.{vectors.VEC_TABLE} WHERE rowid IN ({marks})", rowids)
        conn.commit()
        deleted += len(rowids)
        batches += 1
    return deleted


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--dry-run", action="store_true",
                   help="report the plan and exit; write nothing")
    p.add_argument("--batch-size", type=int, default=BATCH_SIZE,
                   help=f"rows deleted per transaction (default: {BATCH_SIZE})")
    args = p.parse_args(argv)

    try:
        conn = vectors.connect(store.DB_PATH) if args.dry_run else vectors.connect_rw()
    except (store.WritesDisabled, FileNotFoundError,
            store.MisconfiguredDatabase) as e:
        print(f"reduce_vector_index: {e}", file=sys.stderr)
        return 2
    # The real run attaches `cold` writable although nothing here writes it:
    # `connect_rw`'s handle is not URI-opened, so a `mode=ro` attach would name
    # a literal `file:...?mode=ro` path. The attach's one side effect is
    # `switch_to_wal` on `cold`, a no-op on the live file, which was already
    # WAL on 2026-10-05.
    try:
        store.attach_cold(conn, readonly=args.dry_run)
    except (FileNotFoundError, store.MisconfiguredDatabase,
            store.MisconfiguredColdDB) as e:
        conn.close()
        print(f"reduce_vector_index: {e}", file=sys.stderr)
        return 2

    try:
        before = _kind_report(conn)
        if args.dry_run:
            after = _project_after(before)
            _print_report(before, after, after_label="projected")
            print("reduce_vector_index: DRY RUN — nothing written")
            return 0
        deleted = reduce_index(conn, args.batch_size)
        after = _kind_report(conn)
    finally:
        conn.close()

    _print_report(before, after, after_label="after")
    print(f"reduce_vector_index: deleted {deleted} row(s)")
    print("reduce_vector_index: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
