#!/usr/bin/env python3.11
"""init_vectors — the install-time command that creates the vector index's
own database file.

**On a fresh install there is nothing that creates `LIBRARY_VECTORS_DB`.**
`library_vectors` and `signal_vectors` live in their own file, outside
`LIBRARY_DB` (memory-retention epic clause 7, issue_01M35R5GA1SKRG2CMQRF189NDE),
attached at connection time by `vectors._attach_vectors_db` — a build that
never ran this against a fresh path has no vectors database to attach, and
`vectors.available` reports `no vectors database at ...` (the same shape of
gap issue_01M3JMHPN98Z3WD8GSSC841VM7 found for the old single-file design,
now against the file this creates rather than a table inside `LIBRARY_DB`).

**Run once, independent of `alembic` having run against `LIBRARY_DB`** — the
vec0 schema references no ordinary table, so nothing about this needs
`alembic upgrade head` to have created one first:

    LIBRARY_VECTORS_DB=<path> LIBRARY_DB=<path> .venv/bin/python init_vectors.py

**`LIBRARY_DB` still has to be SET, even to a path with nothing at it
yet** — `init` imports `vectors`, which imports `store`, whose own
module-level check requires `LIBRARY_DB` or `ORIENT_CONTROL_DB` to name
something, the same requirement every process in this service starts
under. It does not have to exist or be migrated: `store`'s check only
refuses a path that DOES exist and turns out to hold one of Orient's
tables.

**Not the app's own first-start path**, for the reason it never was:
`vectors.connect` never asks for a writable handle, so a missing file there
is a search that fails forever rather than something downstream would
create. **Not alembic**, because its connection does not load `sqlite-vec`
(`alembic/README`) — a vec0 virtual table is invisible to a connection that
has not loaded the extension, so a migration that tried this would raise
`no such module: vec0` on the very statement meant to create it.

**Idempotent, and a no-op on a database that already holds both tables** —
`CREATE VIRTUAL TABLE IF NOT EXISTS`, so a second run finds them present and
writes nothing.

**Creates the file if it does not exist, and that is the one place in this
service allowed to.** `vectors.connect` / `connect_rw` refuse a missing
`LIBRARY_VECTORS_DB` rather than create an empty one — clause 7's whole
point — so something has to be the exception, on a path a human or a deploy
script chose deliberately by running this command.

**Refuses when `LIBRARY_VECTORS_DB` names the same file as `LIBRARY_DB`.**
Creating the vec0 tables back inside the main database would silently undo
the split this command exists to set up. `LIBRARY_DB` is read directly from
the environment for this comparison rather than through `store` — importing
`store` resolves `LIBRARY_DB` itself and can refuse for reasons that have
nothing to do with this check, and already ran by the time this runs (see
above). Skipped, not refused, when `LIBRARY_DB` is unset and
`ORIENT_CONTROL_DB` is what satisfied `store`'s own check instead — the
read-only shadow's own configuration, where there is no `LIBRARY_DB` path
to compare against.

**Refuses when `LIBRARY_VECTORS_DB` names Orient's control database, or a
copy of it, whatever its path.** The check above only catches it duplicating
`LIBRARY_DB`; it says nothing about a path that has never been compared to
`LIBRARY_DB` at all, and `store`'s own import-time guard resolves
`LIBRARY_DB`, not `LIBRARY_VECTORS_DB`, so it does not see this file either.
`connect_rw` checks the open handle against `store`'s own fingerprint
(`store.ORIENT_FINGERPRINT_SQL` / `store.ORIENT_ONLY_TABLES`) before writing
anything — the same check `store.connect_rw` runs, reused rather than
reimplemented, and the one check ADR-0020 exists to make unbypassable stays
in place even though this module's other checks are its own.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path


class Refused(RuntimeError):
    """The command cannot proceed and nothing was written."""


def _vectors_db() -> Path:
    raw = os.environ.get("LIBRARY_VECTORS_DB")
    if not raw:
        raise Refused(
            "LIBRARY_VECTORS_DB is not set. Set it to the vector index's own "
            "database file — a fresh path this command will create, or an "
            "existing one split_vectors.py's migration already produced.")
    return Path(raw).expanduser()


def _refuse_same_as_library_db(vectors_path: Path) -> None:
    raw = os.environ.get("LIBRARY_DB")
    if not raw:
        return
    library_db = Path(raw).expanduser()
    if vectors_path.resolve() == library_db.resolve():
        raise Refused(
            f"LIBRARY_VECTORS_DB names {vectors_path}, the same file as "
            f"LIBRARY_DB. The vector index lives in its own file, outside "
            f"the vault database — creating its tables here would put them "
            f"straight back where the split moves them away from.")
    if (vectors_path.exists() and library_db.exists()
            and os.path.samestat(vectors_path.stat(), library_db.stat())):
        raise Refused(
            f"LIBRARY_VECTORS_DB names {vectors_path}, the same file as "
            f"LIBRARY_DB ({library_db}) by device and inode.")


def connect_rw(path: Path) -> sqlite3.Connection:
    """A writable connection with `sqlite-vec` loaded, creating `path` if it
    does not exist yet, refused if it turns out to be open on Orient's
    control database.

    A plain `sqlite3.connect` on a path with nothing at the end of it creates
    an empty file — the behaviour every other writable connection in this
    service goes out of its way to refuse, and the one this command exists to
    be the exception for: a fresh install has no vectors database until this
    runs, and this is what makes that file.

    **Checked against `store`'s own fingerprint, the same check
    `store.connect_rw` and `alembic/env.py`'s `refuse_unless_ours` both run:
    `store.ORIENT_FINGERPRINT_SQL` against `store.ORIENT_ONLY_TABLES`, asked
    of the OPEN HANDLE.** `_refuse_same_as_library_db` above only catches
    `LIBRARY_VECTORS_DB` naming the same file as `LIBRARY_DB`; it says
    nothing about `LIBRARY_VECTORS_DB` naming Orient's control database
    directly, at a path that has nothing to do with `LIBRARY_DB` at all — and
    `store`'s own import-time guard never sees this path either, since it
    resolves `LIBRARY_DB`, not `LIBRARY_VECTORS_DB`. Without this, a fresh
    install pointed at Orient's control database by mistake would create the
    vec0 tables inside it and write to it as a second writer, the exact
    failure ADR-0020 exists to prevent.

    **Deliberately the schema fingerprint alone, not also `LIBRARY_DB`'s
    path/device-inode half (`store._refuse_orients_path`).** The schema check
    is conclusive on its own for a one-shot CLI tool that opens the file once
    and exits; the path check would only save one open on the refusal path,
    which is not worth the asymmetry with a check this module already runs
    against the live handle (round 4 style note).

    `sqlite-vec` is loaded the same way every other writable handle in this
    service loads it: switched on only for the load itself, so nothing later
    in the process can pull in an arbitrary extension through this handle.

    Closed on any failure between `sqlite3.connect` and the return, matching
    `store._refuse_orients_handle`'s discipline: the caller here never
    receives a handle that raised getting ready.
    """
    import sqlite_vec
    import store

    conn = sqlite3.connect(str(path))
    try:
        found = conn.execute(
            store.ORIENT_FINGERPRINT_SQL, store.ORIENT_ONLY_TABLES).fetchone()
        if found is not None:
            raise Refused(
                f"{path} holds {found[0]!r} — a table that stays with "
                f"Orient, so this is Orient's control database, or a copy "
                f"of it, rather than the vector index's own file. Refusing "
                f"the write: a second writer to that file is the failure "
                f"ADR-0020 exists to prevent.")
        conn.enable_load_extension(True)
        try:
            sqlite_vec.load(conn)
        finally:
            conn.enable_load_extension(False)
    except BaseException:
        conn.close()
        raise
    return conn


def init(path: Path) -> list[tuple[str, bool]]:
    """Create whichever of the two vec0 tables are missing. Returns
    `[(table, created)]` for both, in fixed order — `created` is False for
    a table this call found already present.

    Both statements run inside one transaction: a table this call creates and
    then fails to create the other beside stays uncommitted, so a retry after
    a failure sees neither rather than one orphaned table.
    """
    import vectors

    tables = ((vectors.VEC_TABLE, vectors.VEC_TABLE_DDL),
              (vectors.SIGNAL_VEC_TABLE, vectors.SIGNAL_VEC_TABLE_DDL))
    conn = connect_rw(path)
    try:
        present = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "(?, ?)", tuple(name for name, _ in tables))}
        conn.execute("BEGIN")
        for name, ddl in tables:
            conn.execute(ddl)
        conn.commit()
        return [(name, name not in present) for name, _ in tables]
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def main(argv=None) -> int:
    del argv
    try:
        path = _vectors_db()
        _refuse_same_as_library_db(path)
        results = init(path)
    except RuntimeError as e:
        print(f"init_vectors: {e}", file=sys.stderr)
        return 1
    except sqlite3.Error as e:
        print(f"init_vectors: {e}", file=sys.stderr)
        return 1

    for name, created in results:
        print(f"{name}: {'created' if created else 'already present'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
