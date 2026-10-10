#!/usr/bin/env python3.11
"""init_cold — the install-time command that creates the cold tier's own
database file.

**On a fresh install there is nothing that creates `LIBRARY_COLD_DB`.**
Signals move there once they pass their kind's hot window
(memory-retention epic clause 9, issue_01M35R5QKX7QDCM822K3J18SWX), outside
`LIBRARY_DB`, attached on demand by `store.attach_cold` — a build that never
ran this against a fresh path has no cold database to attach, and a read
path that attaches it on a hot miss gets `FileNotFoundError`.

**The schema is REPLAYED FROM `main.signals`'S OWN `sqlite_master`, not a
DDL constant this file carries** (library/specs/archives-cold-tier.md §2) —
the opposite of `init_vectors.py`'s vec0 tables, which have no prior schema
to drift from. `signals` has been altered once already (`67e87ed76f03`,
`acting_human`), so a second DDL copy here would be a second definition of
that schema, free to drift from whatever `alembic upgrade head` actually
produced. Replaying `main`'s own `CREATE TABLE` and `CREATE INDEX`
statements verbatim means the cold file's schema is, by construction,
whatever the live one is on the day this runs — never a stale copy.

**Run once, after `alembic upgrade head` against `LIBRARY_DB`** — there has
to be a `signals` table in `main` for this to copy from:

    LIBRARY_COLD_DB=<path> LIBRARY_DB=<path> .venv/bin/python init_cold.py

**Idempotent, and a no-op on a cold file that already holds a `signals`
table** — this does not re-check that table's columns against `main`'s
current ones; a migration that alters `signals` without also updating the
cold file is caught by `tests/test_init_cold.py`'s schema-parity test, run
before merge, not by this command at deploy time.

**Creates the file if it does not exist, and that is the one place in this
service allowed to** — `store.attach_cold` refuses a missing `LIBRARY_COLD_DB`
rather than create an empty one, clause 9's whole point, so something has to
be the exception: a path a human or a deploy script chose deliberately by
running this command.

**Refused when `LIBRARY_COLD_DB` names Orient's control database, or a copy
of it, whatever its path** — the same check `init_vectors.py` runs against
the OPEN HANDLE (`store.ORIENT_FINGERPRINT_SQL` / `store.ORIENT_ONLY_TABLES`)
before writing anything, reused rather than reimplemented. Without it, a
fresh install pointed at Orient's control database by mistake would copy
`signals`' schema into it and write to it as a second writer — the failure
ADR-0020 exists to prevent.

**Deliberately NO refusal for `LIBRARY_COLD_DB` naming the same file as
`LIBRARY_DB`** (archives-cold-tier.md, "Considered and not added") — that
misconfiguration was weighed and declined: there is no occurrence of it, and
a reviewer who can name one should add the check by reusing
`init_vectors.py`'s equivalent function.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path


class Refused(RuntimeError):
    """The command cannot proceed and nothing was written."""


def _cold_db() -> Path:
    raw = os.environ.get("LIBRARY_COLD_DB")
    if not raw:
        raise Refused(
            "LIBRARY_COLD_DB is not set. Set it to the cold tier's own "
            "database file — a fresh path this command will create.")
    return Path(raw).expanduser()


def _main_signals_ddl() -> list[str]:
    """`main.signals`'s own `CREATE TABLE` and `CREATE INDEX` statements,
    read from `LIBRARY_DB`'s `sqlite_master` rather than carried as a
    constant — see the module docstring's "REPLAYED" note for why."""
    import store

    conn = sqlite3.connect(store.db_uri(store.DB_PATH, readonly=True), uri=True)
    try:
        rows = conn.execute(
            "SELECT sql FROM sqlite_master WHERE tbl_name = 'signals' "
            "AND sql IS NOT NULL ORDER BY (type = 'table') DESC").fetchall()
    finally:
        conn.close()
    if not rows:
        raise Refused(
            f"{store.DB_PATH} has no `signals` table to replay. Run "
            f"`alembic upgrade head` against LIBRARY_DB first.")
    return [r[0] for r in rows]


def connect_rw(path: Path) -> sqlite3.Connection:
    """A writable connection, creating `path` if it does not exist yet,
    refused if it turns out to be open on Orient's control database.

    **Checked against `store`'s own fingerprint, the same check
    `init_vectors.connect_rw` and `store.connect_rw` both run:
    `store.ORIENT_FINGERPRINT_SQL` against `store.ORIENT_ONLY_TABLES`, asked
    of the OPEN HANDLE** — a fresh install pointed at Orient's control
    database by mistake would otherwise copy `signals`' schema into it and
    write to it as a second writer, the exact failure ADR-0020 exists to
    prevent.

    Closed on any failure between `sqlite3.connect` and the return, matching
    `store._refuse_orients_handle`'s discipline.
    """
    import store

    conn = sqlite3.connect(str(path))
    try:
        found = conn.execute(
            store.ORIENT_FINGERPRINT_SQL, store.ORIENT_ONLY_TABLES).fetchone()
        if found is not None:
            raise Refused(
                f"{path} holds {found[0]!r} — a table that stays with "
                f"Orient, so this is Orient's control database, or a copy "
                f"of it, rather than the cold tier's own file. Refusing "
                f"the write: a second writer to that file is the failure "
                f"ADR-0020 exists to prevent.")
    except BaseException:
        conn.close()
        raise
    return conn


def init(path: Path) -> bool:
    """Create `signals` in `path`, replaying `main`'s own DDL, unless it is
    already there. Returns whether this call created it."""
    conn = connect_rw(path)
    try:
        present = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND "
            "name = 'signals'").fetchone()
        if present is not None:
            return False
        statements = _main_signals_ddl()
        conn.execute("BEGIN")
        for statement in statements:
            conn.execute(statement)
        conn.commit()
        return True
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def main(argv=None) -> int:
    del argv
    try:
        path = _cold_db()
        created = init(path)
    except Refused as e:
        print(f"init_cold: {e}", file=sys.stderr)
        return 1
    except sqlite3.Error as e:
        print(f"init_cold: {e}", file=sys.stderr)
        return 1

    print(f"signals: {'created' if created else 'already present'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
