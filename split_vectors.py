"""split_vectors — the one-time data migration that moves `library_vectors`
and `signal_vectors` out of the main vault database into their own file
(memory-retention epic clause 7, issue_01M35R5GA1SKRG2CMQRF189NDE).

**A script, not an alembic migration, for the same reason `init_vectors.py`
is one.** Alembic's connection does not load `sqlite-vec` (`alembic/README`)
— a vec0 virtual table is invisible to a connection that has not loaded the
extension, so a revision that tried to `INSERT` from one, let alone `DROP`
one, would raise `no such module: vec0` on its first statement.

**Copy, verify, THEN mutate the source — never the other order.** `source`
is read through an attached, loaded handle while the vec0 tables are copied
into `dest`; `verify` reads every vector back through the vec0 interface and
compares bytes, exhaustively rather than sampled, for `migrate.py`'s reason
(recorded on its own `verify`): a shadow-table copy would reproduce every
count exactly and return nonsense from a KNN, and a migration verified on
counts alone can be wholly broken and read as clean. Only once `verify`
reports no problems does `drop` touch `source` at all — `DROP TABLE` on
each vec0 table, which removes its shadow tables too, since the virtual
table owns them.

**`VACUUM` runs at the end, against `source`, and needs exclusive access.**
The deploy order this ships with (PR body) stops the service first, so
there is nobody else's lock to contend with; run against a live, serving
process this would stall behind every reader's connection and likely lose
the race against `store.BUSY_TIMEOUT_MS`.

**The DDL is the canonical one `vectors.py` owns, not a copy of `source`'s
own `sqlite_master.sql`.** `migrate.py`'s Orient cutover copied the source's
own schema text because it served an arbitrary, evolving source; this script
serves exactly one shape — the Library's own two vec0 tables — so there is
one definition of them and it lives in `vectors.py`.

**Both `LIBRARY_DB` and `LIBRARY_VECTORS_DB` have to be SET in the
environment this runs under, even though neither is read by name below.**
Importing `vectors` for its DDL constants imports `store`, and both modules
resolve their own database path at import — the same requirement every
process in this service starts under. Neither has to exist yet: point
`LIBRARY_VECTORS_DB` at `dest` — the value the environment should carry
going forward once this finishes, per the deploy order (PR body).

    LIBRARY_DB=<source> LIBRARY_VECTORS_DB=<dest> \\
        .venv/bin/python split_vectors.py <source> <dest>
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path
from urllib.parse import quote

VEC_TABLES = ("library_vectors", "signal_vectors")


class RefusedError(RuntimeError):
    """The source is the live, write-enabled database, or dest already exists."""


def live_path() -> Path | None:
    """The database this shell's own environment names as `LIBRARY_DB`, or None.

    **No longer gated on `LIBRARY_WRITES_ENABLED`** (security-reviewer,
    issue_01M35R5GA1SKRG2CMQRF189NDE round 1). The prior version returned
    `None` unless `LIBRARY_WRITES_ENABLED == "1"` was ALSO exported, so the
    deploy runbook's own documented invocation — `LIBRARY_DB` set to the real
    file, `LIBRARY_WRITES_ENABLED` not exported because nothing in a
    migration's shell has reason to export a service-runtime flag — resolved
    no live path at all, and `guard()` compared against nothing. `LIBRARY_DB`
    has no default (`store.resolve_db` has none by design), so a shell naming
    it is already saying something about which file this is; whether writes
    are enabled on it only changes how the refusal is worded (`_writes_note`,
    below) — a read-only shadow's file is still live for reads, and dropping
    its vec0 tables out from under it is not safe merely because writes are
    disabled. Unset entirely remains the ordinary, non-live case: a shell
    naming nothing is simply a shell with no opinion, the normal shape of
    running this against a scratch copy.
    """
    raw = os.environ.get("LIBRARY_DB")
    return Path(raw).expanduser() if raw else None


def _writes_note() -> str:
    """The parenthetical `guard()`'s refusal appends after naming `LIBRARY_DB`
    — wording only; the comparison itself runs the same whether or not writes
    are enabled (that unconditional run is the whole of the finding this
    function exists to word rather than to decide)."""
    if os.environ.get("LIBRARY_WRITES_ENABLED") == "1":
        return "write-enabled"
    return "writes not enabled in this shell, but still live for reads"


def _health_url() -> str:
    import mtls  # local: split_vectors runs before LIBRARY_DB need be set
    return f"http://{mtls.HOST}:{mtls.PLAIN_PORT}/health"


def _service_running() -> tuple[bool, str]:
    """Whether a Library process is answering `/health` on its own listener,
    and what the probe actually saw — `guard()` names it in the refusal.

    **An HTTP probe of the fixed port every listener shares, chosen over a
    lock file or a PID file because this has to work on Linux too**
    (company/decisions/0010): a PID file is whatever `launchd` or `systemd`
    happens to be tracking and can outlive a crashed process on either
    platform, where "does something answer this port" is the same question
    and the same answer on both. `mtls.HOST`/`mtls.PLAIN_PORT` name the one
    port the plain listener always binds (`serve_dual.py`), so no
    configuration has to be guessed.

    **Cannot say WHICH database the answering process serves** — `/health`
    reports no path, deliberately (its own docstring: a filesystem path in
    an unauthenticated response is exactly what that route was hardened
    against, convivy/library#32). This machine runs one Library instance, so
    "a Library is answering" and "it is serving the live `LIBRARY_DB`" are
    the same fact in practice, and `--i-mean-it` is the escape hatch for the
    rare case they are not.

    **Only a refused connection reads as "not running."** `urlopen` wraps a
    closed port in `urllib.error.URLError` whose `.reason` is the
    `ConnectionRefusedError` — that combination is the one outcome that
    actually proves nothing is listening. Everything else — a timeout, an
    HTTP error response (`HTTPError`, itself a `URLError`/`OSError`
    subclass), an HTTP success response, any other `OSError`, or a malformed
    URL raising something else entirely — means the probe failed to get a
    clean answer, not that it got a clean "no". The prior version caught
    bare `OSError`, which `TimeoutError` and `HTTPError` both are, and read
    every one of those as "no service" too (reviewer + security-reviewer,
    issue_01M35R5GA1SKRG2CMQRF189NDE round 2). That is exactly the failure
    mode this check exists to catch: the plist's own comments document an
    up-to-60s drain window (measured worst case 32.2s) in which the service
    is alive, still holding `LIBRARY_DB` open, and finishing an in-flight
    write — a slow, not absent, `/health`. An inconclusive probe now reads
    as "running" and `guard()` refuses, the same fail-closed default the
    live-database comparison above already uses.
    """
    import urllib.error
    import urllib.request
    url = _health_url()
    try:
        with urllib.request.urlopen(url, timeout=1.0) as resp:
            return True, f"it answered {resp.status} at {url}"
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ConnectionRefusedError):
            return False, f"nothing is listening at {url} (connection refused)"
        return True, f"probing {url} raised {exc!r}"
    except OSError as exc:
        return True, f"probing {url} raised {exc!r}"
    except Exception as exc:  # noqa: BLE001 — inconclusive, not a clean refusal
        return True, f"probing {url} raised {exc!r}"


def guard(source: Path, *, allow_live: bool = False) -> None:
    """Refuse `source` when it is what `LIBRARY_DB` names, or when a Library
    process is answering requests at all, unless the caller overrides.

    Three checks, and `--i-mean-it` is the one door past all of them: the
    resolved path catches a symlink or a `~` that expands to the live file; the
    device-and-inode pair catches a hard link or a bind mount the path
    comparison alone would wave through; the service probe catches an
    operator who skipped or raced "stop the service" — the one failure mode
    neither environment comparison can see, and the gap a prior version left
    wide open (security-reviewer, issue_01M35R5GA1SKRG2CMQRF189NDE round 1:
    the old `guard()` ran no comparison at all unless
    `LIBRARY_WRITES_ENABLED=1` was also exported, which the deploy runbook's
    own documented command never sets).

    **The service probe's own refusal is fail-closed as of round 2** — see
    `_service_running` for why an inconclusive probe (a timeout, an error
    response, a success response, any other `OSError`) now refuses same as a
    confirmed-running one, rather than reading as "no service" the way a
    bare `except OSError` used to.
    """
    if allow_live:
        return
    live = live_path()
    if live is not None:
        if source.resolve() == live.resolve():
            raise RefusedError(
                f"source {source} IS the live LIBRARY_DB ({live}, "
                f"{_writes_note()}). Pass --i-mean-it if this is genuinely "
                f"the deploy-time run against the live database with the "
                f"service stopped.")
        if (live.exists() and source.exists()
                and os.path.samestat(source.stat(), live.stat())):
            raise RefusedError(
                f"source {source} is the same file as the live LIBRARY_DB "
                f"({live}, {_writes_note()}) by device and inode.")
    running, detail = _service_running()
    if running:
        raise RefusedError(
            f"the probe could not confirm nothing is listening at "
            f"{_health_url()} — {detail}. Stop the service first. Pass "
            f"--i-mean-it if this is genuinely the deploy-time run with the "
            f"service intentionally left up.")


def _uri(path: Path, *, readonly: bool) -> str:
    """A SQLite URI for `path`, with the path percent-encoded — the same
    protection `store.db_uri` and `migrate._uri` give against a path
    containing `?`, which SQLite's URI parser would otherwise split on,
    silently dropping `mode=ro`."""
    encoded = quote(str(path), safe="/")
    return f"file:{encoded}?mode=ro" if readonly else f"file:{encoded}"


def connect(path: Path, *, readonly: bool) -> sqlite3.Connection:
    """A connection with `sqlite-vec` loaded, which the vec0 tables require."""
    import sqlite_vec
    conn = sqlite3.connect(_uri(path, readonly=readonly), uri=True)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    try:
        sqlite_vec.load(conn)
    finally:
        conn.enable_load_extension(False)
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def export_vectors(source: Path, dest: Path, *, allow_live: bool = False) -> dict:
    """Write `dest` holding a copy of every vec0 row in `source`. Returns row
    counts. `source` is not modified.

    Rows are read back through `source`'s own vec0 interface and inserted
    through `dest`'s, matching `migrate.py`'s reasoning for the Orient
    cutover: a vec0 virtual table's shadow tables are private layout, and
    copying them directly as ordinary tables opens cleanly, reports the
    right row count, and returns nonsense from a KNN.
    """
    import vectors

    source = source.resolve()
    guard(source, allow_live=allow_live)
    dest = dest.resolve()
    if dest.exists():
        raise FileExistsError(f"{dest} exists; refusing to overwrite")

    src = connect(source, readonly=True)
    try:
        present = [t for t in VEC_TABLES if t in _tables(src)]
        if not present:
            raise ValueError(
                f"{source} holds neither {' nor '.join(VEC_TABLES)} — "
                f"already split, or never had them")
        # **REFUSED, NOT EXPORTED PARTIAL, because the two tables have always
        # arrived and left together** — `init_vectors.py`'s `init()` creates
        # both in one call, and a completed `drop_and_vacuum` removes both in
        # one transaction (see there). The one way `source` holds just one is
        # a `drop_and_vacuum` that crashed between its two `DROP TABLE`
        # statements before this PR's fix, leaving the other still present
        # and unvacuumed. Exporting only what remains would silently move
        # half the vectors and report success — the natural "just rerun it"
        # recovery from that exact crash — rather than surfacing the source
        # as needing a human look (reviewer, issue_01M35R5GA1SKRG2CMQRF189NDE
        # round 1).
        if len(present) != len(VEC_TABLES):
            missing = [t for t in VEC_TABLES if t not in present]
            raise ValueError(
                f"{source} holds {', '.join(present)} but not "
                f"{', '.join(missing)} — a partial split, most likely a "
                f"drop_and_vacuum that crashed between its two DROP TABLEs; "
                f"refusing rather than silently exporting only what remains")

        dst = connect(dest, readonly=False)
        try:
            ddls = {vectors.VEC_TABLE: vectors.VEC_TABLE_DDL,
                    vectors.SIGNAL_VEC_TABLE: vectors.SIGNAL_VEC_TABLE_DDL}
            for t in present:
                dst.execute(ddls[t])
            dst.commit()

            counts = {}
            for t in present:
                # `rowid` IS AMONG THESE COLUMNS, and `verify` below keys its
                # comparison by it — `PRAGMA table_info` exposes `rowid` as a
                # real column on a vec0 table (`migrate.py` found this first),
                # so selecting and inserting it verbatim reproduces the
                # source's rowid gaps exactly rather than reassigning them.
                cols = [r[1] for r in src.execute(f"PRAGMA table_info({t})")]
                collist = ", ".join(cols)
                marks = ", ".join("?" * len(cols))
                rows = src.execute(f"SELECT {collist} FROM {t}").fetchall()
                dst.executemany(
                    f"INSERT INTO {t}({collist}) VALUES ({marks})",
                    [tuple(r[c] for c in cols) for r in rows])
                counts[t] = len(rows)
            dst.commit()
            return counts
        finally:
            dst.close()
    finally:
        src.close()


def verify(source: Path, dest: Path, *, probes: int = 25, k: int = 10,
           seed: int | None = None, allow_live: bool = False) -> list[str]:
    """Compare `dest` against `source`. Returns the problems found, empty on
    pass. Reused almost verbatim from `migrate.py`'s own `verify` — the same
    acceptance test applies to the same two vec0 tables, only the source has
    changed shape from "another service's whole control database" to "this
    service's own vault before the split".
    """
    import random

    source = source.resolve()
    dest = dest.resolve()
    guard(source, allow_live=allow_live)
    problems: list[str] = []
    src, dst = connect(source, readonly=True), connect(dest, readonly=True)
    try:
        present = [t for t in VEC_TABLES if t in _tables(src)]
        rng = random.Random(seed) if seed is not None else random.Random()
        for t in present:
            if t not in _tables(dst):
                problems.append(f"{t}: did not arrive in {dest} at all")
                continue
            n = src.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            m = dst.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            if n != m:
                problems.append(f"count mismatch {t}: source {n}, dest {m}")
                continue
            if not n:
                continue
            idcol = "item_id" if t == "library_vectors" else "signal_id"

            # Every vector, read back through the vec0 interface and compared
            # by bytes. No sampling — `migrate.py`'s own `verify` records why
            # a sampled check is worse than none: it fails a good migration
            # some fraction of the time, and a cutover that has seen that
            # before learns to wave the failure through.
            #
            # Keyed by `rowid`, not `idcol`: `idcol` is not unique — an item
            # chunked into several vectors shares one across all of them —
            # and a dict keyed on it would collapse every chunk onto the
            # last, hiding damage to any earlier one (`migrate.py`, PR #22).
            def read_vectors(conn, table=t, col=idcol):
                return {r["rowid"]: (r[col], bytes(r["embedding"]))
                        for r in conn.execute(
                            f"SELECT rowid, {col}, embedding FROM {table}")}
            a, b = read_vectors(src), read_vectors(dst)
            if a.keys() != b.keys():
                problems.append(
                    f"{t}: {len(a.keys() - b.keys())} rows did not arrive")
            differing = sum(1 for key in a.keys() & b.keys() if a[key] != b[key])
            if differing:
                problems.append(
                    f"{t}: {differing} vectors differ from the source — the "
                    f"index did not survive the move, whatever the counts say")

            # A KNN spot check on DISTANCES only — which of a set of
            # equally-distant rows comes back is arbitrary and differs
            # between two correct databases (`migrate.py`'s own `verify`
            # measured this: 3 probes in 400 against a migration that was
            # perfect, all from ties in `signal_vectors`'s many identical
            # embeddings).
            for _ in range(probes):
                probe = src.execute(
                    f"SELECT embedding FROM {t} LIMIT 1 OFFSET ?",
                    (rng.randrange(n),)).fetchone()[0]
                q = (f"SELECT distance FROM {t} "
                     f"WHERE embedding MATCH ? AND k = ? ORDER BY distance")
                want = [r["distance"] for r in src.execute(q, (probe, k))]
                got = [r["distance"] for r in dst.execute(q, (probe, k))]
                if want != got:
                    problems.append(
                        f"{t}: nearest-neighbour DISTANCES differ, so the "
                        f"index itself is wrong rather than merely ordered "
                        f"differently")
                    break
    finally:
        src.close()
        dst.close()
    return problems


def drop_and_vacuum(source: Path, *, allow_live: bool = False) -> tuple[int, int]:
    """Remove the vec0 tables from `source` and reclaim the space. Returns
    `(size_before, size_after)` in bytes, both of the main database file —
    the "hot database's size, recorded before and after" clause 7 asks for.

    **Only called after `verify` has passed.** Nothing here re-checks that;
    `main`'s own call order is the enforcement.

    `DROP TABLE` on a vec0 virtual table drops its shadow tables with it —
    they are private layout the virtual table owns, not separate tables a
    caller must remember to clean up (`migrate.py`'s `unaccounted` excludes
    them from its own seam check for the same reason).

    **Both `DROP TABLE` statements run inside ONE explicit transaction.**
    Python's `sqlite3` does not open an implicit transaction ahead of DDL, so
    under its default isolation level each `DROP TABLE` commits itself the
    instant it runs — confirmed empirically, a second connection sees the
    first table gone before `conn.commit()` below ever executes — and a crash
    between the two dropped one vec0 table while leaving the other present
    and unvacuumed, with `conn.commit()` a no-op over statements already
    committed. `isolation_level = None` puts this connection in autocommit
    mode so `BEGIN`/`COMMIT` are issued by hand, and the SQLite engine itself
    DOES include DDL in an explicit transaction — the autocommit-on-DDL
    behaviour above is pysqlite's own default transaction handling, not a
    limit of SQLite, so wrapping the drops in `BEGIN`/`COMMIT` here closes the
    gap the same way `init_vectors.py`'s `init()` already does for its own
    two `CREATE VIRTUAL TABLE` statements (reviewer,
    issue_01M35R5GA1SKRG2CMQRF189NDE round 1).
    """
    source = source.resolve()
    guard(source, allow_live=allow_live)
    size_before = source.stat().st_size

    conn = connect(source, readonly=False)
    conn.isolation_level = None
    try:
        present = [t for t in VEC_TABLES if t in _tables(conn)]
        conn.execute("BEGIN")
        for t in present:
            conn.execute(f"DROP TABLE {t}")
        conn.execute("COMMIT")
        conn.execute("VACUUM")
    finally:
        conn.close()

    size_after = source.stat().st_size
    return size_before, size_after


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("source", type=Path,
                   help="the vault database to remove the vec0 tables from")
    p.add_argument("dest", type=Path,
                   help="the vectors database to create")
    p.add_argument("--i-mean-it", action="store_true",
                   help="permit the live, write-enabled LIBRARY_DB as the source")
    args = p.parse_args(argv)
    try:
        moved = export_vectors(args.source, args.dest, allow_live=args.i_mean_it)
    except (RefusedError, ValueError, FileExistsError) as e:
        print(f"split_vectors: {e}", file=sys.stderr)
        return 2
    for t, n in moved.items():
        print(f"  exported {t:16} {n}")

    problems = verify(args.source, args.dest, allow_live=args.i_mean_it)
    for msg in problems:
        print(f"  PROBLEM {msg}", file=sys.stderr)
    if problems:
        print("split_vectors:", f"FAIL ({len(problems)}) — {args.source} left untouched")
        return 1

    before, after = drop_and_vacuum(args.source, allow_live=args.i_mean_it)
    vectors_size = args.dest.resolve().stat().st_size
    print(f"  {args.source} before: {before} bytes")
    print(f"  {args.source} after:  {after} bytes")
    print(f"  {args.dest} (vectors): {vectors_size} bytes")
    print("split_vectors: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
