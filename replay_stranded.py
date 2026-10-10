"""Replay the rows stranded in Orient's read-only copy into the Library, matching
on content where the ids collide.

`issue_01M094X927D4S4D1M3RN4YZX9X`. Orient's copy of the vaults was kept as the
write cutover's rollback and never froze — the `vault_plane` default still sent
some writers there until the inversion landed, so both files diverged for as
long as that ran. This carries what only Orient's copy holds into the Library,
which is the file everything now reads.

**IT NEVER WRITES TO ORIENT'S COPY, and `guard_source` is what proves the file
handed to it actually is that copy** — by the same schema fingerprint
`store.py` uses to refuse the reverse mistake, rather than by trusting a path.
`guard_dest` is the complementary half: it refuses to write anywhere but the
Library's own live file, resolved the same way the service resolves it, so a
misconfigured environment refuses instead of silently doing nothing or writing
to the wrong database.

**THREE TABLES CANNOT BE MATCHED OR INSERTED BY ID.** `issue_comments`,
`issue_events` and `decision_events` carry INTEGER primary keys that both
databases number from 1, and the ids collide: nineteen currently hold
genuinely different rows in the two files, some of them live canonical writes
on the Library's side. An id-keyed comparison would report agreement that does
not exist, and `INSERT OR REPLACE` against a colliding id would silently
overwrite a row that was never in question. So these three are matched on
content — `item_id` plus the columns that describe the event — and inserted
with the id omitted; the destination assigns a fresh one. **No `INSERT OR
REPLACE`, no `ON CONFLICT DO UPDATE`, anywhere in this file.** The other five
moved tables are ULID-keyed, safe to match and insert by id, and a plain
insert against a genuine collision raises rather than overwriting.

**A replayed item or signal arrives looking already embedded, and that is
wrong.** Orient's copy carries `embedded_at` from ITS OLD index, built before
the cutover; the Library never embedded these rows into its own
`library_vectors`/`signal_vectors`. Copying that column verbatim would make
`vectors.embed_missing()`'s sweep — which filters `WHERE embedded_at IS
NULL` — skip them forever, silently. `RENULL_ON_ARRIVAL` clears it on the way
in, and `main` drains the sweep afterwards.

**Row counts before and after are not what this asserts on**, though the
issue's method note describes it that way. The Library's own database is live
traffic the whole time this runs — this session alone wrote to it minutes
before this file existed — so a table-wide count delta would be confounded by
ordinary concurrent writes with nothing wrong. What this asserts instead:
every specific row this run selected to replay is present afterward, the
content-matched stranded count is zero when re-measured after the write, and
every row that existed in a content-matched table BEFORE this ran is
byte-identical after (a digest keyed by the row's own id, not by count).
"""
from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import store
import vectors

ORIENT_DEFAULT = Path.home() / ".orient-control" / "control.db"
BACKUP_DIR = Path.home() / ".library-backups"

# ULID-keyed: the id identifies the same row in both files. Plain insert,
# id preserved — a genuine collision raises rather than overwriting.
BY_ID = ("issue_items", "signals", "decision_items", "signal_acks",
         "courses", "issue_leases", "lease_refusals", "course_events")

# INTEGER-keyed: the id is meaningless across the two files. Matched and
# inserted on content; the destination assigns the key.
BY_CONTENT = {
    "issue_comments": ("item_id", "actor", "kind", "body", "at"),
    "issue_events": ("item_id", "actor", "from_state", "to_state", "note", "at"),
    "decision_events": ("item_id", "actor", "from_state", "to_state",
                        "directive", "at"),
}

# Cleared on arrival so the Library's own embed sweep — which only ever looks
# at rows it has not embedded — picks these up instead of skipping them
# forever on the strength of a stamp from an index it never built.
RENULL_ON_ARRIVAL = {"issue_items": ("embedded_at",), "signals": ("embedded_at",)}

# **THE EXACT `str(sqlite3.IntegrityError)` SQLITE REPORTS for each collision
# this class of run is known to handle safely, confirmed empirically rather
# than assumed** (security-reviewer, PR #39) — SQLite reports column names,
# never a named CONSTRAINT clause or a partial UNIQUE INDEX's own name, so
# `ix_issue_leases_live_root` (one live lease per root) reports as
# `issue_leases.root_id`, indistinguishable BY SHAPE from a benign dedup key
# unless the exact signature is checked. A day-scoped dedup key firing twice,
# once into each database, is the one case this script has actually hit and
# verified safe to skip. Anything else — a live-lease collision, a NOT NULL
# violation from a schema that drifted, or a genuine content anomaly — gets a
# different, unrecognized-collision refusal instead of the same reassuring
# label, because a materially more dangerous collision must not read like the
# benign one to an operator skimming the report.
KNOWN_BENIGN_COLLISIONS = frozenset({
    "UNIQUE constraint failed: issue_items.once_key",
    "UNIQUE constraint failed: decision_items.once_key",
    "UNIQUE constraint failed: signals.dedup_key",
    "UNIQUE constraint failed: signal_acks.signal_id, signal_acks.identity",
})


class RefusedError(RuntimeError):
    """The source is not Orient's copy, or the destination is not the Library's
    own live file, or which is which cannot be established."""


def _ident(name: str) -> str:
    """A SQL identifier, quoted, with an embedded `"` doubled rather than
    trusted absent (security-reviewer, PR #39). Column names here only ever
    come from `PRAGMA table_info()` against the live, migration-controlled
    schema, never from row data or CLI input — but the schema is exactly the
    thing a script sharing this write path with an admin exists to trust
    less, not more, than it has to."""
    return '"' + name.replace('"', '""') + '"'


def _uri(path: Path, *, readonly: bool) -> str:
    """A SQLite URI for `path`, with the path percent-encoded.

    Duplicated from `store.py`/`migrate.py` rather than shared, deliberately —
    the same reason both of those give: this must not depend on either
    resolving successfully to do its own job safely.
    """
    encoded = quote(str(path), safe="/")
    return f"file:{encoded}?mode=ro" if readonly else f"file:{encoded}"


def _table_columns(conn: sqlite3.Connection, schema: str, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA {schema}.table_info({table})")]


def guard_source(source: Path) -> None:
    """Refuse a file that is not Orient's control database, by content.

    The same fingerprint `store.py` uses to refuse the opposite mistake —
    a `LIBRARY_DB` that names Orient's file — asked here of the other
    direction: a `--source` that does not name it. A file missing every one of
    `ORIENT_ONLY_TABLES` is not Orient's copy, whatever the path claims.
    """
    if not source.is_file():
        raise RefusedError(f"no database at {source}")
    conn = sqlite3.connect(_uri(source, readonly=True), uri=True)
    try:
        present = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    if not present & set(store.ORIENT_ONLY_TABLES):
        raise RefusedError(
            f"{source} holds none of {store.ORIENT_ONLY_TABLES} — refusing to "
            f"treat an unidentified file as Orient's stranded copy.")


def guard_dest() -> Path:
    """The Library's own live database, resolved the way the service resolves
    it, or a refusal.

    This writes into that file and nowhere else. `resolve_db` refusing to
    guess is what makes this refuse too, rather than falling back to a path
    nobody confirmed and writing there.
    """
    path, source = store.resolve_db()
    if source != "LIBRARY_DB":
        raise RefusedError(
            f"resolved database is {path} (from {source}), not LIBRARY_DB. "
            f"This replays into the Library's own live file only. Set "
            f"LIBRARY_DB and LIBRARY_WRITES_ENABLED=1 and run it again.")
    if not path.is_file():
        raise RefusedError(
            f"LIBRARY_DB names {path}, which does not exist. This replays "
            f"INTO the Library's own live database and does not create one.")
    return path


def _guard_distinct(source: Path, dest: Path) -> None:
    source, dest = source.resolve(), dest.resolve()
    if source == dest:
        raise RefusedError(f"source and destination are the same path ({source})")
    if source.exists() and dest.exists() and source.stat().st_ino == dest.stat().st_ino:
        raise RefusedError(f"source and destination are the same inode ({source})")


def _digest_by_id(conn: sqlite3.Connection, schema: str, table: str,
                  cols: list[str]) -> dict:
    """One hash per row, keyed by the row's own id — not a hash of the whole
    table, because only pre-existing rows are asserted on and the table grows
    during this run."""
    collist = ", ".join(_ident(c) for c in cols)
    out = {}
    for row in conn.execute(f"SELECT {collist} FROM {schema}.{table}"):
        h = hashlib.sha256()
        for value in row:
            h.update(f"{type(value).__name__}:{value!r}\x1f".encode())
        out[row[0]] = h.hexdigest()
    return out


def stranded(conn: sqlite3.Connection) -> dict[str, int]:
    """Rows present in the attached `o` schema and absent from `main`. The same
    measurement `quiet-check.py` takes, kept identical deliberately — a second
    spelling of one fingerprint could drift."""
    out = {}
    for t in BY_ID:
        out[t] = conn.execute(
            f"SELECT COUNT(*) FROM o.{t} WHERE id NOT IN "
            f"(SELECT id FROM main.{t})").fetchone()[0]
    for t, cols in BY_CONTENT.items():
        key = " || char(31) || ".join(f"coalesce({c},'')" for c in cols)
        out[t] = conn.execute(
            f"SELECT COUNT(*) FROM o.{t} WHERE ({key}) NOT IN "
            f"(SELECT {key} FROM main.{t})").fetchone()[0]
    return out


def _check_schemas_match(conn: sqlite3.Connection) -> None:
    for t in BY_ID:
        src, dst = (_table_columns(conn, s, t) for s in ("o", "main"))
        if src != dst:
            raise RefusedError(
                f"{t}'s columns differ between source and destination "
                f"({src} vs {dst}) — refusing a column-by-position insert "
                f"against schemas that have drifted apart.")


def replay(conn: sqlite3.Connection) -> tuple[dict[str, int], dict[str, list]]:
    """Insert every stranded row, once, in one transaction. Returns
    (moved-per-table, skipped-per-table). Must be called with `o` already
    attached read-only and `main` the Library's own writable file.

    **A BY_ID ROW CAN COLLIDE ON A UNIQUE CONSTRAINT OTHER THAN ITS ID, AND
    THAT IS NOT THE SAME SITUATION AS AN ID COLLISION.** `issue_items.once_key`,
    `decision_items.once_key`, `signals.dedup_key` and
    `signal_acks(signal_id, identity)` can all be shared with a row that
    already exists under a DIFFERENT id — found in practice: a
    `once_key`-deduplicated bug-guard filing landed once in Orient's copy
    during the split-brain window and again, for real, in the Library a day
    later, because its own dedup check could only see the database it was
    actually configured to read. The two rows are not the same event —
    different bodies, different timestamps — so the earlier one is neither a
    safe overwrite nor a safe blind insert. **It is skipped rather than
    failing the whole run**, one row at a time inside the same transaction, so
    one collision does not block every other row this run is here to move.
    What to do with a skipped row is a judgement call this function does not
    make; it reports the row and what it collided with, and stops there.

    **AN UNRECOGNIZED COLLISION FAILS THE WHOLE RUN INSTEAD OF SKIPPING.**
    `KNOWN_BENIGN_COLLISIONS` is the exact, closed set of signatures this has
    been checked against; anything else — a live-lease collision, a NOT NULL
    violation, a signature this docstring has not been updated to cover — is
    refused rather than silently filed under the same "benign dedup key"
    label a genuinely different, more consequential collision must not share.
    """
    moved: dict[str, int] = {}
    skipped: dict[str, list] = {}
    for t in BY_ID:
        cols = _table_columns(conn, "main", t)
        renull = RENULL_ON_ARRIVAL.get(t, ())
        collist = ", ".join(_ident(c) for c in cols)
        marks = ", ".join("?" * len(cols))
        rows = conn.execute(
            f"SELECT {collist} FROM o.{t} WHERE id NOT IN "
            f"(SELECT id FROM main.{t})").fetchall()
        n, table_skipped = 0, []
        for row in rows:
            values = [None if c in renull else row[i] for i, c in enumerate(cols)]
            try:
                conn.execute(
                    f"INSERT INTO main.{t} ({collist}) VALUES ({marks})", values)
                n += 1
            except sqlite3.IntegrityError as e:
                if str(e) not in KNOWN_BENIGN_COLLISIONS:
                    raise RefusedError(
                        f"{t} id {row[0]!r}: {e} — this does not match any of "
                        f"the once_key/dedup_key/signal_acks collisions this "
                        f"script has been checked against and knows are safe "
                        f"to skip. Stopping here rather than guessing whether "
                        f"it's safe; investigate by hand.") from e
                table_skipped.append((row[0], str(e)))
        moved[t] = n
        if table_skipped:
            skipped[t] = table_skipped
    for t, key_cols in BY_CONTENT.items():
        cols = [c for c in _table_columns(conn, "main", t) if c != "id"]
        collist = ", ".join(_ident(c) for c in cols)
        key = " || char(31) || ".join(f"coalesce({c},'')" for c in key_cols)
        cur = conn.execute(
            f"INSERT INTO main.{t} ({collist}) "
            f"SELECT {collist} FROM o.{t} WHERE ({key}) NOT IN "
            f"(SELECT {key} FROM main.{t})")
        moved[t] = cur.rowcount
    return moved, skipped


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--source", type=Path, default=ORIENT_DEFAULT,
                   help=f"Orient's stranded copy (default: {ORIENT_DEFAULT})")
    p.add_argument("--i-mean-it", action="store_true",
                   help="write; without this, report the plan and exit")
    args = p.parse_args(argv)

    try:
        dest = guard_dest()
        source = args.source.expanduser()
        guard_source(source)
        _guard_distinct(source, dest)
    except RefusedError as e:
        print(f"replay: {e}", file=sys.stderr)
        return 2

    try:
        plan_conn = sqlite3.connect(_uri(dest, readonly=True), uri=True)
        try:
            plan_conn.execute(
                "ATTACH DATABASE ? AS o", (_uri(source, readonly=True),))
            _check_schemas_match(plan_conn)
            counts = stranded(plan_conn)
        finally:
            plan_conn.close()
    except (RefusedError, sqlite3.Error) as e:
        print(f"replay: {e}", file=sys.stderr)
        return 2

    total = sum(counts.values())
    print(f"replay: {total} row(s) stranded in {source}, absent from {dest}")
    for t in BY_ID:
        if counts[t]:
            print(f"    {t:18} {counts[t]:4}  matched by id")
    for t in BY_CONTENT:
        if counts[t]:
            print(f"    {t:18} {counts[t]:4}  matched by content "
                  f"({', '.join(BY_CONTENT[t])})")
    if not total:
        print("replay: nothing stranded; nothing to do")
        return 0
    if not args.i_mean_it:
        print("\nreplay: dry run. Pass --i-mean-it to write.")
        return 0
    if not store.WRITES_ENABLED:
        print("replay: LIBRARY_WRITES_ENABLED is not set to '1'. --i-mean-it "
              "asks to write, and this goes through store.connect_rw(), the "
              "same door every other write path in this service is refused "
              "at. Set it and run it again.", file=sys.stderr)
        return 2

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = BACKUP_DIR / f"library-pre-replay-{stamp}.db"
    if backup.exists():
        print(f"replay: {backup} already exists; refusing to overwrite",
              file=sys.stderr)
        return 2

    committed = False
    skipped: dict[str, list] = {}
    conn = store.connect_rw()
    try:
        conn.execute("VACUUM INTO ?", (str(backup),))
        print(f"replay: backed up {dest} to {backup}")

        conn.execute("ATTACH DATABASE ? AS o", (_uri(source, readonly=True),))
        try:
            try:
                _check_schemas_match(conn)
                before_digests = {
                    t: _digest_by_id(conn, "main", t,
                                     [c for c in _table_columns(conn, "main", t)])
                    for t in BY_CONTENT}
                fresh_counts = stranded(conn)
                moved, skipped = replay(conn)
                conn.commit()
                committed = True
            except Exception:
                # However far the write got, it did not commit — roll it back
                # explicitly rather than leaving an aborted transaction open
                # on the connection DETACH below is about to run against.
                conn.rollback()
                raise

            print("replay: wrote")
            for t, n in moved.items():
                # A row can be legitimately SKIPPED (a UNIQUE collision on
                # something other than id — see `replay`'s docstring), so the
                # count that must match the pre-write measurement is moved
                # PLUS skipped, not moved alone.
                expected = fresh_counts[t]
                accounted = n + len(skipped.get(t, ()))
                if accounted != expected:
                    raise RefusedError(
                        f"{t}: inserted {n} and skipped {len(skipped.get(t, ()))} "
                        f"= {accounted} row(s) accounted for, but the pre-write "
                        f"measurement, taken inside this same transaction, said "
                        f"{expected} — refusing to report success on a number "
                        f"that does not match what was just examined")
                if n:
                    print(f"    {t:18} {n}")
            for t, rows in skipped.items():
                print(f"    {t:18} {len(rows)} SKIPPED (unique-constraint "
                      f"collision, not an id collision — needs a human look):")
                for rid, err in rows:
                    print(f"        {rid}: {err}")

            remaining = stranded(conn)
            # A skipped row is still, correctly, stranded — it was never
            # written. Subtract the known skips before asserting on the rest.
            still = {t: (n - len(skipped.get(t, ())))
                    for t, n in remaining.items()}
            still = {t: n for t, n in still.items() if n}
            if still:
                raise RefusedError(
                    f"still stranded after the write, beyond the reported "
                    f"skips: {still} — the content match should be zero "
                    f"everywhere else")

            for t in BY_CONTENT:
                after = _digest_by_id(conn, "main", t,
                                      [c for c in _table_columns(conn, "main", t)])
                changed = [rid for rid, h in before_digests[t].items()
                          if after.get(rid) != h]
                if changed:
                    raise RefusedError(
                        f"{t}: {len(changed)} pre-existing row(s) changed "
                        f"during the replay (ids: {changed[:10]}...) — this "
                        f"must never happen from a plain insert; investigate "
                        f"before trusting anything else this run reported")
        finally:
            conn.execute("DETACH DATABASE o")
    except (RefusedError, sqlite3.Error) as e:
        # **THE COMMIT LINE ABOVE IS THE WATERSHED.** Every failure before it
        # left the database exactly as it was — the transaction never
        # committed, so closing the connection below discards it. Every
        # failure after it is reporting on data that already landed; saying
        # so is the only thing that keeps a caller from reading "replay:
        # <error>" as "nothing happened" when the opposite may be true.
        conn.close()
        if committed:
            print(f"replay: WROTE, then failed verifying it: {e}\n"
                  f"replay: the write already committed to {dest}. The "
                  f"pre-replay backup is at {backup} if this needs undoing.",
                  file=sys.stderr)
        else:
            print(f"replay: {e} (nothing committed; {dest} is unchanged)",
                  file=sys.stderr)
        return 2
    conn.close()

    if skipped:
        print("replay: content match is zero everywhere else; no pre-existing "
              "row moved")
    else:
        print("replay: content match is zero everywhere; no pre-existing row moved")

    print("replay: draining the embed sweep for what arrived")
    drained = 0
    try:
        while True:
            n = vectors.embed_missing()
            if not n:
                break
            drained += n
            print(f"    embedded {n} (running total {drained})")
    except Exception as e:
        # The write is already committed and verified by this point — an
        # embed failure here is a separate, later problem, not a reason to
        # doubt anything reported above. Reported distinctly rather than
        # crashing raw, since a caller reading a traceback here could
        # mistake it for the write itself having gone wrong.
        print(f"replay: embed sweep failed after {drained} embedded: {e}\n"
              f"replay: the write itself is unaffected and already verified; "
              f"the un-embedded rows can be swept later with "
              f"vectors.embed_missing().", file=sys.stderr)
        return 1
    print(f"replay: embed sweep drained, {drained} total")

    if skipped:
        total_skipped = sum(len(v) for v in skipped.values())
        print(f"replay: PASS, with {total_skipped} row(s) skipped — see above. "
              f"Not done until those are looked at by hand.")
        return 1
    print("replay: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
