"""Which database this process serves, and the refusals that decide it.

**The subject is P4 of the cutover sequence, called there "the single most
dangerous line in the cutover".** `store.DB_PATH` used to read
`ORIENT_CONTROL_DB` with a default pointing at Orient's file, so enabling writes
with the variable unset — or set wrong — would have made the Library a second
writer to a 234 MB live database, with both processes reporting success.

Every test here drives `store.resolve_db(env)` with an explicit environment
rather than the process's own. The resolution runs at import in the real service,
which is what makes a bad configuration a refusal to START; a test that had to
re-import the module to observe it would be testing the import system.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import migrate
import reference
import store
import vectors


def orient_db(path):
    """A file that IS Orient's control database, by content.

    One of the tables that stays with Orient at the cutover is enough — that is
    exactly the signal `store` fingerprints on, and building the whole 31-table
    schema would test SQLite rather than the guard.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE reading_rooms (id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE issue_items (id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()
    return path


def library_db(path):
    """A file holding the moving tables and none of the staying ones — the shape
    `migrate.py` writes and asserts."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE issue_items (id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE signals (id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()
    return path


# --- the shadow, which is what runs until the cutover window --------------------

def test_nothing_set_refuses_rather_than_guessing():
    """No variables at all is the mini's login shell — and the exact shape of
    the steward's incident, where a check run from a bare shell silently read
    Orient's stale default and reported a clean result having never touched the
    live file. This refuses instead of guessing which database was meant."""
    with pytest.raises(store.MisconfiguredDatabase, match="LIBRARY_DB"):
        store.resolve_db({})


def test_the_shadow_follows_orient_control_db(tmp_path):
    path, source = store.resolve_db(
        {"ORIENT_CONTROL_DB": str(tmp_path / "control.db")})
    assert path == tmp_path / "control.db"
    assert source == "ORIENT_CONTROL_DB"


# --- LIBRARY_DB, the file this service owns -------------------------------------

def test_library_db_wins_whenever_it_is_set(tmp_path):
    """Set-but-ignored is not an outcome.

    A `LIBRARY_DB` the service silently declined to read — because writes happened
    to be off — would be a wrong answer rather than an error, and the operator
    would have no way to tell from the outside which file was being served.
    """
    own = library_db(tmp_path / "library.db")
    path, source = store.resolve_db(
        {"LIBRARY_DB": str(own), "ORIENT_CONTROL_DB": str(tmp_path / "control.db")})
    assert path == own
    assert source == "LIBRARY_DB"


def test_library_db_is_used_with_writes_enabled(tmp_path):
    own = library_db(tmp_path / "library.db")
    path, _ = store.resolve_db(
        {"LIBRARY_DB": str(own), "LIBRARY_WRITES_ENABLED": "1"})
    assert path == own


def test_a_file_that_does_not_exist_yet_resolves(tmp_path):
    """The migration writes the Library's file inside the cutover window, so a
    boot before that has a path with nothing at the end of it. `connect` is where
    a missing file becomes an error, and it names it as one."""
    path, _ = store.resolve_db({"LIBRARY_DB": str(tmp_path / "not-yet.db")})
    assert path == tmp_path / "not-yet.db"


def test_library_db_expands_a_tilde(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    path, _ = store.resolve_db({"LIBRARY_DB": "~/.library/library.db"})
    assert path == tmp_path / ".library" / "library.db"


# --- the refusals ---------------------------------------------------------------

def test_writes_enabled_without_library_db_refuses():
    """**The refusal P4 exists for.** With no `LIBRARY_DB` and writes on there is
    no file this process can honestly claim to own, and the old code's fallback
    was Orient's."""
    with pytest.raises(store.MisconfiguredDatabase) as exc:
        store.resolve_db({"LIBRARY_WRITES_ENABLED": "1"})
    assert "LIBRARY_DB" in str(exc.value)


def test_writes_enabled_without_library_db_refuses_even_pointed_at_orient(tmp_path):
    """The same refusal with `ORIENT_CONTROL_DB` exported, which is what an
    operator running the migration in the window has in their shell.

    This is the exact failure: step 3 exports `ORIENT_CONTROL_DB` because
    `migrate.py` refuses without it, and the old resolution would then have read
    that same variable and written to it.
    """
    with pytest.raises(store.MisconfiguredDatabase):
        store.resolve_db({"LIBRARY_WRITES_ENABLED": "1",
                          "ORIENT_CONTROL_DB": str(tmp_path / "control.db")})


def test_library_db_naming_orients_file_refuses(tmp_path):
    live = orient_db(tmp_path / "control.db")
    with pytest.raises(store.MisconfiguredDatabase) as exc:
        store.resolve_db({"LIBRARY_DB": str(live),
                          "ORIENT_CONTROL_DB": str(live)})
    assert "Orient" in str(exc.value)


def test_library_db_naming_orients_file_refuses_with_the_variable_unset(
        monkeypatch, tmp_path):
    """**The configuration the guard has to work in.** After the cutover the plist
    sets `LIBRARY_DB` and does not set `ORIENT_CONTROL_DB`, so a guard that only
    compared the two variables would have nothing to compare against precisely
    where it matters. The content check is what still holds."""
    monkeypatch.setenv("HOME", str(tmp_path))
    live = orient_db(tmp_path / "somewhere-else.db")
    with pytest.raises(store.MisconfiguredDatabase) as exc:
        store.resolve_db({"LIBRARY_DB": str(live)})
    assert "reading_rooms" in str(exc.value)


def test_a_copy_of_orients_database_refuses(tmp_path):
    """A copy at a path nobody would have listed. Path comparison passes it; the
    schema does not, and writing to a copy is the same data loss as writing to
    the original — the rows just go somewhere nobody reads."""
    copied = orient_db(tmp_path / "backup-2026-08-15.db")
    with pytest.raises(store.MisconfiguredDatabase):
        store.resolve_db({"LIBRARY_DB": str(copied),
                          "ORIENT_CONTROL_DB": str(tmp_path / "control.db")})


def test_a_symlink_to_orients_file_refuses(tmp_path):
    live = orient_db(tmp_path / "control.db")
    link = tmp_path / "library.db"
    link.symlink_to(live)
    with pytest.raises(store.MisconfiguredDatabase):
        store.resolve_db({"LIBRARY_DB": str(link),
                          "ORIENT_CONTROL_DB": str(live)})


def test_a_hard_link_to_orients_file_refuses(tmp_path):
    """The inode check, which is the one a path comparison cannot make: two
    names, one file, and `resolve()` reports them as different paths because
    that is what they are."""
    live = orient_db(tmp_path / "control.db")
    hard = tmp_path / "library.db"
    hard.hardlink_to(live)
    assert hard.resolve() != live.resolve(), (
        "if these resolved equal the path check would be doing this test's work")
    with pytest.raises(store.MisconfiguredDatabase):
        store.resolve_db({"LIBRARY_DB": str(hard),
                          "ORIENT_CONTROL_DB": str(live)})


def test_a_file_that_is_not_a_database_refuses(tmp_path):
    """Fails closed, the doctrine `migrate.guard` and `serve.sh` already state: a
    file this cannot identify is not a file it may serve from."""
    junk = tmp_path / "library.db"
    junk.write_bytes(b"not a database, and long enough not to look empty" * 10)
    with pytest.raises(store.MisconfiguredDatabase):
        store.resolve_db({"LIBRARY_DB": str(junk)})


def test_an_empty_file_is_not_refused(tmp_path):
    """SQLite reads a zero-length file as an empty database, so this must not be
    mistaken for the unreadable case above — a freshly created file is a normal
    state, not a corrupt one."""
    empty = tmp_path / "library.db"
    empty.touch()
    path, _ = store.resolve_db({"LIBRARY_DB": str(empty)})
    assert path == empty


# --- the URI, where the resolved path becomes an opened file --------------------

def test_a_path_holding_a_question_mark_opens_the_file_it_names(monkeypatch,
                                                                tmp_path):
    """**Interpolating the path raw defeats `mode=ro`.** SQLite splits the URI on
    its FIRST `?`, so `file:/tmp/weird?dir/test.db?mode=ro` opens `/tmp/weird` —
    a different file, created on demand, writable. Found in `migrate.py` by a
    security reviewer (convivy/library#22); `store` and `vectors` carried it
    still.

    The assertion is doubled on purpose. Reading the right row proves the right
    file was opened; the absence of the truncated path proves the wrong one was
    not created, which is the half that would otherwise pass silently if SQLite's
    behaviour changed.
    """
    weird = tmp_path / "weird?dir"
    weird.mkdir()
    db = weird / "test.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE marker (v TEXT)")
    conn.execute("INSERT INTO marker VALUES ('the real file')")
    conn.commit()
    conn.close()

    monkeypatch.setattr(store, "DB_PATH", db)
    with store.connect() as opened:
        assert opened.execute("SELECT v FROM marker").fetchone()[0] == "the real file"
    assert not (tmp_path / "weird").exists(), (
        "the truncated path was created, so the URI was not encoded")


def test_the_knowledge_index_encodes_its_path_too(monkeypatch, tmp_path):
    """`reference.connect` was the last raw-path URI in the repo
    (security-reviewer, convivy/library#32), and its path is caller-supplied
    through `LIBRARY_KB_INDEX` rather than fixed."""
    weird = tmp_path / "weird?dir"
    weird.mkdir()
    index = weird / "index.db"
    conn = sqlite3.connect(index)
    conn.execute("CREATE TABLE marker (v TEXT)")
    conn.execute("INSERT INTO marker VALUES ('the real index')")
    conn.commit()
    conn.close()

    monkeypatch.setattr(reference, "INDEX_DB", index)
    opened = reference.connect()
    try:
        assert opened.execute("SELECT v FROM marker").fetchone()[0] == "the real index"
    finally:
        opened.close()
    assert not (tmp_path / "weird").exists()


def test_connect_names_the_path_when_the_index_is_genuinely_absent(
        monkeypatch, tmp_path):
    monkeypatch.setattr(reference, "INDEX_DB", tmp_path / "missing.db")
    with pytest.raises(reference.ReferenceIndexUnavailable, match="no knowledge index"):
        reference.connect()


def test_connect_distinguishes_a_stat_failure_from_absence(monkeypatch,
                                                            tmp_path):
    """`Path.is_file()` returns `False` on any `OSError`, so a permission
    failure used to raise the exact same "no knowledge index" message as a
    genuinely missing file — the twelve occurrences filed in `serve.log` on
    2026-09-03 left no evidence of which one they were
    (`issue_01M1N7J265ST2V6VZ2C9TA4BN7`). `connect()` now carries a stat
    failure that is not ENOENT into `ReferenceIndexUnavailable` with its own
    errno and strerror, instead of collapsing it into the absence message."""
    present = tmp_path / "index.db"
    present.write_bytes(b"")
    monkeypatch.setattr(reference, "INDEX_DB", present)

    def boom(self):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(Path, "stat", boom)

    with pytest.raises(reference.ReferenceIndexUnavailable) as exc:
        reference.connect()
    assert "no knowledge index" not in str(exc.value)
    assert "Permission denied" in str(exc.value)


def test_connect_refuses_when_the_index_path_is_a_directory(monkeypatch, tmp_path):
    """The `sqlite3.Error` catch, not a `stat()` check, is what has to catch this.

    A directory `stat()`s just fine, so it passes the earlier check and reaches
    `sqlite3.connect(..., mode=ro)`, which fails on it with
    `sqlite3.OperationalError: disk I/O error`. This is the case the review round
    cited for not adding an `S_ISREG` check on top of the `stat()` above: the
    `sqlite3.Error` catch already covers it, and this test is what proves that
    rather than leaving it argued.
    """
    monkeypatch.setattr(reference, "INDEX_DB", tmp_path)
    with pytest.raises(reference.ReferenceIndexUnavailable):
        reference.connect()


def test_the_read_only_uri_survives_a_question_mark(tmp_path):
    """And it is still read-only. The raw form does not merely open the wrong
    file: it drops `mode=ro` with everything else after the first `?`, so the
    handle the caller believes is read-only accepts a write."""
    weird = tmp_path / "weird?dir"
    weird.mkdir()
    db = weird / "test.db"
    sqlite3.connect(db).close()
    conn = sqlite3.connect(store.db_uri(db, readonly=True), uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE planted (v TEXT)")
    finally:
        conn.close()


# --- the guard after boot, and the lists it depends on ---------------------------

def test_a_writable_connection_re_checks_the_path(monkeypatch, tmp_path):
    """**The import-time guard is a statement about one instant**
    (security-reviewer, convivy/library#32). `LIBRARY_DB` naming a symlink that
    resolved somewhere harmless at boot, repointed at Orient's database
    afterwards, would reach `connect_rw` with nothing re-run — the second-writer
    failure with no refusal anywhere.
    """
    live = orient_db(tmp_path / ".orient-control" / "control.db")
    own = tmp_path / "library.db"
    library_db(own)

    # `ORIENT_DB_DEFAULT` stands in for the real `~/.orient-control/control.db`,
    # and it is the candidate that matters: after the cutover the plist leaves
    # `ORIENT_CONTROL_DB` unset, so the default is the only thing the re-check
    # has to compare against — and it is the live file.
    monkeypatch.setattr(store, "ORIENT_DB_DEFAULT", live)
    monkeypatch.setattr(store, "DB_PATH", own)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    store.connect_rw().close()          # the configuration as resolved at boot

    own.unlink()
    own.symlink_to(live)                # repointed underneath the running process
    with pytest.raises(store.MisconfiguredDatabase) as exc:
        store.connect_rw()
    assert "reading_rooms" in str(exc.value), (
        "the refusal has to come from what the handle holds, not from the path — "
        "a path check has a window between its last stat and the open")


def test_the_shadow_still_refuses_a_writable_handle(monkeypatch, tmp_path):
    """The re-check is added AFTER the shadow's refusal, not before it. A
    `WritesDisabled` that turned into a `MisconfiguredDatabase` would still be a
    refusal, and would still be the wrong one — the caller would go looking at
    its database path instead of at the switch."""
    monkeypatch.setattr(store, "DB_PATH", orient_db(tmp_path / "control.db"))
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    with pytest.raises(store.WritesDisabled):
        store.connect_rw()


def test_the_orient_fingerprint_agrees_with_the_declared_seam():
    """`store.ORIENT_ONLY_TABLES` and `migrate.STAYS` are two copies of one fact,
    and neither module can import the other (security-reviewer,
    convivy/library#32). A rename on Orient's side that updated one and not the
    other would leave the fingerprint quietly non-conclusive for the live database
    it exists to catch, with nothing failing.

    `alembic_version` is the one deliberate difference: it stays with Orient, and
    it is no fingerprint — the Library's own tree creates a table of that name in
    its own file the moment its baseline is stamped.
    """
    assert set(store.ORIENT_ONLY_TABLES) == set(migrate.STAYS) - {"alembic_version"}
    assert "alembic_version" not in store.ORIENT_ONLY_TABLES


def test_the_vector_index_is_checked_against_the_resolved_database(
        monkeypatch, tmp_path, vector_index):
    """`vectors.available` used to read `ORIENT_CONTROL_DB` itself when called
    with no path — a second place deciding which database this process serves,
    which would have probed Orient's file after the cutover (reviewer,
    convivy/library#32).

    The two variables are pointed at different files ON PURPOSE, and only one of
    them holds an index. Setting both to something unreadable would have passed
    against either version of the code, which is the way this test could have
    been written and proved nothing.
    """
    served = library_db(tmp_path / "library.db")
    vector_index(served)
    monkeypatch.setattr(store, "DB_PATH", served)
    monkeypatch.setenv("ORIENT_CONTROL_DB", str(tmp_path / "no-such.db"))
    ok, why = vectors.available()
    assert ok, f"available() answered about a database other than the resolved one ({why})"


def test_an_unopenable_index_names_the_sqlite_message(tmp_path):
    """A missing database under `mode=ro` is the likely cause on a fresh Linux
    install. `why` used to interpolate the SQLite message verbatim, on the
    reasoning that a "unable to open database file" text carries no secret
    on its own — but `_open_or_reason` is the one place this wording is
    decided for every caller, including `connect_rw_if_available()`, whose
    `reason` reaches an authenticated response and, for revision, a
    permanent `issue_comments` row. A cause-specific exception elsewhere
    through this same path (`_attach_vectors_db`'s `FileNotFoundError`) DOES
    carry a secret, the server's own absolute path, so the bound now applies
    uniformly rather than per-exception-type (security-reviewer,
    issue_01M35R5GA1SKRG2CMQRF189NDE round 2): the full text still reaches
    the service log, and the caller gets a fixed, bounded reason with no
    path and no exception text."""
    ok, why = vectors.available(tmp_path / "no-such-dir" / "library.db")
    assert not ok
    assert why == "the vector index could not be opened"
    assert "OperationalError" not in why
    assert str(tmp_path) not in why


# --- the vectors database's own location (memory-retention epic clause 7,
# issue_01M35R5GA1SKRG2CMQRF189NDE) ------------------------------------------


def test_library_vectors_db_unset_refuses_rather_than_guessing():
    with pytest.raises(vectors.MisconfiguredVectorsDB,
                       match="LIBRARY_VECTORS_DB is not set"):
        vectors.resolve_vectors_db_path({})


def test_library_vectors_db_expands_a_tilde(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert vectors.resolve_vectors_db_path(
        {"LIBRARY_VECTORS_DB": "~/vectors.db"}) == tmp_path / "vectors.db"


def test_resolve_vectors_db_path_does_not_require_the_file_to_exist(tmp_path):
    """Unlike `blobs.resolve_blob_dir`'s directory, existence is checked at
    connection time, not here — `init_vectors.py` has to be able to import
    `vectors` before the file it exists to create is there."""
    missing = tmp_path / "does-not-exist-yet.db"
    assert vectors.resolve_vectors_db_path(
        {"LIBRARY_VECTORS_DB": str(missing)}) == missing


def test_connect_refuses_a_missing_vectors_database(tmp_path, monkeypatch):
    """`vectors.connect` — a read-only handle — refuses a missing
    `LIBRARY_VECTORS_DB` rather than reporting an index with nothing in it,
    the same shape of refusal `store.connect` gives for a missing
    `LIBRARY_DB`."""
    served = library_db(tmp_path / "library.db")
    monkeypatch.setattr(vectors, "VECTORS_DB_PATH",
                        tmp_path / "no-such-vectors.db")
    with pytest.raises(FileNotFoundError, match="no vectors database at"):
        vectors.connect(served)


def test_connect_rw_refuses_a_missing_vectors_database(tmp_path, monkeypatch):
    """The writable half of the same refusal — `_do_checkout` and every
    embed route open through here, and a missing vectors file must fail
    loud rather than attach nothing and let the next vec0 query say `no
    such table`."""
    served = library_db(tmp_path / "library.db")
    monkeypatch.setattr(store, "DB_PATH", served)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    monkeypatch.setattr(vectors, "VECTORS_DB_PATH",
                        tmp_path / "no-such-vectors.db")
    with pytest.raises(FileNotFoundError, match="no vectors database at"):
        vectors.connect_rw()


def test_connect_rw_if_available_reports_none_and_the_same_reason_as_available(
        tmp_path, monkeypatch):
    """The best-effort degrade path a write-side caller falls back on
    (`_do_checkout`, `_do_revision`, `_revision_briefing`) — `available()`'s
    own wording, not a second, independently-worded guess at the same cause
    (reviewer + security-reviewer, issue_01M35R5GA1SKRG2CMQRF189NDE round 1)."""
    served = library_db(tmp_path / "library.db")
    monkeypatch.setattr(store, "DB_PATH", served)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    monkeypatch.setattr(vectors, "VECTORS_DB_PATH",
                        tmp_path / "no-such-vectors.db")
    conn, reason = vectors.connect_rw_if_available()
    assert conn is None
    ok, available_reason = vectors.available(served)
    assert not ok
    assert reason == available_reason
    assert reason == "the vector index file is missing or could not be opened"


def test_connect_rw_if_available_reason_names_no_path(tmp_path, monkeypatch):
    """`reason` reaches `/checkout`, `/claim` and `/revise` responses, and for
    revision it lands permanently in `issue_comments` — a fixed, bounded
    string, not the raw exception text `_attach_vectors_db` builds from
    `VECTORS_DB_PATH`'s own absolute path (security-reviewer,
    issue_01M35R5GA1SKRG2CMQRF189NDE round 2)."""
    served = library_db(tmp_path / "library.db")
    monkeypatch.setattr(store, "DB_PATH", served)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    missing = tmp_path / "no-such-vectors.db"
    monkeypatch.setattr(vectors, "VECTORS_DB_PATH", missing)
    conn, reason = vectors.connect_rw_if_available()
    assert conn is None
    assert str(missing) not in reason
    assert str(tmp_path) not in reason
    assert "FileNotFoundError" not in reason


def test_connect_rw_if_available_returns_a_working_connection_when_it_can(
        tmp_path, monkeypatch):
    served = library_db(tmp_path / "library.db")
    monkeypatch.setattr(store, "DB_PATH", served)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    vectors_db = tmp_path / "vectors.db"
    import sqlite_vec
    setup = sqlite3.connect(vectors_db)
    setup.enable_load_extension(True)
    sqlite_vec.load(setup)
    setup.enable_load_extension(False)
    setup.execute(vectors.VEC_TABLE_DDL)
    setup.execute(vectors.SIGNAL_VEC_TABLE_DDL)
    setup.commit()
    setup.close()
    monkeypatch.setattr(vectors, "VECTORS_DB_PATH", vectors_db)
    conn, reason = vectors.connect_rw_if_available()
    try:
        assert reason is None
        assert conn is not None
        conn.execute(f"SELECT COUNT(*) FROM {vectors.VEC_TABLE}").fetchone()
    finally:
        conn.close()


def test_available_names_the_missing_vectors_database(tmp_path, monkeypatch):
    """The route-facing surface: `GET /signals/search` reads `vectors.
    available` before it does anything else, and a missing vectors file has
    to read as "unavailable, here is why" rather than a 500 — a fixed,
    bounded reason, not the file's own path (security-reviewer,
    issue_01M35R5GA1SKRG2CMQRF189NDE round 2)."""
    served = library_db(tmp_path / "library.db")
    monkeypatch.setattr(vectors, "VECTORS_DB_PATH",
                        tmp_path / "no-such-vectors.db")
    ok, why = vectors.available(served)
    assert not ok
    assert why == "the vector index file is missing or could not be opened"


# --- LIBRARY_VECTORS_DB naming Orient's control database (security-reviewer,
# issue_01M35R5GA1SKRG2CMQRF189NDE round 3) -----------------------------------


def test_library_vectors_db_naming_orients_file_refuses_a_read(monkeypatch,
                                                                tmp_path):
    """Orient's control database still holds vec0 tables from before the
    cutover, so a `LIBRARY_VECTORS_DB` naming it would otherwise ATTACH
    silently, with every later query in this module reading Orient's
    vectors and every later write landing there instead — the second-writer
    failure ADR-0020 exists to prevent. `_attach_vectors_db` runs the same
    Orient-identity check `store.resolve_db` runs for `LIBRARY_DB`
    (`store._refuse_orients_database`), against the file about to attach."""
    served = library_db(tmp_path / "library.db")
    live = orient_db(tmp_path / "control.db")
    monkeypatch.setattr(store, "DB_PATH", served)
    monkeypatch.setattr(vectors, "VECTORS_DB_PATH", live)
    with pytest.raises(store.MisconfiguredDatabase, match="reading_rooms"):
        vectors.connect(served)


def test_library_vectors_db_naming_orients_file_refuses_the_write(monkeypatch,
                                                                   tmp_path):
    served = library_db(tmp_path / "library.db")
    live = orient_db(tmp_path / "control.db")
    monkeypatch.setattr(store, "DB_PATH", served)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    monkeypatch.setattr(vectors, "VECTORS_DB_PATH", live)
    with pytest.raises(store.MisconfiguredDatabase, match="reading_rooms"):
        vectors.connect_rw()


def test_a_copy_of_orients_database_named_by_library_vectors_db_refuses(
        monkeypatch, tmp_path):
    """A copy at a path nobody would have listed — the path comparison half
    of `_refuse_orients_database` passes it, and the schema fingerprint does
    not, matching `test_a_copy_of_orients_database_refuses` for
    `LIBRARY_DB`."""
    served = library_db(tmp_path / "library.db")
    copied = orient_db(tmp_path / "backup-2026-08-15.db")
    monkeypatch.setattr(store, "DB_PATH", served)
    monkeypatch.setattr(vectors, "VECTORS_DB_PATH", copied)
    with pytest.raises(store.MisconfiguredDatabase, match="reading_rooms"):
        vectors.connect(served)


def test_a_normal_vectors_file_is_accepted(monkeypatch, tmp_path, vector_index):
    """The negative control: an ordinary vectors file, holding neither of
    `store.ORIENT_ONLY_TABLES`, attaches and reads exactly as before —
    proving the checks above refuse the Orient case rather than every
    attach."""
    served = library_db(tmp_path / "library.db")
    vector_index(served)
    monkeypatch.setattr(store, "DB_PATH", served)
    conn = vectors.connect(served)
    try:
        conn.execute(f"SELECT COUNT(*) FROM {vectors.VEC_TABLE}").fetchone()
    finally:
        conn.close()


def test_attach_refusal_opens_no_second_connection_to_the_vectors_file(
        monkeypatch, tmp_path):
    """**The fingerprint check runs through the handle the `ATTACH` itself
    opens, not a throwaway probe connection** (reviewer + security-reviewer,
    round 4). The prior version fingerprinted `VECTORS_DB_PATH` on a separate
    `sqlite3.connect`, closed it, and only then ran `ATTACH` — a symlink swap
    in that gap would validate one file and attach another. Counting every
    `sqlite3.connect` call that targets the vectors path catches a
    reintroduction of that probe: it should stay at zero, on both the read
    and the write door, while the refusal itself still fires."""
    served = library_db(tmp_path / "library.db")
    live = orient_db(tmp_path / "control.db")
    monkeypatch.setattr(store, "DB_PATH", served)
    monkeypatch.setattr(vectors, "VECTORS_DB_PATH", live)

    real_connect = sqlite3.connect
    vectors_path_connects = []

    def counting_connect(target, *args, **kwargs):
        if str(live) in str(target):
            vectors_path_connects.append(target)
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", counting_connect)
    with pytest.raises(store.MisconfiguredDatabase, match="reading_rooms"):
        vectors.connect(served)
    assert vectors_path_connects == []

    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    with pytest.raises(store.MisconfiguredDatabase, match="reading_rooms"):
        vectors.connect_rw()
    assert vectors_path_connects == []


def test_the_write_refusal_survives_a_swap_after_the_path_check(monkeypatch,
                                                                tmp_path):
    """**A path check has a window; a check through the handle does not**
    (security-reviewer, convivy/library#32, round 2).

    The swap is staged at the worst possible instant: `DB_PATH.is_file()` sees
    the Library's own file, and the path is repointed at Orient's database before
    `sqlite3.connect` opens it. A guard that stats the path before opening
    validates a file that is no longer the one opened, and waves this through.
    """
    live = orient_db(tmp_path / "control.db")
    own = tmp_path / "library.db"
    library_db(own)
    monkeypatch.setattr(store, "ORIENT_DB_DEFAULT", live)
    monkeypatch.setattr(store, "DB_PATH", own)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)

    real_is_file = Path.is_file

    def swap_after_the_check(self):
        answer = real_is_file(self)
        if self == own and own.exists() and not own.is_symlink():
            own.unlink()
            own.symlink_to(live)
        return answer

    monkeypatch.setattr(Path, "is_file", swap_after_the_check)
    with pytest.raises(store.MisconfiguredDatabase) as exc:
        store.connect_rw()
    assert "reading_rooms" in str(exc.value)


def test_a_handle_that_cannot_be_identified_refuses_the_write(monkeypatch,
                                                              tmp_path):
    """The inconclusive case on the write path: the file opened and the schema
    could not be read.

    Fails closed, matching every other inconclusive read in this service. It also
    has to close the handle before raising — a connection the caller never
    receives is a lock nobody would release.
    """
    junk = tmp_path / "library.db"
    junk.write_bytes(b"not a database" * 100)
    monkeypatch.setattr(store, "DB_PATH", junk)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    with pytest.raises(store.MisconfiguredDatabase) as exc:
        store.connect_rw()
    assert "could not be identified" in str(exc.value)


# --- LIBRARY_COLD_DB, the same refusal `resolve_db` gives `LIBRARY_DB` --------
#
# `store.resolve_cold_db_path` stays LAZY — called fresh inside `attach_cold`,
# never cached at this module's own import the way `DB_PATH` is (its own
# docstring) — so this is the one function-level test for the refusal `app.py`
# now calls eagerly at ITS OWN import (`tests/test_archives_cold_tier_reads.py`
# holds the subprocess test for that wiring; this module's own rule against
# re-import tests, above, is about `store.DB_PATH`'s wiring, not this one).


def test_cold_db_nothing_set_refuses_rather_than_guessing():
    with pytest.raises(store.MisconfiguredColdDB, match="LIBRARY_COLD_DB"):
        store.resolve_cold_db_path({})
