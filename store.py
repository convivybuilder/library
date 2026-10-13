"""Read-only access to the vault database, during the shadow period.

**THE SHADOW IS ENFORCED BY THE CONNECTION, NOT BY A PROMISE.** Phase 3's risk
plan calls for a period in which the Library serves reads while Orient still owns
writes, and the cheapest possible guarantee of that is a SQLite connection opened
`mode=ro`: a write attempt raises rather than being caught by review. The live
database is ~220 MB behind running pages and a decision plane the operator uses, so "we
only wrote reads" is not a claim worth resting on code review.

**Raw SQL with explicit column lists, deliberately, rather than a copy of Orient's
models.** During the shadow the rows belong to Orient, and an ORM model here would
be a second definition of a schema this service does not own — free to drift, with
nothing to catch it. Naming columns explicitly also puts the protocol's response
shape where a reader can see it, which is the thing the contract actually governs.

When the vaults move behind the protocol (Phase 3 §4) the Library owns the schema
and this becomes a real model layer. Not yet.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import itertools
import json
import math
import os
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote

# WRITES ARE OFF UNLESS SAID OTHERWISE, and this is the same reasoning that makes
# `connect()` open `mode=ro`: the shadow is enforced by the connection rather than
# by a promise. §4 requires that Orient still owns writes for the whole read-only
# shadow, so a Library that could write to the vault during it would be the second
# writer ADR-0020 exists to prevent — and the failure would be silent, since both
# processes would succeed.
#
# The write surface is built before the migration because the migration cannot
# land without it, so this flag is what keeps "the routes exist" from meaning
# "the routes are live". It flips at the write cutover, once the vault is the
# Library's own file and Orient has stopped writing to it.
WRITES_ENABLED = os.environ.get("LIBRARY_WRITES_ENABLED") == "1"

# **UNSET APPROVES EVERYTHING, and that is the point of the switch rather than
# a gap in it** (`issue_01M0EQTMZMAB93V9FFP4AD52JK`). `POST /merge/admission`
# answers the same shape either way — `{approved, reason}` — so flipping this
# is a Library-only deploy change the Bosun never has to coordinate with: a
# stub that always approves is indistinguishable from a real gate to anyone
# downstream, which is what lets it turn real on its own schedule instead of
# on the day the Bosun ships its half.
#
# Set, real evaluation would run — and none is built yet, so that branch
# answers an honest refusal (`approved: false`, naming that enforcement is
# configured but no evaluator exists) rather than keep approving under a flag
# that claims to be a gate.
#
# **DO NOT MAKE THIS DEFAULT TO ENFORCED.** The epic's thread is explicit that
# the default flips to enforced-by-default in the SAME change that builds the
# real evaluator, not as a follow-up — flip it here today and every merge
# refuses with "no evaluator is built" the moment this deploys, for a gate
# nothing yet implements.
MERGE_ADMISSION_ENFORCED = os.environ.get(
    "LIBRARY_MERGE_ADMISSION_ENFORCED") == "1"

# Where Orient's control database lives when nothing says otherwise. A reader may
# fall back to it — during the shadow this service reads Orient's file on purpose
# — and `migrate.py` deliberately may not, because a guard that defaults is
# comparing against a path nobody confirmed.
ORIENT_DB_DEFAULT = Path.home() / ".orient-control" / "control.db"

# The tables that stay with Orient at the cutover, and therefore the fingerprint
# of Orient's control database. Any one of them present in a file means that file
# is Orient's, whatever its path claims.
#
# **THIS IS THE ONLY CONCLUSIVE HALF OF THE GUARD BELOW, and the path comparison
# is the half that cannot be trusted alone.** After the cutover the Library's
# plist sets `LIBRARY_DB` and does not set `ORIENT_CONTROL_DB`, so a guard
# comparing the two variables has nothing to compare against in exactly the
# configuration it exists to protect. Identity by content holds in that
# configuration, and through a copy, a bind mount, or a path nobody thought to
# list.
ORIENT_ONLY_TABLES = ("reading_rooms", "room_entries", "room_requests",
                      "push_subscriptions")

# The question above, as one statement, because it is asked through two different
# drivers. `connect_rw` asks it of a `sqlite3` handle before a write; the alembic
# tree asks it of a SQLAlchemy connection before a migration, since that
# connection is opened by SQLAlchemy and is not the one `resolve_db` checked. Two
# spellings of one fingerprint could drift; one string cannot.
ORIENT_FINGERPRINT_SQL = (
    "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
    f"({','.join('?' * len(ORIENT_ONLY_TABLES))}) LIMIT 1")


class MisconfiguredDatabase(RuntimeError):
    """This process cannot establish which database it owns.

    **Raised at import for a configuration that was wrong when the process
    started**, which is what makes that case a refusal to start rather than a
    refusal to serve. A service that came up and then answered every request with
    a 500 would be a slower way of saying the same thing, after a supervisor had
    already reported it healthy.

    **And raised from `connect_rw` for a path that stopped being the Library's
    while the process ran**, which import cannot see. That one reaches a caller,
    so `app.misconfigured_database` turns it into a 503 naming the cause rather
    than the generic 500 an uncaught exception would produce.
    """


def db_uri(path, *, readonly: bool) -> str:
    """A SQLite URI for `path`, with the path percent-encoded.

    **INTERPOLATING A RAW PATH DEFEATS THE READ-ONLY GUARANTEE, and it is not a
    hardening nicety** (security-reviewer, convivy/library#22, where the same bug
    was found in `migrate.py`). SQLite's URI parser splits on the FIRST `?` in the
    whole string, so a path containing `?` or `#` has everything after it
    swallowed as query or dropped as fragment — directory components included, and
    `mode=ro` along with them. `file:/tmp/weird?dir/test.db?mode=ro` opens
    `/tmp/weird`, CREATES it, and accepts a `CREATE TABLE` through the handle the
    caller believes is read-only.

    That makes the file this module resolved and the file actually opened two
    different things, which is the divergence everything above exists to prevent.

    **Duplicated from `migrate.py` rather than shared, deliberately.** `migrate.py`
    cannot import this module, because importing it resolves the service's
    database and can refuse; a migration tool must run whatever the service is
    configured to do. This module cannot import `migrate.py` either, which is a
    command-line program. Six lines in two places beats a coupling in either
    direction.
    """
    encoded = quote(str(path), safe="/")
    return f"file:{encoded}?mode=ro" if readonly else f"file:{encoded}"


def _orients_table_in(path: Path) -> str | None:
    """The name of an Orient-only table found in `path`, or None.

    A file that does not exist yet is not Orient's — the migration writes the
    Library's file during the cutover window, and a boot before that point has a
    path with nothing at the end of it. `connect()` is where a missing file
    becomes an error, and it says so in those terms.

    **Anything else that goes wrong is a refusal**, matching the doctrine
    `migrate.guard` and `serve.sh` already state: an inconclusive read of a safety
    check is not a pass. A file at `LIBRARY_DB` that cannot be opened or whose
    schema cannot be listed is a file this cannot vouch for.
    """
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(db_uri(path, readonly=True), uri=True)
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise MisconfiguredDatabase(
            f"LIBRARY_DB points at {path}, which could not be read as a SQLite "
            f"database ({exc}). Refusing to start rather than serve from a file "
            f"this cannot identify.") from exc
    present = {name for (name,) in rows}
    for table in ORIENT_ONLY_TABLES:
        if table in present:
            return table
    return None


def _refuse_orients_path(own: Path, env=None) -> None:
    """Refuse a path that resolves to Orient's control database.

    **This is the single most dangerous line in the cutover** (the cutover
    sequence, P4). `LIBRARY_WRITES_ENABLED=1` against Orient's file makes this
    process a second writer to a 234 MB live database — the precise failure
    ADR-0020 exists to prevent, with both processes reporting success and nothing
    in either log saying otherwise.

    Two checks, because they catch different mistakes: the resolved path catches a
    symlink or a `~` that expands to the same file, and the device-and-inode pair
    catches a hard link or a bind mount the path comparison waves through.
    `samestat` rather than `st_ino` alone — inode numbers are unique within a
    filesystem, not across them, so comparing the number by itself can refuse two
    genuinely different files on two volumes (security-reviewer, convivy/library#32).
    That direction is a false refusal rather than a bypass, which is why it is a
    correctness fix and not a hole.

    **This is the only check that can fire without opening the file**, which is
    what it is for: the direct `LIBRARY_DB=<Orient's path>` case is caught here,
    at import, before anything has to read a schema. It is not what guards a
    running process — `connect_rw` asks `_refuse_orients_handle` instead, because
    a check over a path has a window between its last stat and the open.
    """
    env = os.environ if env is None else env
    candidates = [ORIENT_DB_DEFAULT]
    named = env.get("ORIENT_CONTROL_DB")
    if named:
        candidates.append(Path(named).expanduser())
    resolved = own.resolve()
    for live in candidates:
        if resolved == live.resolve():
            raise MisconfiguredDatabase(
                f"{own} IS Orient's control database ({live}). The Library owns "
                f"its own file; point LIBRARY_DB at it, and leave LIBRARY_DB "
                f"unset to read Orient's during the shadow.")
        if (own.exists() and live.exists()
                and os.path.samestat(own.stat(), live.stat())):
            raise MisconfiguredDatabase(
                f"{own} is the same file as Orient's control database ({live}) "
                f"by device and inode. The Library owns its own file; point "
                f"LIBRARY_DB at it.")


def _refuse_orients_handle(conn: sqlite3.Connection) -> None:
    """Refuse a connection that turns out to be open on Orient's database.

    **Asked of the handle, so there is no window to race.** The path checks above
    describe a file at the moment they run, and a name repointed between the last
    `stat` and the `connect` leaves them vouching for a file that is not the one
    opened. This asks the open connection what it holds; it is already bound to an
    inode, and renaming the path afterwards cannot move it.

    The connection is closed before raising, because the caller never receives it
    and a handle nobody holds is a lock nobody releases.
    """
    try:
        rows = conn.execute(ORIENT_FINGERPRINT_SQL, ORIENT_ONLY_TABLES).fetchone()
    except sqlite3.Error as exc:
        conn.close()
        # A lock held past the busy timeout says nothing about which file this
        # is, so it goes to `app.database_busy` as contention rather than being
        # refused here as a database the Library cannot identify (reviewer,
        # convivy/library#112).
        if (isinstance(exc, sqlite3.OperationalError)
                and exc.sqlite_errorcode & 0xFF == sqlite3.SQLITE_BUSY):
            raise
        raise MisconfiguredDatabase(
            f"the database at {DB_PATH} could not be identified before a "
            f"write ({exc}). Refusing the connection rather than writing to a "
            f"file this cannot vouch for.") from exc
    if rows is not None:
        conn.close()
        raise MisconfiguredDatabase(
            f"the database open at {DB_PATH} holds {rows[0]!r} — a table that "
            f"stays with Orient, so this handle is on Orient's control database. "
            f"Refusing the write: a second writer to that file is the failure "
            f"ADR-0020 exists to prevent, and both processes would report "
            f"success.")


def _refuse_orients_database(own: Path, env) -> None:
    """The path checks above, plus the conclusive one: what the file contains.

    The schema check catches everything the path comparison cannot, including a
    copy of Orient's database at a path nobody would have listed, and it is the
    only half that works in the post-cutover configuration where
    `ORIENT_CONTROL_DB` is unset.

    **It is a blocklist, and a blocklist has a known blind spot worth stating.** A
    dump of Orient's database with those four tables dropped reads as "not
    Orient's" here while still holding every vault row. A positive fingerprint is
    not available instead: the Library's own file and Orient's share all twelve
    moving tables, which is the point of the migration. What this catches is the
    mistake — a path typed wrong, a backup named as the destination — rather than
    a determined attempt to defeat it, and the file that matters most, the one
    Orient is actively writing, is caught by all three checks at once.

    Run at import only, where opening the file to read its schema costs nothing a
    caller is waiting on. A running process asks the same question of an already
    open handle, in `_refuse_orients_handle`.
    """
    _refuse_orients_path(own, env)
    found = _orients_table_in(own)
    if found is not None:
        raise MisconfiguredDatabase(
            f"LIBRARY_DB points at {own}, which holds {found!r} — a table that "
            f"stays with Orient, so this file is Orient's control database or a "
            f"copy of it. The Library owns its own file; point LIBRARY_DB at it.")


def resolve_db(env=None) -> tuple[Path, str]:
    """The database this process serves, and where that path came from.

    **`LIBRARY_DB` HAS NO DEFAULT, which is the whole point.** The variable that
    used to name this file was Orient's, so an unset variable resolved to Orient's
    database — right for a reader in the shadow, and the second-writer failure the
    moment writes are enabled. With no default there is no value an omission can
    fall back to.

    Three cases, and the order matters:

    - **`LIBRARY_DB` is set** — this process serves its own file, in the shadow or
      after it. Set-but-ignored is not one of the outcomes: a variable that named
      a file the service then did not read would be a wrong answer rather than an
      error, which is the class of failure this function exists to remove.
    - **`LIBRARY_DB` unset with writes enabled** — refuse. There is no file this
      process can honestly claim to own, and the fallback would be Orient's.
    - **`LIBRARY_DB` unset with writes disabled and `ORIENT_CONTROL_DB` set** —
      the read-only shadow, reading Orient's file at the path exported.

    **An unset `ORIENT_CONTROL_DB` in that third case refuses rather than
    guessing.** This used to fall back to Orient's well-known default path
    silently, which is exactly the trap that misled a black-box check run from a
    shell with neither variable exported: it read a real database and reported a
    clean result, having never touched the live file at all. A caller who means
    to read Orient's copy says so; one that means to read the Library's sets
    `LIBRARY_DB`. Neither set is not a request to guess between them.

    The second return value is the source, for the boot line the cutover's step 6
    tells an operator to read before continuing.
    """
    env = os.environ if env is None else env
    raw = env.get("LIBRARY_DB")
    if raw:
        own = Path(raw).expanduser()
        _refuse_orients_database(own, env)
        return own, "LIBRARY_DB"
    if env.get("LIBRARY_WRITES_ENABLED") == "1":
        raise MisconfiguredDatabase(
            "LIBRARY_WRITES_ENABLED=1 with LIBRARY_DB unset. Writes are on and "
            "this process has no file of its own to write to; falling back to "
            "ORIENT_CONTROL_DB would make it a second writer to Orient's live "
            "database. Set LIBRARY_DB to the Library's file, or unset "
            "LIBRARY_WRITES_ENABLED to serve the read-only shadow.")
    named = env.get("ORIENT_CONTROL_DB")
    if named:
        return Path(named).expanduser(), "ORIENT_CONTROL_DB"
    raise MisconfiguredDatabase(
        "neither LIBRARY_DB nor ORIENT_CONTROL_DB is set. This process cannot "
        "honestly claim to serve the Library's file or Orient's; guessing "
        f"Orient's default path ({ORIENT_DB_DEFAULT}) is what let a stale, "
        "diverged copy pass a verification that never opened the live "
        "database. Set LIBRARY_DB to read the Library's own file, or "
        "ORIENT_CONTROL_DB to read Orient's explicitly.")


DB_PATH, DB_SOURCE = resolve_db()


# A caller-registered SQL function, applied to every connection this module
# opens from here on (library#185 round 3). Exists so a SQL-side comparison
# can call the exact Python function a read gate already runs on the same
# row, instead of a second, hand-written approximation of that function's
# fold — two independent spellings of one test are the shape that keeps
# drifting apart (round 2's `TRIM`/`LOWER` SQL disagreeing with `_is_handoff`
# and `_private_handoff` on a tab, a non-breaking space, or a padded
# product). One registry, applied in one place, closes that off by
# construction: there is only one definition left to call.
_CONNECTION_FUNCTIONS: list[tuple[str, int, object, bool]] = []


def register_connection_function(name: str, narg: int, func, *,
                                 deterministic: bool = False) -> None:
    """Make `func` callable as `name(...)` in SQL on every connection
    `connect()`/`connect_rw()` open from here on — SQLite functions are
    per-connection, not per-schema, so registering once on the handle either
    of them returns covers `main` and whatever `attach_cold` ATTACHes onto
    that same handle, with no second registration needed for the cold
    schema. `vectors.connect()` opens its own handle rather than going
    through `connect()` (it also loads `vec0` and attaches the vectors
    database), so it calls `_apply_connection_functions` itself, below, on
    the same registry.

    Pass `deterministic=True` only for a function of its arguments alone.
    SQLite may reuse a deterministic function's result for a constant
    argument, so a function that reads mutable state (the projects registry,
    which `projects.reload()` replaces) must stay non-deterministic or it can
    answer from before the reload (library#185 round 3)."""
    _CONNECTION_FUNCTIONS.append((name, narg, func, deterministic))


def _apply_connection_functions(conn: sqlite3.Connection) -> None:
    for name, narg, func, deterministic in _CONNECTION_FUNCTIONS:
        conn.create_function(name, narg, func, deterministic=deterministic)


def connect() -> sqlite3.Connection:
    """A read-only connection. Raises if the file is missing — a Library serving
    an empty result set because it could not find the database would be reporting
    "nothing matches" on the strength of never having looked, which is the one
    answer this service must never give by accident.

    **`mode=ro` reads a WAL database correctly in every state, confirmed
    empirically rather than assumed** (issue_01M2KW35JX19GVG9Z13R10HKNZ,
    `tests/test_wal_journal_mode.py`): with a writer connected, with no writer
    connected and `-wal`/`-shm` present, and with no writer connected and
    those files absent. `mode=ro` restricts writes to the main database file
    only — SQLite still creates or opens `-shm`/`-wal` beside a read-only
    handle, which every deployment of this service can do, since the reader
    and the writer both run as the same user in the same directory. There is
    no state here that needed a code change."""
    if not DB_PATH.is_file():
        raise FileNotFoundError(f"no vault database at {DB_PATH}")
    conn = sqlite3.connect(db_uri(DB_PATH, readonly=True), uri=True)
    conn.row_factory = sqlite3.Row
    _apply_connection_functions(conn)
    return conn


class WritesDisabled(RuntimeError):
    """Raised instead of opening a writable connection during the shadow."""


def connect_rw() -> sqlite3.Connection:
    """A writable connection, refused while the shadow is running.

    Refusing here rather than at the route means a future route that forgets the
    check still cannot write: there is one way to obtain a writable handle and it
    is this one. A caller that gets past it holds a real write lock on the vault,
    so the refusal is the control rather than a warning.

    **THE DATABASE IS RE-IDENTIFIED HERE, because the import-time guard is a
    statement about one instant** (security-reviewer, convivy/library#32).
    `resolve_db` resolves `LIBRARY_DB` once and this process reuses that path for
    its whole lifetime, so a symlink repointed at Orient's database after boot
    reaches this function with none of the checks re-run, and produces the
    second-writer failure with no refusal anywhere.

    **The check runs through the OPEN HANDLE rather than over the path, and that
    is what makes it a check rather than a narrower race.** Re-running the path
    and inode comparison before the open would validate a file that a swap could
    replace between the last `stat` and the `connect` — shrinking an unbounded
    window to a few microseconds, not closing it, which is more than the first
    version of this admitted (security-reviewer, round 2). Asking the connection
    itself what it holds has no window at all: the handle is already bound to an
    inode, and a name repointed afterwards cannot change what it is bound to.

    One `sqlite_master` lookup on an open connection, against a write that is
    about to take a real lock on a 240 MB file.
    """
    if not WRITES_ENABLED:
        raise WritesDisabled(
            "the Library is in its read-only shadow: Orient still owns writes "
            "until the vault migration and the write cutover. Set "
            "LIBRARY_WRITES_ENABLED=1 only once the vault is this service's own "
            "file and Orient has stopped writing to it.")
    if not DB_PATH.is_file():
        raise FileNotFoundError(f"no vault database at {DB_PATH}")
    # The busy timeout is set at the open rather than by a pragma afterwards, so
    # `_refuse_orients_handle`'s read waits on the same timeout as every later
    # statement (reviewer, convivy/library#112). The lock is held for the
    # statement rather than the request. Orient learned this the expensive way:
    # awaiting a model call inside an open write transaction queued every other
    # writer behind it, and eight concurrent posts returned in multiples of
    # `busy_timeout` with six of them 500s.
    conn = sqlite3.connect(DB_PATH, timeout=BUSY_TIMEOUT_MS / 1000)
    _refuse_orients_handle(conn)
    # Set AFTER the identity check, not before: a junk file that is not a
    # SQLite database at all raises `sqlite3.DatabaseError` on the very first
    # statement run against it, and `_refuse_orients_handle` is what turns
    # that into the stated `MisconfiguredDatabase` refusal. A `PRAGMA
    # journal_mode` issued first would hit that same raw error before the
    # identity check ever ran, on a file this service cannot vouch for.
    #
    # **ONLY SWITCH A DATABASE THAT IS NOT ALREADY IN WAL, AND NEVER LET A
    # CONTESTED SWITCH FAIL THE WRITE.** Switching into WAL needs exclusive
    # access. If another connection holds even a read lock, the switch waits
    # out this connection's whole busy timeout and then raises `database is
    # locked`. That was measured on 2026-09-16: 2.17 s against a 2 s timeout,
    # then OperationalError. An earlier version of this comment said a
    # contested switch "raises nothing", and it was wrong (orchestrator
    # review, issue_01M2KW35JX19GVG9Z13R10HKNZ). Run on every write open of a
    # busy database still in rollback mode, that would have made every write
    # stall and then fail.
    #
    # So: read the mode first. Reading it takes no exclusive lock, and on a WAL
    # database it answers at once. If the mode is already WAL, which the live
    # store will be because it is switched with the service stopped before this
    # code is deployed, do nothing. Otherwise try the switch, and if it is
    # contested, keep the connection in whatever mode the file has. The next
    # uncontested write open will switch it. A fresh database, such as a test's
    # `tmp_path` or a file `migrate.py` has just created, has nobody to contend
    # with, so it comes up in WAL on its first write open.
    switch_to_wal(conn)
    conn.row_factory = sqlite3.Row
    _apply_connection_functions(conn)
    return conn


def switch_to_wal(conn: sqlite3.Connection, schema: str = "main") -> None:
    """Put one schema on `conn` into WAL, best-effort, and set `synchronous`
    on it. Factored out of `connect_rw` so `vectors._attach_vectors_db` can
    apply the same switch to the ATTACHed vectors database — `PRAGMA
    journal_mode` with no schema name touches `main` only, so the attached
    schema needs its own call or it silently stays in whatever mode the file
    was created in.

    See `connect_rw` for why the switch is read-before-write and why a
    contested one is swallowed rather than failing the caller's write.
    """
    prefix = "" if schema == "main" else f"{schema}."
    if (conn.execute(f"PRAGMA {prefix}journal_mode").fetchone()[0].lower()
            != WAL_JOURNAL_MODE.lower()):
        try:
            conn.execute(f"PRAGMA {prefix}journal_mode={WAL_JOURNAL_MODE}")
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) and "busy" not in str(e):
                raise
    conn.execute(f"PRAGMA {prefix}synchronous={SYNCHRONOUS_MODE}")


# ---------------------------------------------------------------------------
# The cold tier: a second database file holding signals past their kind's
# hot window (library/decisions/0001-what-the-archives-keep-embed-and-move-
# cold, memory-retention epic clause 9). `cold_tier.py` is the mover that
# relocates rows there; this module is where every READ path attaches it,
# because every one of them already opens its connection through `connect`
# or `connect_rw` here.
# ---------------------------------------------------------------------------

COLD_SCHEMA = "cold"


class MisconfiguredColdDB(RuntimeError):
    """`LIBRARY_COLD_DB` is unset, so this process cannot say where the cold
    tier lives.

    **Mirrors `vectors.MisconfiguredVectorsDB`**: the cold file's location is
    configuration, not a constant. Raised from `attach_cold`, the first
    place anything in this module actually needs the path (see
    `resolve_cold_db_path`'s own docstring for why that is lazy rather than
    import-time here) — and from `cold_tier.py` / `init_cold.py` at THEIR
    import, where failing fast is exactly right.
    """


def resolve_cold_db_path(env=None) -> Path:
    """The file signals move to once they pass their kind's hot window.

    **No default**, for the reason `resolve_db` and `vectors.
    resolve_vectors_db_path` have none: a fallback path is indistinguishable
    from the real file once the two happen to share a name.

    **Existence is NOT checked here**, matching `resolve_vectors_db_path`:
    `init_cold.py` is the one caller allowed to create this file, and a
    fresh install has nothing at this path until it runs. `attach_cold`
    below checks existence at attach time instead.

    **NOT RESOLVED AT MODULE IMPORT, unlike `DB_PATH` and `vectors.
    VECTORS_DB_PATH` — called fresh inside `attach_cold`, every time.** This
    module is imported by every script and route in the service, the vast
    majority of which never touch a signal row that could be cold; forcing
    `LIBRARY_COLD_DB` on all of them the way `DB_PATH` forces `LIBRARY_DB`
    would widen this change's blast radius to code with no stake in it. The
    cost is one `environ.get` per attach rather than one per process — cheap
    against the write lock or disk read `attach_cold`'s own caller is about
    to take. `cold_tier.py` and `init_cold.py`, the two places that exist
    only to work on this file, resolve it eagerly at their own import
    instead, matching `vectors.py` / `init_vectors.py`'s shape, because for
    those two tools failing fast at startup is exactly right.
    """
    env = os.environ if env is None else env
    raw = env.get("LIBRARY_COLD_DB")
    if not raw:
        raise MisconfiguredColdDB(
            "LIBRARY_COLD_DB is not set. The cold tier's location is "
            "configuration, not a constant, and there is no default this "
            "can fall back to. Set LIBRARY_COLD_DB to the cold tier's own "
            "database file — init_cold.py creates it on a fresh install.")
    return Path(raw).expanduser()


# The same fingerprint query `vectors._ORIENT_FINGERPRINT_SQL_VECDB` runs for
# the attached vectors schema, qualified to `cold` instead — a bare
# `sqlite_master` resolves against `main` only, so an attached schema needs
# its own qualified form.
_ORIENT_FINGERPRINT_SQL_COLD = (
    f"SELECT name FROM {COLD_SCHEMA}.sqlite_master WHERE type='table' AND "
    f"name IN ({','.join('?' * len(ORIENT_ONLY_TABLES))}) LIMIT 1")


def cold_attached(conn: sqlite3.Connection) -> bool:
    """Whether `conn` already has the cold tier attached, so `attach_cold`
    can be called more than once on the same connection — several read paths
    attach lazily, only once a hot miss is found, and a caller looping over
    several ids must not re-`ATTACH` on the second miss."""
    return any(row[1] == COLD_SCHEMA
               for row in conn.execute("PRAGMA database_list"))


def attach_cold(conn: sqlite3.Connection, *, readonly: bool) -> None:
    """ATTACH the cold tier onto `conn`, as schema `cold`. Idempotent: a
    `conn` that already has it attached is left alone.

    **Mirrors `vectors._attach_vectors_db` exactly** — raises
    `FileNotFoundError` rather than attaching a missing file SQLite would
    otherwise create on the spot; refused when `LIBRARY_COLD_DB` names
    Orient's control database or a copy of it, through the path check
    first and then the schema fingerprint asked of this same handle, never
    a throwaway probe connection a symlink swap could race; `readonly`
    picks a `mode=ro` URI for a connection opened with SQLite's URI flag
    (`connect`, below), or a plain path for `connect_rw`'s handle, which is
    not URI-opened and has no `mode=ro` to ask for regardless.

    A busy attached schema is contention, not a misconfigured database,
    the same distinction `_refuse_orients_handle` draws for `LIBRARY_DB`:
    `SQLITE_BUSY` reading the fingerprint passes through uncaught.
    """
    if cold_attached(conn):
        return
    cold_db_path = resolve_cold_db_path()
    if not cold_db_path.is_file():
        raise FileNotFoundError(f"no cold-tier database at {cold_db_path}")
    _refuse_orients_path(cold_db_path, os.environ)
    if readonly:
        conn.execute(f"ATTACH DATABASE ? AS {COLD_SCHEMA}",
                     (db_uri(cold_db_path, readonly=True),))
    else:
        conn.execute(f"ATTACH DATABASE ? AS {COLD_SCHEMA}", (str(cold_db_path),))
    try:
        found = conn.execute(_ORIENT_FINGERPRINT_SQL_COLD,
                             ORIENT_ONLY_TABLES).fetchone()
    except sqlite3.Error as exc:
        conn.execute(f"DETACH DATABASE {COLD_SCHEMA}")
        if (isinstance(exc, sqlite3.OperationalError)
                and exc.sqlite_errorcode & 0xFF == sqlite3.SQLITE_BUSY):
            raise
        raise MisconfiguredDatabase(
            f"the cold-tier database at {cold_db_path} could not be "
            f"identified after attaching it ({exc}). Refusing the "
            f"connection rather than using a file this cannot vouch "
            f"for.") from exc
    if found is not None:
        conn.execute(f"DETACH DATABASE {COLD_SCHEMA}")
        raise MisconfiguredDatabase(
            f"{cold_db_path} holds {found[0]!r} — a table that stays with "
            f"Orient, so this file is Orient's control database or a copy "
            f"of it. The cold tier owns its own file; point LIBRARY_COLD_DB "
            f"at it.")
    if not readonly:
        switch_to_wal(conn, schema=COLD_SCHEMA)


def attach_cold_or_reason(conn: sqlite3.Connection, *, readonly: bool) -> str | None:
    """`attach_cold`, translating a failure into `cold_unavailable_reason`'s
    fixed wording — a route's one-line way to attach and bail with a 503 on
    anything but contention. Returns None on success.

    **`SQLITE_BUSY` passes through UNCAUGHT**, the same distinction
    `_refuse_orients_handle` draws for `LIBRARY_DB`: contention says
    nothing about which file this is, so it is not this function's to
    answer — a route that wants `app.database_busy`'s own handling lets it
    propagate rather than reporting a database this cannot identify.
    """
    try:
        attach_cold(conn, readonly=readonly)
        return None
    except sqlite3.OperationalError as exc:
        if exc.sqlite_errorcode & 0xFF == sqlite3.SQLITE_BUSY:
            raise
        return cold_unavailable_reason(exc)
    except Exception as exc:  # noqa: BLE001 — any other failure means "unavailable"
        return cold_unavailable_reason(exc)


def cold_unavailable_reason(exc: BaseException) -> str:
    """Translate an `attach_cold` failure into the fixed, bounded wording a
    caller may see — the full exception, path included, goes to the service
    log only, mirroring `vectors._open_or_reason`'s own split for the same
    reason (security-reviewer, issue_01M35R5GA1SKRG2CMQRF189NDE round 2)."""
    print(f"library: cold tier unavailable — {type(exc).__name__}: {exc}",
          file=sys.stderr)
    if isinstance(exc, FileNotFoundError):
        return "the cold tier's database is missing or could not be opened"
    return "the cold tier could not be opened"


class ColdTierUnavailable(RuntimeError):
    """`_cold_dedup_existing` could not confirm whether a `cold_dedup_keys`
    hit's row still exists, because `cold_signal_by_id` could not attach the
    cold tier to look.

    **The one vocabulary `insert_signal`'s callers are built against**, so
    `POST /signals` and `_record_review_signal` share a single exception
    rather than each separately catching `attach_cold`'s raw failures
    (`FileNotFoundError`, `MisconfiguredColdDB`, `MisconfiguredDatabase`) —
    round-2 review of archives-cold-tier.md's PR, which found the dedup
    fallback was the one cold-tier caller not already going through
    `attach_cold_or_reason`.

    `.reason` is `cold_unavailable_reason`'s fixed, bounded wording — the
    same string a read path's `attach_cold_or_reason` returns — so a caller
    that reports it never repeats the path or exception type the full
    exception (logged by `_cold_dedup_existing` at the point it is raised)
    carries.
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def cold_db_available() -> tuple[bool, str]:
    """Whether the cold tier can be attached right now, and why not when it
    cannot — `vectors.available`'s own shape, for the service's own startup
    line (`app.lifespan`).

    **Informational, not a gate: every failure becomes `False`, `SQLITE_BUSY`
    included** — unlike `attach_cold_or_reason`, which re-raises contention
    because a route's own busy handling needs to see it. A one-off startup
    line has no caller to hand that to, and `vectors.available`'s own
    `_open_or_reason` draws the identical line for the vectors file.

    **`connect()` itself is inside the same try as `attach_cold`** — a
    misconfigured or missing `LIBRARY_DB` (a pre-existing condition this
    function did not cause) must not turn an unrelated startup check into an
    unhandled crash of the lifespan handler it runs inside of.
    """
    try:
        conn = connect()
        try:
            attach_cold(conn, readonly=True)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — any failure here means "unavailable"
        return False, cold_unavailable_reason(exc)
    return True, "ready"


def cold_signal_by_id(signal_id: str) -> sqlite3.Row | None:
    """One signal row from the cold tier, or None, on its OWN read-only
    connection with `cold` attached `mode=ro` — never on a write handle.

    **`connect_rw`'s handle is not URI-opened, so a `mode=ro` attach on it
    cannot be parsed as a URI, and attaching read-write there would give a
    write route access to the archive it has no business touching** — the
    reason `insert_signal`'s registry-hit fetch calls this rather than
    attaching onto its own `connect_rw()` handle (plan §4).

    Raises on an attach failure (`FileNotFoundError`, `MisconfiguredDatabase`,
    or a busy `sqlite3.OperationalError`) rather than swallowing it — the
    caller decides how to answer, the same split `attach_cold` draws
    everywhere else.
    """
    conn = connect()
    try:
        attach_cold(conn, readonly=True)
        return conn.execute(f"SELECT * FROM {COLD_SCHEMA}.signals WHERE id = ?",
                            (signal_id,)).fetchone()
    finally:
        conn.close()


_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid() -> str:
    """A ULID: 48-bit millisecond timestamp, 80 bits of randomness, Crockford
    base32. Sortable by creation time, unique from the random tail.

    The same generator Orient uses, reproduced rather than imported, because
    during the shadow the two services share no code — and an id scheme that
    drifted between them would make the two halves of one vault unsortable
    against each other."""
    ts = int(time.time() * 1000) & ((1 << 48) - 1)
    val = (ts << 80) | int.from_bytes(os.urandom(10), "big")
    out = []
    for _ in range(26):
        out.append(_CROCKFORD[val & 0x1F])
        val >>= 5
    return "".join(reversed(out))


# Field bounds, carried across from Orient's `signal_api` unchanged. A captured
# email or review can be genuinely large, so the body cap is generous and still
# bounded; the identifier caps are what stop a caller writing a novel into a
# column meant to hold a slug.
MAX_TEXT = 200_000
MAX_IDENT = 64
MAX_ACTOR = 200
# RFC 5321 §4.5.3.1.3's own bound on an address, and `people.sign_in_email`'s
# column width (`ad6a0a903c09`) — not a deliverability check, only a ceiling
# on what this service will store.
MAX_EMAIL = 254
# `people.display_name`'s column width (`76bfd9912739`); SQLite does not
# enforce it, so this is the ceiling `bootstrap_library.py`'s own
# `DISPLAY_NAME_MAX` and `create_person` both hold the line at.
MAX_DISPLAY_NAME = 300
MAX_STREAM = 200

# The operator, 2026-09-30: `POST /signals` refuses a submitter's 121st capture inside
# any 60-second window with a 429, keyed on `Identity.actor` — see
# `check_signal_rate_limit`. Named here, beside the field bounds, so a test can
# patch either one without reaching into the rate limiter's own module state.
SIGNAL_RATE_LIMIT = 120
SIGNAL_RATE_WINDOW_SECONDS = 60

# **A BLOB HAS NO NATURAL SIZE LIMIT THE WAY `body` DOES, and that is exactly
# why IT NEEDS ONE ANYWAY.** `body`'s cap bounds a column; `content` bounds a
# disk write an authenticated-but-untrusted caller controls the size of. 20 MB
# is generous against everything this exists to hold today — the largest
# observed transcript turn in the KB's own measurement is under 200 KB — and
# still a real ceiling against a buggy or malicious caller filling the mini's
# disk one signal at a time.
MAX_BLOB_BYTES = 20_000_000

# **THE ENVELOPE CAP IS DERIVED FROM THE BLOB CAP RATHER THAN CHOSEN BESIDE IT.**
# Two independent numbers drift, and this pair drifts silently in the direction
# that breaks a legal request: raise `MAX_BLOB_BYTES` alone and the blob route
# keeps advertising the larger content while the envelope refuses it before the
# handler ever runs, so the route's own error message names a limit the caller
# already satisfied. Derived, that cannot happen, and
# `test_the_envelope_holds_the_largest_body_a_route_will_accept` encodes the
# largest legal body rather than comparing the two constants — an inequality
# between them was the first version of that test, and it stayed true while the
# property it stood for was false.
#
# **THE MULTIPLIER IS MEASURED ACROSS ALL OF UNICODE RATHER THAN READ OFF AN
# ESCAPE SET, BECAUSE PICKING IT FROM EXAMPLES WAS WRONG TWICE** (reviewer and
# security-reviewer, PR #66, independently, in two consecutive rounds).
#
# Round 1 shipped 1.4x on a comment claiming 2x. Round 2 shipped 2x, reasoning
# from `"`, `\` and newline — all single-byte characters whose escape doubles
# them. Both rounds were then broken by a character class nobody had listed:
# first ordinary punctuation, then ordinary NON-ASCII text. `json.dumps` defaults
# to `ensure_ascii=True`, which every `requests` caller gets, and that escapes a
# 2-byte Cyrillic, Greek, Hebrew, Arabic or accented-Latin character to six bytes
# of `\uXXXX` — 3x — and a 4-byte emoji to twelve, also 3x.
#
# So the number is no longer chosen. `JSON_MAX_ESCAPE_EXPANSION` is the maximum
# over EVERY code point, both encoder settings, and
# `test_the_escape_expansion_ceiling_holds_across_all_of_unicode` recomputes it
# exhaustively rather than sampling the characters someone thought of. A sampled
# test is what let both earlier versions ship green.
#
# The worst case is 6x, at the control characters, which cost `\u0001` for one
# byte. Ordinary text in any script is at most 3x. Covering the true maximum is
# what makes the guarantee ABSOLUTE — no `content` under `MAX_BLOB_BYTES` can be
# refused by the envelope, in any script, under either encoder — and that is
# worth more than a smaller ceiling, because every version of this that traded
# the guarantee for a smaller number has been a defect a reviewer found.
#
# **THE COST IS A 128 MB CEILING ON ONE REQUEST, and the way to bring it down is
# not a smaller multiplier.** It is to stop sending a blob as an escaped JSON
# string: base64 expands by a fixed 1.34x and never escapes, which would allow an
# envelope near 35 MB with the same absolute guarantee. That is a change to the
# wire format and so a protocol version, which is why it is named here rather
# than done here.
JSON_MAX_ESCAPE_EXPANSION = 6

MAX_REQUEST_BYTES = MAX_BLOB_BYTES * JSON_MAX_ESCAPE_EXPANSION + 8_000_000

# **THE REVOCATION BOUND** (platform/decisions/0066). Worst case, a certificate
# revoked in `machine_certificates` keeps working for the length of one
# already-open request — the registry is read fresh per request, not per
# connection, so nothing beyond that one request survives a revocation. This
# constant is what turns "one already-open request" into a number rather than
# an unbounded wait: uvicorn has no per-request timeout of its own, so without
# this middleware a slow-drip request opened before a row flips to revoked has
# no ceiling at all.
#
# **60s, not the 30s the decision record proposed**, and reused rather than
# re-derived: `app._DRAIN_DEADLINE_SECONDS` already measured the slowest
# legitimate request this service serves — a single-item write queued behind
# `connect_rw`'s busy-timeout convoy, Orient's own production measurement at
# 32.2s — and margined it to 60s for a colder disk or a colder model load
# without naming a number for that second cost. Naming it now: with this
# service's fastembed cache cleared to force a genuine cold path, importing
# `fastembed`, constructing `TextEmbedding()` (which downloads the ONNX
# weights from Hugging Face when the cache is empty) and embedding one string
# measured at 3.49s over this network on 2026-09-01 — a number network
# variability can move, not a constant. 32.2 + 3.49 = 35.69, and 60 / 35.69 ≈
# 1.68x — short of the 1.86x margin it was set for, but still comfortable —
# and means one measurement serves both deadlines rather than the request
# deadline needing its own, independently-argued number.
#
# **This bound is real but not load-bearing to the same degree as the drain
# deadline it borrows from**: the convoy figure is Orient's own production
# data, the cold-model figure is one measurement on one network on one day.
# `LIBRARY_REQUEST_DEADLINE_SECONDS` overrides it for whoever remeasures and
# finds a different number, rather than requiring a code change.
REQUEST_DEADLINE_SECONDS = float(
    os.environ.get("LIBRARY_REQUEST_DEADLINE_SECONDS", "60"))

# How long `connect_rw` waits on SQLite's lock before the statement fails with
# `database is locked`. Named once because two things read it: the connection's
# `timeout`, and the `Retry-After` that `app.database_busy` answers with when the
# wait runs out.
BUSY_TIMEOUT_MS = 5000

# **THE JOURNAL MODE `connect_rw` PUTS EVERY WRITE CONNECTION IN**
# (issue_01M2KW35JX19GVG9Z13R10HKNZ). `journal_mode` is a property of the FILE,
# not the connection — once a database's header says WAL, it stays WAL until
# something changes it back — so issuing this on every write open is a no-op
# after the first real switch and is what makes a fresh database (a test's
# `tmp_path`, a database `migrate.py` just created) come up in WAL without a
# separate migration step. Under a rollback journal a reader holds a SHARED lock
# across a writer's commit and a writer's commit blocks every reader; under WAL,
# writers still take turns but no reader ever waits on one. That is the property
# this exists for — the service's fastest clients give up (`signal_client`'s 2s
# tee path, agentctl's 3s finalize) before `BUSY_TIMEOUT_MS` above even elapses,
# so a reader queued behind a writer under the rollback journal was an outright
# lost record, not a slow one.
WAL_JOURNAL_MODE = "WAL"

# **FULL, NOT WAL's OWN DEFAULT OF NORMAL — chosen rather than left unstated.**
# Under WAL, `synchronous=NORMAL` is safe against an application crash (a commit
# either fully lands in the WAL or it does not) but not against power loss: a
# commit can be written to the WAL and acknowledged before the fsync that makes
# it durable actually happens, so a crash of the machine itself, not just the
# process, can silently drop the most recent commits. This store holds work-item
# state, the review-verdict signals a merge gate reads, and the Archives'
# record of what happened — an operator or a future review reading back a
# quietly-missing recent commit is worse than paying an fsync on every commit,
# especially since writes here are already serialized behind one connection on a
# 5-second busy timeout rather than issued at a rate the extra sync would strain.
SYNCHRONOUS_MODE = "FULL"

# **THE SANITY FLOOR `mtls.build_tls_ssl_context` CHECKS THE DEADLINE
# AGAINST**, not merely that it is positive (security review, 2026-09-01,
# platform/decisions/0066's Handoff: "compare it against the slowest measured
# route and refuse a value that cannot be one"). The same 32.2s convoy figure
# plus the 3.49s cold-model figure above, named once here rather than
# recomputed at the call site. A deadline below this cannot even bound this
# service's own slowest legitimate request, so it is not a revocation bound —
# it is a timeout that fires on ordinary traffic.
SLOWEST_MEASURED_ROUTE_SECONDS = 32.2 + 3.49

# The ceiling on the other side of the same check. A value more than an order
# of magnitude past the slowest measured route no longer functions as a
# per-request bound — every real request finishes far under it — so it is,
# in practice, no bound at all, the failure mode named for
# `LIBRARY_REQUEST_DEADLINE_SECONDS=31536000` in the same review.
REQUEST_DEADLINE_CEILING_SECONDS = SLOWEST_MEASURED_ROUTE_SECONDS * 10

SIGNAL_COLUMNS = ("id", "source", "kind", "actor", "ingested_by", "product",
                  "stream", "body", "refs", "meta", "privacy_class",
                  "dedup_key", "produced_at", "acting_human")

# **THE FULL ROW SHAPE, KEPT EXPLICIT BESIDE `SIGNAL_COLUMNS` RATHER THAN
# DERIVED FROM A `SELECT *`.** `SIGNAL_COLUMNS` is what `insert_signal` WRITES;
# this is every column a `SELECT *` against `signals` returns, the two it never
# writes (`ingested_at`, stamped by the insert itself; `embedded_at`, stamped
# by `vectors.insert_vectors`) included. `signals_source` below selects this
# exact list from both files rather than `SELECT *`, so a schema drift between
# `main.signals` and `cold.signals` — a migration that forgot the cold file,
# `init_cold.py`'s own worry (§2) — fails a two-tier read with `no such
# column` rather than silently misaligning which value lands under which name.
TWO_TIER_SIGNAL_COLUMNS = (*SIGNAL_COLUMNS, "ingested_at", "embedded_at")


def signals_source(extra_predicate: str = "", *, hot_indexed_by: str = "") -> str:
    """The FROM-clause source every two-tier signal read selects from: hot
    UNION ALL cold, with the cold arm's `NOT EXISTS` excluding any row the
    mover has copied to cold but not yet deleted from hot — the transient
    two-copy state a crash between the mover's two transactions can leave
    (library/specs/archives-cold-tier.md §1, §3). Requires `cold` already
    attached on the connection this runs against (`attach_cold`).

    Returns a bare parenthesized subquery, unaliased — callers alias it
    themselves (`AS signals` for a drop-in replacement of the literal table
    name, or another alias for a correlated subquery, as `_signal_filters`'s
    `unreferenced_by` clause does with `AS ref`), since the alias a caller
    needs varies by call site and only one of them is `signals`.

    **`extra_predicate` IS A LITERAL FROM THE CALLING FILE, NEVER A REQUEST
    VALUE** — the same rule `SIGNAL_ORDER_CLAUSES` and `_customer_wall`'s
    `alias` document, for the same reason: it reaches this string unescaped.
    Pushed into BOTH arms, ahead of the de-dup clause, so a caller filtering
    on an indexed column (`kind = 'pr_state'`) keeps each arm seekable on its
    own index rather than filtering the whole union afterward — a predicate
    with a parameter placeholder needs that value bound TWICE in the
    caller's own params list, once per arm, in the order the two arms appear
    here (hot, then cold).

    **`hot_indexed_by`, when given, names an index on `main.signals` only —
    never the cold arm.** `init_cold.py` replays `main.signals`'s own
    `sqlite_master` to build a fresh cold file, so a cold file created before
    a given index's own migration ran does not carry it; `INDEXED BY` on a
    missing index is an error, not a fallback scan, so this can only be
    pushed into the arm whose schema this call already knows. Also a literal
    from the calling file, for the same reason `extra_predicate` is.
    """
    cols = ",".join(TWO_TIER_SIGNAL_COLUMNS)
    indexed_by = f" INDEXED BY {hot_indexed_by}" if hot_indexed_by else ""
    hot_where = f" WHERE {extra_predicate}" if extra_predicate else ""
    cold_extra = f"{extra_predicate} AND " if extra_predicate else ""
    return (
        f"(SELECT {cols} FROM main.signals{indexed_by}{hot_where} "
        f"UNION ALL "
        f"SELECT {cols} FROM {COLD_SCHEMA}.signals c WHERE {cold_extra}"
        f"NOT EXISTS (SELECT 1 FROM main.signals h WHERE h.id = c.id))")


# **THE ONE DEFINITION OF WHAT A PRIVACY CLASS MAY BE**, and the one spelling of
# the class itself. Five places compared a row's class against `"customer"` byte
# for byte — the write gate, the dedup no-leak check, both list filters and the
# single-signal wall — so a value matching none of them was customer content by
# intent and internal content to every filter (security-reviewer, PR #4).
#
# The allow-list closed the hole. The constant closes the shape: a first pass
# migrated the write gate and left three read-side sites still spelling the class
# out, which the allow-list then covered for (reviewer, PR #4) — a second control
# masking a first, rather than the consolidation this was introduced to be. Every
# comparison now reads the name from here, and `app._customer_wall` is the single
# SQL form of the read-side filter.
PRIVACY_CUSTOMER = "customer"
PRIVACY_CLASSES = frozenset({"internal", PRIVACY_CUSTOMER})

# Orient's bound, carried across. Long enough for any real idempotency key and
# short enough that it cannot be used as a second body column.
MAX_DEDUP_KEY = 300

# The Chart House vocabularies, carried across from Orient's `control_db`. Held
# as sets for the same reason `PRIVACY_CLASSES` is: a value outside them stored
# once is a row every filter and every view silently disagrees about.
#
# **"WORK ITEM" IN EVERY STRING A PERSON OR AN AGENT READS; "issue" IN EVERY
# IDENTIFIER, ON PURPOSE** (issue_01M12NE0SE2PN0HBTS8RB8XAN9, epic
# issue_01M0EEPS591VQHYEBJY7NGF4C3). Route documentation, refusal text and the
# protocol contract say work item. The `issue_items` table, the `issue_` id
# prefix and Orient's `/engineering/issues/` URLs keep their names: each is
# stored in rows or linked from elsewhere, so renaming one is a migration and
# a protocol version, not a wording fix.
MAX_TITLE = 300
MAX_ITEM_TEXT = 100_000
ITEM_TYPE_EPIC = "epic"
# `feature` is GONE, not merely ungated (the operator, 2026-08-19,
# issue_01M0EEPS591VQHYEBJY7NGF4C3). Every use it had was either the ARCH-403
# gate itself (removed with the decision plane, issue_01M0ASMCB4F68PF3NJG7BV7K6C)
# or Orient's triage-lane sort, which `needs = "operator"` already produces on
# its own. A work item is either an epic's sub-item or a correction — there is
# no third category a dropped type would have to keep meaning.
#
# **`bug`, `nit`, `improvement` and `task` ARE GONE TOO** (the operator,
# 2026-09-24, issue_01M0EEPS591VQHYEBJY7NGF4C3: "why do we have bug, nit, and
# improvement work items? I don't want them. I also don't want task", "I just
# want work items"). A work item carries no type at all; `epic` is the only
# value this vocabulary still holds. A non-epic row stores `type = NULL`.
ITEM_TYPES = frozenset({ITEM_TYPE_EPIC})

# The maturity ladder (ADR-0024). `approved` is the bucket that becomes the build
# queue, which is why moving an item into it is worth an operator's attention.
#
# **THE LADDER ITSELF NAMES NO CAPABILITY — the gate lives at the one route that
# may set `approved`.** The operator settled it on 2026-08-18 that the Library authorizes
# and records, with a restriction on WHO may approve applied outside if one were
# ever wanted; `epic.approve` (issue_01M32CA70BNV0WEW150V3KYBJ9) is that
# restriction, and it sits on `POST /items/{item_id}/approve`, not on this
# constant or on `_approval_invariant`, which still checks nothing about the
# caller. `/correct` and `/revision` refuse a request naming `maturity` outright
# rather than checking a capability on it, so there is still no `decide`-shaped
# capability anywhere in `ROLE_CAPS` gating this value — only a route that is
# the sole writer of `approved`.
ITEM_MATURITY_PROPOSED = "proposed"
ITEM_MATURITY_APPROVED = "approved"
ITEM_MATURITIES = (ITEM_MATURITY_PROPOSED, ITEM_MATURITY_APPROVED, "built",
                   "superseded")
ITEM_MATURITY_SET = frozenset(ITEM_MATURITIES)
ITEM_SEVERITIES = frozenset({"high", "medium", "low"})
ITEM_NEEDS = frozenset({"agent", "operator", "either"})
ITEM_DEFAULT_NEEDS = "agent"
# Who cannot be handed work by a drain. Named because two rules in this service
# read it — the claimable listing and the checkout refusal — and the second spelled
# it out inline until this constant existed (reviewer, convivy/library#34, on a
# comment that claimed three readers when only one used the name).
ITEM_NEEDS_OPERATOR = "operator"
ITEM_STATE_OPEN = "open"
ITEM_STATE_CLAIMED = "claimed"

# A resolution IS the terminal state, which is why these two sets are the same
# strings rather than a mapping between them. Resolving sets `state` to the
# resolution, so "how did this end" and "what state is it in" cannot disagree.
ITEM_RESOLUTIONS = frozenset({"fixed", "wontfix", "duplicate", "redirected"})
ITEM_TERMINAL = ITEM_RESOLUTIONS

# **A QUERY, NOT A STATE.** No row ever holds this value: `claimable` is derived
# at read time from the state, the claim's age, the lease covering the item and
# the `needs` field. It is named beside the real states because a caller passes
# it where a state goes — and because the route compared it against the column
# for one release, which answered 0 on a queue of 258.
ITEM_STATE_CLAIMABLE = "claimable"

ITEM_STATE_PR_OPEN = "pr-open"

# `awaiting-decision` is GONE (issue_01M0ASMCB4F68PF3NJG7BV7K6C) along with
# `/items/{id}/escalate` and the decision plane it existed to hand items to —
# with no route that could ever create a fresh decision again, an item parked
# there would have had no way back out except `resolve`'s ADR-0026 exception.
# An item that needs a human sets `needs="operator"` instead, the pattern
# already used everywhere else.
#
# `parked` is GONE too, with `/park`, `/unpark` and the `ITEM_HELD` set it was
# the only member of (ADR platform/decisions/0074). An item waiting on
# something names it in `prerequisites`, and `blocked` is derived from those at
# read time. `park_reason`, `unpark_condition` and `parked_at` stay on the rows
# that were once parked, as history.
#
# The states from which work can still move: every state that is not terminal,
# which is what every work route checks.
ITEM_ACTIVE = frozenset({ITEM_STATE_OPEN, ITEM_STATE_CLAIMED,
                         ITEM_STATE_PR_OPEN})
MAX_REF = 500

ITEM_COMMENT_KINDS = frozenset({"note", "diagnosis", "question", "decline"})
MAX_COMMENTS_PER_ITEM = 200

# An item's `topics` are its grouping labels — the axis a queue is read one area
# at a time along. The column has existed since items did and is returned on
# every read; until 2026-09-08 no route wrote it and no route filtered on it, so
# a queue of 150 items across 15 areas could only be grouped by typing the area
# into the front of each title by hand.
#
# **A TOPIC IS HELD TO `KIND_ALLOWED_CHARS` FOR THE REASON A SIGNAL KIND IS.**
# Both are short labels a human reads to tell one group from another, and both
# are matched exactly by a filter. A topic carrying a combining mark, a format
# character or a confusable non-ASCII letter renders as an existing topic and
# does not match it — so a filter answers "none" while a viewer shows the item
# sitting in that group. See KIND_ALLOWED_CHARS above for the full reasoning;
# this is the same hole one field over rather than a new argument.
MAX_TOPIC = 64
MAX_TOPICS_PER_ITEM = 16

# An item's `prerequisites` name what it is waiting on: another item, by id,
# or a testable condition a human judges. `blocked` is derived from this list
# at read time (see `blocked_clause` and `prerequisite_statuses` below) rather
# than stored, the same "a QUERY, not a STATE" shape `ITEM_STATE_CLAIMABLE`
# already is. 20 is generous against what ADR-0024's ladder actually produces
# — an item waiting on more than a handful of other items or conditions is a
# scoping problem, not a queue this field exists to represent.
MAX_PREREQUISITES_PER_ITEM = 20

# **A TEXT PREREQUISITE HAS ITS OWN BOUND, NOT `MAX_REF`.** A text entry is an
# unpark condition in its new home (ADR platform/decisions/0074), and the retired
# park route clipped those at `MAX_ITEM_TEXT`. Three of the 26 parked items migrated on
# 2026-09-11 carried conditions over 500 characters, the longest 898, and none
# could be cut: one carries test instructions a truncation drops, and two are
# either/or conditions that splitting would turn into all-of, since every
# prerequisite must be met. 2,000 holds the longest real one with room, and
# keeps a full item (20 entries) near 40 KB on every read that renders it.
MAX_PREREQUISITE_TEXT = 2000

# **THE SIGNAL KINDS THAT RECORD A REVIEW OUTCOME, AND WHOSE WRITE IS GATED ON
# `signal.create_review` (identity.py) — NOT THE GENERAL `signal.create`.**
# A `review_verdict` row is what a conditions-based merge gate would read — who
# approved a PR, at which head — so minting one must not be something an
# `agent`-role credential can do. `POST /signals` refuses a kind in this set
# unless the caller holds the capability; the GitHub capture poller (`kind:
# system` in api-tokens.json) and approved clients are the intended writers.
# Adding a kind here is adding a kind no `agent` can write, so a new producer
# that is not `system` or an approved client will need the capability granted.
SIGNAL_REVIEW_KINDS = frozenset({"review_verdict", "review_comment"})

# **THE ONLY CHARACTERS A `kind` MAY CARRY, AND WHY ANYTHING ELSE IS REFUSED
# RATHER THAN NORMALISED AWAY** (security-reviewer, PR #82 round 3). `kind` is
# an open vocabulary, but its character set is closed: `.strip()` removes only
# Unicode whitespace, so a `"review_verdict"` carrying a trailing U+200B ZERO
# WIDTH SPACE survived every normalisation, failed the exact-match membership
# test, and stored a row whose kind reads as `review_verdict` to any terminal,
# log viewer, dashboard or substring match. Growing what normalisation removes
# reopens the hole for the next character class, so `POST /signals` refuses a
# kind carrying any character outside printable ASCII (0x20 space through 0x7E
# tilde) — closing format characters, control characters, combining marks and
# confusable non-ASCII letters at once.
KIND_ALLOWED_CHARS = frozenset(map(chr, range(0x20, 0x7F)))

ITEM_INSERT_COLUMNS = ("id", "product", "title", "body", "refs", "severity",
                       "state", "type", "created_by", "created_by_kind",
                       "needs", "parent_id", "found_during", "topics",
                       "prerequisites", "once_key", "item_key")

# **THE SHORT, STABLE KEY A PERSON TYPES** (issue_01M2P5CBY5JY8SK5W344CV82AE)
# — `E9` for an epic, `E9·4` for a sub-item of it, `CQ·63` for a correction (an
# item whose parent is its product's corrections queue). U+00B7 MIDDLE DOT, short
# enough for a 320pt panel and distinct from a decimal point or a slug's own
# hyphen. Minted once, by `mint_item_key`, at the one place an item comes into
# existence; nothing afterwards — a retype, a re-parent under `/correct` —
# ever changes it, which is what makes a reference to it in a merged PR body
# still resolve to the same item years later.
ITEM_KEY_SEPARATOR = "·"
ITEM_KEY_EPIC_PREFIX = "E"
ITEM_KEY_CORRECTION_PREFIX = "CQ"

# The three scopes `issue_item_key_counters` hands out numbers for.
# `scope_id` is the product slug for an epic or a correction — so a product's
# epics and its corrections are two independent sequences, both starting at
# 1 — and the epic's own id for one of its sub-items, so two epics in the same
# product never share a counter.
ITEM_KEY_SCOPE_EPIC = "epic"
ITEM_KEY_SCOPE_ITEM = "item"
ITEM_KEY_SCOPE_CORRECTION = "correction"


def _next_item_key_number(conn, scope_kind: str, scope_id: str) -> int:
    """The next number `(scope_kind, scope_id)` will ever hand out, advancing
    the counter so the same number is never handed out twice — the backing
    guarantee `insert_item`'s key minting rests on, rather than a `MAX()` over
    live rows, which a resolved or deleted item would silently free up again.

    **Assumes the caller already holds the write lock on `conn`.** Reading and
    advancing a counter is a check-then-act, so it is only atomic because
    `insert_item` begins the surrounding transaction with `BEGIN IMMEDIATE`
    before this runs — the same precondition every other check-then-act in
    this file states for itself (`grant_or_revoke`, `upsert_review_record`).
    """
    row = conn.execute(
        "SELECT next_value FROM issue_item_key_counters "
        "WHERE scope_kind = ? AND scope_id = ?", (scope_kind, scope_id)
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO issue_item_key_counters "
            "(scope_kind, scope_id, next_value) VALUES (?, ?, 2)",
            (scope_kind, scope_id))
        return 1
    conn.execute(
        "UPDATE issue_item_key_counters SET next_value = next_value + 1 "
        "WHERE scope_kind = ? AND scope_id = ?", (scope_kind, scope_id))
    return row["next_value"]


def mint_item_key(conn, *, product: str, item_type: str,
                  parent_id: str | None) -> str:
    """The key a work item being filed right now will carry for its whole
    life (issue_01M2P5CBY5JY8SK5W344CV82AE).

    `item_type` is checked first, ahead of `parent_id`: an epic gets a
    per-product key, `E<n>`, fixed for its lifetime and independent of
    where it sits — a sub-epic nested under another epic gets its own
    `E<n>`, not a key built from its parent's, because an epic's own key
    and its position in the tree already live on two different axes (the
    same argument the issue makes for why a Course position cannot be the
    key). Only once `item_type` is not `epic` does `parent_id` decide the
    rest of the three-way split: a non-epic nested under an epic extends
    that epic's key; a non-epic with no epic parent is a correction. **A
    non-epic parent id is a correction too** (`library/specs/one-home-per-
    work-item`) — a corrections queue has no row in `issue_items` and so no
    `item_key` to extend, and the lookup below returns `None` for it exactly
    as it would for a dangling id, which is the same `CQ·n` a NULL parent
    gets. The epic-parent case is trusted to already carry its own
    `item_key` — `_validate_parent` establishes that an epic parent really
    is one before this is ever called, and an epic is minted its key in the
    same transaction that creates it, whatever its own `parent_id`.
    """
    if item_type == ITEM_TYPE_EPIC:
        n = _next_item_key_number(conn, ITEM_KEY_SCOPE_EPIC, product)
        return f"{ITEM_KEY_EPIC_PREFIX}{n}"
    parent = conn.execute(
        "SELECT item_key FROM issue_items WHERE id = ?",
        (parent_id,)).fetchone() if parent_id is not None else None
    if parent is not None:
        n = _next_item_key_number(conn, ITEM_KEY_SCOPE_ITEM, parent_id)
        return f"{parent['item_key']}{ITEM_KEY_SEPARATOR}{n}"
    n = _next_item_key_number(conn, ITEM_KEY_SCOPE_CORRECTION, product)
    return f"{ITEM_KEY_CORRECTION_PREFIX}{ITEM_KEY_SEPARATOR}{n}"


# The Course vocabularies, carried across from Orient's `control_db` and held
# here for the reason every other vocabulary in this file is: a value outside
# them, stored once, is a row that every filter and every view silently
# disagrees about.
#
#   live        — the plan in force. At most one per product, and the partial
#                 unique index on `courses` is what enforces that rather than a
#                 route remembering to check.
#   superseded  — a previous route, kept beside the live one rather than
#                 deleted. It answers "what did we intend before", which a
#                 deleted row cannot, and it is excluded from the unplanned
#                 invariant.
COURSE_LIVE = "live"
COURSE_SUPERSEDED = "superseded"
COURSE_STATUSES = (COURSE_LIVE, COURSE_SUPERSEDED)
COURSE_STATUSES_SET = frozenset(COURSE_STATUSES)

# What a course event records. `reordered` is the only action that moves the
# array, and the only one that carries an `entries` snapshot.
COURSE_ACTION_CREATED = "created"
COURSE_ACTION_REORDERED = "reordered"
COURSE_ACTION_RETITLED = "retitled"
COURSE_ACTION_SUPERSEDED = "superseded"
# The two that move the array as a side effect of an epic's maturity rather than
# of a caller reordering it: `approved` appends the epic, `demoted` removes it.
# Both carry the snapshot, and both name the epic and the reason in `note`.
COURSE_ACTION_APPROVED = "approved"
COURSE_ACTION_DEMOTED = "demoted"
# `courses.waypoints` moved — a synthetic waypoint was named
# (issue_01M12NE0VHY8828VJPFX8ARZ24). Carries a `waypoints` snapshot on the
# event rather than `entries`, which this action never touches.
COURSE_ACTION_WAYPOINT_ADDED = "waypoint_added"

# A bound on the order rather than a target. A project's route to its bead is a
# planning artifact a human reads end to end, so a Course of 500 epics is a
# symptom rather than a plan. The cap exists so a client loop cannot grow one
# array until every read of the table is expensive for everyone.
MAX_COURSE_ENTRIES = 500

# The same reasoning as `MAX_COURSE_ENTRIES`, applied to the two axes a
# synthetic waypoint adds: how many a Course can hold, and how many items one
# of them can name. Both are unbounded client input otherwise, and a Course
# nobody reads to the bottom of is the failure `MAX_COURSE_ENTRIES` already
# guards against on the other axis.
MAX_COURSE_WAYPOINTS = 100
MAX_WAYPOINT_MEMBERS = 50

# The optional note a caller may attach to a reorder or a supersede, saying why
# the route changed. Long enough for a real argument and short enough that it
# cannot be used as a second body column.
MAX_COURSE_NOTE = 10_000

# The decision-plane vocabulary is GONE (issue_01M0ASMCB4F68PF3NJG7BV7K6C,
# retired 2026-08-19). `PENDING`, `HUMAN_DECISIONS`, `AGENT_STATES`,
# `ALL_STATES` and `DECISION_INSERT_COLUMNS` lived here; the 120 decisions
# they governed are migrated into the Archives (`retire_decisions.py`),
# preserving their ids so `dec_<id>` still resolves through `GET /signals/{id}`.

# The per-(agent, crew, model, target) tool policy (ADR-0048). Bounded the same
# as every other identifier-shaped field this service stores — `MAX_IDENT`
# covers a role, a crew slug, or a model name, none of which is meant to hold
# more than a short name.
MAX_POLICY_FIELD = MAX_IDENT

POLICY_ALLOW = "allow"
POLICY_DENY = "deny"
POLICY_DECISIONS = frozenset({POLICY_ALLOW, POLICY_DENY})

# A Reference Room document's checkout (`issue_01M0DVAH9KAACHH0RRWQZ3KZTE`).
# `doc_id` is a KB document id from the knowledge index — `platform/decisions/
# 0047-a-reviewers-suite-scope-caps-at-the-foreground-ceiling-and-vivy-is-the-
# first-repo-across-it` is a real one at 115 characters — so the bound is
# generous rather than `MAX_IDENT`'s short-slug size, matching `MAX_TITLE`.
MAX_DOC_ID = MAX_TITLE

# **THE CLASSES THAT SUPPLY A DEFAULT WHEN NO EXPLICIT ROW EXISTS** (ADR-0048).
# Classified by MCP SERVER, not by tool — a connector is a whole account
# surface exposing a dozen-plus tools, and tracking per-tool repeats the
# roster's own drift problem, which is the failure this mechanism exists to
# not repeat. Nine entries today; growing this list is the "must have an
# opinion" moment a newly wired MCP server is supposed to get, not something
# that happens by omission.
#
# **Do not lean on the `claude_ai_<Service>` prefix as an enforced boundary —
# ADR-0048 is explicit that nothing confirms it is one.** This is the
# fallback the ADR names instead: an enumerated list, already the right size
# to maintain by hand.
#
# `claude_ai_Claude_Docs` joined on 2026-10-05, the day the connector appeared in
# live co sessions while this list still left it allowed: it
# creates, shares and exports documents in the operator's claude.ai account.
POLICY_CONNECTOR_SERVERS = frozenset({
    "claude_ai_Claude_Docs",
    "claude_ai_Gmail", "claude_ai_Google_Calendar", "claude_ai_Google_Drive",
    "claude_ai_Notion", "claude_ai_Resy", "claude_ai_Slack",
    "claude_ai_Spotify", "claude_ai_Uber_Eats",
})

# **DENIED FLEET-WIDE BY DEFAULT, NOT ONLY FOR BLACK-BOX CREWS** — the exit
# ADR-0043's transport wall never aimed at, since a built-in tool is not an
# MCP server and `--strict-mcp-config` does not touch it (ADR-0048). Grants
# are the exception, per combination, for the agents whose purpose is the
# tool (a `scout` or `consultant`-shaped agent getting `WebFetch`).
POLICY_EXFIL_BUILTINS = frozenset({"WebFetch", "RemoteTrigger", "DesignSync"})

TOOL_POLICY_MATCH_COLUMNS = ("agent", "crew", "model", "target")

# **THE BOUND ON HOW MANY PERMISSION ROWS ONE `POST /black-box/registrations`
# CALL MAY WRITE** (issue_01M19QQNTWD4RD9MSZZRKHYM7N). `connect_rw()`
# serialises every write in the Library through one SQLite writer on a
# 5-second `busy_timeout`, so an unbounded batch here stalls every other
# writer for as long as it takes to walk it — the same reasoning
# `crew_registry.MAX_INCLUDE_JSON` gives. Sized against the surface ADR-0048
# actually tracks rather than against nothing: `POLICY_CONNECTOR_SERVERS`
# and `POLICY_EXFIL_BUILTINS` together name 12 targets, so a black-box
# crew's roster — a handful of agents, not hundreds — declaring an explicit
# decision for every one of them plus a generous margin of ordinary
# built-ins is nowhere near 200.
MAX_BLACK_BOX_PERMISSIONS = 200


def insert_item(row: dict, can_return_existing, claim_for,
                in_scope) -> tuple[int, dict]:
    """File one Chart House item, or idempotently return its `once_key` match.

    The same two-check shape `insert_signal` uses, and for the same reason: the
    pre-check catches the ordinary collision and the `IntegrityError` catch
    catches the race, both answering a caller the gate refuses — one that could
    not have created the row, or could not read it — with a bare 409 worded
    identically. `once_key` is an idempotency key like `dedup_key`, so it is an
    existence oracle in exactly the same way.

    **The state event is written in the same transaction as the item.** Orient
    appends it through the relationship so the two commit together; here they are
    two inserts and one commit, which is the same guarantee spelled out. An item
    whose history does not start with its own creation is a row the audit trail
    cannot explain.

    **`claim_for(conn, item)` and `in_scope(product)` ARE HANDED IN BECAUSE THIS
    MODULE CANNOT ASK.** `lease` imports `store`, so a claim lookup here would
    close the cycle, and `identity` imports `store` for the same reason a scope
    check would. The caller already imports both and passes `lease.claim_status`
    and a scope predicate down, the same shape every other echo site uses — the
    difference is only that those sites can name the dependency directly.

    An idempotent 200 answers with a row this call did NOT create, and by then it
    may have been checked out, so its claim has to be read rather than assumed.
    The 201 is a row inserted `open` a line earlier and could take the unclaimed
    answer for free; it asks anyway, so that no branch here carries a reason for
    being the exception.
    """
    if row.get("type") is not None and row.get("type") not in ITEM_TYPES:
        raise ValueError(f"type {row.get('type')!r} is not one of "
                         f"{sorted(ITEM_TYPES)} or None")
    conn = connect_rw()
    try:
        if row["once_key"]:
            existing = conn.execute("SELECT * FROM issue_items WHERE once_key = ?",
                                    (row["once_key"],)).fetchone()
            if existing is not None:
                if not can_return_existing(dict(existing)):
                    return 409, {"error": "once_key already in use"}
                return 200, {**item_dict(
                                 existing, claim=claim_for(conn, existing),
                                 prerequisites=prerequisite_status(
                                     conn, existing, in_scope=in_scope)),
                             "idempotent": True}
        # **FROM HERE, THE WRITE LOCK IS HELD FOR THE WHOLE TRANSACTION.**
        # `mint_item_key` reads and advances a counter row — a check-then-act
        # that two concurrent creates in the same scope (the same product, or
        # the same epic) must not both win — and `BEGIN IMMEDIATE` is what
        # makes that safe, the same precondition `grant_or_revoke` and
        # `upsert_review_record` state for their own counters and checks.
        conn.execute("BEGIN IMMEDIATE")
        now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
        values = dict(row)
        values["created_at"] = now.isoformat(" ")
        # **A NON-EPIC WITH NO PARENT GETS ITS PRODUCT'S CORRECTIONS QUEUE,
        # NEVER A BARE NULL** (`library/specs/one-home-per-work-item`, PR 2).
        # `mint_item_key` would mint the same `CQ·n` either way — its own
        # lookup treats a queue id exactly like a missing parent — so this
        # has to run before that call only to make `values["parent_id"]`
        # itself correct for the row this transaction is about to write, not
        # to change what key it gets.
        if (values["type"] != ITEM_TYPE_EPIC
                and values["parent_id"] is None):
            values["parent_id"] = get_or_create_corrections_queue(
                conn, row["product"])
        values["item_key"] = mint_item_key(
            conn, product=row["product"], item_type=row["type"],
            parent_id=values["parent_id"])
        cols = (*ITEM_INSERT_COLUMNS, "created_at")
        try:
            conn.execute(
                f"INSERT INTO issue_items ({','.join(cols)}) "
                f"VALUES ({','.join('?' * len(cols))})",
                [values[c] for c in cols])
            conn.execute(
                "INSERT INTO issue_events (item_id, actor, from_state, to_state, at) "
                "VALUES (?,?,?,?,?)",
                (row["id"], row["created_by"], None, ITEM_STATE_OPEN,
                 now.isoformat(" ")))
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            if not row["once_key"]:
                return 409, {"error": "conflict"}
            existing = conn.execute("SELECT * FROM issue_items WHERE once_key = ?",
                                    (row["once_key"],)).fetchone()
            if existing is not None and can_return_existing(dict(existing)):
                return 200, {**item_dict(
                                 existing, claim=claim_for(conn, existing),
                                 prerequisites=prerequisite_status(
                                     conn, existing, in_scope=in_scope)),
                             "idempotent": True}
            if existing is None:
                # THE CONFLICT WAS NOT A `once_key` COLLISION. Something else
                # violated a constraint — an id clash, effectively unreachable at
                # ULID odds — and reporting it as a `once_key` collision would
                # send the caller looking for a key that is not in use. Orient
                # falls through to the generic message here and so does this
                # (reviewer, PR #5).
                return 409, {"error": "conflict"}
            return 409, {"error": "once_key already in use"}
        stored = conn.execute("SELECT * FROM issue_items WHERE id = ?",
                              (row["id"],)).fetchone()
        return 201, item_dict(
            stored, claim=claim_for(conn, stored),
            prerequisites=prerequisite_status(conn, stored, in_scope=in_scope))
    finally:
        conn.close()


def get_item_for_parenting(conn, parent_id: str):
    """The columns `_validate_parent` needs, or None."""
    return conn.execute(
        "SELECT id, product, type, parent_id FROM issue_items WHERE id = ?",
        (parent_id,)).fetchone()


def get_corrections_queue(conn, queue_id: str):
    """One product's corrections queue by id, or None.

    `_validate_parent` tries this table FIRST and falls through to
    `get_item_for_parenting` only on a miss — a corrections queue is never a
    row in `issue_items` (`library/specs/one-home-per-work-item`), so the two
    lookups are disjoint and a caller runs the other one second, never
    instead.
    """
    return conn.execute(
        "SELECT id, product, created_at FROM corrections_queues WHERE id = ?",
        (queue_id,)).fetchone()


def corrections_queue_dict(row) -> dict:
    """Serialise a `corrections_queues` row for the wire."""
    return {"id": row["id"], "product": row["product"],
            "created_at": _iso(row["created_at"])}


def get_or_create_corrections_queue(conn, product: str) -> str:
    """`product`'s corrections queue id, minting the row the first time a
    correction needs one (`library/specs/one-home-per-work-item`, PR 2).

    **CALL ONLY INSIDE A `BEGIN IMMEDIATE` THE CALLER ALREADY HOLDS.** This
    is the same check-then-act shape `_next_item_key_number` states for
    itself, and for the same reason: SQLite's single writer lock, taken
    before this runs, is what stops two concurrent first-corrections in one
    product from each seeing no row and minting two. `UNIQUE(product)`
    (migration `8d88e5026d39`) is the schema-level backstop behind that
    lock, not a substitute for holding it.
    """
    row = conn.execute(
        "SELECT id FROM corrections_queues WHERE product = ?",
        (product,)).fetchone()
    if row is not None:
        return row["id"]
    queue_id = f"cq_{new_ulid()}"
    now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    conn.execute(
        "INSERT INTO corrections_queues (id, product, created_at) "
        "VALUES (?, ?, ?)", (queue_id, product, now.isoformat(" ")))
    return queue_id


def parse_timestamp(value):
    """A caller-supplied ISO-8601 timestamp as naive UTC, or None.

    **Unparseable becomes None rather than an error**, matching Orient: a capture
    must not fail because a source stamped it oddly, and the capture is usually
    the only copy. Storing the raw string instead would put a non-normalised
    value into a column both services read from once §4 merges the vault.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = _dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    return parsed.isoformat(" ")


def insert_signal(row: dict, can_return_existing, *,
                  reveal_human: bool = False) -> tuple[int, dict]:
    """Insert one Archives record, or idempotently return the row its
    `dedup_key` already matched. Returns `(status, body)`.

    **The collision is checked twice and refused the same way both times.** The
    pre-check catches the ordinary case; the `IntegrityError` catch catches the
    race where another capture committed between the check and the insert. Both
    answer a caller the gate refuses — one that could not have created the row,
    or could not read it — with a bare 409 carrying nothing, and the two denials
    are worded identically — a caller that could distinguish them would learn
    that a row exists, which is the disclosure the no-leak rule is about.

    The catch is guarded on `dedup_key` being set. Without that guard an
    unrelated `IntegrityError` would run a lookup for `dedup_key IS NULL` and
    could answer with an arbitrary row.

    **A `dedup_key` miss in hot falls through to the registry before either
    check answers "no row" (archives-cold-tier.md §4).** Moving a signal
    removes its key from hot's `UNIQUE (dedup_key)` constraint, so a repost
    of an already-moved signal's `dedup_key` would otherwise mint a second,
    live row with a key `main.signals` no longer enforces uniqueness over.
    `_cold_dedup_existing` may raise `ColdTierUnavailable` on a cold-tier
    attach failure — that propagates to the caller rather than being
    swallowed here, the same way `WritesDisabled` already does, so a caller
    can answer 503 (or swallow it as its own best-effort contract requires)
    rather than silently minting a duplicate.
    """
    # CHECKED AGAIN HERE, not only at the route. The route is where the caller
    # gets a readable 400; this is what stops the NEXT write route storing a
    # class no filter recognises because its author did not know the check
    # existed. Cheap, and the failure it prevents is silent.
    if row.get("privacy_class") not in PRIVACY_CLASSES:
        raise ValueError(
            f"privacy_class {row.get('privacy_class')!r} is not one of "
            f"{sorted(PRIVACY_CLASSES)}; a class no filter recognises would be "
            f"customer content to its author and internal content to every wall")
    conn = connect_rw()
    try:
        if row["dedup_key"]:
            existing = conn.execute(
                "SELECT * FROM signals WHERE dedup_key = ?",
                (row["dedup_key"],)).fetchone()
            if existing is None:
                existing = _cold_dedup_existing(conn, row["dedup_key"])
            if existing is not None:
                if not can_return_existing(dict(existing)):
                    return 409, {"error": "dedup_key already in use"}
                return 200, {**signal_dict(existing, reveal_human=reveal_human),
                             "idempotent": True}
        now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
        values = dict(row)
        # `ingested_at` is stamped here because it means "when this service took
        # the row". `produced_at` is NOT defaulted, because it means "when the
        # thing happened" and this service does not know that — Orient leaves the
        # column NULL for exactly that reason. Filling it with the ingest time
        # would turn "unknown" into a confident wrong answer, and nothing
        # downstream could tell the two apart.
        values["ingested_at"] = now.isoformat(" ")
        cols = (*SIGNAL_COLUMNS, "ingested_at")
        try:
            conn.execute(
                f"INSERT INTO signals ({','.join(cols)}) "
                f"VALUES ({','.join('?' * len(cols))})",
                [values[c] for c in cols])
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            if not row["dedup_key"]:
                return 409, {"error": "conflict"}
            existing = conn.execute(
                "SELECT * FROM signals WHERE dedup_key = ?",
                (row["dedup_key"],)).fetchone()
            if existing is None:
                existing = _cold_dedup_existing(conn, row["dedup_key"])
            if existing is not None and can_return_existing(dict(existing)):
                return 200, {**signal_dict(existing, reveal_human=reveal_human),
                             "idempotent": True}
            return 409, {"error": "dedup_key already in use"}
        stored = conn.execute("SELECT * FROM signals WHERE id = ?",
                              (row["id"],)).fetchone()
        return 201, signal_dict(stored, reveal_human=reveal_human)
    finally:
        conn.close()


def _cold_dedup_existing(conn, dedup_key: str) -> sqlite3.Row | None:
    """The registry's answer to `insert_signal`'s dedup check: the cold row
    a `cold_dedup_keys` hit names, or None — no hit, or a hit naming a row
    that is missing, the window a crash between the discard sweep's cold
    delete and its own registry delete can leave (`discard_sweep.py`'s
    unscoped end-of-pass step cleans that up), or between the mover's hot
    delete and its registry insert (archives-cold-tier.md §3). Either way
    this is an ordinary miss to `insert_signal`'s caller, not a 503 — the
    row genuinely is not there to return.

    `conn` is `insert_signal`'s own write handle, read here for the registry
    lookup alone (`cold_dedup_keys` lives in `main`, so this needs no
    attach); the row itself is fetched on a SEPARATE connection by
    `cold_signal_by_id`.

    **Raises `ColdTierUnavailable` on a cold-tier attach failure** — mirroring
    `attach_cold_or_reason`'s own split: `SQLITE_BUSY` passes through
    uncaught, since contention says nothing about which file this is, and
    every other failure is translated to the same fixed, bounded wording a
    read path's 503 already carries, with the full exception going to the
    service log here rather than at the eventual caller. This propagates
    uncaught out of this function, since a route that cannot confirm the
    dedup_key is genuinely free must not mint a second row for it.
    """
    hit = conn.execute(
        "SELECT signal_id FROM cold_dedup_keys WHERE dedup_key = ?",
        (dedup_key,)).fetchone()
    if hit is None:
        return None
    try:
        return cold_signal_by_id(hit["signal_id"])
    except sqlite3.OperationalError as exc:
        if exc.sqlite_errorcode & 0xFF == sqlite3.SQLITE_BUSY:
            raise
        raise ColdTierUnavailable(cold_unavailable_reason(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — mirrors attach_cold_or_reason
        raise ColdTierUnavailable(cold_unavailable_reason(exc)) from exc


# **A SLIDING WINDOW, PER ACTOR, IN ONE PROCESS'S MEMORY.** `serve_dual.py`
# runs the plain and mutual-TLS listeners as two `uvicorn.Server` instances
# sharing one process and one event loop (its own module docstring), so a
# module-level dict here is the whole service's state, not one listener's —
# a second in-memory counter per process, the wrong shape the ruling warns
# against, would need a second process to exist first. FastAPI still runs
# `/signals`'s handler on the event loop while other requests' sync work runs
# in the threadpool concurrently, so the read-modify-write below is locked
# rather than trusted to the GIL to serialize it correctly.
#
# A fixed window can admit `SIGNAL_RATE_LIMIT` right before its boundary and
# another `SIGNAL_RATE_LIMIT` right after — the ruling's own named failure,
# 240 across a boundary — so this keeps every accepted timestamp and counts
# how many fall inside the trailing `SIGNAL_RATE_WINDOW_SECONDS`, rather than
# resetting a counter on a clock boundary.
_signal_rate_lock = threading.Lock()
_signal_rate_windows: dict[str, list[float]] = {}


def check_signal_rate_limit(actor: str, *, now: float | None = None
                             ) -> tuple[bool, int]:
    """Whether `actor`'s next signal lands inside `SIGNAL_RATE_LIMIT` per
    `SIGNAL_RATE_WINDOW_SECONDS`. Returns `(True, 0)` and reserves the slot
    when it does; returns `(False, retry_after)` and reserves nothing when it
    does not, `retry_after` being whole seconds until the oldest reserved
    slot ages out of the window.

    **Call only once per request, at the point a request is about to write**,
    since a call that returns `True` immediately counts. `/signals` calls this
    after every other refusal has already had its chance, so a request
    refused for a bad body, an oversized field or a missing capability never
    reaches here and never spends a slot.

    **`now` is `time.monotonic()` by default**, chosen over wall-clock time
    because this measures elapsed time between requests within one process's
    uptime, and a wall-clock adjustment must not shrink or stretch the
    window. A caller that wants to control the clock — every test in
    `test_signal_rate_limit.py` — passes its own.

    **Bounded memory**: an actor's list is pruned to only the timestamps
    still inside the window on every call, including this one's — a call
    against an actor idle for longer than the window starts that actor over
    from an empty list rather than an ever-growing history of expired
    entries. This is what bounds each actor's own state to at most
    `SIGNAL_RATE_LIMIT` timestamps; the dict's total size is bounded in turn
    because `actor` is `Identity.actor`, resolved from the fixed, small set of
    credentials this service knows, never a value a caller supplies.
    """
    now = time.monotonic() if now is None else now
    cutoff = now - SIGNAL_RATE_WINDOW_SECONDS
    with _signal_rate_lock:
        timestamps = [t for t in _signal_rate_windows.get(actor, ())
                      if t > cutoff]
        if len(timestamps) >= SIGNAL_RATE_LIMIT:
            _signal_rate_windows[actor] = timestamps
            retry_after = max(
                1, math.ceil(timestamps[0] + SIGNAL_RATE_WINDOW_SECONDS - now))
            return False, retry_after
        timestamps.append(now)
        _signal_rate_windows[actor] = timestamps
        return True, 0


# `insert_decision` is GONE with the decision plane
# (issue_01M0ASMCB4F68PF3NJG7BV7K6C). `retire_decisions.py` is the one-time
# migration that moved what it wrote into the Archives.

# `signal_acks` is still Orient's table (§4 moves it with the rest of the
# Archives), which is why there is no `INSERT_COLUMNS` tuple or dedicated dict
# builder here the way `signals` and `issue_items` have — the ack row itself has
# nothing this service serves back beyond the two fields the route already
# echoes. `store.connect_rw()` reaches it the same way it reaches every other
# vault table during the shadow: through the one connection the write cutover
# gates.

def ack_signal(signal_id: str, identity: str) -> None:
    """Idempotently record `identity`'s acknowledgment of `signal_id`.

    **Checked twice and refused the same way both times**, the same shape
    `insert_signal` uses for `dedup_key`: the pre-check catches the ordinary
    repeat ack, and the `IntegrityError` catch catches the race where two acks
    from the same identity land between the check and the insert — a phone
    double-tap. `uq_signal_acks_signal_identity` is the arbiter either way, and
    the outcome the caller wanted (this identity has acked this signal) is
    already true, so both paths return normally rather than raising.
    """
    conn = connect_rw()
    try:
        existing = conn.execute(
            "SELECT id FROM signal_acks WHERE signal_id = ? AND identity = ?",
            (signal_id, identity)).fetchone()
        if existing is not None:
            return
        now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
        try:
            conn.execute(
                "INSERT INTO signal_acks (id, signal_id, identity, acked_at) "
                "VALUES (?,?,?,?)",
                (f"ack_{new_ulid()}", signal_id, identity, now.isoformat(" ")))
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
    finally:
        conn.close()


def unack_signal(signal_id: str, identity: str) -> None:
    """Withdraw `identity`'s acknowledgment of `signal_id`, idempotently — a
    second withdrawal, or one that never existed, is not an error."""
    conn = connect_rw()
    try:
        conn.execute(
            "DELETE FROM signal_acks WHERE signal_id = ? AND identity = ?",
            (signal_id, identity))
        conn.commit()
    finally:
        conn.close()


def policy_class_default(target: str) -> str:
    """The class default for `target` when no explicit row overrides it.

    Connectors and the exfiltration-capable built-ins default DENIED; anything
    else — a vault MCP server (`kb`, `ops`), an ordinary CLI built-in — defaults
    ALLOWED. This is a pure function of the two class constants above, not a
    database read, because the class membership is code the Library ships
    rather than state a caller can change: widening a class is a deploy, not a
    grant.
    """
    if target in POLICY_CONNECTOR_SERVERS or target in POLICY_EXFIL_BUILTINS:
        return POLICY_DENY
    return POLICY_ALLOW


def policy_version() -> int:
    """A cheap, monotonically increasing number that changes whenever an
    explicit policy row is written — what a cache checks before deciding
    whether a full refresh is worth the cost (ADR-0048). `tool_policy_events.id`
    already increments on every write and SQLite already indexes it as the
    primary key, so this is one `MAX()` rather than a second counter that could
    drift from the table it is meant to describe.

    **It does not move when a class changes.** `POLICY_CONNECTOR_SERVERS` and
    `POLICY_EXFIL_BUILTINS` live in code, so a deploy that adds a member writes
    no event and leaves this number where it was. No client compares it today:
    agent-platform's dispatch path resolves on every dispatch. A cache that
    skipped a resolve on an unchanged version would keep a class addition
    unapplied until the next explicit write.
    """
    conn = connect()
    try:
        row = conn.execute("SELECT MAX(id) AS v FROM tool_policy_events").fetchone()
        return row["v"] or 0
    finally:
        conn.close()


def resolve_policy(agent: str, crew: str, model: str) -> dict:
    """Every explicit override on file for this exact (agent, crew, model)
    triple, plus the version a caller can cache against.

    **One lookup, matching ADR-0048's own description of the mechanism**: this
    reads `tool_policy` for rows matching the triple and returns them as a
    `{target: decision}` map. It does not enumerate every known target —
    `policy_class_default` covers whatever this map has no entry for, and the
    caller (the route) is what hands both back together, since a resolution
    the caller cannot act on without a second lookup would not be one.
    """
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT target, decision FROM tool_policy "
            "WHERE agent = ? AND crew = ? AND model = ?",
            (agent, crew, model)).fetchall()
        return {row["target"]: row["decision"] for row in rows}
    finally:
        conn.close()


def upsert_policy(row: dict) -> dict:
    """Set the decision for one (agent, crew, model, target) combination,
    overwriting whatever was there. Returns the stored row.

    **State to read and revise, not an event to approve** (ADR-0048) — this
    overwrites in place on the unique combination rather than appending, which
    is why `tool_policy_events` exists beside it: the current-state table
    answers "what applies now" and the events table is what preserves "what
    used to apply and when it changed," matching the shape `issue_items`/
    `issue_events` and `courses`/`course_events` already use here.

    **The event is written in the same transaction as the upsert**, so a grant
    whose history does not start with its own change is not a state this table
    can reach — the same invariant `insert_item` and `insert_decision` hold for
    their own event tables.
    """
    conn = connect_rw()
    try:
        now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
        now_iso = now.isoformat(" ")
        conn.execute(
            "INSERT INTO tool_policy "
            "(agent, crew, model, target, decision, created_by, created_at, "
            " updated_by, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT (agent, crew, model, target) DO UPDATE SET "
            "  decision = excluded.decision, "
            "  updated_by = excluded.updated_by, "
            "  updated_at = excluded.updated_at",
            (row["agent"], row["crew"], row["model"], row["target"],
             row["decision"], row["actor"], now_iso, row["actor"], now_iso))
        conn.execute(
            "INSERT INTO tool_policy_events "
            "(actor, agent, crew, model, target, decision, at) "
            "VALUES (?,?,?,?,?,?,?)",
            (row["actor"], row["agent"], row["crew"], row["model"],
             row["target"], row["decision"], now_iso))
        conn.commit()
        stored = conn.execute(
            "SELECT * FROM tool_policy WHERE agent = ? AND crew = ? AND "
            "model = ? AND target = ?",
            (row["agent"], row["crew"], row["model"], row["target"])).fetchone()
        return dict(stored)
    finally:
        conn.close()


# What a removal records in `tool_policy_events.decision`, a NOT NULL column
# whose other values are `POLICY_DECISIONS`. Never a decision a row can hold.
POLICY_REMOVED = "removed"


def remove_policy(key: dict, actor: str) -> dict | None:
    """Delete the explicit row for one (agent, crew, model, target)
    combination, returning it, or None when no row is on file (CQ·134).

    **The event is written in the same transaction as the delete**, as
    `upsert_policy` does for a write, so a row never leaves the table without
    a history entry saying who removed it and when. The event's `decision`
    is `POLICY_REMOVED`, and its id moves `policy_version`, so a cache sees
    a removal the way it sees a write."""
    conn = connect_rw()
    try:
        now_iso = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None).isoformat(" ")
        params = (key["agent"], key["crew"], key["model"], key["target"])
        row = conn.execute(
            "SELECT * FROM tool_policy WHERE agent = ? AND crew = ? AND "
            "model = ? AND target = ?", params).fetchone()
        if row is None:
            return None
        conn.execute(
            "DELETE FROM tool_policy WHERE agent = ? AND crew = ? AND "
            "model = ? AND target = ?", params)
        conn.execute(
            "INSERT INTO tool_policy_events "
            "(actor, agent, crew, model, target, decision, at) "
            "VALUES (?,?,?,?,?,?,?)",
            (actor, *params, POLICY_REMOVED, now_iso))
        conn.commit()
        return dict(row)
    finally:
        conn.close()


def list_policy(filters: dict, limit: int, offset: int) -> tuple[list[dict], int]:
    """Every explicit row, optionally narrowed by exact match on any of
    agent, crew, model and target, ordered by that key, with the total
    (CQ·134). `filters` keys are always those four names, chosen by the
    route, so only values reach the query as parameters."""
    where = "".join(f" AND {field} = ?" for field in filters)
    params = list(filters.values())
    conn = connect()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) FROM tool_policy WHERE 1 = 1{where}",
            params).fetchone()[0]
        rows = conn.execute(
            "SELECT agent, crew, model, target, decision, created_by, "
            "created_at, updated_by, updated_at FROM tool_policy "
            f"WHERE 1 = 1{where} ORDER BY agent, crew, model, target "
            "LIMIT ? OFFSET ?", [*params, limit, offset]).fetchall()
        return [dict(r) for r in rows], total
    finally:
        conn.close()


def is_administrator(conn: sqlite3.Connection, person_id: str) -> bool:
    """Whether `person_id` has a row in `administrators`.

    Takes the connection rather than opening one so the route can ask it on a
    read-only handle before any write lock is taken, and
    `change_person_products` can ask it again inside its own transaction. One
    query in one place means those two answers cannot drift apart."""
    return conn.execute("SELECT 1 FROM administrators WHERE person_id = ?",
                        (person_id,)).fetchone() is not None


def person_exists(conn: sqlite3.Connection, person_id: str) -> bool:
    """Whether `person_id` has a row in `people`."""
    return conn.execute("SELECT 1 FROM people WHERE id = ?",
                        (person_id,)).fetchone() is not None


def change_person_products(administrator: str, person_id: str,
                           grant: list[str], revoke: list[str],
                           default: "list[str] | tuple[str, ...]" = ()) -> dict | None:
    """Grant and revoke products for one person, recording each change.
    Returns `{"granted": [...], "revoked": [...]}`, listing only the products
    that actually changed visibility, or `None` when `administrator` is not an
    administrator or `person_id` names nobody.

    **`default` is this person's visibility default — the project slugs the
    caller found `created_by` this person in the registry
    (`projects.slugs_created_by`) — and this function never resolves it
    itself.** `projects` has no cycle with `store` the way `identity` does, but
    the obligation is the same one `identity.in_scope` names below: a second
    caller computing the same default a different way, or forgetting to, is
    drift this keeps from happening by construction only if every caller
    passes the one the registry gives. Omitted, every name in `grant`/`revoke`
    is treated as an explicit grant exactly as before this parameter existed —
    every call in this file's tests that predates this parameter still means
    what it said.

    **A removed default has no row in `person_products` to delete, because it
    was never granted — it was never absent either.**
    `person_product_removals` is that table's mirror for the other direction:
    one row per product this person's default currently excludes, identified
    by who excluded it and when. Restoring a removed default deletes that row
    and never touches `person_products`, since no grant row for a default
    project should exist to begin with. A product that is both an explicit
    grant and a default (an explicit grant made before the registry recorded
    this person as its creator, say) is revoked in both tables at once, so a
    single revoke closes both paths to visibility rather than leaving the
    default one open.

    **The administrator and the person are re-checked INSIDE the write
    transaction**, after `BEGIN IMMEDIATE` has taken the lock, even though the
    route asked the same two questions on a read-only handle first. The route's
    check keeps a caller who is not an administrator from ever holding the write
    lock; this one makes the record honest, since an administrator removed
    between the two would otherwise be recorded as having made a change they
    were no longer entitled to make.

    **Every change appends to `person_product_changes` in the same transaction
    as the row it describes**, so a grant THIS FUNCTION makes always has its own
    change row. That is narrower than "every grant": `76bfd9912739` records that
    `person_products` rows were written by hand before any write path existed,
    and revoking one of those appends a `revoke` with no `grant` before it. A
    history that starts with a revocation is therefore a real state and not a
    corruption — read it as "nothing recorded how this grant arrived", not as a
    lost row. `action` stays `'grant'`/`'revoke'` whichever table a change
    touches: the vocabulary names what the call did to visibility ("made it
    visible", "made it invisible"), not which table recorded it.

    A revocation deletes the `person_products` row and its
    `granted_by`/`granted_at` with it, which is the reason the history is a
    separate append-only table and not columns on that one (`0652cc6b2cee`). A
    request that changes nothing writes nothing.

    **PRODUCT SCOPE IS THE CALLER'S TO ENFORCE, AND THIS FUNCTION CANNOT.** It
    re-checks the administrator and the person — that is what makes the record
    honest, as the first paragraph says — and it checks nothing about whether
    `administrator` may see the products being granted. The route does that with
    `identity.in_scope` before it calls here.

    **Cannot rather than merely does not**: `identity` imports `store`, so
    `store` importing `identity` back is a cycle, which is the same reason
    `insert_item` gives. Every store writer in this file leaves scope to its
    route for that reason, so this is structural and not a convention a future
    author may tidy up. A second caller — a command, another route — inherits
    the obligation and gets no refusal from here if it forgets
    (reviewer, PR #135 rounds 1 and 2).
    """
    conn = connect_rw()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if not (is_administrator(conn, administrator)
                and person_exists(conn, person_id)):
            conn.rollback()
            return None
        now_iso = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None).isoformat(" ")
        default_set = set(default)
        explicit_held = {r["product"] for r in conn.execute(
            "SELECT product FROM person_products WHERE person_id = ?",
            (person_id,))}
        # A product can only ever be removed from a default it belongs to, so
        # an empty `default_set` means `removed_defaults` is necessarily
        # empty too — queried and written only when `default_set` is
        # non-empty, which is also what keeps this call working against a
        # schema predating `person_product_removals` for a caller that never
        # passes a default (every call in this file's tests before this
        # parameter existed, and any call for a person the registry names no
        # creations for).
        removed_defaults = {r["product"] for r in conn.execute(
            "SELECT product FROM person_product_removals WHERE person_id = ?",
            (person_id,))} if default_set else set()
        visible = (default_set - removed_defaults) | explicit_held
        granted = sorted(set(grant) - visible)
        revoked = sorted(set(revoke) & visible)
        for product in granted:
            if product in removed_defaults:
                conn.execute(
                    "DELETE FROM person_product_removals WHERE person_id = ? "
                    "AND product = ?", (person_id, product))
            elif product not in default_set:
                conn.execute(
                    "INSERT INTO person_products (person_id, product, "
                    "granted_at, granted_by) VALUES (?, ?, ?, ?)",
                    (person_id, product, now_iso, administrator))
        for product in revoked:
            if product in explicit_held:
                conn.execute(
                    "DELETE FROM person_products WHERE person_id = ? AND "
                    "product = ?", (person_id, product))
            if product in default_set and product not in removed_defaults:
                conn.execute(
                    "INSERT INTO person_product_removals (person_id, "
                    "product, removed_at, removed_by) VALUES (?, ?, ?, ?)",
                    (person_id, product, now_iso, administrator))
        for action, products in (("grant", granted), ("revoke", revoked)):
            for product in products:
                conn.execute(
                    "INSERT INTO person_product_changes (person_id, product, "
                    "action, changed_at, changed_by) VALUES (?, ?, ?, ?, ?)",
                    (person_id, product, action, now_iso, administrator))
        conn.commit()
        return {"granted": granted, "revoked": revoked}
    finally:
        conn.close()


def person_id_by_email(conn: sqlite3.Connection, email: str) -> str | None:
    """The `person_id` whose `sign_in_email` equals `email`, or `None`.

    `email` is compared exactly, so the caller must already have trimmed and
    lower-cased it the way `set_person_email` stores it — this function does
    no normalisation of its own, the same division of labour
    `change_person_products` draws for a product name."""
    row = conn.execute("SELECT id FROM people WHERE sign_in_email = ?",
                       (email,)).fetchone()
    return row["id"] if row else None


# A sentinel rather than an exception: `set_person_email` already answers
# "not an administrator, or no such person" with `None`, the shape
# `change_person_products` uses, and a second failure mode needs a value
# distinguishable from both that and a successful `{"email": ...}`. Compare
# with `is`, never `==` — it is not a dict and carries no fields of its own.
EMAIL_TAKEN = object()


def set_person_email(administrator: str, person_id: str,
                     email: "str | None") -> dict | None:
    """Set or clear `person_id`'s sign-in email, recording the change with its
    administrator and time. Returns `{"email": email}` (the value now on the
    record, `None` for a clear) on success; `None` when `administrator` is
    not an administrator or `person_id` names nobody, `change_person_products`'s
    own refusal shape; or `EMAIL_TAKEN` when a DIFFERENT person already holds
    `email`.

    **`email` must already be trimmed and lower-cased** — this function does
    no normalisation, the same division `change_person_products` draws for
    product scope: validation is the caller's to do, this just writes.

    **The administrator, the person and the uniqueness are all re-checked
    INSIDE the write transaction**, after `BEGIN IMMEDIATE` has taken the
    lock, for the reason `change_person_products` gives for its own
    re-check: `BEGIN IMMEDIATE` serializes every write against this
    database, so nothing else can land a conflicting email, or remove the
    administrator or the person, between the route's read-only check and
    this one.

    Every change appends to `person_email_changes` in the same transaction as
    the row it describes, `change_person_products`'s reason: a change this
    function makes always has its own history row. A request that changes
    nothing still writes a row here, unlike `change_person_products` — an
    administrator re-setting the same email, or clearing one already clear,
    is a real act this function was asked to perform and not a no-op the
    caller forgot it already did.
    """
    conn = connect_rw()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if not (is_administrator(conn, administrator)
                and person_exists(conn, person_id)):
            conn.rollback()
            return None
        if email is not None:
            holder = person_id_by_email(conn, email)
            if holder is not None and holder != person_id:
                conn.rollback()
                return EMAIL_TAKEN
        now_iso = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None).isoformat(" ")
        conn.execute("UPDATE people SET sign_in_email = ? WHERE id = ?",
                    (email, person_id))
        conn.execute(
            "INSERT INTO person_email_changes (person_id, email, changed_at, "
            "changed_by) VALUES (?, ?, ?, ?)",
            (person_id, email, now_iso, administrator))
        conn.commit()
        return {"email": email}
    finally:
        conn.close()


def create_person(administrator: str, display_name: str,
                  email: "str | None" = None) -> dict | None:
    """Create a new person with a fresh `person_<ulid>` id, recording
    `administrator` as both `created_by` and `updated_by`. Returns
    `{"person_id": ..., "display_name": ..., "email": email}` on success;
    `None` when `administrator` is not an administrator,
    `change_person_products`'s own refusal shape; or `EMAIL_TAKEN` when
    `email` is given and some other person already holds it.

    **`display_name` and `email` must already be validated and, for `email`,
    trimmed and lower-cased** — the same division of labour
    `set_person_email` draws: validation is the caller's to do, this just
    writes.

    **The administrator and the email's uniqueness are checked INSIDE the
    write transaction**, after `BEGIN IMMEDIATE` has taken the lock, the same
    reason `set_person_email` gives for its own re-check — nothing else can
    land a conflicting email, or remove the administrator, between the
    route's read-only check and this one. There is no person to re-check for
    existence, since this call is what creates one.

    **The person row and the email write are one transaction**, so a
    refused email (`EMAIL_TAKEN`) leaves no person row behind. When `email`
    is given, the write and its `person_email_changes` row are exactly the
    ones `set_person_email` would make for this person, so the new row
    reads back identically either way.

    This never writes `administrators`, mints no credential, and grants no
    project: a new person starts with the default set alone, computed by
    `projects.slugs_created_by` finding nothing for a brand-new id.
    """
    conn = connect_rw()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if not is_administrator(conn, administrator):
            conn.rollback()
            return None
        if email is not None and person_id_by_email(conn, email) is not None:
            conn.rollback()
            return EMAIL_TAKEN
        person_id = f"person_{new_ulid()}"
        now_iso = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None).isoformat(" ")
        conn.execute(
            "INSERT INTO people (id, display_name, created_at, created_by, "
            "updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?)",
            (person_id, display_name, now_iso, administrator, now_iso,
             administrator))
        if email is not None:
            conn.execute("UPDATE people SET sign_in_email = ? WHERE id = ?",
                        (email, person_id))
            conn.execute(
                "INSERT INTO person_email_changes (person_id, email, "
                "changed_at, changed_by) VALUES (?, ?, ?, ?)",
                (person_id, email, now_iso, administrator))
        conn.commit()
        return {"person_id": person_id, "display_name": display_name,
                "email": email}
    finally:
        conn.close()


def black_box_registrations(crew: str | None = None) -> list[dict]:
    """The declared shape of one or every black-box registration: crew,
    model, gateway, and who wrote each value and when
    (issue_01M34SVJK9AVJX7VM8Y8A23HR7). `upsert_black_box_registration`
    below writes these rows; this function is their read counterpart,
    since that route's own response only echoes what the call just made.

    `crew` given narrows to that one row, or an empty list when it never
    registered; omitted, every row, ordered by crew. Both cases return a
    list, so a route asking for one crew checks the list's length for
    emptiness.
    """
    conn = connect()
    try:
        if crew is not None:
            rows = conn.execute(
                "SELECT * FROM black_box_registrations WHERE crew = ?",
                (crew,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM black_box_registrations "
                "ORDER BY crew").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def upsert_black_box_registration(row: dict) -> dict:
    """Set the model and gateway binding a black-box crew declares at
    registration (issue_01M19QQNTWD4RD9MSZZRKHYM7N). Returns the stored row.

    **The tool permissions this same request carries are NOT this table's
    concern** — those go through `upsert_policy` above, one call per
    declared permission, because the declared permissions ARE the
    per-combination tool policy rather than a second store for the same
    fact. This function only ever touches the two fields `tool_policy` has
    no column for.

    **Upserts on `crew`, not an insert** — a crew that re-registers with a
    new model or gateway overwrites its own row, the same shape
    `crew_registry.profile_put` uses for a rebind.
    """
    conn = connect_rw()
    try:
        now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
        now_iso = now.isoformat(" ")
        conn.execute(
            "INSERT INTO black_box_registrations "
            "(crew, model, gateway, created_by, created_at, updated_by, "
            " updated_at) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT (crew) DO UPDATE SET "
            "  model = excluded.model, "
            "  gateway = excluded.gateway, "
            "  updated_by = excluded.updated_by, "
            "  updated_at = excluded.updated_at",
            (row["crew"], row["model"], row["gateway"], row["actor"], now_iso,
             row["actor"], now_iso))
        conn.commit()
        stored = conn.execute(
            "SELECT * FROM black_box_registrations WHERE crew = ?",
            (row["crew"],)).fetchone()
        return dict(stored)
    finally:
        conn.close()


def black_box_registration_crews() -> frozenset[str]:
    """Every crew name that has registered itself as a black box
    (`black_box_registrations`), lowercased.

    A self-registration is a declaration independent of the project registry's
    own `private` flag — `POST /signals`'s black-box guard checks both, because
    either one is sufficient to make a crew a black box.

    **Identified through `_refuse_orients_handle`, the same fingerprint
    `connect_rw` asks before a write**, even though this connection is
    read-only. Without it, a `LIBRARY_DB` repointed at Orient's control
    database would not carry this table, and the plain `SELECT` below would
    raise `sqlite3.OperationalError` uncaught — a generic 500 where this
    route's other misconfiguration paths all answer a clean 503.
    """
    conn = connect()
    try:
        _refuse_orients_handle(conn)
        rows = conn.execute("SELECT crew FROM black_box_registrations").fetchall()
        return frozenset(r["crew"].strip().lower() for r in rows
                          if r["crew"] and r["crew"].strip())
    finally:
        conn.close()


def clip(value, limit: int) -> str:
    """Bound a caller-supplied string. A non-string becomes empty rather than
    raising, matching Orient, so one malformed field cannot 500 an ingest."""
    return value[:limit] if isinstance(value, str) else ""


def _iso(raw):
    """A stored timestamp in the shape every existing client already parses.

    **The shadow harness found this on its first run.** SQLite hands back the
    stored text, so the Library was answering `2026-08-08 20:09:32.898431` where
    Orient answers `2026-08-08T20:09:32Z` — the same instant, in a format an
    RFC-3339 parser rejects. Every client of these routes today is a client of
    Orient's, so matching Orient's serialisation is what makes the Library a
    drop-in rather than a migration.

    Seconds precision, matching Orient's `api_common._iso`, which formats a
    naive-UTC datetime with `%Y-%m-%dT%H:%M:%SZ`. The stored values are naive UTC
    on both sides, so the `Z` states the timezone the column already meant rather
    than converting anything.

    Anything unparseable is returned untouched. A column holding a value this
    cannot read is a fact about the data worth surfacing to a caller, and
    swallowing it to `None` would turn a visible oddity into a missing field.
    """
    if not raw:
        return raw
    if isinstance(raw, str):
        try:
            parsed = _dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return raw
    elif isinstance(raw, _dt.datetime):
        parsed = raw
    else:
        return raw
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def _json_or(raw, default):
    """A JSON column that may be NULL, empty, or — on old rows — not JSON at all.

    Returning the default beats raising: one malformed `refs` blob from 2026 must
    not make an item unreadable through the protocol, and the alternative is a
    500 on a read path for a field most callers ignore."""
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


ITEM_COLUMNS = (
    "id, product, title, body, refs, severity, state, claimed_by, claimed_at, "
    "resolution, resolved_by, resolved_at, outcome_note, created_by, created_at, "
    "run_id, type, created_by_kind, needs, park_reason, unpark_condition, "
    "parked_at, parent_id, found_during, topics, maturity, specification, "
    "definition_of_done, prerequisites, model_choice, model_reason, "
    "model_set_by_client, model_acting_human, model_set_at, item_key"
)


def _prerequisite_entries(raw) -> list[dict]:
    """`raw`'s `prerequisites` column parsed into the entries the blocked
    computation applies to — the ONE RULE `blocked_clause`'s SQL and every
    Python reader of this field share, so a listing and a checkout refusal
    cannot answer differently for the same stored value:

    - NULL, empty, unparseable, or valid JSON that is not a list: no
      prerequisites.
    - an array element that is not an object, or an object with neither a
      string `item` nor a string `text`: ignored.
    - an object with a string `item`: unmet unless that id names a row and
      its state is in `ITEM_TERMINAL`.
    - an object with a string `text`: unmet unless `met` is exactly JSON
      `true` — a string, a number, or an absent key all fail closed to
      unmet, the same as a JSON `false`.

    Shared by `prerequisite_statuses`, `unmet_prerequisite_count`,
    `_do_judge_prerequisite` and `/correct`'s old-prerequisites read
    (`app.py`), so there is exactly one place that decides what a stored
    value means. Returns entries as references into `raw`'s own parse, not
    copies, so a caller may mutate one in place and re-serialize the same
    list it read.
    """
    parsed = _json_or(raw, [])
    if not isinstance(parsed, list):
        return []
    return [e for e in parsed if isinstance(e, dict)
            and (isinstance(e.get("item"), str) or isinstance(e.get("text"), str))]


def _entry_unmet(e: dict, states: dict) -> bool:
    """Whether one entry already filtered by `_prerequisite_entries` is
    unmet — the Python half of the same rule, entry by entry. An entry
    carrying a string `item` is judged on that reference (`states` maps
    referenced id to state, a missing id meaning no such row); only when it
    does not is a string `text` judged, on `met` being exactly `True`."""
    if isinstance(e.get("item"), str):
        return states.get(e["item"]) not in ITEM_TERMINAL
    return e.get("met") is not True


def blocked_clause() -> tuple[str, list]:
    """The SQL half of `prerequisite_statuses`' blocked computation — whether
    `issue_items.prerequisites` names an item not yet in `ITEM_TERMINAL` (or
    naming no row at all) or a text condition not yet `met`. Implements
    exactly the rule `_prerequisite_entries`/`_entry_unmet` state above, for
    ANY stored value — not merely the well-formed ones this service's own
    write paths produce, since a hand-edited row has reached this table
    before.

    **Kept beside the Python version deliberately**, the same reasoning
    `lease.claimable_clause` states beside `claim_status`: a listing filter and
    a checkout refusal answering different things is a drain that gets handed
    an item the endpoint then refuses. Two prior versions of this clause
    disagreed with the Python side on a text entry with no `met` key (three-
    valued SQL `NULL = 0` let it through as "not blocking" while Python's
    `bool(missing)` called it unmet) and on `{"item": null}` (a bare
    `IS NOT NULL` guard let it through as "item-shaped" while `"item" in e`
    made Python treat it the same way) — both closed by judging string-ness
    with `json_type(...) IS 'text'` rather than presence or non-NULL-ness,
    which is also how the object-vs-scalar `met` comparison is made to fail
    closed instead of returning SQL NULL.

    **`json_valid`/`json_type` GUARD THE COLUMN THE WAY THE `topics` FILTER
    DOES** (`app.py`'s item listing). `json_each` over an unparseable blob
    aborts the WHOLE statement rather than skipping the bad row, so one
    malformed `prerequisites` value would take down this clause for every row
    in the result set, not only its own. A column that fails the guard reads
    as "no prerequisites", matching `_json_or`'s default.

    **EACH ARRAY ELEMENT IS RE-EXTRACTED FROM THE WHOLE COLUMN BY PATH,
    `json_extract(issue_items.prerequisites, pre.fullkey || '.item')`,
    NEVER FROM `pre.value` DIRECTLY.** `json_each`'s `value` column returns a
    string element already DEQUOTED, and passing that back into `json_type`/
    `json_extract` re-parses it as a fresh top-level JSON document — which
    raises "malformed JSON" for any string that is not itself valid JSON
    (`"a string"` is exactly this: the element `["a string"]`'s `value` is
    the two-word SQL string `a string`, not valid JSON on its own). Neither a
    `CASE WHEN` nor an `AND` guards this, because SQLite does not skip
    evaluating `json_extract`'s arguments just because an earlier operand of
    the same expression is false — confirmed empirically before relying on
    it. `pre.type = 'object'` (from `json_each` itself, no re-parse) filters
    non-object elements before anything touches their content by path.

    **A MISSING PREREQUISITE ITEM BLOCKS, THE SAME AS AN UNRESOLVED ONE.** The
    inner `NOT EXISTS` has nothing to distinguish "no row with this id" from
    "a row that has not reached `ITEM_TERMINAL`" — both fail to prove the
    prerequisite is satisfied, which is the read this field exists to give: a
    dangling reference is not silently satisfied.

    Returns the boolean expression for "this item IS blocked", parameterised
    on `ITEM_TERMINAL`. The claimable filter wraps it in `NOT (...)`; a
    single-row check may read it directly.
    """
    terminal = ",".join("?" * len(ITEM_TERMINAL))
    item_path = "pre.fullkey || '.item'"
    text_path = "pre.fullkey || '.text'"
    met_path = "pre.fullkey || '.met'"
    item_is_str = f"json_type(issue_items.prerequisites, {item_path}) IS 'text'"
    text_is_str = f"json_type(issue_items.prerequisites, {text_path}) IS 'text'"
    item_unmet = (
        f"({item_is_str} AND NOT EXISTS ("
        "SELECT 1 FROM issue_items AS prereq WHERE prereq.id = "
        f"json_extract(issue_items.prerequisites, {item_path}) "
        f"AND prereq.state IN ({terminal})))")
    # `NOT (item_is_str)` matches `_entry_unmet`'s `elif`: an entry with a
    # string `item` is judged on that reference, and `text` is judged only
    # when it doesn't carry one — the shape every write path produces, and
    # the only shape a hand-edited row carrying both keys has a defined
    # answer for.
    text_unmet = (
        f"(NOT ({item_is_str}) AND {text_is_str} AND "
        f"json_type(issue_items.prerequisites, {met_path}) IS NOT 'true')")
    sql = (
        "(CASE WHEN json_valid(issue_items.prerequisites) "
        "AND json_type(issue_items.prerequisites) = 'array' "
        "THEN EXISTS (SELECT 1 FROM json_each(issue_items.prerequisites) AS pre "
        f"WHERE pre.type = 'object' AND ({item_unmet} OR {text_unmet})) "
        "ELSE 0 END)")
    return sql, list(ITEM_TERMINAL)


def prerequisite_statuses(conn, rows, *, in_scope) -> dict:
    """A page's `prerequisites` (rendered) and `blocked` (computed), batched.

    **ONE QUERY FOR THE WHOLE PAGE'S REFERENCED ITEMS**, matching
    `lease.claim_statuses`'s reasoning: a serialiser resolving each row's
    prerequisites with its own query is the same N+1 on the busiest read route
    this service has.

    **`blocked` IS JUDGED ON THE REFERENCED ITEM'S TRUE STATE, WHETHER OR NOT
    THE READER MAY SEE IT.** A prerequisite the caller cannot read is rendered
    `missing` — the same shape `GET /courses/{id}` already gives a Course entry
    outside the reader's scope — but it still counts toward `blocked`: masking
    an out-of-scope prerequisite's identity must not also mask the fact that it
    is unresolved, or redaction would change what a drain may take.

    Returns `{item_id: (rendered_entries, blocked)}` for every row given.
    """
    parsed = {r["id"]: _prerequisite_entries(r["prerequisites"]) for r in rows}
    item_ids = {
        e["item"] for entries in parsed.values() for e in entries
        if isinstance(e.get("item"), str)}
    referenced = {}
    if item_ids:
        placeholders = ",".join("?" * len(item_ids))
        for r in conn.execute(
                f"SELECT id, product, state, title FROM issue_items "
                f"WHERE id IN ({placeholders})", list(item_ids)):
            referenced[r["id"]] = r
    states = {rid: r["state"] for rid, r in referenced.items()}
    out = {}
    for item_id, entries in parsed.items():
        rendered: list[dict] = []
        blocked = False
        for e in entries:
            if _entry_unmet(e, states):
                blocked = True
            if isinstance(e.get("item"), str):
                ref_id = e["item"]
                ref = referenced.get(ref_id)
                if ref is None:
                    rendered.append({"item": ref_id, "missing": True})
                elif in_scope(ref["product"]):
                    rendered.append({"item": ref_id, "state": ref["state"],
                                     "title": ref["title"]})
                else:
                    rendered.append({"item": ref_id, "missing": True})
            else:
                rendered.append({"text": e.get("text"),
                                 "met": e.get("met") is True,
                                 "judged_by": e.get("judged_by"),
                                 "judged_at": e.get("judged_at")})
        out[item_id] = (rendered, blocked)
    return out


def prerequisite_status(conn, row: sqlite3.Row, *, in_scope) -> tuple:
    """One item's `(prerequisites, blocked)` — the single-row form
    `prerequisite_statuses` batches for a page."""
    return prerequisite_statuses(conn, [row], in_scope=in_scope)[row["id"]]


def epic_progress(conn, epic_id: str, epic_state: str, *,
                   scope_clause: str, scope_params: list) -> dict:
    """One epic's progress, computed at read time from its children —
    `issue_items` rows whose `parent_id` is `epic_id` — rather than stored on
    the epic itself. There is no backlink column, no stored rank and no
    materialized progress column to keep in step with a child's state; this is
    the one aggregate query that answers the question fresh on every read.

    "Epic" is defined once, in `lease.py`'s module docstring, beside
    "waypoint" — this function computes progress for the first of the two;
    `waypoint_progress` below computes it for the second.

    **`children`/`by_state`/`terminal`/`percent_terminal` describe the
    children; `reachable` and `reached` answer two different questions.**
    `reachable` asks whether the children are done — every child in
    `ITEM_TERMINAL` — which is vacuously true when there are none, the same
    way a for-all over an empty set is true. `reached` asks whether the epic
    itself is done, from `epic_state` alone, independent of its children: an
    epic can be resolved with an open child (closed early) or open with every
    child resolved (nobody has closed it out yet), and the two flags are how a
    reader tells those apart instead of collapsing them into one bit.

    `scope_clause`/`scope_params` are `app._scope_clause`'s own output,
    applied to the same `issue_items` table `GET /items` filters — so a
    caller never learns a child count for a row it could not list, and this
    function is not a second place that spells the scope rule out.

    `percent_terminal` is `None` at zero children rather than `0` or `100`, so
    an empty epic reads as "no ratio yet" and not as a fabricated one.
    """
    rows = conn.execute(
        f"SELECT state, COUNT(*) AS n FROM issue_items "
        f"WHERE parent_id = ? {scope_clause} GROUP BY state",
        [epic_id, *scope_params]).fetchall()
    by_state = {r["state"]: r["n"] for r in rows}
    children = sum(by_state.values())
    terminal = sum(n for s, n in by_state.items() if s in ITEM_TERMINAL)
    return {
        "children": children,
        "by_state": by_state,
        "terminal": terminal,
        "percent_terminal": (terminal / children) if children else None,
        "reachable": terminal == children,
        "reached": epic_state in ITEM_TERMINAL,
    }


def waypoint_progress(member_ids: list[str], items: dict) -> dict:
    """One synthetic waypoint's progress — `lease.py`'s module docstring
    defines the term — computed from its own named `member_ids` rather than
    from a `parent_id` query, which is the one way this differs from
    `epic_progress` above. `items` maps an id to its resolved row, batched by
    the caller the same way `get_course` already batches entry resolution; a
    missing key means the id could not be resolved, in scope or otherwise.

    **PURE, over data a caller already loaded.** No query here, matching
    `lease.course_position`'s own rule for the same reason: a per-waypoint
    query would be the N+1 this file keeps finding and removing elsewhere.

    **A MISSING MEMBER IS REPORTED, NOT DROPPED** (the work item's own
    words: "An entry whose member was deleted reports that rather than
    being dropped"). It counts toward `total` and never toward `terminal`,
    so a deleted member blocks `reached` rather than silently shrinking the
    set it was removed from.

    **`reached` AND `reachable` ARE THE SAME QUESTION HERE, unlike on an
    epic.** An epic has its own `state`, independent of its children, so
    `epic_progress` keeps the two apart. A synthetic waypoint has no row and
    no state of its own — the specification's own rule is "reached when
    every member it names is terminal" — so there is only one answer to
    give, under the name the Course-level read already uses.
    """
    members = []
    by_state: dict[str, int] = {}
    missing = 0
    for mid in member_ids:
        item = items.get(mid)
        if item is None:
            missing += 1
            members.append({"id": mid, "missing": True})
            continue
        members.append({"id": mid, "state": item["state"]})
        by_state[item["state"]] = by_state.get(item["state"], 0) + 1
    total = len(member_ids)
    terminal = sum(n for s, n in by_state.items() if s in ITEM_TERMINAL)
    return {
        "members": members,
        "total": total,
        "by_state": by_state,
        "missing": missing,
        "terminal": terminal,
        "percent_terminal": (terminal / total) if total else None,
        "reached": terminal == total,
    }


def get_epic_for_relation(conn, epic_id: str):
    """`epic_id`'s `id` and `type`, or None — what `_validate_epic_relation_target`
    needs to confirm a target is an epic that exists.

    **NO PRODUCT FILTER, UNLIKE `get_item_for_parenting`.** A parent edge is
    refused across products; this relation is the carrier for exactly the
    case that refusal leaves unmet, so scoping this lookup to the caller's
    own products would refuse the one case the relation exists for.
    """
    return conn.execute("SELECT id, type FROM issue_items WHERE id = ?",
                        (epic_id,)).fetchone()


def epic_relation_dict(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "item_id": row["item_id"], "epic_id": row["epic_id"],
            "created_by": row["created_by"], "created_at": _iso(row["created_at"])}


def create_epic_relation(conn, *, item_id: str, epic_id: str, created_by: str,
                         now) -> tuple:
    """Write "item_id is part of epic_id's work" as its own row — never as
    `parent_id`, which is what keeps it off `lease.ancestor_ids` and
    `lease.subtree_ids`: both are plain `parent_id` walks, and this table is
    not one they read.

    **Idempotent on the `(item_id, epic_id)` pair.** A caller retrying a
    write it already made finds its own prior row and gets it back on 200,
    rather than racing the table's `UNIQUE` constraint to a 500.

    Returns `(status, body)`, matching every other write in this module.
    """
    existing = conn.execute(
        "SELECT * FROM issue_epic_relations WHERE item_id = ? AND epic_id = ?",
        (item_id, epic_id)).fetchone()
    if existing is not None:
        return 200, epic_relation_dict(existing)
    cur = conn.execute(
        "INSERT INTO issue_epic_relations (item_id, epic_id, created_by, "
        "created_at) VALUES (?, ?, ?, ?)", (item_id, epic_id, created_by, now))
    row = conn.execute("SELECT * FROM issue_epic_relations WHERE id = ?",
                       (cur.lastrowid,)).fetchone()
    return 201, epic_relation_dict(row)


def related_items_for_epic(conn, epic_id: str, *, scope_clause: str,
                           scope_params: list) -> list[dict]:
    """Items related to `epic_id` across products, for the epic's own
    listing to show beside its `parent_id` children.

    **Scoped exactly like `epic_progress`'s own query**, over the same
    `issue_items` columns `_scope_clause` filters, so a caller never learns a
    related item's id in a product it could not otherwise list — the rule
    `_lease_root_visible` applies to a lease's blocking root applies here to
    a relation's target.
    """
    rows = conn.execute(
        f"SELECT issue_items.id AS id, issue_items.product AS product, "
        f"issue_items.title AS title, issue_items.type AS type, "
        f"issue_items.state AS state, "
        f"issue_epic_relations.created_at AS related_at, "
        f"issue_epic_relations.created_by AS related_by "
        f"FROM issue_epic_relations JOIN issue_items "
        f"ON issue_items.id = issue_epic_relations.item_id "
        f"WHERE issue_epic_relations.epic_id = ? {scope_clause} "
        f"ORDER BY issue_epic_relations.id",
        [epic_id, *scope_params]).fetchall()
    return [{"id": r["id"], "url": f"/engineering/issues/{r['id']}",
             "product": r["product"], "title": r["title"], "type": r["type"],
             "state": r["state"], "related_at": _iso(r["related_at"]),
             "related_by": r["related_by"]} for r in rows]


def unmet_prerequisite_count(conn, row: sqlite3.Row) -> int:
    """How many of `row`'s prerequisites are unmet, judged on TRUE state.

    **NO `in_scope`, DELIBERATELY.** `blocked_clause` and `prerequisite_statuses`
    both judge blocking on the referenced item's real state whether or not the
    reader may see it, and a count carries no identity to redact — unlike
    `prerequisite_statuses`' rendering, which needs a scope predicate to decide
    whether to show a `title` or a `missing`. `_do_checkout`'s refusal names
    this count instead of which prerequisite it was, so the count is all it
    needs and the whole rendering pipeline is more than the question asks.
    """
    entries = _prerequisite_entries(row["prerequisites"])
    item_ids = {e["item"] for e in entries if isinstance(e.get("item"), str)}
    states = {}
    if item_ids:
        placeholders = ",".join("?" * len(item_ids))
        for r in conn.execute(
                f"SELECT id, state FROM issue_items WHERE id IN ({placeholders})",
                list(item_ids)):
            states[r["id"]] = r["state"]
    return sum(1 for e in entries if _entry_unmet(e, states))


# `pr_url` names a pull request on this org's own GitHub, never another
# org's — `/link-pr` accepts any string a caller sends, so `_pr_repo` is the
# one place that decides whether a `pr_url` is trusted enough to name a repo.
# Pinned to the org every repo in this codebase actually lives under
# (`convivy/library`, `convivy/vivy`, `convivy/constance`,
# `convivy/agent-platform`); a URL naming any other host or org derives no
# `pr_repo` at all, rather than one a caller could spoof by linking a PR on a
# lookalike repository.
#
# Matches `agent-platform`'s `bin/issue-pr-reconcile` (`_PR_URL_RE`) exactly,
# end-anchor and repo-name class both — keep the two in step.
_PR_REPO_RE = re.compile(
    r"^https://github\.com/(convivy/[A-Za-z0-9][A-Za-z0-9._-]*)/pull/(\d+)/?$")


def _pr_repo(pr_url) -> str | None:
    """`pr_url`'s repo, as `convivy/<name>`, or `None` when `pr_url` is absent
    or does not name a pull request on this org's GitHub."""
    if not isinstance(pr_url, str):
        return None
    m = _PR_REPO_RE.match(pr_url.strip())
    return m.group(1) if m else None


def item_dict(row: sqlite3.Row, *, claim, prerequisites) -> dict:
    """One work item in protocol shape.

    `once_key` and `embedded_at` are omitted on purpose. The first is an
    idempotency key belonging to the write path — publishing it would let a reader
    forge a collision with somebody else's pending write. The second is an
    internal index stamp that says nothing a caller can act on; `available` on an
    adjacency briefing is the fact it stands behind, and that is served elsewhere.

    **EVERY OTHER FIELD ORIENT SERVES IS SERVED HERE**, and the first shadow run
    against live data is why that sentence is worth writing down. The two services
    agreed about every row and every shared value, and the Library still returned
    a narrower record — `created_by` without its pair `created_by_kind`,
    `park_reason` without `parked_at`, `resolution` without `resolved_by`. Each
    read as an oversight rather than a decision because the other half was already
    there, and a client migrating off Orient would have lost them silently.

    **`claim` IS PASSED IN RATHER THAN LOOKED UP**, because it comes from the
    lease covering this item, which is a second row. A serialiser that fetched it
    would issue one query per claimed row of a 200-row page — the N+1 Orient's own
    PR #158 halved and did not remove. `lease.claim_statuses` computes a whole
    page in one query and the caller hands the answer down.

    **AND IT IS REQUIRED, because the default was the bug.** It was `(False,
    None)` — the answer for an unclaimed item — on the reasoning that a caller
    holding no lease information should report the same thing as one holding
    none. That is wrong: a caller holding no lease information does not know the
    item is unclaimed, and eleven echo sites inherited the default and published
    `claim_expires_at: null` on items that were genuinely held. Two more survived
    the first fix in `insert_item`'s idempotent-retry branches, which is the
    evidence that a silent default outlives the sweep that is supposed to find
    every use of it. A caller with no lease information now gets a `TypeError`
    rather than a plausible lie, so the next echo site cannot be added wrong.

    **`prerequisites` IS REQUIRED FOR THE SAME REASON, AND IT IS A `(rendered,
    blocked)` PAIR RATHER THAN THE RAW COLUMN.** `blocked` is a query over the
    referenced items' TRUE state, not something this row alone can answer —
    `prerequisite_status`/`prerequisite_statuses` resolve it against a
    connection and a reader's scope, the same dependency-injection shape
    `claim` already uses here for the same reason (`identity` imports `store`,
    so this module cannot ask a scope question itself). A default of `([],
    False)` would be the identical failure class the comment above just
    named: an item with a real, unmet prerequisite reporting `blocked: false`
    because a caller forgot to look.
    """
    expired, expires = claim
    rendered_prerequisites, blocked = prerequisites
    refs = _json_or(row["refs"], {})
    # `refs` is valid JSON of ANY shape, not necessarily an object — `.get`
    # unconditionally is what `test_search.py`'s Chart House fixture caught,
    # seeding `refs` as `'[]'`. A non-dict `refs` carries no `pr`/`url`.
    refs_obj = refs if isinstance(refs, dict) else {}
    pr_url = refs_obj.get("url")
    return {
        "id": row["id"],
        # The short, stable, human-typed label (issue_01M2P5CBY5JY8SK5W344CV82AE)
        # — `mint_item_key`'s docstring has the format. Served alongside `id`,
        # never instead of it: every tool still passes the ULID, and this is
        # for a person to read and write down.
        "key": row["item_key"],
        "url": f"/engineering/issues/{row['id']}",
        "product": row["product"],
        "title": row["title"],
        "body": row["body"],
        "type": row["type"],
        "state": row["state"],
        "severity": row["severity"],
        "needs": row["needs"],
        "maturity": row["maturity"],
        "parent_id": row["parent_id"],
        # The epic whose work turned this item up, or None — a fact distinct
        # from `parent_id` (ADR-0023's nesting) and from `blocked`'s
        # prerequisites: setting it moves neither.
        "found_during": row["found_during"],
        "topics": _json_or(row["topics"], []),
        "refs": refs,
        # `pr`/`pr_url` are `refs` promoted to named fields, so a caller reads
        # one shape rather than reaching into `refs` itself; `pr_repo` is
        # derived from `pr_url` and is never stored — see `_pr_repo`.
        "pr": refs_obj.get("pr"),
        "pr_url": pr_url,
        "pr_repo": _pr_repo(pr_url),
        "specification": row["specification"],
        "definition_of_done": row["definition_of_done"],
        "resolution": row["resolution"],
        "resolved_by": row["resolved_by"],
        "outcome_note": row["outcome_note"],
        "claimed_by": row["claimed_by"],
        "claimed_at": _iso(row["claimed_at"]),
        # Derived rather than stored, matching Orient and for its reason: the
        # policy is a tunable, so a column of precomputed expiries would go stale
        # the moment somebody changed it. `claim_expired` is guarded on the state
        # as well, because an expiry in the past on an item nobody claimed is not
        # an expired claim.
        "claim_expires_at": _iso(expires),
        "claim_expired": row["state"] == ITEM_STATE_CLAIMED and expired,
        "resolved_at": _iso(row["resolved_at"]),
        "created_by": row["created_by"],
        "created_by_kind": row["created_by_kind"],
        "created_at": _iso(row["created_at"]),
        "run_id": row["run_id"],
        "park_reason": row["park_reason"],
        "unpark_condition": row["unpark_condition"],
        "parked_at": _iso(row["parked_at"]),
        "prerequisites": rendered_prerequisites,
        "blocked": blocked,
        # The operator's model choice (`POST /items/{id}/model`), or None. The
        # client and human are what the route recorded: the client from the
        # credential, the human as reported and not verified.
        "model_choice": None if row["model_choice"] is None else {
            "model": row["model_choice"],
            "reason": row["model_reason"],
            "set_by_client": row["model_set_by_client"],
            "acting_human": row["model_acting_human"],
            "set_at": _iso(row["model_set_at"]),
        },
    }


def course_dict(row: sqlite3.Row) -> dict:
    """One Course in protocol shape.

    **`count` MEANS HOW MANY EPICS THIS COURSE HOLDS, and the collision is worth
    naming.** The list envelope also carries a `count`, meaning how many Courses
    are on this page. Same key, two subjects — a client reading `count` gets a
    different number depending on where it looked, which is exactly the kind of
    thing nobody discovers until it is wrong in production. Orient puts this one
    on the object and the Library now agrees, so the divergence is closed rather
    than left for the cutover to find.

    **`waypoints` IS THE RAW CONFIG, UNRESOLVED, THE SAME SHAPE `entries`
    ALREADY IS HERE.** Neither route that calls this resolves an id against
    `issue_items` — that costs a query, and a caller that only wants the list
    page should not pay for a resolution it never reads. `GET /courses/{id}`
    layers the resolved form on top, under its own key, the same way it
    already does for `entries` (`items`) rather than mutating this one in
    place — two keys with different shapes under the same name on two
    routes is the `count` collision above, and this file does not repeat it.
    **ADDITIVE and absent from no row**: the column backing it carries a
    `DEFAULT '[]'`, so a Course that predates it reads as having none rather
    than raising on a missing key.
    """
    entries = _json_or(row["entries"], [])
    return {
        "id": row["id"],
        "product": row["product"],
        "title": row["title"],
        "status": row["status"],
        "version": row["version"],
        "entries": entries,
        "count": len(entries),
        "waypoints": _json_or(row["waypoints"], []),
        "created_by": row["created_by"],
        "created_at": _iso(row["created_at"]),
        "updated_by": row["updated_by"],
        "updated_at": _iso(row["updated_at"]),
    }


def signal_dict(row: sqlite3.Row, *, reveal_human: bool = False) -> dict:
    """One Archives record.

    **`body` is included and `privacy_class` travels with it**, so a caller can
    see which rows are customer-authored rather than having to infer it. The
    capability that gates customer content is checked before this is ever called;
    carrying the class in the response is what lets a client refuse to log or
    forward a row it should not persist.

    **`dedup_key` and `embedded_at` STAY OMITTED, and they are the only two.**
    The first is an idempotency key belonging to the write path, publishable only
    at the cost of letting a reader forge a collision with somebody else's pending
    write — the same reasoning `item_dict` records for `once_key`. The second is
    an internal index stamp a caller cannot act on. `ingested_by` is different and
    is now served: it says which identity captured the row, which is provenance a
    reader can use rather than a key a reader can abuse.
    """
    out = {
        "id": row["id"],
        "source": row["source"],
        "stream": row["stream"],
        "kind": row["kind"],
        "actor": row["actor"],
        "ingested_by": row["ingested_by"],
        "product": row["product"],
        "body": row["body"],
        "refs": _json_or(row["refs"], {}),
        "meta": _json_or(row["meta"], {}),
        "privacy_class": row["privacy_class"],
        "produced_at": _iso(row["produced_at"]),
        "ingested_at": _iso(row["ingested_at"]),
    }
    # **WITHHELD BY DEFAULT, AND THE ASYMMETRY IS THE BUG THIS CLOSES**
    # (security-reviewer, PR #48). `ingested_by` says which CREDENTIAL captured
    # the row and is provenance any reader may have. `acting_human` names a
    # PERSON, usually by work email, and WRITING it is restricted to an
    # approved-client credential precisely because a human's identity is
    # human-reserved data (ADR-0038). Serving it back to every reader holding
    # `signal.read` made the write gate decorative: the fleet's lowest trust
    # tier could read off which named individual is behind a capture.
    #
    # **`_customer_wall` does not cover it**, which is what made this easy to
    # miss. That wall filters by `privacy_class`, and a human can be attested on
    # an `internal`-class capture, so the field travels outside the class the
    # wall knows about.
    #
    # **OMITTED RATHER THAN NULLED.** A key present as `null` cannot be told
    # apart from "no human was attested", so nulling it would answer a question
    # the caller may not ask. Absent is the same shape the customer wall uses:
    # the row simply does not say.
    if reveal_human:
        out["acting_human"] = row["acting_human"]
    return out


# `DECISION_COLUMNS`/`decision_dict` are GONE with the decision plane
# (issue_01M0ASMCB4F68PF3NJG7BV7K6C). A migrated decision reads back as an
# ordinary signal, through `signal_dict` above.


# ---------------------------------------------------------------------------
# review_records: one row per review identity (issue_01M2HS394KTFMRATFK1N3G4ZNB,
# under the "All reviews are memory" epic, issue_01M2F52BXECXGHC1P3PRJ28AV8),
# and its write route (issue_01M2HS3Q36RE5F82QXRN4Z0D0K). See the migration
# (`468a1789d29e`, and `e4894432e1aa` for `auth_reason`) for the uniqueness
# key and the re-write semantics this column list and the functions below
# serve.
# ---------------------------------------------------------------------------

REVIEW_RECORD_COLUMNS = ("id", "repo_identity", "product", "pr", "reviewed_ref",
                        "role", "run_id", "identity_key", "verdict", "findings",
                        "prose", "paths", "doc_ids", "item_ids", "auth",
                        "auth_reason", "content_sha", "version",
                        "source_comment_id", "signal_id", "created_at",
                        "updated_at")

# Bounds for the write route's own fields. `verdict` matches the migration's
# `String(64)` exactly; `role` takes the same short-identifier bound as
# `agent`/`model`/`kind` elsewhere in this module, matching the general
# "identifier-shaped field" convention `MAX_POLICY_FIELD` names. `repo_identity`
# matches the migration's `String(200)`.
MAX_REVIEW_VERDICT = MAX_IDENT
MAX_REVIEW_ROLE = MAX_IDENT
MAX_REVIEW_REPO_IDENTITY = 200

# List-length bounds. A review with more findings, paths, doc ids or item ids
# than this reads as a scoping problem, the same judgement
# `MAX_PREREQUISITES_PER_ITEM` makes for an item's prerequisites — sized more
# generously here because a large diff can legitimately touch this many files.
MAX_REVIEW_FINDINGS = 200
MAX_REVIEW_PATHS = 500
MAX_REVIEW_DOC_IDS = 500
MAX_REVIEW_ITEM_IDS = 500

# Per-string bounds for the three string lists. A path is sized like any
# other reference-shaped field (`MAX_REF`); a doc id reuses `MAX_DOC_ID`,
# already sized for the Reference Room's own longest ids; an item id reuses
# `MAX_IDENT`, generous against `issue_<26 chars>`.
MAX_REVIEW_PATH = MAX_REF
MAX_REVIEW_DOC_ID = MAX_DOC_ID
MAX_REVIEW_ITEM_ID = MAX_IDENT

# The epic's fixed set for `auth` — matches the migration's CHECK constraint,
# so a value rejected by the database is a value rejected here first.
REVIEW_AUTH_RUN_VERIFIED = "run-verified"
REVIEW_AUTH_UNVERIFIED = "unverified"
REVIEW_AUTH_CARD = "card"
REVIEW_AUTH_VALUES = frozenset({REVIEW_AUTH_RUN_VERIFIED, REVIEW_AUTH_UNVERIFIED,
                                REVIEW_AUTH_CARD})


def review_content_sha(verdict, findings, prose, paths, doc_ids, item_ids,
                        auth) -> str:
    """The one definition of what a review's canonical content hashes to.

    `content_sha` on `review_records` is this value, and the write route's
    re-write check (comment 2748 on issue_01M2HS394KTFMRATFK1N3G4ZNB) compares
    a new write's hash against the stored one to decide idempotent-return
    versus supersede — so there is exactly one place that decides what
    "identical content" means, rather than the write route and a future
    backfill each hashing it their own way. The hash covers every field whose
    change should supersede the record, which is why `auth` is included: a
    write that only changes `auth` (a run-bearing record first stored
    `unverified`, then written again once its run checks out and it becomes
    `run-verified`) must not hash identical to what's
    stored, or that write would be silently treated as a no-op.

    `json.dumps(..., sort_keys=True)` sorts keys at every nesting level, not
    only the top one, so a `findings` list whose entries hold the same keys in
    a different order still hashes the same. `separators=(",", ":")` fixes
    the whitespace, so the only thing that can move the hash is the data
    itself.
    """
    canonical = json.dumps(
        {"verdict": verdict, "findings": findings, "prose": prose,
         "paths": paths, "doc_ids": doc_ids, "item_ids": item_ids,
         "auth": auth},
        sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def review_record_dict(row: sqlite3.Row) -> dict:
    """One review record in protocol shape, decoding the JSON columns the
    same way `item_dict` and `signal_dict` decode theirs."""
    return {
        "id": row["id"],
        "repo_identity": row["repo_identity"],
        "product": row["product"],
        "pr": row["pr"],
        "reviewed_ref": row["reviewed_ref"],
        "role": row["role"],
        "run_id": row["run_id"],
        "identity_key": row["identity_key"],
        "verdict": row["verdict"],
        "findings": _json_or(row["findings"], []),
        "prose": row["prose"],
        "paths": _json_or(row["paths"], []),
        "doc_ids": _json_or(row["doc_ids"], []),
        "item_ids": _json_or(row["item_ids"], []),
        "auth": row["auth"],
        "auth_reason": row["auth_reason"],
        "content_sha": row["content_sha"],
        "version": row["version"],
        "source_comment_id": row["source_comment_id"],
        "signal_id": row["signal_id"],
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


def review_record_version_dict(row: sqlite3.Row) -> dict:
    """One superseded version of a review record, in protocol shape —
    `review_record_dict`'s counterpart for `review_record_versions`
    (issue_01M2HS42EA7V4DEXQX1R1PWS9Y). `review_record_versions` carries no
    `repo_identity`, `identity_key`, `role` or `reviewed_ref`: those form the
    record's uniqueness key and are identical across every version by
    construction (the migration's own docstring), so a reader already has
    them from the record this version belongs to.
    """
    return {
        "record_id": row["record_id"],
        "version": row["version"],
        "product": row["product"],
        "pr": row["pr"],
        "verdict": row["verdict"],
        "findings": _json_or(row["findings"], []),
        "prose": row["prose"],
        "paths": _json_or(row["paths"], []),
        "doc_ids": _json_or(row["doc_ids"], []),
        "item_ids": _json_or(row["item_ids"], []),
        "auth": row["auth"],
        "auth_reason": row["auth_reason"],
        "content_sha": row["content_sha"],
        "run_id": row["run_id"],
        "source_comment_id": row["source_comment_id"],
        "signal_id": row["signal_id"],
        "written_at": _iso(row["written_at"]),
    }


# The write route's supersede outcomes, named once so the store function and
# its tests agree on the literal spelling.
REVIEW_OUTCOME_CREATED = "created"
REVIEW_OUTCOME_UNCHANGED = "unchanged"
REVIEW_OUTCOME_SUPERSEDED = "superseded"
# `skip_if_verified`'s and `only_if_after`'s own outcomes — never produced by
# a call that passes neither, so a route-reachable write can never answer
# either of these.
REVIEW_OUTCOME_LEFT_VERIFIED = "left_verified"
REVIEW_OUTCOME_ALREADY_CURRENT = "already_current"


class ReviewRecordWouldDowngrade(RuntimeError):
    """Raised by `write_review_record` instead of superseding a run-verified
    record with a write whose own computed `auth` is `unverified`, on the
    same uniqueness key (issue_01M3WBNHB615DTK9VARCRN9S2Y).

    `POST /review-records` computes `auth` itself and never takes the
    caller's word for it, but any agent run can read an `agents-file` token
    granted `review_record.create` — so without this refusal, a plain
    re-post at the same PR head could silently replace a run-verified
    `REQUEST_CHANGES` with an unverified record, dropping the block from
    Deck's PR ladder, which counts `REQUEST_CHANGES` only when run-verified.

    **Checked against a fresh read inside this call's own `BEGIN IMMEDIATE`
    transaction, the same race `skip_if_verified` closes for the backfill**
    (see that guard's own reasoning in `write_review_record`'s docstring): a
    pre-read taken before the transaction opens could see a run-verified row
    a concurrent write has since superseded, and refuse a write that is no
    longer a downgrade by the time it would land.

    Carries the still-current record, in `review_record_dict` shape, as
    `existing` — so the route can name what it refused to replace without a
    second read outside the transaction.
    """
    def __init__(self, existing: dict):
        self.existing = existing
        super().__init__(
            f"existing {existing['role']} record for {existing['repo_identity']} "
            f"at {existing['reviewed_ref']} is run-verified "
            f"(verdict {existing['verdict']}); an unverified write may not "
            f"supersede it")


def _review_record_is_downgrade(existing: sqlite3.Row | None, row: dict) -> bool:
    """True when `row`'s own computed `auth` is `unverified` and `existing`'s
    is `run-verified` — the one case `write_review_record` refuses outright
    rather than deciding by content. `auth` is part of `review_content_sha`
    (see that function's docstring), so this pairing's `content_sha` can
    never equal `existing`'s regardless of how everything else compares: an
    identical unverified re-post of a run-verified record's content still
    has a different hash, because its `auth` differs. There is no content
    under which this pairing could answer `unchanged` — it is refused before
    the content comparison runs, not routed through it.
    """
    return (existing is not None
            and existing["auth"] == REVIEW_AUTH_RUN_VERIFIED
            and row["auth"] == REVIEW_AUTH_UNVERIFIED)


def _review_record_identity_key(run_id: str | None, content_sha: str) -> str:
    """The dispatch run's id when the review carries one, else
    `content:<content_sha>` — see the migration's docstring for why a
    runless review is keyed on its content rather than left NULL."""
    return run_id if run_id else f"content:{content_sha}"


def _find_review_record(conn, *, repo_identity: str, identity_key: str,
                        role: str, reviewed_ref: str):
    return conn.execute(
        "SELECT * FROM review_records WHERE repo_identity = ? AND "
        "identity_key = ? AND role = ? AND reviewed_ref = ?",
        (repo_identity, identity_key, role, reviewed_ref)).fetchone()


def _supersede_review_record(conn, existing: sqlite3.Row, row: dict,
                             content_sha: str, now_iso: str) -> sqlite3.Row:
    """The current row's content moves whole into `review_record_versions`,
    at the version it was current for, and the record's own row takes the
    new write's content. See the migration's docstring for why `pr`,
    `product`, `source_comment_id` and `signal_id` ride along on the version
    rather than being treated as identity — losing any of them at supersede
    time would make that version's association or provenance unrecoverable.
    """
    conn.execute(
        "INSERT INTO review_record_versions "
        "(record_id, version, product, pr, verdict, findings, prose, paths, "
        " doc_ids, item_ids, auth, auth_reason, content_sha, run_id, "
        " source_comment_id, signal_id, written_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (existing["id"], existing["version"], existing["product"],
         existing["pr"], existing["verdict"], existing["findings"],
         existing["prose"], existing["paths"], existing["doc_ids"],
         existing["item_ids"], existing["auth"], existing["auth_reason"],
         existing["content_sha"], existing["run_id"],
         existing["source_comment_id"], existing["signal_id"],
         existing["updated_at"]))
    conn.execute(
        "UPDATE review_records SET verdict = ?, findings = ?, prose = ?, "
        "paths = ?, doc_ids = ?, item_ids = ?, auth = ?, auth_reason = ?, "
        "content_sha = ?, run_id = ?, pr = ?, product = ?, "
        "source_comment_id = ?, signal_id = NULL, version = version + 1, "
        "updated_at = ? WHERE id = ?",
        (row["verdict"], json.dumps(row["findings"]), row["prose"],
         json.dumps(row["paths"]), json.dumps(row["doc_ids"]),
         json.dumps(row["item_ids"]), row["auth"], row["auth_reason"],
         content_sha, row["run_id"], row["pr"], row["product"],
         row["source_comment_id"], now_iso, existing["id"]))
    return conn.execute("SELECT * FROM review_records WHERE id = ?",
                        (existing["id"],)).fetchone()


def _review_record_guard_outcome(existing: sqlite3.Row | None, *,
                                 skip_if_verified: bool,
                                 only_if_after: tuple[str, str] | None) -> str | None:
    """`None` when neither backfill guard blocks a write against `existing`,
    else the outcome to answer without writing. Both guards are no-ops
    against `existing is None` — there is nothing yet to protect from a
    fresh insert.

    **`only_if_after`'s tie-break is the signal id specifically because
    that is what the load query's own `ORDER BY produced_at ASC, id ASC`
    already uses** (`review_record_backfill.py`) — a different tie-break
    here could disagree with which of two same-second comments the load
    order already decided was later, refusing a write the load order
    thinks should win, or the reverse.

    A `None` `existing["signal_id"]` (a record a prior pass crashed on,
    between the write and its link) sorts before every real signal id, so
    it never blocks — the position check must fall through to the ordinary
    content comparison for that row, which is what lets the caller's
    post-write relink repair reach it.
    """
    if existing is None:
        return None
    if skip_if_verified and existing["auth"] == REVIEW_AUTH_RUN_VERIFIED:
        return REVIEW_OUTCOME_LEFT_VERIFIED
    if only_if_after is not None:
        given_at, given_signal_id = only_if_after
        existing_at, existing_signal_id = existing["updated_at"], existing["signal_id"]
        if existing_at != given_at:
            at_or_after = existing_at > given_at
        elif existing_signal_id is None:
            at_or_after = False
        else:
            at_or_after = existing_signal_id >= given_signal_id
        if at_or_after:
            return REVIEW_OUTCOME_ALREADY_CURRENT
    return None


def write_review_record(row: dict, *, at: str | None = None,
                        skip_if_verified: bool = False,
                        only_if_after: tuple[str, str] | None = None,
                        ) -> tuple[int, str, dict]:
    """Insert, idempotently return, or supersede one review record on its
    uniqueness key (`repo_identity`, `identity_key`, `role`, `reviewed_ref`).

    Returns `(status, outcome, review_record_dict(...))`. `row` carries every
    column `REVIEW_RECORD_COLUMNS` except `id`, `identity_key`, `content_sha`,
    `version`, `signal_id`, `created_at` and `updated_at`, which this function
    derives or assigns.

    **The three-way decision on an existing key, settled on comment 2748 of
    issue_01M2HS394KTFMRATFK1N3G4ZNB and restated in the migration's
    docstring:** identical `content_sha` changes nothing and answers 200
    `unchanged`; different content supersedes the record (200 `superseded`);
    no existing row inserts fresh (201 `created`).

    **`BEGIN IMMEDIATE` before the read, matching `record_spend`'s reasoning**:
    this is the check-then-act that needs the lock, so the read and the write
    it decides must not straddle a race with another writer. The
    `IntegrityError` catch below is the race the pre-check cannot close by
    itself — another writer's insert lands between this transaction's read
    and its own insert — and it is handled the same way `record_spend`
    handles the equivalent race: roll back, re-read inside a fresh
    transaction, and apply the same three-way decision to whatever won.

    **`at` exists for `review_record_backfill.py`, which dates each record
    from its own review comment rather than from when the backfill ran.**
    It must never come from a request payload — `POST /review-records` calls
    this with `at` unset, so every caller reachable from that route keeps
    stamping `created_at`/`updated_at` from `datetime.now()`, byte for byte
    the same as before this parameter existed.

    **`skip_if_verified` and `only_if_after` exist for
    `review_record_backfill.py` too, and for the same reason `at` does: a
    caller that dates and gates its own writes rather than trusting a
    request payload for either.** `POST /review-records` passes neither, so
    every route-reachable write keeps today's plain three-way decision —
    except for the one pairing `ReviewRecordWouldDowngrade` refuses
    outright, below, which the route reaches precisely because it passes
    neither. Their point is atomicity, not convenience — the backfill's own
    `plan()` reads a key's stored state once, up front, then writes across a
    ~11k-row pass that can take minutes; a snapshot read that far ahead of
    the write it justifies is exactly the check-then-act gap a live write to
    the same key can land inside. Evaluating both guards here, against a
    fresh read inside this call's own transaction, closes that gap rather
    than narrowing it. See `_review_record_guard_outcome`.

    - `skip_if_verified`: when the existing record's `auth` is already
      `REVIEW_AUTH_RUN_VERIFIED`, roll back and answer
      `REVIEW_OUTCOME_LEFT_VERIFIED` without writing — a review the Library
      already holds under its own, run-verified authority must never be
      superseded by a comment that is a copy of it.
    - `only_if_after`: an `(produced_at, signal_id)` pair naming the
      position, in `review_record_backfill.py`'s own load order, of the
      comment this write is for. When the existing record's own
      `(updated_at, signal_id)` is at or after that position, roll back and
      answer `REVIEW_OUTCOME_ALREADY_CURRENT` without writing — the record
      already reflects a comment at or past this one, so writing this one
      now would only replay a decision an earlier (or this same, resumed)
      pass already made.

    **Independently of both guards above, and checked against the same
    fresh read: when `existing` is `run-verified` and `row`'s own computed
    `auth` is `unverified`, roll back and raise
    `ReviewRecordWouldDowngrade`** (`_review_record_is_downgrade`) — storing
    nothing. The backfill never reaches this: it always calls with
    `skip_if_verified=True` and writes only `REVIEW_AUTH_UNVERIFIED`, so
    `skip_if_verified`'s own check above already answers
    `REVIEW_OUTCOME_LEFT_VERIFIED` for that same pairing before this one is
    reached. `POST /review-records` is the only route-reachable caller this
    can raise for.
    """
    content_sha = review_content_sha(
        row["verdict"], row["findings"], row["prose"], row["paths"],
        row["doc_ids"], row["item_ids"], row["auth"])
    identity_key = _review_record_identity_key(row["run_id"], content_sha)
    conn = connect_rw()
    try:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        existing = _find_review_record(
            conn, repo_identity=row["repo_identity"], identity_key=identity_key,
            role=row["role"], reviewed_ref=row["reviewed_ref"])
        guarded = _review_record_guard_outcome(
            existing, skip_if_verified=skip_if_verified,
            only_if_after=only_if_after)
        if guarded is not None:
            conn.rollback()
            return 200, guarded, review_record_dict(existing)
        if _review_record_is_downgrade(existing, row):
            conn.rollback()
            raise ReviewRecordWouldDowngrade(review_record_dict(existing))
        now_iso = at if at is not None else _dt.datetime.now(
            _dt.timezone.utc).replace(tzinfo=None).isoformat(" ")
        if existing is not None:
            if existing["content_sha"] == content_sha:
                conn.rollback()
                return 200, REVIEW_OUTCOME_UNCHANGED, review_record_dict(existing)
            stored = _supersede_review_record(conn, existing, row, content_sha,
                                              now_iso)
            conn.commit()
            return 200, REVIEW_OUTCOME_SUPERSEDED, review_record_dict(stored)
        record_id = f"rev_{new_ulid()}"
        try:
            conn.execute(
                "INSERT INTO review_records "
                "(id, repo_identity, product, pr, reviewed_ref, role, run_id, "
                " identity_key, verdict, findings, prose, paths, doc_ids, "
                " item_ids, auth, auth_reason, content_sha, version, "
                " source_comment_id, signal_id, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,NULL,?,?)",
                (record_id, row["repo_identity"], row["product"], row["pr"],
                 row["reviewed_ref"], row["role"], row["run_id"], identity_key,
                 row["verdict"], json.dumps(row["findings"]), row["prose"],
                 json.dumps(row["paths"]), json.dumps(row["doc_ids"]),
                 json.dumps(row["item_ids"]), row["auth"], row["auth_reason"],
                 content_sha, row["source_comment_id"], now_iso, now_iso))
        except sqlite3.IntegrityError:
            conn.rollback()
            conn.execute("BEGIN IMMEDIATE")
            existing = _find_review_record(
                conn, repo_identity=row["repo_identity"],
                identity_key=identity_key, role=row["role"],
                reviewed_ref=row["reviewed_ref"])
            if existing is None:
                # The constraint that raised was this one, so the row it
                # collided with has to be visible to this same read — a miss
                # here is a bug in this function, not a legitimate outcome.
                raise
            guarded = _review_record_guard_outcome(
                existing, skip_if_verified=skip_if_verified,
                only_if_after=only_if_after)
            if guarded is not None:
                conn.rollback()
                return 200, guarded, review_record_dict(existing)
            if _review_record_is_downgrade(existing, row):
                conn.rollback()
                raise ReviewRecordWouldDowngrade(review_record_dict(existing))
            if existing["content_sha"] == content_sha:
                conn.rollback()
                return 200, REVIEW_OUTCOME_UNCHANGED, review_record_dict(existing)
            stored = _supersede_review_record(conn, existing, row, content_sha,
                                              now_iso)
            conn.commit()
            return 200, REVIEW_OUTCOME_SUPERSEDED, review_record_dict(stored)
        conn.commit()
        stored = conn.execute("SELECT * FROM review_records WHERE id = ?",
                              (record_id,)).fetchone()
        return 201, REVIEW_OUTCOME_CREATED, review_record_dict(stored)
    finally:
        conn.close()


def set_review_record_signal(record_id: str, version: int, signal_id: str) -> None:
    """Stamp the signal a review-record write minted onto the record —
    called after that write already committed, so this is its own,
    independent statement rather than part of the same transaction.

    **`WHERE id = ? AND version = ?`, not `id` alone.** The route calls this
    only after `_record_review_signal` has already run, which is itself
    after the record write committed — so a concurrent supersede can land in
    between and move `version` on. Matching both means a stale write here
    can only ever miss (0 rows affected, silently — matching this function's
    own best-effort caller) rather than stamp a signal id onto a version it
    was never minted for.
    """
    conn = connect_rw()
    try:
        conn.execute(
            "UPDATE review_records SET signal_id = ? WHERE id = ? AND version = ?",
            (signal_id, record_id, version))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The ledger store (issue_01M0B8YCFN4C1CYNANVG87CANG)
# ---------------------------------------------------------------------------

# Scopes a ledger may be granted at. The Keel's shape names a project, a
# workstream and a run; a RUN is deliberately absent, because a run's grant
# travels in its token and `agentctl` enforces it in-process. Giving a run a row
# here would put a write-per-run on the admission path to duplicate a number the
# token already carries.
# **PROJECT ONLY, and `workstream` is deliberately NOT here yet** (reviewer,
# PR #50). The epic names project and workstream scope, and the first version of
# this accepted both — but nothing resolves a work item to a workstream:
# `issue_items` has no such column, and both `_refuse_on_capped_ledger` and
# the spend route hardcode `scope_kind="project"`. So a workstream grant was
# accepted, stored, and then enforced by nothing.
#
# That is the worst of the three available answers. An operator who granted a
# workstream budget would reasonably believe it was enforced, and would get
# silent non-enforcement with no error anywhere — which is exactly the failure
# a ledger exists to prevent, wearing the costume of a working feature.
# Refusing the scope until there is data to resolve it is the reversible
# direction, and the epic's own "done when" asks only that a PROJECT has a
# ledger with a balance.
LEDGER_SCOPES = frozenset({"project"})
LEDGER_STATES = frozenset({"live", "closed"})

# Which of the two halves reported a spend. `agentctl` streams fleet spend; an
# app backend reports its own runtime spend. Kept as a closed set for the reason
# `PRIVACY_CLASSES` is: a value no filter recognises is worse than a refusal.
LEDGER_SOURCES = frozenset({"agentctl", "app"})

# Which kind of spend an entry is. `gateway` is metered provider spend —
# money a budget can be enforced against. `subscription` is subscription
# usage billed flat, so its dollar figure is an estimate rather than a
# metered fact — it may inform a human, and nothing here lets it move a
# number admission control refuses on (issue_01M35QG5NA13JE8FQVM9XET4HD).
#
# **OPTIONAL, NOT YET REQUIRED.** This is the first of two contract steps:
# the Library accepts `lane` and stores it when given, and an entry with no
# `lane` is still recorded, same as before this column existed. Refusing a
# write with no `lane` is the second step, made once `agentctl` sends one.
LEDGER_LANES = frozenset({"gateway", "subscription"})

MAX_LEDGER_SCOPE_ID = MAX_IDENT
# Millionths of a dollar. A day's cap of $160 is 160_000_000, and SQLite's
# INTEGER is 64-bit, so the range above any real budget is not worth bounding
# further. The cap exists to refuse a nonsense value rather than to fit a column.
MAX_LEDGER_USD_MICRO = 1_000_000_000_000    # $1,000,000/day
# The same ceiling in the unit the wire speaks. **It bounds a SPEND REPORT as
# well as a cap** (security-reviewer, PR #50): spend deliberately never refuses
# on the total, so an unbounded report is a denial of service on the admission
# gate rather than a bad row — one call drives a product's ledger past every
# threshold and refuses every other agent on that product until local midnight.
MAX_LEDGER_USD = MAX_LEDGER_USD_MICRO // 1_000_000

# The ceiling `report_ceiling_micro` applies to an uncapped ledger — one with
# no refusing cap set. See that function's docstring for why this is a flat
# constant rather than anything derived from the ledger's own history. $1,000:
# a real spend under this always records.
UNCAPPED_REPORT_CEILING_USD_MICRO = 1_000_000_000

# **HOW FAR ABOVE ITS OWN CEILING A SINGLE REPORT MAY GO BEFORE IT READS AS A
# MISTAKE RATHER THAN AS SPEND.**
#
# **WHAT THIS DOES NOT DO, STATED FIRST BECAUSE AN EARLIER VERSION OF THIS
# COMMENT CLAIMED IT DID** (security-reviewer, PR #50 round 3, on the bound this
# one replaces). It does NOT stop a caller drawing a product past a cap for the
# rest of the day, and no magnitude bound can: `_refuse_on_capped_ledger`
# refuses once the accumulated total reaches a threshold, a step function on
# whether the total has crossed the line rather than on how far past it a
# report goes. A report that itself crosses a cap is legitimate and expected —
# that is the ledger working, not this bound's business. Restricting who holds
# `ledger.spend`, or limiting how often it may be called, are the only shapes
# that would change that; both are out of scope here.
#
# **WHAT IT DOES DO**: a single call cannot write an implausible number into
# the store a cost query reads to judge what a model choice cost. Round 3
# talked an earlier version of this comment out of framing the bound as
# protecting the admission gate — a magnitude bound cannot do that, for the
# reason above — and the Bosun co (2026-09-02, the streaming writer that will
# hit this 422) is who made the actual case: with no caps required at all,
# nothing else in this service catches a report that is wrong by a factor of a
# thousand, and a silent error that size in a total a human trusts is worse
# than a visible refusal.
#
# **TEN TIMES, NOT ONE TIMES, AND THE SLACK IS THE WHOLE POINT.** An overrun is
# legitimate and expected — two runs bypassing checkout each spend against one
# cap, and this service must record that rather than lose it. A bound at 1x
# would refuse exactly those real overruns, which is the failure this ledger
# exists to avoid arriving from the other direction. At 10x a genuine overrun
# still records and a report orders of magnitude wrong does not.
#
# A report past the bound is REFUSED rather than recorded, which loses a real
# spend if one ever legitimately exceeds it. That trade is deliberate: a single
# spend ten times the ceiling it draws against means either the ceiling is
# badly wrong or something upstream is broken, and in both cases a human should
# look before the number lands. Its weak point is the same as before: nothing
# in this service yet routes the 422 to a human, and the justification rests on
# somebody seeing it — the Bosun's side answers this by treating the refusal as
# a fault in its own accounting rather than resubmitting.
MAX_REPORT_MULTIPLE_OF_CAP = 10


class DisproportionateSpend(ValueError):
    """A single report far out of proportion to the ceiling it draws against.

    Carries the numbers and what the ceiling was computed from, so the route
    can name what the report exceeded rather than leaving the caller to infer
    it.
    """

    def __init__(self, reported_micro: int, ceiling_micro: int, basis: str,
                basis_micro: int | None):
        super().__init__("this spend is out of all proportion to the ceiling "
                         "it draws on")
        self.reported_micro = reported_micro
        self.ceiling_micro = ceiling_micro
        # One of "soft_cap", "hard_cap", "uncapped" — which rule in
        # `report_ceiling_micro` produced the number above.
        self.basis = basis
        # The refusing cap the ceiling was computed from, or None for the
        # flat "uncapped" ceiling, which is computed from nothing.
        self.basis_micro = basis_micro


def report_ceiling_micro(ledger_row) -> tuple[int, str, int | None]:
    """The largest single report this ledger admits, and what that number was
    computed from: `(ceiling_micro, basis, basis_micro)`.

    **TAKES ONLY THE LEDGER ROW — no connection and no day.** It read both
    while the uncapped ceiling scaled with `ledger_days` history, and keeping
    them after that mechanism was removed would advertise a data dependency
    this function deliberately no longer has. A signature that still asked for
    a connection is an invitation to put a query back behind it.

    **WHERE A REFUSING CAP EXISTS — the soft cap if set, otherwise the hard
    cap — THE CEILING IS TEN TIMES IT.** The warning threshold is never the
    basis: it refuses nothing, so it bounds nothing.

    **WHERE NO REFUSING CAP EXISTS, OR WHERE IT IS ZERO, THE CEILING IS THE
    FLAT `UNCAPPED_REPORT_CEILING_USD_MICRO` CONSTANT** — not anything derived
    from the ledger's own spend history. An earlier version of this bound
    scaled the uncapped ceiling to ten times the median of the project's last
    30 days in `ledger_days`, and security review on this PR (#77) found that
    self-defeating: every row that median reads was written through
    `POST /ledger-entries` under `ledger.spend`, which every agent-role
    credential holds, so the credential the bound constrains could also write
    the evidence the bound measures itself against. A caller reporting near
    the floor on day one makes that day one's history; the ceiling roughly
    multiplies by ten per day and reaches `MAX_LEDGER_USD_MICRO` in under two
    weeks, and the poisoned history cannot self-correct because `ledger.void`
    is reserved to `approved_client`, not to the agent that inflated it.

    The capped case does not have this problem because its denominator is a
    number a human set through `PUT /ledgers`. A project no human has capped
    has no human-set number to scale against, so the bound falls back to one a
    human set in the code instead — visible in a diff, arguable in review, and
    not movable by any caller. What does not change from the scaled version:
    the bound is still about a number wrong on its face rather than about
    protecting the admission gate, an uncapped project still gets a real bound
    rather than dropping straight to `MAX_LEDGER_USD_MICRO`, and a genuine
    spend under the constant always records.

    `MAX_LEDGER_USD_MICRO` remains the absolute backstop above all of this.
    """
    soft = ledger_row["soft_cap_usd_micro"]
    hard = ledger_row["hard_cap_usd_micro"]
    if soft is not None:
        refusing, basis = soft, "soft_cap"
    elif hard is not None:
        refusing, basis = hard, "hard_cap"
    else:
        refusing, basis = None, None
    # A ZERO CAP HAS NO RATIO: `0 * 10` is `0`, which would refuse every
    # positive report against a deliberately frozen ledger. `refusing` being
    # falsy here covers both "no cap set" and "cap set to zero", and both fall
    # through to the flat uncapped ceiling rather than to a ceiling of zero
    # (security-reviewer, PR #50 round 3) — do not change this to `is not
    # None`, which would silently reintroduce that refusal.
    if refusing:
        return refusing * MAX_REPORT_MULTIPLE_OF_CAP, basis, refusing
    return UNCAPPED_REPORT_CEILING_USD_MICRO, "uncapped", None


def _day_shape_ok(day) -> bool:
    """`YYYY-MM-DD`, checked in Python as well as by the table's CHECK.

    Both, for `insert_signal`'s reason: the route gives the caller a readable
    400, and this stops the NEXT writer storing a shape the CHECK would have
    caught only as an opaque IntegrityError.
    """
    if not isinstance(day, str) or len(day) != 10:
        return False
    # `.isascii()` ALONGSIDE `.isdigit()`, so this and the table's CHECK agree on
    # what a digit is (security-reviewer, PR #50). `str.isdigit()` is True for
    # fullwidth digits and superscripts, which the migration's
    # `GLOB '[0-9]...'` rejects — a Python-level check that is LOOSER than the
    # database's turns a clean 400 into an opaque IntegrityError. Not
    # exploitable today, since `_within_a_day` parses with
    # `date.fromisoformat` inside a try/except and fails closed, but two
    # definitions of "digit" in one validation path is a disagreement waiting
    # to be reached from somewhere that does not.
    return (day[4] == "-" and day[7] == "-"
            and all(part.isascii() and part.isdigit()
                    for part in (day[:4], day[5:7], day[8:])))


def live_ledger(conn, scope_kind: str, scope_id: str):
    """The one live cap set for a scope, or None.

    "Or None" is not an error case and callers must not treat it as one: a scope
    with no ledger is UN-CAPPED, which most projects are expected to remain.
    Admission refuses on a capped ledger whose total has reached a refusing
    threshold, never on a missing one — refusing the uncapped would stop the
    whole fleet on the day this ships.
    """
    return conn.execute(
        "SELECT * FROM ledgers WHERE scope_kind = ? AND scope_id = ? "
        "AND state = 'live'", (scope_kind, scope_id)).fetchone()


def ledger_total(conn, ledger_id: str, day: str) -> int:
    """Micro-dollars spent on this ledger for this day. Zero when no
    `ledger_days` row exists yet.

    **Nothing subtracts anything.** Spend accumulates; this is the total this
    ledger has recorded for `day`, not a balance counting down from a cap.

    **THIS IS THE ENFORCEABLE TOTAL, NOT EVERY ENTRY'S TOTAL.** `ledger_days`
    is written by `record_spend` from `gateway`-lane and no-`lane` entries
    only — a `subscription`-lane entry never moves it (see `record_spend`'s
    docstring). That is also why this function needs no `lane` argument: the
    column it reads already excludes the one lane admission control must not
    act on. `ledger_lane_total` reads the entries directly for the one lane
    this function's total does not carry.
    """
    row = conn.execute(
        "SELECT spent_usd_micro FROM ledger_days WHERE ledger_id = ? AND day = ?",
        (ledger_id, day)).fetchone()
    return row["spent_usd_micro"] if row else 0


def ledger_lane_total(conn, ledger_id: str, day: str, lane: str) -> int:
    """Micro-dollars of unvoided spend on this ledger for this day whose
    `lane` column exactly matches `lane`. Zero when there is none.

    **Exact match — an entry with no `lane` is never counted here**, unlike
    `ledger_total`'s enforceable figure, which folds a no-`lane` entry into
    the `gateway` side on the reasoning that a spend nobody has tagged yet
    is not known to be the one lane that is not real money. This function
    answers a narrower question: what has this ledger recorded, today, that
    IS tagged `lane`. It reads `ledger_entries` directly rather than a
    materialized column, because nothing on the admission path calls it —
    `GET /ledgers` is the one caller, and that route is not latency-critical
    the way checkout is.
    """
    row = conn.execute(
        "SELECT COALESCE(SUM(usd_micro), 0) s FROM ledger_entries "
        "WHERE ledger_id = ? AND day = ? AND lane = ? AND voided_at IS NULL",
        (ledger_id, day, lane)).fetchone()
    return row["s"]


LEDGER_STATE_UNCAPPED = "uncapped"
LEDGER_STATE_OK = "ok"
LEDGER_STATE_WARNING = "warning"
LEDGER_STATE_SOFT_CAPPED = "soft_capped"
LEDGER_STATE_HARD_CAPPED = "hard_capped"


def ledger_state(ledger_row, total_micro: int) -> str:
    """Where `total_micro` sits against `ledger_row`'s cap set, as one of
    `uncapped`, `ok`, `warning`, `soft_capped`, `hard_capped`.

    **`uncapped` ONLY WHEN NO THRESHOLD IS SET AT ALL** — a ledger with, say,
    only a hard cap and no warn or soft cap is `ok` or `hard_capped`, never
    `uncapped`, because a threshold this row DOES carry still governs it.

    **COMPARED WITH `>=`, NOT `>`.** A total that has reached a threshold has
    consumed the allowance it names, and this is also what makes a cap of zero
    a deliberate freeze that refuses everything from the first dollar, matching
    the property the draw-down model's `headroom <= 0` had.

    **THE HIGHEST THRESHOLD CROSSED WINS.** `upsert_ledger` refuses a cap set
    whose present values are not non-decreasing in the order warn, soft, hard,
    so checking them in that order and letting a later match overwrite an
    earlier one always lands on the most severe state a total has reached.
    """
    warn = ledger_row["warn_usd_micro"]
    soft = ledger_row["soft_cap_usd_micro"]
    hard = ledger_row["hard_cap_usd_micro"]
    if warn is None and soft is None and hard is None:
        return LEDGER_STATE_UNCAPPED
    state = LEDGER_STATE_OK
    if warn is not None and total_micro >= warn:
        state = LEDGER_STATE_WARNING
    if soft is not None and total_micro >= soft:
        state = LEDGER_STATE_SOFT_CAPPED
    if hard is not None and total_micro >= hard:
        state = LEDGER_STATE_HARD_CAPPED
    return state


def _crossed_threshold(ledger_row, before_micro: int, after_micro: int) -> str | None:
    """Which of `warn`, `soft_cap`, `hard_cap` this entry newly crossed, or
    None.

    "Newly crossed" means the total was below the threshold before this entry
    and is at or above it after — an entry landing on an already-crossed
    threshold reports nothing, which is what keeps the signal `record_spend`
    writes from firing on every entry past the line rather than once. The
    highest threshold crossed wins, the same reasoning `ledger_state` uses and
    for the same reason: `upsert_ledger` enforces the three are non-decreasing,
    so checking in ascending order and overwriting lands on the most severe.
    """
    crossed = None
    for name, column in (("warn", "warn_usd_micro"),
                         ("soft_cap", "soft_cap_usd_micro"),
                         ("hard_cap", "hard_cap_usd_micro")):
        threshold = ledger_row[column]
        if threshold is not None and before_micro < threshold <= after_micro:
            crossed = name
    return crossed


def record_spend(row: dict) -> tuple[bool, dict, str | None]:
    """Append one spend entry and move the day's total, atomically.

    Returns `(idempotent, entry, crossed)` — `idempotent` True when
    `dedup_key` matched a row that already existed, in which case the total is
    NOT moved again and the stored row is returned unchanged. `crossed` is
    which threshold (`warn`, `soft_cap`, `hard_cap`) this entry newly crossed,
    or None — always None when idempotent, since nothing new happened.

    **`row["lane"]` MAY BE `None`, `"gateway"` OR `"subscription"`, and only
    `"subscription"` skips the day's total** (issue_01M35QG5NA13JE8FQVM9XET4HD).
    `ledger_total`'s docstring carries the reasoning; this is the write side
    of the same split — `None` moves the total exactly as it always has,
    because every entry this function has ever accepted before `lane` existed
    carried no lane and was real, enforceable spend.

    **THIS NEVER REFUSES ON A CAPPED LEDGER, AND THAT IS THE DESIGN.** The
    Keel's shape is explicit that a spend which has already happened cannot be
    refused: two runs bypassing checkout spend a multiple of one cap, and
    "streaming spend means the ledger reflects that overrun as it happens.
    What the Library cannot do is refuse it." Enforcement fires at the next
    CHECKOUT, against a ledger this function has already made accurate. A
    version that refused here would keep the ledger tidy by losing the record of
    the money, which is the one outcome that makes the next admission decision
    worse rather than better.

    **The entry and the total move in ONE transaction.** A total that can
    disagree with the entries behind it is a total nothing can audit, and the
    admission path reads the total rather than the entries precisely because
    summing them on every checkout would not scale.

    **WHAT ACTUALLY STOPS A DUPLICATE MOVING THE TOTAL IS THE ORDER OF THE TWO
    STATEMENTS, not the pre-check below and not the rollback.** The entry insert
    runs FIRST and carries the unique constraint, so a duplicate raises before
    the total's update is ever reached and there is nothing staged to undo.
    Established by mutation rather than by reading: disabling the pre-check
    leaves the tests green, and so does committing instead of rolling back in
    the `IntegrityError` path, because in both cases the total's update never
    ran.

    **Three mechanisms overlap here and any one of them is currently enough**,
    which is worth writing down so a later reader does not remove two of them on
    the grounds that the third is doing the work. Keep the insert first, keep
    the rollback, and keep the constraint;
    `test_the_total_always_equals_the_unvoided_entries_behind_it` asserts the
    outcome rather than any one mechanism, so it still fails if the last of
    them goes.

    The pre-check is a cheaper path to the same answer for the ordinary repeat,
    and `BEGIN IMMEDIATE` is `lease.py`'s reasoning applied here: it is the
    check-then-act that needs the lock. Both are worth keeping and neither is
    the guarantee.
    """
    if row["source"] not in LEDGER_SOURCES:
        raise ValueError(
            f"source {row['source']!r} is not one of {sorted(LEDGER_SOURCES)}")
    lane = row.get("lane")
    if lane is not None and lane not in LEDGER_LANES:
        raise ValueError(
            f"lane {lane!r} is not one of {sorted(LEDGER_LANES)}, or null")
    if not _day_shape_ok(row["day"]):
        raise ValueError(f"day {row['day']!r} is not YYYY-MM-DD")
    if not isinstance(row["usd_micro"], int) or isinstance(row["usd_micro"], bool):
        raise ValueError("usd_micro must be an integer number of millionths")
    if not 0 <= row["usd_micro"] <= MAX_LEDGER_USD_MICRO:
        # BOUNDED HERE TOO, not only at the route. The route gives the caller a
        # readable 400; this is what stops the NEXT writer overflowing the
        # column, which arrives as a 500 from deep inside the INSERT rather
        # than as a refusal (security-reviewer, PR #50).
        raise ValueError(
            f"usd_micro must be between 0 and {MAX_LEDGER_USD_MICRO}")
    # THE CALLER-SUPPLIED STRINGS ARE BOUNDED, reusing the caps this module
    # already defines rather than inventing new ones (reviewer, PR #50). A
    # SQLite column's `String(100)` is type affinity and enforces no length at
    # all, so without this an agent-role credential writes an arbitrarily large
    # string into any of these columns.
    for field, cap in (("run_id", MAX_ACTOR), ("agent", MAX_IDENT),
                       ("model", MAX_IDENT), ("item_id", MAX_IDENT),
                       ("person", MAX_ACTOR), ("team", MAX_IDENT),
                       ("dedup_key", MAX_DEDUP_KEY)):
        value = row.get(field)
        if value is not None and len(value) > cap:
            raise ValueError(f"{field} is longer than {cap} characters")

    conn = connect_rw()
    try:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        dedup_key = row.get("dedup_key")
        if dedup_key:
            existing = conn.execute(
                "SELECT * FROM ledger_entries WHERE dedup_key = ?",
                (dedup_key,)).fetchone()
            if existing is not None:
                conn.rollback()
                return True, dict(existing), None
        # **THE PROPORTIONATE BOUND IS CHECKED HERE, INSIDE THE TRANSACTION AND
        # AFTER THE DEDUP SHORT-CIRCUIT ABOVE** (reviewer, PR #50 round 3).
        #
        # It used to run in the route, before this function was called, and that
        # made an IDEMPOTENT REPLAY able to flip from 200 to 422: report $5,000
        # against a $1,000 cap (admitted, ceiling $10,000), lower the cap to
        # $100, then replay the identical request — the bound is now $1,000 and
        # the caller is told its report was rejected, when in fact that exact
        # entry is already recorded and unchanged. Reproduced before fixing.
        #
        # No money moved either way, but the caller was told the opposite of the
        # truth, which is this module's "a spend already incurred is never
        # dropped" contract broken from the reporting side. A replay must answer
        # with the row that exists, whatever the cap set has done since.
        #
        # Reading the ledger row in the same transaction as the insert is the
        # smaller second benefit: the bound and the write now see one
        # consistent state.
        ledger_row = conn.execute(
            "SELECT * FROM ledgers WHERE id = ?", (row["ledger_id"],)).fetchone()
        # THE TOTAL BEFORE THIS ENTRY, read in the same transaction so the
        # crossing computed below is judged against a total this entry has not
        # yet moved. `ledger_total` reads the same row the write below updates.
        before_micro = ledger_total(conn, row["ledger_id"], row["day"])
        if ledger_row is not None:
            ceiling, basis, basis_micro = report_ceiling_micro(ledger_row)
            if row["usd_micro"] > ceiling:
                conn.rollback()
                raise DisproportionateSpend(
                    row["usd_micro"], ceiling, basis, basis_micro)
        # THE EPIC, DERIVED HERE FROM `item_id` — never accepted from the
        # caller. Read on the same connection and inside the same transaction
        # as the insert below, so a spend and the epic it is filed under
        # cannot disagree (the same reasoning `ledger_row` above is read here
        # rather than by a separate call). The four-way rule is spelled out in
        # migration `d12fde7d50e9`'s docstring; a dangling `item_id` derives
        # nothing rather than refusing the spend, matching this function's own
        # "a spend already incurred is never refused."
        epic_id = None
        item_id = row.get("item_id")
        if item_id:
            # Constrained to the entry's own product: a legitimate parent is
            # always same-product with its child (`_validate_parent` enforces
            # this at create and re-parent), so an item in another product is
            # never a legitimate one, and deriving NULL for it — the same
            # answer a missing id gets — keeps an out-of-scope item id
            # indistinguishable from a nonexistent one (security review,
            # PR #167 round 1).
            item = conn.execute(
                "SELECT type, parent_id FROM issue_items "
                "WHERE id = ? AND product = ?",
                (item_id, row["product"])).fetchone()
            if item is not None:
                if item["type"] == ITEM_TYPE_EPIC:
                    epic_id = item_id
                else:
                    # A non-epic's `parent_id` IS the epic dimension: an
                    # epic's own id for a sub-item, or a corrections queue id
                    # for a correction (`library/specs/one-home-per-work-
                    # item`, PR 2 — every non-epic row carries one or the
                    # other once this PR's backfill has run). `None` here
                    # names a row the backfill could not reach — a direct
                    # insert outside `insert_item`, surfaced by `GET
                    # /items?home=invalid` — and derives nothing, the same
                    # answer a dangling or missing `item_id` gets.
                    epic_id = item["parent_id"]
        now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
        now_iso = now.isoformat(" ")
        entry_id = f"lde_{new_ulid()}"
        # **WHETHER THIS ENTRY MOVES `ledger_days` AT ALL.** `subscription` is
        # the one lane that is not money a budget can be enforced against —
        # `ledger_total`'s docstring is the other half of this split. An
        # entry with no `lane` moves it, matching today's behaviour for every
        # client that has not started sending one.
        enforceable = lane != "subscription"
        try:
            conn.execute(
                "INSERT INTO ledger_entries "
                "(id, ledger_id, day, usd_micro, at, ingested_at, source, "
                " product, run_id, agent, model, item_id, epic_id, person, "
                " team, reported_by, dedup_key, lane) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (entry_id, row["ledger_id"], row["day"], row["usd_micro"],
                 row.get("at") or now_iso, now_iso, row["source"],
                 row["product"], row.get("run_id"), row.get("agent"),
                 row.get("model"), item_id, epic_id, row.get("person"),
                 row.get("team"), row["reported_by"], dedup_key, lane))
            # LAZY, so nothing has to create a day's rows ahead of time and a
            # quiet day costs no row at all. The `+ excluded` form is what keeps
            # the increment inside the statement rather than in a read-then-write
            # this function would have to lock around a second time.
            #
            # SKIPPED FOR A SUBSCRIPTION-LANE ENTRY: this table is the
            # enforceable total, and a subscription estimate must never move
            # the number admission control refuses on.
            if enforceable:
                conn.execute(
                    "INSERT INTO ledger_days (ledger_id, day, spent_usd_micro, updated_at) "
                    "VALUES (?,?,?,?) "
                    "ON CONFLICT (ledger_id, day) DO UPDATE SET "
                    "  spent_usd_micro = spent_usd_micro + excluded.spent_usd_micro, "
                    "  updated_at = excluded.updated_at",
                    (row["ledger_id"], row["day"], row["usd_micro"], now_iso))
        except sqlite3.IntegrityError:
            # THE RACE THE PRE-CHECK CANNOT CATCH: another writer committed the
            # same dedup_key between our BEGIN and our INSERT. Roll the whole
            # transaction back — which discards our total's update too, the
            # reason the two statements are in one transaction — and answer with
            # the row that won, exactly as the pre-check would have.
            conn.rollback()
            if not dedup_key:
                raise
            won = conn.execute(
                "SELECT * FROM ledger_entries WHERE dedup_key = ?",
                (dedup_key,)).fetchone()
            if won is None:
                raise
            return True, dict(won), None
        conn.commit()
        # `after_micro` MATCHES WHAT WAS ACTUALLY WRITTEN ABOVE, not simply
        # "before plus this report": a subscription-lane entry left
        # `ledger_days` untouched, and crediting it here would report a
        # crossing that never happened to the number admission control reads.
        after_micro = before_micro + row["usd_micro"] if enforceable else before_micro
        crossed = (_crossed_threshold(ledger_row, before_micro, after_micro)
                  if ledger_row is not None else None)
        stored = conn.execute(
            "SELECT * FROM ledger_entries WHERE id = ?", (entry_id,)).fetchone()
        return False, dict(stored), crossed
    finally:
        conn.close()


def ledger_entry_dict(row: sqlite3.Row) -> dict:
    """One spend entry in protocol shape, for `GET /ledger-entries`.

    **BUILT FROM A NAMED FIELD LIST, so a column added to `ledger_entries` later
    does not reach a client by default** — the same discipline `signal_dict` and
    `item_dict` hold to, over the same kind of `SELECT *` row.

    **`person`, `team` and `dedup_key` are withheld, and they are the only
    three.** `person` and `team` are the deferred person axis (ADR-0021):
    nullable columns nothing populates today, kept so the record is not lost if
    a second operator ever exists — but who may read a person identifier has not
    been decided, and serving them now would decide it by accident. `dedup_key`
    is a write-side idempotency key that can encode a transcript tuple
    (`run_id, message_id, request_id`); a reader has no use for it and narrower
    is the reversible direction.

    **`usd`, not `usd_micro`** — dollars on the wire, matching `GET /ledgers`
    and `POST /ledger-entries`.

    **`voided_at` is served, and it is the one addition.** A reader that
    cannot tell a voided entry from a live one cannot reconcile the day's
    total against the entries behind it — the same invariant
    `test_the_total_always_equals_the_unvoided_entries_behind_it` checks from the
    write side. `voided_by` is not served, for `person`'s reason: who voided
    an entry is the same undecided disclosure question ADR-0021 has not
    settled, and narrower is the reversible direction.

    **`lane` is served, and may be `null`.** `GET /ledgers` is where a reader
    sees a figure broken out by lane; this is where a reader sees which lane
    each entry behind that figure actually carries, including an entry
    nobody has tagged yet (issue_01M35QG5NA13JE8FQVM9XET4HD).
    """
    return {
        "id": row["id"],
        "ledger_id": row["ledger_id"],
        "day": row["day"],
        "usd": row["usd_micro"] / 1_000_000,
        "at": _iso(row["at"]),
        "ingested_at": _iso(row["ingested_at"]),
        "source": row["source"],
        "lane": row["lane"],
        "product": row["product"],
        "run_id": row["run_id"],
        "agent": row["agent"],
        "model": row["model"],
        "item_id": row["item_id"],
        "epic_id": row["epic_id"],
        "reported_by": row["reported_by"],
        "voided_at": _iso(row["voided_at"]),
    }


MAX_VOID_REASON = 500


def upsert_ledger(row: dict) -> dict:
    """Create or re-set the live cap set for a scope. Returns the stored row.

    **Overwrites the live cap set in place rather than appending a second
    one.** The partial unique index permits exactly one live ledger per scope,
    because two live cap sets for one project is an ambiguity admission
    control cannot resolve — it would have to pick one, and either choice is
    wrong half the time.

    Raising or lowering a cap mid-day therefore takes effect immediately and
    does not reset the day's spend: `ledger_days` is keyed on the ledger, so
    the total already recorded stays where it is and only the thresholds move.
    That is the behaviour an operator raising a cap to unblock a stuck run
    wants, and the opposite behaviour — a raise that forgives the morning's
    spend — would make the cap unenforceable by repeated re-setting.

    **Each present cap is validated independently** — `None` means that
    threshold is not set and is left alone, a non-negative integer no greater
    than `MAX_LEDGER_USD_MICRO` is accepted, anything else is refused.

    **The present caps must be non-decreasing in the order warn, soft, hard.**
    A hard cap below a soft cap would revoke a project's runs before admission
    ever refused it — the enforcement that is supposed to happen first would
    never fire — so a set violating this is refused with a readable error
    rather than stored and left to produce that nonsense the first time a
    total crosses it.

    **`per_run_cap_usd_micro` is validated the same way and excluded from
    that ordering.** It bounds one run's own spend, not the project's
    accumulating total, so it has no place among warn/soft/hard and is not
    compared against any of them (issue_01M4BDRV8C47NQN5X8T82Y2B9A). It
    changes no ledger state and is read only by `agentctl`, through the
    checkout response's `budget.cap_usd`.
    """
    if row["scope_kind"] not in LEDGER_SCOPES:
        raise ValueError(
            f"scope_kind {row['scope_kind']!r} is not one of {sorted(LEDGER_SCOPES)}")
    caps = {}
    for field in ("warn_usd_micro", "soft_cap_usd_micro", "hard_cap_usd_micro",
                  "per_run_cap_usd_micro"):
        value = row.get(field)
        if value is None:
            caps[field] = None
            continue
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"{field} must be a non-negative integer or null")
        if value > MAX_LEDGER_USD_MICRO:
            raise ValueError(f"{field} exceeds {MAX_LEDGER_USD_MICRO}")
        caps[field] = value
    present = [caps[f] for f in
              ("warn_usd_micro", "soft_cap_usd_micro", "hard_cap_usd_micro")
              if caps[f] is not None]
    if any(a > b for a, b in itertools.pairwise(present)):
        raise ValueError(
            "caps must be non-decreasing: warn_usd_micro <= soft_cap_usd_micro "
            "<= hard_cap_usd_micro, among whichever are set")

    conn = connect_rw()
    try:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        now_iso = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None).isoformat(" ")
        existing = conn.execute(
            "SELECT id FROM ledgers WHERE scope_kind = ? AND scope_id = ? "
            "AND state = 'live'",
            (row["scope_kind"], row["scope_id"])).fetchone()
        if existing is None:
            ledger_id = f"ldg_{new_ulid()}"
            conn.execute(
                "INSERT INTO ledgers (id, scope_kind, scope_id, warn_usd_micro, "
                " soft_cap_usd_micro, hard_cap_usd_micro, per_run_cap_usd_micro, "
                " state, granted_by, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,'live',?,?,?)",
                (ledger_id, row["scope_kind"], row["scope_id"],
                 caps["warn_usd_micro"], caps["soft_cap_usd_micro"],
                 caps["hard_cap_usd_micro"], caps["per_run_cap_usd_micro"],
                 row["granted_by"], now_iso, now_iso))
        else:
            ledger_id = existing["id"]
            conn.execute(
                "UPDATE ledgers SET warn_usd_micro = ?, soft_cap_usd_micro = ?, "
                "hard_cap_usd_micro = ?, per_run_cap_usd_micro = ?, "
                "granted_by = ?, updated_at = ? "
                "WHERE id = ?",
                (caps["warn_usd_micro"], caps["soft_cap_usd_micro"],
                 caps["hard_cap_usd_micro"], caps["per_run_cap_usd_micro"],
                 row["granted_by"], now_iso, ledger_id))
        conn.commit()
        return dict(conn.execute(
            "SELECT * FROM ledgers WHERE id = ?", (ledger_id,)).fetchone())
    finally:
        conn.close()


# `granted_by` ON A LEDGER `ensure_live_ledger` CREATES, below — never a human
# name, so it can never collide with one. A ledger `upsert_ledger` creates
# always carries `identity.attested_human`'s resolved value because setting a
# cap is a human act; a ledger `ensure_live_ledger` creates has no cap and no
# human behind it, and the column is NOT NULL regardless.
LEDGER_AUTO_GRANTED = "system:recorded-on-first-report"


def ensure_live_ledger(scope_kind: str, scope_id: str) -> dict:
    """The live ledger for this scope, creating an uncapped one (all three
    caps null) if none exists yet. Returns the stored row.

    **For `POST /ledger-entries` alone, after `live_ledger` has already
    answered None.** `live_ledger` itself must keep answering None for a
    scope nobody has touched — admission's "a missing ledger is not a
    refusal" and `GET /ledgers`' listing both depend on that, and neither
    should materialize a row just by reading. Recording a spend is the one
    caller with something to write, since the alternative is losing that
    project's spend the way the route used to.

    **Race-safe the same way `record_spend`'s dedup is**: the pre-check
    is the cheap path for the ordinary case, and `uq_ledgers_one_live_per_scope`
    is what actually stops two live ledgers for one scope — including the
    scope's FIRST human-set cap landing, through `upsert_ledger`, in the
    middle of this function's own transaction. Either writer commits first and
    the other's `IntegrityError` falls back to a re-select, so this never
    raises into a spend report and never produces a second live row.
    """
    if scope_kind not in LEDGER_SCOPES:
        raise ValueError(
            f"scope_kind {scope_kind!r} is not one of {sorted(LEDGER_SCOPES)}")
    conn = connect_rw()
    try:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM ledgers WHERE scope_kind = ? AND scope_id = ? "
            "AND state = 'live'", (scope_kind, scope_id)).fetchone()
        if existing is not None:
            conn.rollback()
            return dict(existing)
        now_iso = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None).isoformat(" ")
        ledger_id = f"ldg_{new_ulid()}"
        try:
            conn.execute(
                "INSERT INTO ledgers (id, scope_kind, scope_id, warn_usd_micro, "
                " soft_cap_usd_micro, hard_cap_usd_micro, per_run_cap_usd_micro, "
                " state, granted_by, created_at, updated_at) "
                "VALUES (?,?,?,NULL,NULL,NULL,NULL,'live',?,?,?)",
                (ledger_id, scope_kind, scope_id, LEDGER_AUTO_GRANTED,
                 now_iso, now_iso))
        except sqlite3.IntegrityError:
            conn.rollback()
            won = conn.execute(
                "SELECT * FROM ledgers WHERE scope_kind = ? AND scope_id = ? "
                "AND state = 'live'", (scope_kind, scope_id)).fetchone()
            if won is None:
                raise
            return dict(won)
        conn.commit()
        return dict(conn.execute(
            "SELECT * FROM ledgers WHERE id = ?", (ledger_id,)).fetchone())
    finally:
        conn.close()


def void_spend_entry(entry_id: str, voided_by: str,
                     reason: str | None) -> tuple[bool, dict]:
    """Void one spend entry, correcting the day's total in the same
    transaction. Returns `(already_voided, entry)`.

    **Idempotent, the shape `record_spend` already answers a duplicate
    `dedup_key` with**: voiding an already-voided entry returns the row
    unchanged and subtracts nothing, rather than double-reversing it.

    **The invariant this preserves is the one `record_spend` protects on the
    way in, mirrored on the way out**: `ledger_days.spent_usd_micro` keeps
    equaling the sum of the ENFORCEABLE entries behind it (not `subscription`,
    see `record_spend`'s docstring) that are NOT voided. A `subscription`-lane
    entry never joined that total, so voiding one leaves it untouched rather
    than subtracting an amount it never added. The entry stays in the table
    rather than being deleted — see the migration's module docstring for why
    the log is append-only and a void is a mark rather than a removal.

    Raises `LookupError` for an entry id that does not exist; the route turns
    that into a 404 rather than the 200-with-a-lie an unconditioned UPDATE
    would produce.
    """
    if len(voided_by) > MAX_ACTOR:
        raise ValueError(f"voided_by is longer than {MAX_ACTOR} characters")
    if reason is not None and len(reason) > MAX_VOID_REASON:
        raise ValueError(f"reason is longer than {MAX_VOID_REASON} characters")
    conn = connect_rw()
    try:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        entry = conn.execute(
            "SELECT * FROM ledger_entries WHERE id = ?", (entry_id,)).fetchone()
        if entry is None:
            conn.rollback()
            raise LookupError(f"no ledger entry {entry_id!r}")
        if entry["voided_at"] is not None:
            conn.rollback()
            return True, dict(entry)
        now_iso = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None).isoformat(" ")
        conn.execute(
            "UPDATE ledger_entries SET voided_at = ?, voided_by = ?, "
            "void_reason = ? WHERE id = ?",
            (now_iso, voided_by, reason, entry_id))
        # MIRRORS `record_spend`'s OWN ENFORCEABLE CHECK: a subscription-lane
        # entry never added itself to `ledger_days`, so voiding it must not
        # subtract from a total it never joined.
        if entry["lane"] != "subscription":
            conn.execute(
                "UPDATE ledger_days SET spent_usd_micro = spent_usd_micro - ?, "
                "updated_at = ? WHERE ledger_id = ? AND day = ?",
                (entry["usd_micro"], now_iso, entry["ledger_id"], entry["day"]))
        conn.commit()
        stored = conn.execute(
            "SELECT * FROM ledger_entries WHERE id = ?", (entry_id,)).fetchone()
        return False, dict(stored)
    finally:
        conn.close()
