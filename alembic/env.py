"""Where alembic runs, and the one database it is allowed to run against.

**IT REFUSES ANY DATABASE THAT IS NOT THE LIBRARY'S OWN, and that refusal is the
reason this file has more than boilerplate in it.** `store` resolves one of two
databases depending on configuration: the Library's own file when `LIBRARY_DB`
names it, and Orient's control database during the read-only shadow. Running
`alembic upgrade head` in the second state would create the Library's twelve
tables inside Orient's live database — a schema change to a 240 MB file this
service does not own, committed by a tool nobody would think to guard.

So the migration tree is available exactly when `LIBRARY_DB` is set, and the
refusal names what to do about it. During the shadow there is nothing here to
run: the Library owns no schema until the cutover hands it one.

**There is no `target_metadata` and no autogenerate, deliberately.** The schema is
defined by the DDL in the revisions rather than by ORM models, for the reason
`store`'s own docstring gives — a model layer here would be a second definition of
a schema free to drift. `alembic revision --autogenerate` would compare the
database against an empty metadata and propose dropping every table in it, so the
absence is a guard as much as an omission. Write revisions by hand.

**The vec0 tables are outside this tree entirely.** `library_vectors` and
`signal_vectors` are invisible to a connection that has not loaded `sqlite-vec`,
and this connection does not load it: alembic sees a database without them, and
any autogenerate would propose creating what already exists. They move through
`migrate.py` at the cutover, across the vec0 interface. A database built from the
baseline alone has no vector index — `GET /signals/search` refuses with a stated
reason rather than failing oddly, since `vectors.available` checks for exactly
this — and nothing yet creates them on a fresh file.

**`LIBRARY_COLD_DB`, when set, is ATTACHed too, as schema `cold`** — see
`attach_cold_if_configured`. Unlike `LIBRARY_DB`, this is optional: most
revisions in this tree touch `main` only and always will, and a revision
that alters `signals`' schema is the one place a future author needs this
attach, with `schema="cold"` passed to whatever `op` call does the
altering (archives-cold-tier.md §2).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, pool

# The service modules sit at the repo root, one level up from `alembic/`. The
# same path shape `pytest.ini` declares, for the same reason: this tree imports
# `store` rather than duplicating its resolution.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import store  # noqa: E402
except RuntimeError as exc:
    # `store.resolve_db` now refuses at import when neither `LIBRARY_DB` nor
    # `ORIENT_CONTROL_DB` is set, rather than guessing Orient's stale default —
    # and that refusal fires here, before `library_database()` below gets a
    # chance to give its own purpose-built message. Re-raised as `SystemExit`
    # so an operator sees an actionable line instead of a bare traceback.
    raise SystemExit(
        f"alembic: could not resolve the Library's database ({exc}). This "
        f"tree migrates the Library's schema only. Set LIBRARY_DB to the "
        f"Library's file and run it again.") from exc

target_metadata = None


def library_database() -> Path:
    """The Library's own database, or a refusal.

    `store.resolve_db` has already refused a `LIBRARY_DB` that names Orient's
    file, by resolved path, by inode and by schema; this adds the one condition
    alembic needs on top of that — that `LIBRARY_DB` was set at all. `DB_SOURCE`
    is what distinguishes "the file I named" from "the shadow's fallback", which
    is a distinction the path alone cannot make.
    """
    path, source = store.resolve_db()
    if source != "LIBRARY_DB":
        raise SystemExit(
            f"alembic: refusing to run against {path}, which is not the "
            f"Library's own database (resolved from {source}). This tree "
            f"migrates the Library's schema only, and during the read-only "
            f"shadow the Library owns no schema. Set LIBRARY_DB to the "
            f"Library's file and run it again.")
    return path


def refuse_unless_ours(connection) -> None:
    """Refuse the connection SQLAlchemy actually opened, if it is Orient's.

    **`library_database()` validates a PATH, and this validates the HANDLE, and
    the difference is the whole finding** (security-reviewer, convivy/library#33).
    `store.resolve_db` resolves the path, opens a short-lived connection of its
    own to read the schema, closes it, and returns a string. SQLAlchemy then opens
    a second, independent connection to that string. A name repointed at Orient's
    database in between leaves the check vouching for a file that is not the one
    migrated — the same stat-then-open window `store.connect_rw` was fixed to
    close two commits earlier, reintroduced here on the path that runs `CREATE
    TABLE`.

    Asked of the open connection, there is no window: the handle is already bound
    to an inode, and renaming the path afterwards cannot move it.

    `exec_driver_sql` rather than `text()`, so the statement reaches SQLite's own
    driver in the qmark form `store` writes it in, without a second spelling of
    the fingerprint.

    **AND IT ROLLS BACK, which is not tidiness.** SQLAlchemy autobegins a
    transaction on the first statement, so a bare `SELECT` here leaves one open;
    the migration then runs inside it, and `engine.connect()`'s exit rolls back
    what was never explicitly committed. The first version of this created every
    table and left `alembic_version` empty — a migration that reports success and
    records nothing, which the next `upgrade head` would then run again. Caught by
    the tests already in this file, which is the argument for having written them
    before the guard.
    """
    found = connection.exec_driver_sql(
        store.ORIENT_FINGERPRINT_SQL, store.ORIENT_ONLY_TABLES).fetchone()
    connection.rollback()
    if found is not None:
        raise SystemExit(
            f"alembic: refusing to migrate the database now open, which holds "
            f"{found[0]!r} — a table that stays with Orient, so this connection "
            f"is on Orient's control database rather than the Library's. "
            f"Nothing was run.")


_ORIENT_FINGERPRINT_SQL_COLD = (
    "SELECT name FROM cold.sqlite_master WHERE type='table' AND name IN "
    f"({','.join('?' * len(store.ORIENT_ONLY_TABLES))}) LIMIT 1")


def attach_cold_if_configured(connection) -> None:
    """ATTACH `LIBRARY_COLD_DB` onto every migration run's connection, as
    schema `cold`, when the variable is set — called once, centrally, from
    `run_migrations_online`, so a revision that alters `signals` can apply
    the same operation to the cold file with `schema="cold"` without having
    to attach it itself (archives-cold-tier.md §2).

    **A no-op when the variable is unset, unlike `library_database()`'s
    refusal for `LIBRARY_DB`.** Most revisions in this tree touch `main`
    only and always will, so an ordinary `alembic upgrade head` run — this
    revision's own (`cold_dedup_keys`) included — never needs
    `LIBRARY_COLD_DB` set at all. Only a FUTURE revision that alters
    `signals` with `schema="cold"` depends on it, and that revision fails
    with alembic's own `no such schema: cold` if the variable is missing
    then — not every migration that came before it.

    Checked against the same Orient fingerprint `refuse_unless_ours` asks of
    `main`, qualified to `cold` — a cold file pointed at Orient's control
    database would otherwise let THIS tool, not only the running service,
    write a schema change into it.
    """
    raw = os.environ.get("LIBRARY_COLD_DB")
    if not raw:
        return
    cold_path = Path(raw).expanduser()
    store._refuse_orients_path(cold_path, os.environ)
    connection.exec_driver_sql("ATTACH DATABASE ? AS cold", (str(cold_path),))
    found = connection.exec_driver_sql(
        _ORIENT_FINGERPRINT_SQL_COLD, store.ORIENT_ONLY_TABLES).fetchone()
    # AND IT ROLLS BACK, for the exact reason `refuse_unless_ours` does
    # (see that function's own docstring): SQLAlchemy autobegins a
    # transaction on the first statement run on a connection, ATTACH
    # included, so a bare SELECT after it leaves one open, and the
    # migrations that follow would run inside it rather than their own —
    # which is how the very bug that docstring names reappeared here first:
    # every table created, `alembic_version` left empty, until this line.
    connection.rollback()
    if found is not None:
        raise SystemExit(
            f"alembic: refusing to migrate — LIBRARY_COLD_DB names a database "
            f"that holds {found[0]!r}, a table that stays with Orient, so it "
            f"is Orient's control database or a copy of it rather than the "
            f"cold tier's own file. Nothing was run.")


def run_migrations_offline() -> None:
    """Emit SQL without connecting — `alembic upgrade head --sql`.

    Worth having before a cutover step that touches a database this size: it is
    how an operator reads what a revision would do without a handle open on the
    file. There is no handle to identify here, so `refuse_unless_ours` has nothing
    to ask; `library_database()` still refuses a database that is not the
    Library's, which is what stops a preview being generated against Orient's.

    The preview is exact for a revision made of plain DDL, which is what the
    baseline is. A column-level change has to be written a particular way to run
    on SQLite at all; the note above `run_migrations_online` says which way, and
    why it is not something this function can fix.
    """
    context.configure(url=f"sqlite:///{library_database()}",
                      target_metadata=target_metadata,
                      literal_binds=True,
                      dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


# **A COLUMN CHANGE MUST USE `op.batch_alter_table`, and `op.alter_column` is the
# trap** (found through reviewer, convivy/library#33, and then through CI).
# Alembic renders `op.alter_column` as `ALTER TABLE t ALTER COLUMN c DROP NOT
# NULL`. Standard SQLite has no such statement, so CI — ubuntu, SQLite 3.4x —
# fails it outright: `near "ALTER": syntax error`. The deployment machine runs a
# much newer SQLite through Homebrew and ACCEPTS it, so the same revision passes
# by hand on the mini and fails in CI, which is the worst shape a difference can
# take.
#
# **THIS GAP IS THE OPERATING SYSTEM, NOT THE PYTHON VERSION, and aligning the
# interpreters did not close it.** Builder moved to python 3.11 everywhere on
# 2026-09-04; measured the same day, Homebrew's 3.11 and 3.14 both carry SQLite
# 3.53.3, while CI's ubuntu 3.11 carries 3.4x. So a migration still passes on
# the mini and fails in CI, and this rule still binds.
#
# `render_as_batch` does not close this. It governs how autogenerate RENDERS a
# revision, and this tree has no autogenerate; nothing it does makes a
# hand-written `op.alter_column` safe. It is set on the online run so that if
# autogenerate is ever enabled it proposes the batch form, and that is all it is
# for.
#
# So: write a column change as `with op.batch_alter_table("t") as batch: ...`,
# which recreates the table the way SQLite requires, and works on both.


def run_migrations_online() -> None:
    engine = create_engine(f"sqlite:///{library_database()}",
                           poolclass=pool.NullPool)
    with engine.connect() as connection:
        refuse_unless_ours(connection)
        attach_cold_if_configured(connection)
        context.configure(connection=connection,
                          target_metadata=target_metadata,
                          # SQLite cannot ALTER most things in place, so a later
                          # revision that changes a column rewrites its table.
                          # Declared here so a revision author gets the batch
                          # behaviour by default rather than discovering the
                          # limitation mid-cutover. Online only: see
                          # `run_migrations_offline` for why it does not help
                          # there.
                          render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()


# `alembic stamp` reaches here too, which is why the refusal lives in
# `library_database()` rather than in one branch: step 5 of the cutover stamps,
# and a stamp written into Orient's database would be as wrong as an upgrade run
# against it.
if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()