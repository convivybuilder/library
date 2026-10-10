"""Split the vaults out of Orient's control database into the Library's own.

Phase 3 §4, as a data migration rather than a redesign. It writes a NEW database
holding everything that moves and touches the source only through a read-only
handle, so a run that goes wrong costs a file in a scratch directory.

**IT REFUSES TO READ FROM THE LIVE DATABASE, and that guard is the first thing in
the file because the failure it prevents already happened.** A wrong environment
variable name once migrated the live control DB while its operator believed a dry
run was in progress. So the source is a required argument rather than a default,
and it is compared against the live path by resolved path AND by inode before any
writable handle is opened. `--i-mean-it` is what a real cutover passes; without
it, pointing at the live file is an error rather than a prompt.

**And the guard refuses when it cannot tell which database is live**, rather than
falling back to a guessed default the way a reader may. That fallback was the
first version's hole: with `ORIENT_CONTROL_DB` unset it compared the source
against a path nobody had confirmed, so the live database could be read with no
refusal and no `--i-mean-it` (security-reviewer, PR #22).

**THE SEAM IS DECLARED, AND EVERY TABLE MUST BE ACCOUNTED FOR.** `MOVES`,
`STAYS` and `VEC_TABLES` together have to name every table in the source — vec0's
shadow tables excepted, since the virtual table owns them — and `split` raises
when one is left over. A migration that silently ignores a table it has never
heard of is how a new table gets left behind at the cutover — and the source is a
live schema that grows.

**The vec0 indexes move through the vec0 interface, never by copying their shadow
tables.** A vec0 virtual table is invisible to a connection that has not loaded
`sqlite-vec`, and its shadow tables are private layout. Copy those as ordinary
tables and the result opens cleanly, reports the right row count, and returns
nonsense from a KNN — a corruption that every count-based check passes. So
`verify` reads every vector back through the vec0 interface and compares bytes,
and that comparison is the real acceptance test rather than the row counts.

**It compares all of them rather than a sample**, for reasons `verify` records.

**The two indexes do not share a shape.** `library_vectors` is
`(embedding, vault, +item_id)` and `signal_vectors` is `(embedding, +signal_id)`,
so the columns are read per table. A hardcoded list migrates the first and raises
`no such column: vault` on the second, which is how this was found.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import sys
from pathlib import Path
from urllib.parse import quote

# The Chart House: work items, their history, and the leases that arbitrate them.
CHART_HOUSE = ("issue_items", "issue_comments", "issue_events",
               "issue_leases", "lease_refusals", "courses", "course_events")

# The Archives. **`signal_acks` moves WITH `signals` although no foreign key says
# so.** It carries `signal_id` with a uniqueness constraint and no `FOREIGN KEY`
# clause, so `PRAGMA foreign_key_list` reports a clean seam that is not one.
# Leaving it behind splits a relationship the schema never declared.
ARCHIVES = ("signals", "signal_acks")

# The decision plane. ADR-0033 supersedes ADR-0022's carve-out — a decision may be
# made in Orient, in Deck or in a session, and the RECORD lives in the Library.
DECISIONS = ("decision_items", "decision_comments", "decision_events")

MOVES = CHART_HOUSE + ARCHIVES + DECISIONS

# Orient's own product state, which is not a vault and does not move.
# `alembic_version` stays because it tracks Orient's migration history; the
# Library owns its schema after the cutover and needs its own baseline.
STAYS = ("reading_rooms", "room_entries", "room_requests",
         "push_subscriptions", "alembic_version")

VEC_TABLES = ("library_vectors", "signal_vectors")

# vec0 keeps its private layout in shadow tables named after the virtual table.
# They are neither moved nor counted as unaccounted-for: the virtual table owns
# them, and recreating it through the extension rebuilds them.
_VEC_PREFIXES = tuple(f"{t}_" for t in VEC_TABLES)


def live_path() -> Path | None:
    """The live control database, or None when nothing says where it is.

    Reading Orient's own variable rather than inventing a Library one is
    deliberate: two names for one file is how a dry run ends up pointed
    somewhere its operator did not intend.

    **IT RETURNS None RATHER THAN GUESSING, and an earlier version guessed.** It
    fell back to `~/.orient-control/control.db` when the variable was unset — the
    same silent guess `store.resolve_db` made at the time, and has since stopped
    making for the same reason. A guard that defaults is comparing the source
    against a path nobody confirmed, so with the variable unset an operator could
    point `source` straight at the real live database and be waved through
    (security-reviewer, PR #22). The guard's whole job is knowing which file is
    live; when it cannot know, saying so is the only safe answer.
    """
    raw = os.environ.get("ORIENT_CONTROL_DB")
    return Path(raw) if raw else None


class RefusedError(RuntimeError):
    """The source is the live database, or which database is live is unknown."""


def guard(src: Path, *, allow_live: bool = False) -> None:
    """Refuse the live database unless a cutover explicitly asked for it.

    Both comparisons are needed and they catch different mistakes: the resolved
    path catches a symlink or a `~` that expands to the same file, and the inode
    catches a hard link or a bind mount the path comparison waves through.

    **AN UNSET `ORIENT_CONTROL_DB` REFUSES.** It is an inconclusive read, and an
    inconclusive read of a safety check has to fail closed; proceeding on a
    guessed default is the same class of mistake as the wrong-variable-name
    incident this guard exists because of.
    """
    if allow_live:
        return
    live = live_path()
    if live is None:
        raise RefusedError(
            "ORIENT_CONTROL_DB is unset, so this cannot establish which database "
            "is live and will not guess. Export it, or pass --i-mean-it if you "
            "genuinely intend to read the live database.")
    if src.resolve() == live.resolve():
        raise RefusedError(f"source {src} IS the live control DB ({live})")
    if live.exists() and src.exists() and src.stat().st_ino == live.stat().st_ino:
        raise RefusedError(f"source {src} is the same inode as the live DB ({live})")


def _uri(path: Path, *, readonly: bool) -> str:
    """A SQLite URI for `path`, with the path percent-encoded.

    **INTERPOLATING A RAW PATH DEFEATS THE READ-ONLY GUARANTEE, and it is not a
    hardening nicety** (security-reviewer, PR #22). SQLite's URI parser splits on
    the FIRST `?` in the whole string, so a path containing `?` or `#` has
    everything after it swallowed as query or dropped as fragment — directory
    components included, and `mode=ro` along with them. Reproduced:
    `file:/tmp/weird?dir/test.db?mode=ro` opens `/tmp/weird`, creates it, and
    accepts a `CREATE TABLE` through the handle the caller believes is read-only.

    That makes the file `guard` approved and the file actually opened two
    different things, which is the one divergence this module exists to prevent.
    `quote` with `/` kept safe is what SQLite's own URI documentation prescribes.
    """
    encoded = quote(str(path), safe="/")
    return f"file:{encoded}?mode=ro" if readonly else f"file:{encoded}"


def connect(path: Path, *, readonly: bool) -> sqlite3.Connection:
    """A connection with `sqlite-vec` loaded, which the vec0 tables require.

    Extension loading is switched off again immediately, so nothing later in the
    process can pull in an arbitrary extension through this handle."""
    import sqlite_vec
    uri = _uri(path, readonly=readonly)
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    try:
        sqlite_vec.load(conn)
    finally:
        conn.enable_load_extension(False)
    return conn


def _tables(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["name"]: r["sql"] for r in conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'")}


def unaccounted(conn: sqlite3.Connection) -> list[str]:
    """Tables the seam does not mention, which is the thing to fail on.

    Shadow tables belonging to a vec0 index are excluded: the virtual table owns
    them and recreating it rebuilds them.
    """
    known = set(MOVES) | set(STAYS) | set(VEC_TABLES)
    return sorted(t for t in _tables(conn)
                  if t not in known and not t.startswith(_VEC_PREFIXES))


def _digest(conn: sqlite3.Connection, table: str) -> str:
    """A hash of one table's whole contents, in a deterministic row order.

    `rowid` is the order because the copy is `INSERT ... SELECT *`, which
    preserves it, so the two sides line up row for row. A `WITHOUT ROWID` table
    has none and falls back to ordering by every column, which is slower and
    equally deterministic. Values go in with their type name attached, so a
    string `"1"` cannot hash the same as an integer `1`.
    """
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    collist = ", ".join(f'"{c}"' for c in cols)
    try:
        rows = conn.execute(f"SELECT {collist} FROM {table} ORDER BY rowid")
        h = hashlib.sha256()
        for row in rows:
            for value in row:
                h.update(f"{type(value).__name__}:{value!r}\x1f".encode())
            h.update(b"\x1e")
        return h.hexdigest()
    except sqlite3.OperationalError:
        rows = conn.execute(f"SELECT {collist} FROM {table} ORDER BY {collist}")
        h = hashlib.sha256()
        for row in rows:
            for value in row:
                h.update(f"{type(value).__name__}:{value!r}\x1f".encode())
            h.update(b"\x1e")
        return h.hexdigest()


def counts(conn: sqlite3.Connection, tables) -> dict[str, int]:
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in tables}


def split(src: Path, dest: Path, *, allow_live: bool = False) -> dict:
    """Write `dest` holding everything that moves. Returns the row counts.

    The source is opened read-only and never written to. Ordinary tables move by
    `INSERT ... SELECT` across an attached read-only handle; the vec0 indexes are
    recreated through the extension and refilled row by row.
    """
    # **RESOLVE FIRST, THEN GUARD THE RESOLVED PATH.** The order is the whole
    # point. A first version guarded `src` and resolved afterwards, which left a
    # window two lines wide: `guard` resolves internally to compare, and a
    # symlink swapped between that and the caller's own resolve is opened with a
    # check that already said "not live" (security-reviewer, PR #22, round 2).
    # Resolving up front makes `guard`'s internal resolve idempotent rather than
    # a second, independent race.
    src = src.resolve()
    guard(src, allow_live=allow_live)
    # `dest` is resolved for the same reason `src` is, and because pathlib keeps
    # exactly two leading slashes: unresolved, `//data/x` becomes `file://data/x`,
    # where SQLite reads `data` as a URI authority and refuses the open
    # (security-reviewer, PR #22). It fails closed rather than misdirecting, and
    # a migration that will not start is still a migration that will not start.
    dest = dest.resolve()
    if dest.exists():
        raise FileExistsError(f"{dest} exists; refusing to overwrite")

    ro = connect(src, readonly=True)
    try:
        stray = unaccounted(ro)
        if stray:
            raise ValueError(
                f"tables on neither side of the seam: {', '.join(stray)}. "
                f"Add each to MOVES or STAYS — a table the split has never heard "
                f"of is one that gets left behind at the cutover.")
        schema = _tables(ro)
        missing = [t for t in MOVES if t not in schema]
        if missing:
            raise ValueError(f"source is missing tables the seam expects: {missing}")

        rw = connect(dest, readonly=False)
        try:
            for t in MOVES:
                rw.execute(schema[t])
            # **`tbl_name` SAYS WHICH TABLE AN INDEX BELONGS TO. An earlier
            # version matched `" ON <table>"` in the index's SQL text and got it
            # wrong both ways** (reviewer, PR #22). An index written
            # `ON [issue_leases]` or with backticks was not recognised and was
            # silently dropped — including `ix_issue_leases_live_root`, the
            # partial unique index that IS the one-live-lease-per-root rule, with
            # `split` reporting success and `verify` returning a clean pass. And a
            # table whose name merely starts with a moved table's name would have
            # its index copied and then fail on a table that was never created.
            # SQLite already answers the question exactly; asking it removes both.
            moving = set(MOVES)
            for r in ro.execute(
                    "SELECT sql, tbl_name FROM sqlite_master WHERE type='index' "
                    "AND sql IS NOT NULL"):
                if r["tbl_name"] in moving:
                    rw.execute(r["sql"])
            rw.commit()

            rw.execute("ATTACH DATABASE ? AS srcdb", (_uri(src, readonly=True),))
            try:
                for t in MOVES:
                    # ORDER BY rowid so the copy's row order is stated rather
                    # than inherited from the planner. `_digest` orders both
                    # sides independently, so this is belt to that braces.
                    rw.execute(f"INSERT INTO main.{t} SELECT * FROM srcdb.{t} ORDER BY rowid")
                rw.commit()
            finally:
                rw.execute("DETACH DATABASE srcdb")

            for vt in VEC_TABLES:
                if vt not in schema:
                    continue
                rw.execute(schema[vt])
                # **`rowid` IS AMONG THESE COLUMNS, and `verify` leans on it.**
                # `PRAGMA table_info` exposes `rowid` as a real column on a vec0
                # table, so it is selected and inserted verbatim rather than
                # reassigned — which is what lets `verify` key its comparison by
                # rowid and catch two chunks of one item swapped. Source rowids
                # have gaps wherever rows were deleted over a vault's life, and
                # those gaps are reproduced exactly. Were this to stop holding,
                # the key sets would diverge and `verify` would report rows that
                # did not arrive, so it fails loud rather than silently.
                cols = [r[1] for r in ro.execute(f"PRAGMA table_info({vt})")]
                collist = ", ".join(cols)
                marks = ", ".join("?" * len(cols))
                rows = ro.execute(f"SELECT {collist} FROM {vt}").fetchall()
                rw.executemany(
                    f"INSERT INTO {vt}({collist}) VALUES ({marks})",
                    [tuple(r[c] for c in cols) for r in rows])
            rw.commit()
            return counts(rw, [t for t in MOVES + VEC_TABLES if t in schema])
        finally:
            rw.close()
    finally:
        ro.close()


def verify(src: Path, dest: Path, *, probes: int = 25, k: int = 10,
           seed: int | None = None, allow_live: bool = False) -> list[str]:
    """Compare `dest` against `src`. Returns the problems found, empty on pass.

    **The vector comparison is the acceptance test and the counts are not.** A
    shadow-table copy reproduces every count exactly and returns nonsense from a
    search, so a migration verified on counts alone can be wholly broken and read
    as clean.

    **It guards the source too.** The guard used to live in `split` alone, so
    anything calling `verify` directly ran full KNN scans against whatever it was
    handed, live database included (security-reviewer, PR #22). Read-only bounds
    that to lock contention rather than damage, and a safety check covering half
    a module is not one.

    **THE VECTORS ARE COMPARED EXHAUSTIVELY RATHER THAN SAMPLED, and pushing on
    the sample size is what showed sampling was the wrong tool** (security-
    reviewer, PR #22). The argument ran: five probes miss a 1% corruption 95% of
    the time, 64 miss it 53%, and 300 would be needed for 95% detection. Raising
    it to 300 then failed against the real database — and the failure was the
    check's, not the migration's. `signal_vectors` holds thousands of identical
    embeddings, so a `k = 10` query truncates a much larger tied group at
    distance 0.0 arbitrarily, and the two databases break that tie differently.
    Distances matched exactly; only which of the equally-correct rows came back
    differed, on 3 probes in 400.

    A sampled check that fails a good migration about 1% of the time is worse
    than no check, because a cutover reads the failure as corruption or, having
    seen it before, learns to wave it through. Reading every vector back through
    the vec0 interface and comparing bytes removes the sampling, the confidence
    arithmetic and the tie sensitivity together: 87,000 rows is a few seconds.

    The KNN comparison stays, on **distances only**, because that is what
    exercises the index rather than the storage — a broken index returns wrong
    distances, and it is tie-order that is arbitrary rather than distance.
    """
    import random

    src = src.resolve()
    dest = dest.resolve()
    guard(src, allow_live=allow_live)
    problems: list[str] = []
    ro, dst = connect(src, readonly=True), connect(dest, readonly=True)
    try:
        schema = _tables(ro)
        for t in MOVES:
            if t not in schema:
                continue
            a = ro.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            b = dst.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            if a != b:
                problems.append(f"count mismatch {t}: source {a}, dest {b}")
                continue
            # **Content, not only the count** — the same right-count-wrong-data
            # gap the vectors are checked for below. Both are exhaustive; the
            # difference is only the method, a hash over the row stream here
            # against a byte comparison per row there, because these tables have
            # no vec0 interface to read back through.
            if _digest(ro, t) != _digest(dst, t):
                problems.append(f"content differs in {t}, though the counts agree")

        present = set(_tables(dst))
        for t in STAYS:
            if t in present:
                problems.append(f"{t} leaked into the destination")

        # **Every index on a moved table has to arrive.** One of them is the
        # partial unique index enforcing one live lease per root, so a missing
        # index is a lost invariant rather than a slower query.
        def indexes(conn, names):
            return {r["name"] for r in conn.execute(
                "SELECT name, tbl_name FROM sqlite_master WHERE type='index' "
                "AND sql IS NOT NULL") if r["tbl_name"] in names}
        want, got = indexes(ro, set(MOVES)), indexes(dst, set(MOVES))
        for missing in sorted(want - got):
            problems.append(f"index {missing} did not arrive")

        rng = random.Random(seed) if seed is not None else random.Random()
        for vt in VEC_TABLES:
            if vt not in schema:
                continue
            n = ro.execute(f"SELECT COUNT(*) FROM {vt}").fetchone()[0]
            m = dst.execute(f"SELECT COUNT(*) FROM {vt}").fetchone()[0]
            if n != m:
                problems.append(f"count mismatch {vt}: source {n}, dest {m}")
                continue
            if not n:
                continue
            idcol = "item_id" if vt == "library_vectors" else "signal_id"

            # Every vector, read back through the vec0 interface and compared by
            # bytes. No sampling, so no confidence level to argue about and no
            # partial corruption that a draw can miss.
            #
            # **KEYED BY `rowid`, BECAUSE `item_id` IS NOT UNIQUE and keying by it
            # made "exhaustive" a lie** (reviewer, PR #22). `insert_vectors`
            # writes one row per CHUNK and a 100,000-character item chunks to 78
            # of them, all sharing an `item_id`. A dict keyed on that collapses
            # every chunk onto the last one, so corrupting any earlier chunk
            # passed silently — reproduced with 3 chunks under one id, where
            # damaging chunk 1 or 2 was invisible. Most of the corruption surface
            # was still riding on the KNN draw the exhaustive check was supposed
            # to have replaced.
            def vectors(conn, table=vt, col=idcol):
                return {r["rowid"]: (r[col], bytes(r["embedding"]))
                        for r in conn.execute(
                            f"SELECT rowid, {col}, embedding FROM {table}")}
            a, b = vectors(ro), vectors(dst)
            if a.keys() != b.keys():
                problems.append(f"{vt}: {len(a.keys() - b.keys())} rows did not arrive")
            differing = sum(1 for key in a.keys() & b.keys() if a[key] != b[key])
            if differing:
                problems.append(
                    f"{vt}: {differing} vectors differ from the source — the "
                    f"index did not survive the move, whatever the counts say")

            # And a KNN spot check on DISTANCES only. Which of a set of
            # equally-distant rows comes back is arbitrary and differs between
            # two correct databases: `signal_vectors` holds thousands of
            # identical embeddings, so comparing ids here failed 3 probes in 400
            # against a migration that was perfect.
            #
            # **IT CATCHES NOTHING THE BYTE COMPARISON ABOVE WOULD MISS TODAY,
            # and it is kept for the day that stops being true** (security-
            # reviewer, PR #22). `sqlite-vec` 0.1.9 does brute-force KNN over the
            # same stored bytes a plain SELECT reads, so distance is a function
            # of what was already compared and there is no separate index
            # structure to corrupt. Upgrade to real ANN indexing or quantization
            # and the query path stops being a function of the storage — at which
            # point this becomes the only check that would notice. Naming the
            # assumption is the point: a control that has quietly stopped
            # protecting anything still passes.
            for _ in range(probes):
                probe = ro.execute(
                    f"SELECT embedding FROM {vt} LIMIT 1 OFFSET ?",
                    (rng.randrange(n),)).fetchone()[0]
                q = (f"SELECT distance FROM {vt} "
                     f"WHERE embedding MATCH ? AND k = ? ORDER BY distance")
                want = [r["distance"] for r in ro.execute(q, (probe, k))]
                got = [r["distance"] for r in dst.execute(q, (probe, k))]
                if want != got:
                    problems.append(
                        f"{vt}: nearest-neighbour DISTANCES differ, so the index "
                        f"itself is wrong rather than merely ordered differently")
                    break
    finally:
        ro.close()
        dst.close()
    return problems


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("source", type=Path, help="the database to read (never written)")
    p.add_argument("dest", type=Path, help="the Library database to create")
    p.add_argument("--i-mean-it", action="store_true",
                   help="permit the live control DB as the source (a real cutover)")
    args = p.parse_args(argv)
    try:
        moved = split(args.source, args.dest, allow_live=args.i_mean_it)
    except (RefusedError, ValueError, FileExistsError) as e:
        print(f"migrate: {e}", file=sys.stderr)
        return 2
    for t, n in moved.items():
        print(f"  moved {t:32} {n}")
    problems = verify(args.source, args.dest, allow_live=args.i_mean_it)
    for msg in problems:
        print(f"  PROBLEM {msg}", file=sys.stderr)
    print("migrate:", "PASS" if not problems else f"FAIL ({len(problems)})")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
