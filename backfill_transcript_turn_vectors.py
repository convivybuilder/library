"""One-time backfill: give the Archives' stamped-but-vectorless `transcript-turn`
signals their vectors back, in both tiers.

**Why these rows exist.** The not-embedded rule (convivy/library#177, committed
2026-09-30) made `transcript-turn` outside `EMBEDDED_SIGNAL_KINDS` until the
operator's 2026-10-05 ruling. A row of such a kind went through `embed_item_by_id` at ingest,
or `embed_missing` in the sweep, with no model call and zero vectors, and its
`embedded_at` was stamped. The sweep selects `WHERE embedded_at IS NULL`, so that
stamp kept the row out of every later pass.

**Two ways a row got here.** A turn captured after 2026-09-30 is stamped at ingest.
A turn captured before it, whose own embed failed, was left NULL, and a sweep run
after 2026-09-30 stamped it with zero vectors. That second row can already be
older than 30 days, so the cold-tier mover (scheduled every 6 hours) can have moved
it cold with its stamp.

**The hot leg clears the stamp, and the sweep embeds the row.** `embedded_at` goes
back to NULL on each hot candidate, and `python vectors.py` (`vectors.embed_missing`
until it drains) embeds it through the one write path, `write_if_current`. Until the
sweep reaches a cleared row, it holds NULL and no vectors, and the dry run's
unstamped count (`find_unstamped_hot`) reports it, so the drain is done only when
that count reads 0.

**The cold leg writes vectors directly.** The sweep reads `main.signals` alone, so
it cannot reach a cold row. This leg embeds each cold candidate's text and inserts
its vectors. A cold candidate is any cold turn that holds no vectors and has no hot
copy, stamped or not. A clear that commits between the mover's SELECT and its INSERT
copies a NULL stamp into the cold file, and that row is the one an unstamped
candidate catches. The leg sets the stamp when it is NULL, so the row's vectors and
stamp agree, the way `vectors.insert_vectors` keeps them. A row that already carries
a stamp keeps it. A cold row that also has a hot copy is skipped: the hot copy is the
one the hot leg and the sweep handle, which keeps a mover pass's transient two-copy
state from getting two sets of vectors.

**Vectors are counted from the `library_vectors` side.** One scan collects every
Archives item id that holds a vector, and the candidates are checked against that
set in memory. A COUNT with a NOT EXISTS against the vec0 table ran over five
minutes on the live database. Each cold group repeats that scan inside its
`BEGIN IMMEDIATE`, so an item another run embedded after this run's snapshot is
skipped, not written twice.

**Safe with the service up.** Hot clears commit in batches of `BATCH_SIZE` rows
with `BATCH_SLEEP_SECONDS` between them, each batch one short UPDATE transaction.
The UPDATE repeats the `embedded_at IS NOT NULL` predicate. Cold vectors are
written in groups bounded by `vectors.MAX_CHUNKS_PER_TXN`, with `BATCH_SLEEP_SECONDS`
between groups, and each model call is made outside any write lock. A group reads
its candidates' bodies as it is built, not all at once.

**A clear can race the mover.** If the mover takes a row before its clear reaches it,
the clear's UPDATE matches nothing and the count leaves the row out. The row is then
cold and stamped, and the next run's cold leg embeds it. The drain's dry run confirms
that none is left.

**Re-running is a no-op.** A cleared row holds NULL, a cold row that got vectors
has them, and no later pass selects either.

    LIBRARY_DB=<path> LIBRARY_VECTORS_DB=<path> LIBRARY_COLD_DB=<path> \\
        .venv/bin/python backfill_transcript_turn_vectors.py --dry-run

    LIBRARY_DB=<path> LIBRARY_VECTORS_DB=<path> LIBRARY_COLD_DB=<path> \\
        LIBRARY_WRITES_ENABLED=1 .venv/bin/python backfill_transcript_turn_vectors.py
"""
from __future__ import annotations

import argparse
import datetime as _dt
import sqlite3
import sys
import time

import store
import vectors

KIND = "transcript-turn"

# The pacing the product backfills measured (`backfill_common.py`): 200 single-row
# commits take 3 to 6 ms, and the pause lets a waiting writer retry its busy
# handler several times while the lock is free.
BATCH_SIZE = 200
BATCH_SLEEP_SECONDS = 1.0


def _vectored_ids(conn) -> set[str]:
    """Every Archives item id that holds at least one `library_vectors` row.

    One scan, read once. A NOT EXISTS per candidate probes the vec0 table once per
    signal instead, which is the query that ran over five minutes on the live
    database.
    """
    return {r[0] for r in conn.execute(
        f"SELECT DISTINCT item_id FROM {vectors.VEC_TABLE} WHERE vault = ?",
        (vectors.VAULT_ARCHIVES,))}


def find_hot_candidates(conn, vectored: set[str]) -> list[str]:
    """Hot `transcript-turn` ids whose `embedded_at` is set and that hold no vectors,
    oldest ingest first."""
    stamped = conn.execute(
        "SELECT id FROM main.signals WHERE kind = ? AND embedded_at IS NOT NULL "
        "ORDER BY ingested_at, id", (KIND,)).fetchall()
    return [r[0] for r in stamped if r[0] not in vectored]


def find_unstamped_hot(conn, vectored: set[str]) -> list[str]:
    """Hot `transcript-turn` ids whose `embedded_at` is NULL and that hold no vectors,
    oldest ingest first. The clear leaves each cleared turn here until the sweep embeds
    it, and the cleared turns are the rows the drain must finish. This is read-only and
    does not change which rows the clear selects."""
    unstamped = conn.execute(
        "SELECT id FROM main.signals WHERE kind = ? AND embedded_at IS NULL "
        "ORDER BY ingested_at, id", (KIND,)).fetchall()
    return [r[0] for r in unstamped if r[0] not in vectored]


def find_cold_candidates(conn, vectored: set[str]) -> list[sqlite3.Row]:
    """Cold `transcript-turn` rows that hold no vectors and have no hot copy, stamped
    or not, oldest ingest first. Only the ids are read here; `embed_cold` reads each
    body when its group is built."""
    rows = conn.execute(
        "SELECT c.id FROM cold.signals AS c "
        "WHERE c.kind = ? "
        "AND NOT EXISTS (SELECT 1 FROM main.signals h WHERE h.id = c.id) "
        "ORDER BY c.ingested_at, c.id", (KIND,)).fetchall()
    return [r for r in rows if r["id"] not in vectored]


def clear_stamps(conn, ids: list[str]) -> int:
    """Set `embedded_at` back to NULL on `ids`, one committed batch at a time.
    Returns how many rows the UPDATEs changed. An id the mover took out of hot before
    its batch ran matches no row, so it is not counted."""
    cleared = 0
    for start in range(0, len(ids), BATCH_SIZE):
        if start > 0:
            time.sleep(BATCH_SLEEP_SECONDS)
        batch = ids[start:start + BATCH_SIZE]
        cur = conn.executemany(
            "UPDATE main.signals SET embedded_at = NULL "
            "WHERE id = ? AND kind = ? AND embedded_at IS NOT NULL",
            [(sid, KIND) for sid in batch])
        conn.commit()
        cleared += cur.rowcount
    return cleared


def _groups(items):
    """Consecutive `(row, chunks)` pairs grouped so that each group's chunks fit one
    write transaction, the same bound `vectors.embed_missing` applies."""
    group, chunks_in_group = [], 0
    for row, item_chunks in items:
        if group and chunks_in_group + len(item_chunks) > vectors.MAX_CHUNKS_PER_TXN:
            yield group
            group, chunks_in_group = [], 0
        group.append((row, item_chunks))
        chunks_in_group += len(item_chunks)
    if group:
        yield group


def _items(conn, rows):
    """`(body row, chunks)` for each candidate, reading each body when its turn comes.
    The grouper needs the next item's chunk count before it can close a group, so one
    body past the current group is read early."""
    for r in rows:
        body = conn.execute(
            "SELECT id, kind, stream, body FROM cold.signals WHERE id = ?",
            (r["id"],)).fetchone()
        yield body, vectors.chunks(vectors.text_for_signal(body))


def embed_cold(conn, rows: list[sqlite3.Row]) -> int:
    """Write vectors for the cold rows. Returns how many rows were written.

    Each group's model call runs with no write lock held, and the groups are paced
    as the hot batches are. Each group's inserts then land in one `BEGIN IMMEDIATE`
    transaction, with the same reason `embed_missing` gives. The items are read
    again inside that transaction, so an item another run embedded after the snapshot
    is skipped rather than given a second set. A NULL stamp is set to now, as
    `vectors.insert_vectors` would set it, and a stamp already there is kept.
    """
    written = 0
    for n, group in enumerate(_groups(_items(conn, rows))):
        if n:
            time.sleep(BATCH_SLEEP_SECONDS)
        flat = vectors.embed_texts([c for _, item in group for c in item])
        conn.execute("BEGIN IMMEDIATE")
        vectored = _vectored_ids(conn)
        now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None).isoformat(" ")
        cursor = 0
        for row, item_chunks in group:
            blobs = flat[cursor:cursor + len(item_chunks)]
            cursor += len(item_chunks)
            if row["id"] in vectored:
                continue
            for blob in blobs:
                conn.execute(
                    f"INSERT INTO {vectors.VEC_TABLE}(embedding, vault, item_id) "
                    f"VALUES (?, ?, ?)", (blob, vectors.VAULT_ARCHIVES, row["id"]))
            conn.execute(
                "UPDATE cold.signals SET embedded_at = ? "
                "WHERE id = ? AND embedded_at IS NULL", (now, row["id"]))
            written += 1
        conn.commit()
    return written


def _open(*, dry_run: bool):
    """A read-only handle for the dry run, which cannot write even by accident, and
    the writable handle otherwise. The cold tier is attached on either. A cold file
    that is missing raises from the attach, and that closes the handle first.

    `vectors.connect` does not check that the hot file exists, so the dry run checks
    it here.
    """
    if dry_run:
        if not store.DB_PATH.is_file():
            raise FileNotFoundError(f"no vault database at {store.DB_PATH}")
        conn = vectors.connect(store.DB_PATH)
    else:
        conn = vectors.connect_rw()
    try:
        store.attach_cold(conn, readonly=dry_run)
    except BaseException:
        conn.close()
        raise
    return conn


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="report the counts and write nothing")
    args = parser.parse_args(argv)

    try:
        conn = _open(dry_run=args.dry_run)
    except (store.WritesDisabled, FileNotFoundError, store.MisconfiguredDatabase,
            store.MisconfiguredColdDB) as e:
        print(f"backfill_transcript_turn_vectors: {e}", file=sys.stderr)
        return 2

    try:
        vectored = _vectored_ids(conn)
        hot = find_hot_candidates(conn, vectored)
        cold = find_cold_candidates(conn, vectored)
        unstamped = find_unstamped_hot(conn, vectored)
        print(f"backfill_transcript_turn_vectors: {len(hot)} hot and {len(cold)} "
              f"cold transcript-turn signal(s) hold no vectors")
        print(f"backfill_transcript_turn_vectors: {len(unstamped)} unstamped hot "
              f"transcript-turn signal(s) hold no vectors")
        if args.dry_run:
            print("backfill_transcript_turn_vectors: DRY RUN — nothing written")
            return 0
        cleared = clear_stamps(conn, hot)
        written = embed_cold(conn, cold)
    finally:
        conn.close()

    print(f"backfill_transcript_turn_vectors: cleared {cleared} hot stamp(s) and "
          f"wrote vectors for {written} cold signal(s); run `python vectors.py` to "
          f"embed the cleared hot ones")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
