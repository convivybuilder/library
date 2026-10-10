"""The Library's migration tree: what it builds, and what it refuses to touch.

**Stage B step 5 of the cutover stamps this baseline onto a 240 MB database in a
window with Orient down.** The tests here are what makes that step something
other than a leap: the baseline builds a database the service can actually write
to, it creates exactly the tables `migrate.py` moves, and the tree refuses to run
against anything that is not the Library's own file.
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import migrate
import store

REPO = Path(__file__).resolve().parent.parent
BASELINE = "0b1e7c4a9d52"
# The revision immediately BEFORE the decision-plane drop (`a37e9060eee8`'s own
# `down_revision`). Named explicitly rather than reached via `downgrade -1`, so
# the test below still exercises the drop's guarded downgrade after a later
# migration lands on top of it — `-1` from head would instead undo whatever is
# newest, which is a different revision every time the tree grows.
BEFORE_THE_DECISION_PLANE_DROP = "c68fdb59edfc"


def alembic(*args, env_extra=None, cwd=None):
    """Run the real `alembic` command, which is what the cutover runs.

    Driving the CLI rather than `alembic.command` on purpose: step 5 is a person
    typing `alembic stamp head`, and the parts most likely to be wrong — that
    `alembic.ini` is found, that `env.py` imports `store` from the repo root, that
    the refusal reaches a shell as a non-zero exit — are exactly the parts an
    in-process call would skip.
    """
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(cwd or REPO), capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(REPO), **(env_extra or {})})


@pytest.fixture
def built(tmp_path):
    """An empty file with every revision applied, and the path to it."""
    db = tmp_path / "library.db"
    result = alembic("upgrade", "head", env_extra={"LIBRARY_DB": str(db)})
    assert result.returncode == 0, result.stderr
    return db


@pytest.fixture
def baseline_only(tmp_path):
    """An empty file with ONLY the baseline applied — the twelve tables the
    cutover moved, and nothing a later revision has added since.

    **Distinct from `built` on purpose.** The tests using this fixture assert
    exact facts about the vault migration itself — which tables it creates,
    how many indexes it carries — and a later revision adding its own table
    would make those assertions fail for a reason unrelated to the migration
    under test. `built` still exists for everything that wants "a database
    this service can actually run on," which is every later revision's tables
    too.
    """
    db = tmp_path / "library.db"
    result = alembic("upgrade", BASELINE, env_extra={"LIBRARY_DB": str(db)})
    assert result.returncode == 0, result.stderr
    return db


def tables(db) -> set[str]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'")}
    finally:
        conn.close()


# --- what the baseline builds ---------------------------------------------------

def test_the_baseline_creates_every_table_that_moves(baseline_only):
    """**Tied to the declared seam rather than to a copy of it.** `migrate.MOVES`
    is what the migration carries across, so a table added to the vault and to the
    seam without reaching this tree is a table the Library would serve from a
    database its own baseline cannot build.

    The vec0 tables are excluded from both sides of this comparison, and from the
    baseline, because alembic's connection cannot see them.
    """
    ordinary = set(migrate.MOVES)
    assert tables(baseline_only) == ordinary | {"alembic_version"}


def test_the_baseline_creates_nothing_that_stays(built):
    """Orient keeps `reading_rooms`, `room_entries`, `room_requests` and
    `push_subscriptions`. A baseline creating one of them would produce a file
    `store`'s own guard identifies as Orient's control database — the Library
    would refuse to start on the database its own tree built."""
    assert not tables(built) & set(store.ORIENT_ONLY_TABLES)


def test_the_decision_plane_tables_are_gone_at_head(built):
    """The baseline still creates them (`test_the_baseline_creates_every_
    table_that_moves` checks that against `baseline_only`) — this is the
    later revision that drops them, run into by `built`, which goes all the
    way to head."""
    assert not tables(built) & {"decision_items", "decision_comments",
                                "decision_events"}


def test_dropping_the_decision_plane_tables_has_no_downgrade(built):
    """`built` is already at head, past the drop — the refusal has to fire
    before `downgrade` recreates anything, not merely leave the tables
    absent the way they already were.

    **Targets the drop's own revision explicitly, not `downgrade -1`.** `built`
    goes all the way to head, and a later migration landing on top of the drop
    means `-1` undoes THAT one instead — passing for a reason unrelated to what
    this test is named for.
    """
    result = alembic("downgrade", BEFORE_THE_DECISION_PLANE_DROP,
                     env_extra={"LIBRARY_DB": str(built)})
    assert result.returncode != 0
    combined = result.stderr + result.stdout
    assert "no downgrade" in combined
    assert "would come back empty" in combined
    assert not tables(built) & {"decision_items", "decision_comments",
                                "decision_events"}, (
        "a failed downgrade recreated a table that should stay dropped")


def test_the_service_can_write_to_a_database_built_from_the_baseline(
        monkeypatch, built):
    """**The proof that the DDL is right, rather than merely present.** The
    columns are copied from a real migrated file, so the way they could be wrong
    is subtle — a missing default, a constraint dropped in transcription — and a
    schema comparison against a fixture would only ever be checking this file
    against itself. Driving the service's own insert paths checks it against the
    code that has to live on it.
    """
    monkeypatch.setattr(store, "DB_PATH", built)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)

    status, item = store.insert_item(
        {"id": store.new_ulid(), "product": "keel", "title": "a filed item",
         "body": "body", "refs": "{}", "severity": "low", "state": "open",
         "type": None, "created_by": "test", "created_by_kind": "agent",
         "needs": "agent", "parent_id": None, "found_during": None,
         "topics": "[]", "prerequisites": "[]", "once_key": None},
        lambda row: True, lambda conn, row: (False, None), lambda p: True)
    assert status == 201, item

    status, signal = store.insert_signal(
        {"id": store.new_ulid(), "source": "test", "kind": "note",
         "actor": "test", "ingested_by": "test", "product": "keel",
         "stream": "s", "body": "b", "refs": "{}", "meta": "{}",
         "privacy_class": "internal", "dedup_key": None, "produced_at": None,
         "acting_human": None},
        lambda row: True)
    assert status == 201, signal

    store.ack_signal(signal["id"], "operator")


def test_the_creation_event_lands_too(monkeypatch, built):
    """`insert_item` writes the item and its first state event in one
    transaction, so the baseline's `issue_events` has to carry the columns that
    write names — the failure would otherwise surface as a 500 on the first
    filing after the cutover."""
    monkeypatch.setattr(store, "DB_PATH", built)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    item_id = store.new_ulid()
    store.insert_item(
        {"id": item_id, "product": "keel", "title": "t", "body": "b",
         "refs": "{}", "severity": "low", "state": "open", "type": None,
         "created_by": "test", "created_by_kind": "agent", "needs": "agent",
         "parent_id": None, "found_during": None, "topics": "[]",
         "prerequisites": "[]", "once_key": None},
        lambda row: True, lambda conn, row: (False, None), lambda p: True)
    conn = sqlite3.connect(f"file:{built}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT to_state FROM issue_events WHERE item_id = ?",
            (item_id,)).fetchone()
    finally:
        conn.close()
    assert row == ("open",)


def test_the_unique_constraints_survived_the_copy(monkeypatch, built):
    """A `UNIQUE` clause lost in transcription is invisible until two rows
    collide, which for `once_key` means an idempotent retry filing a second item
    instead of returning the first."""
    monkeypatch.setattr(store, "DB_PATH", built)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    row = {"id": store.new_ulid(), "product": "keel", "title": "t", "body": "b",
           "refs": "{}", "severity": "low", "state": "open", "type": None,
           "created_by": "test", "created_by_kind": "agent", "needs": "agent",
           "parent_id": None, "found_during": None, "topics": "[]",
           "prerequisites": "[]", "once_key": "the-same-key"}
    first, _ = store.insert_item(dict(row), lambda r: True,
                                 lambda conn, r: (False, None), lambda p: True)
    assert first == 201
    second, body = store.insert_item({**row, "id": store.new_ulid()},
                                     lambda r: True,
                                     lambda conn, r: (False, None),
                                     lambda p: True)
    assert second == 200 and body["idempotent"] is True


def test_upgrading_to_head_twice_is_a_no_op(built):
    """What step 5's stamp relies on: once at head, `upgrade head` does nothing
    a second time.

    **Reads the head from `alembic heads` rather than a hardcoded revision
    id.** This used to assert `BASELINE in current.stdout` back when the
    baseline was the only revision in the tree — true the day it was written
    and false the moment a second revision landed on top, which is exactly
    the kind of test that goes stale silently rather than failing loudly. This
    holds for whatever the tree's head actually is.

    `built` reached head by running every revision rather than stamping it,
    which is the same end state by the other route — and running both ways is
    the point, since the cutover stamps a database `migrate.py` already filled
    while a fresh Library runs it.
    """
    heads = alembic("heads", env_extra={"LIBRARY_DB": str(built)})
    assert heads.returncode == 0, heads.stderr
    head_id = heads.stdout.split()[0]
    current = alembic("current", env_extra={"LIBRARY_DB": str(built)})
    assert head_id in current.stdout, current.stdout + current.stderr
    again = alembic("upgrade", "head", env_extra={"LIBRARY_DB": str(built)})
    assert again.returncode == 0, again.stderr


def test_stamping_an_already_populated_database_matches_running_it(tmp_path):
    """The cutover's actual shape: `migrate.py` creates the tables, then alembic
    is told they are there. The stamped database and the built one must agree
    about the revision, or the next real migration runs against the wrong
    ancestor."""
    populated = tmp_path / "populated.db"
    built = tmp_path / "built.db"
    assert alembic("upgrade", "head",
                   env_extra={"LIBRARY_DB": str(built)}).returncode == 0

    # Stand in for `migrate.py`: the schema arrives by copy, not by alembic.
    source = sqlite3.connect(f"file:{built}?mode=ro", uri=True)
    ddl = [r[0] for r in source.execute(
        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL "
        "AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version'")]
    source.close()
    dest = sqlite3.connect(populated)
    for statement in ddl:
        dest.execute(statement)
    dest.commit()
    dest.close()

    stamped = alembic("stamp", "head", env_extra={"LIBRARY_DB": str(populated)})
    assert stamped.returncode == 0, stamped.stderr
    assert tables(populated) == tables(built)
    noop = alembic("upgrade", "head", env_extra={"LIBRARY_DB": str(populated)})
    assert noop.returncode == 0, noop.stderr


def test_there_is_no_downgrade_from_the_baseline(built):
    """Reversing the baseline drops every vault table, and alembic puts no
    confirmation between typing it and losing them."""
    result = alembic("downgrade", "base", env_extra={"LIBRARY_DB": str(built)})
    assert result.returncode != 0
    assert "no downgrade" in (result.stderr + result.stdout)
    assert tables(built), "the tables were dropped before the refusal"


# --- what the tree refuses ------------------------------------------------------

def test_alembic_refuses_to_run_during_the_shadow(tmp_path):
    """**The refusal this tree exists to carry.** During the shadow `store`
    resolves Orient's control database, and `alembic upgrade head` against it
    would create the Library's twelve tables inside a 240 MB file this service
    does not own."""
    result = alembic("upgrade", "head",
                     env_extra={"ORIENT_CONTROL_DB": str(tmp_path / "control.db")})
    assert result.returncode != 0
    assert "not the Library's own database" in (result.stderr + result.stdout)


def test_alembic_refuses_to_stamp_during_the_shadow(tmp_path):
    """`stamp` reaches `env.py` the same way `upgrade` does, and a stamp written
    into Orient's database would be as wrong as an upgrade run against it — it
    would create `alembic_version`'s row in a file whose migration history is
    already Orient's."""
    result = alembic("stamp", "head",
                     env_extra={"ORIENT_CONTROL_DB": str(tmp_path / "control.db")})
    assert result.returncode != 0
    assert "not the Library's own database" in (result.stderr + result.stdout)


def test_alembic_refuses_cleanly_with_neither_variable_set():
    """**The subprocess has no `LIBRARY_DB` and no `ORIENT_CONTROL_DB`** — the
    `alembic()` helper only ever hands it `PATH` and `HOME` — which is exactly
    the shell the steward's incident happened in. `store.resolve_db` refuses at
    import now rather than guessing Orient's default, and that refusal has to
    surface as `env.py`'s own actionable message rather than a bare traceback,
    or a person running this from a bad shell sees a wall of Python instead of
    the one line telling them what to set."""
    result = alembic("upgrade", "head")
    assert result.returncode != 0
    combined = result.stderr + result.stdout
    assert "Set LIBRARY_DB" in combined
    assert "Traceback" not in combined


def test_alembic_refuses_a_library_db_naming_orients_file(tmp_path):
    """`env.py` adds one condition to `store.resolve_db`; it does not replace the
    ones already there."""
    live = tmp_path / "control.db"
    conn = sqlite3.connect(live)
    conn.execute("CREATE TABLE reading_rooms (id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()
    result = alembic("upgrade", "head", env_extra={"LIBRARY_DB": str(live)})
    assert result.returncode != 0
    assert "reading_rooms" in (result.stderr + result.stdout)


# --- the indexes, which no write path exercises ---------------------------------

def named_indexes(db) -> set[str]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND sql IS NOT NULL")}
    finally:
        conn.close()


def test_the_partial_unique_indexes_are_there(built):
    """**These two enforce invariants, and losing one fails silently**
    (reviewer, convivy/library#33). `ix_issue_leases_live_root` is one live lease
    per item and `ix_courses_live_product` is one live Course per product. A
    transcription slip that dropped either would still build a database, still
    pass every write test, and surface as a double-booking in production.

    A copying bug already ate this exact index once: `migrate.py`'s index copier
    matched `" ON <table>"` in the SQL text and silently dropped
    `ix_issue_leases_live_root` while reporting a clean pass (PR #22, round 2).
    """
    present = named_indexes(built)
    assert "ix_issue_leases_live_root" in present
    assert "ix_courses_live_product" in present


def test_the_kind_index_is_there(built):
    """**The column that caused a production incident, so a silent drop is a
    failure rather than a slow morning.**

    `signals.kind` is a filter `GET /signals` and `GET /signals/facets` both
    offer, and it had no index until `d3f81a6c07be`. Without one the answer is a
    full scan of the table: `SCAN signals (rows=21031)` against a 255 MB file,
    which is milliseconds against a warm page cache and seconds against a cold
    one. On 2026-08-26 that took `?kind=alert&limit=50` past 30 seconds and
    darkened two sections of `/engineering/overnight`.

    Unlike the partial unique indexes above, losing this degrades performance
    rather than correctness — so it would not fail a write test, and the way it
    surfaces is a page going dark rather than a double-booking. That is the
    argument for asserting it rather than against.
    """
    assert "ix_signals_kind_ingested_at" in named_indexes(built)


def test_every_index_the_migration_carries_is_in_the_baseline(baseline_only):
    """The count, so a dropped index is a failure rather than a smaller number
    nobody reads. 40 named indexes over the twelve moving tables, as read off the
    file `migrate.py` produced from the live database."""
    assert len(named_indexes(baseline_only)) == 40


def test_no_index_belongs_to_a_table_that_stays(built):
    conn = sqlite3.connect(f"file:{built}?mode=ro", uri=True)
    try:
        owners = {r[0] for r in conn.execute(
            "SELECT tbl_name FROM sqlite_master WHERE type='index'")}
    finally:
        conn.close()
    assert not owners & set(store.ORIENT_ONLY_TABLES)


# --- the offline preview, which an operator reads before the window -------------

def test_the_offline_preview_runs(tmp_path):
    """`alembic upgrade head --sql` is how an operator reads what a revision
    would do without a handle open on a 240 MB file. Nothing drove it before, and
    that is how the `render_as_batch` divergence went unnoticed (reviewer,
    convivy/library#33)."""
    db = tmp_path / "library.db"
    result = alembic("upgrade", "head", "--sql", env_extra={"LIBRARY_DB": str(db)})
    assert result.returncode == 0, result.stderr
    assert "CREATE TABLE" in result.stdout
    assert not db.exists(), "offline mode opened the database"


# --- the handle the migration actually runs on ----------------------------------

def test_the_fingerprint_reads_through_sqlalchemys_driver(tmp_path):
    """`refuse_unless_ours` asks `store`'s fingerprint through a SQLAlchemy
    connection rather than a `sqlite3` one, and the two take their parameters
    differently. This pins that the shared statement works through that driver —
    the part of the guard that could break without any test noticing, since the
    path check in `store.resolve_db` catches the same file first and would mask
    it.
    """
    sqlalchemy = pytest.importorskip("sqlalchemy")
    orients = tmp_path / "control.db"
    conn = sqlite3.connect(orients)
    conn.execute("CREATE TABLE reading_rooms (id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()

    engine = sqlalchemy.create_engine(f"sqlite:///{orients}")
    with engine.connect() as connection:
        found = connection.exec_driver_sql(
            store.ORIENT_FINGERPRINT_SQL, store.ORIENT_ONLY_TABLES).fetchone()
    assert found is not None and found[0] == "reading_rooms"

    ours = library_db_file(tmp_path / "library.db")
    engine = sqlalchemy.create_engine(f"sqlite:///{ours}")
    with engine.connect() as connection:
        assert connection.exec_driver_sql(
            store.ORIENT_FINGERPRINT_SQL,
            store.ORIENT_ONLY_TABLES).fetchone() is None


def library_db_file(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE issue_items (id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()
    return path
