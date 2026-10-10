"""`bootstrap_library.py`: the local command that makes the first administrator
and mints the first approved-client credential, once.

**Every fixture builds the adopter's first-install condition rather than a
developer machine's**: a database migrated to head with nothing in it, a
`client-tokens.json` that does not exist inside a directory that does not exist,
no `api-tokens.json`, and no TLS material. A test that passed only because
`~/.library/client-tokens.json` was already there would prove nothing about the
one machine this command exists for.

**The schema is the real one**, built by running `alembic upgrade head`, not a
hand-written `CREATE TABLE`. The guard is a table's primary key and check
constraint, so a copy of the DDL would test the copy.
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import bootstrap_library
import credential_binding
import identity
import mtls
import store

REPO = Path(__file__).resolve().parent.parent
NAME = "Ada Lovelace"


def _break_commit(monkeypatch, *, after=None):
    """Make the next `store.connect_rw()` return a connection whose `commit()`
    misbehaves, and hand back the real `connect_rw` so a test can restore it.

    With no `after`, `commit()` raises before doing anything, which is the
    ordinary failed-commit case. With `after` — a callable — the real commit
    runs FIRST and then `after()` is called, which is how a test reaches the
    state no flag in the calling code can observe: a record that is already
    durable while the caller is being unwound.
    """
    real_connect_rw = store.connect_rw

    class BadCommit:
        def __init__(self, conn):
            self._conn = conn

        def commit(self):
            if after is None:
                raise sqlite3.OperationalError("disk I/O error")
            self._conn.commit()
            after()

        def __getattr__(self, name):
            return getattr(self._conn, name)

    monkeypatch.setattr(store, "connect_rw", lambda: BadCommit(real_connect_rw()))
    return real_connect_rw


@pytest.fixture
def first_install(tmp_path, monkeypatch):
    """A migrated, empty database and no credential of any kind on disk."""
    db = tmp_path / "library.db"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=str(REPO),
        capture_output=True, text=True, check=False,
        env={"PATH": "/usr/bin:/bin", "HOME": str(REPO), "LIBRARY_DB": str(db)})
    assert result.returncode == 0, result.stderr

    # The directory does not exist either: `~/.library` is created by the
    # command on an adopter's machine, it is not there waiting.
    tokens = tmp_path / "state" / ".library" / "client-tokens.json"
    assert not tokens.parent.exists()
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    monkeypatch.setattr(identity, "CLIENT_TOKENS", tokens)
    monkeypatch.setattr(identity, "TOKENS", tmp_path / "no-api-tokens.json")
    for name in ("TLS_CERT", "TLS_KEY", "TLS_CA"):
        monkeypatch.setattr(mtls, name, tmp_path / "no-tls" / name)
    return db


def _rows(db, sql, *args):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def _run(capsys, *argv):
    """Run the command; return `(exit_code, stdout, stderr)`."""
    rc = bootstrap_library.main(list(argv))
    out, err = capsys.readouterr()
    return rc, out, err


# --- the property that distinguishes a recorded event from a row count -------


def test_a_second_bootstrap_is_refused_after_the_person_record_is_emptied(
        first_install, capsys):
    """THE TEST THAT MAKES THE GUARD A RECORD RATHER THAN A COUNT.

    A guard reading "no administrator exists" passes the populated case in the
    next test just as a recorded event does, so that one proves nothing. This is
    the case where they differ: everything a person-shaped table holds has been
    deleted, and only a row recording that the bootstrap HAPPENED still says so.
    """
    rc, _, _ = _run(capsys, NAME)
    assert rc == 0
    tokens_after_first = identity.CLIENT_TOKENS.read_bytes()

    conn = sqlite3.connect(first_install)
    conn.execute("DELETE FROM person_products")
    conn.execute("DELETE FROM administrators")
    conn.execute("DELETE FROM people")
    conn.commit()
    conn.close()
    for table in ("people", "administrators", "person_products"):
        assert _rows(first_install, f"SELECT COUNT(*) FROM {table}") == [(0,)]

    rc, out, err = _run(capsys, "Someone Else")
    assert rc == 1
    assert out == "", "a refusal must not print a credential"
    assert "already" in err
    assert _rows(first_install, "SELECT COUNT(*) FROM people") == [(0,)]
    assert _rows(first_install, "SELECT COUNT(*) FROM administrators") == [(0,)]
    assert identity.CLIENT_TOKENS.read_bytes() == tokens_after_first, (
        "a refused bootstrap minted a second credential")


# --- what one bootstrap does -------------------------------------------------


def test_it_runs_with_no_administrator_no_token_file_and_no_certificate(
        first_install, capsys):
    assert not identity.CLIENT_TOKENS.exists()
    assert not identity.TOKENS.exists()
    assert not mtls.TLS_CA.exists()
    assert _rows(first_install, "SELECT COUNT(*) FROM administrators") == [(0,)]
    rc, _, err = _run(capsys, NAME)
    assert rc == 0, err


def test_it_records_the_event(first_install, capsys):
    rc, token, _ = _run(capsys, NAME)
    assert rc == 0
    rows = _rows(first_install,
                 "SELECT id, person_id, credential_sha256, performed_by "
                 "FROM bootstrap_record")
    assert len(rows) == 1
    record_id, person_id, digest, performed_by = rows[0]
    assert record_id == 1
    assert digest == credential_binding.sha256_token(token.strip())
    assert performed_by, "the record must say which local user ran it"
    people = _rows(first_install, "SELECT id FROM people")
    assert people == [(person_id,)]


def test_it_makes_the_named_person_an_administrator(first_install, capsys):
    rc, _, _ = _run(capsys, NAME)
    assert rc == 0
    person = _rows(first_install, "SELECT id, display_name FROM people")
    assert [name for _, name in person] == [NAME]
    admins = _rows(first_install,
                   "SELECT person_id, granted_by FROM administrators")
    assert admins == [(person[0][0], "bootstrap_library")]


def test_administrator_is_not_a_product_grant(first_install, capsys):
    """A product grant and an administrator are different facts (the schema's
    stated reason for two tables). Making someone an administrator must not
    grant them a product, and a product read must not see it."""
    rc, _, _ = _run(capsys, NAME)
    assert rc == 0
    assert _rows(first_install, "SELECT COUNT(*) FROM person_products") == [(0,)]


def test_the_credential_is_one_the_librarys_own_identity_resolves(
        first_install, capsys):
    rc, out, _ = _run(capsys, NAME)
    assert rc == 0
    token = out.strip()
    assert token, "the credential is the command's one product on stdout"

    assert token in identity._client_tokens()
    ident = identity.resolve(f"Bearer {token}")
    assert ident is not None
    assert ident.kind == "approved_client"
    assert identity.authorized(ident, "person.read")
    person_id = _rows(first_install, "SELECT id FROM people")[0][0]
    assert ident.actor == person_id
    assert identity.in_scope(ident, "any-product-at-all")


def test_stdout_is_the_token_alone_and_status_goes_to_stderr(
        first_install, capsys):
    rc, out, err = _run(capsys, NAME)
    assert rc == 0
    assert out.count("\n") == 1 and out.strip() == out.rstrip("\n")
    person_id = _rows(first_install, "SELECT id FROM people")[0][0]
    assert person_id in err
    assert str(identity.CLIENT_TOKENS) in err
    assert out.strip() not in err, "the credential belongs on stdout only"


def test_the_credential_file_is_private_and_so_is_a_directory_it_created(
        first_install, capsys):
    rc, _, _ = _run(capsys, NAME)
    assert rc == 0
    file_mode = stat.S_IMODE(identity.CLIENT_TOKENS.stat().st_mode)
    dir_mode = stat.S_IMODE(identity.CLIENT_TOKENS.parent.stat().st_mode)
    assert file_mode == 0o600, oct(file_mode)
    assert dir_mode == 0o700, oct(dir_mode)
    leftovers = [p.name for p in identity.CLIENT_TOKENS.parent.iterdir()
                 if p != identity.CLIENT_TOKENS]
    assert leftovers == [], f"a temporary file was left behind: {leftovers}"


def test_the_raw_token_never_reaches_the_database(first_install, capsys):
    rc, out, _ = _run(capsys, NAME)
    assert rc == 0
    assert out.strip().encode() not in first_install.read_bytes()


def test_a_credential_already_in_the_file_survives(first_install, capsys):
    identity.CLIENT_TOKENS.parent.mkdir(parents=True)
    identity.CLIENT_TOKENS.write_text(json.dumps(
        {"an-existing-token": {"name": "orient", "products": ["*"]}}))
    rc, out, _ = _run(capsys, NAME)
    assert rc == 0
    loaded = identity._client_tokens()
    assert loaded["an-existing-token"] == {"name": "orient", "products": ["*"]}
    assert out.strip() in loaded
    assert len(loaded) == 2


# --- a failed bootstrap must not burn the one-time record --------------------


def test_a_file_that_is_not_a_json_object_is_refused_and_nothing_is_recorded(
        first_install, capsys):
    """Overwriting a file the Library could not parse would destroy whatever
    credentials it held; recording the bootstrap without writing the credential
    would leave the guard refusing forever with nothing to authenticate."""
    identity.CLIENT_TOKENS.parent.mkdir(parents=True)
    identity.CLIENT_TOKENS.write_text("[1, 2, 3]")
    rc, out, _ = _run(capsys, NAME)
    assert rc == 1
    assert out == ""
    assert identity.CLIENT_TOKENS.read_text() == "[1, 2, 3]"
    assert _rows(first_install, "SELECT COUNT(*) FROM bootstrap_record") == [(0,)]
    assert _rows(first_install, "SELECT COUNT(*) FROM people") == [(0,)]
    assert _rows(first_install, "SELECT COUNT(*) FROM administrators") == [(0,)]


def test_a_credential_file_that_cannot_be_written_leaves_the_bootstrap_unrecorded(
        first_install, tmp_path, monkeypatch, capsys):
    """The lockout this command's own guard could otherwise cause: the record
    committed, the credential never written, and every retry refused."""
    blocker = tmp_path / "a-file-where-a-directory-should-be"
    blocker.write_text("")
    monkeypatch.setattr(identity, "CLIENT_TOKENS", blocker / "client-tokens.json")

    rc, out, err = _run(capsys, NAME)
    assert rc == 1
    assert out == ""
    assert _rows(first_install, "SELECT COUNT(*) FROM bootstrap_record") == [(0,)]
    assert _rows(first_install, "SELECT COUNT(*) FROM people") == [(0,)]
    assert _rows(first_install, "SELECT COUNT(*) FROM administrators") == [(0,)]

    monkeypatch.setattr(identity, "CLIENT_TOKENS",
                        tmp_path / "fixed" / "client-tokens.json")
    rc, out, err = _run(capsys, NAME)
    assert rc == 0, err
    assert out.strip() in identity._client_tokens()


def test_a_failure_after_the_credential_is_written_restores_the_file(
        first_install, monkeypatch, capsys):
    """The commit is the last step and can still fail. The credential file was
    already replaced by then, so it goes back to what it held."""
    identity.CLIENT_TOKENS.parent.mkdir(parents=True)
    before = json.dumps({"an-existing-token": {"name": "orient", "products": ["*"]}})
    identity.CLIENT_TOKENS.write_text(before)

    _break_commit(monkeypatch)
    rc, out, _ = _run(capsys, NAME)
    assert rc == 1
    assert out == ""
    assert identity.CLIENT_TOKENS.read_text() == before
    assert _rows(first_install, "SELECT COUNT(*) FROM bootstrap_record") == [(0,)]


def test_a_failure_on_a_first_install_leaves_no_credential_file_behind(
        first_install, monkeypatch, capsys):
    """The same commit failure with NO file to restore, which is the adopter's
    actual first install.

    **This is the branch, not a variation on the one above.** That test seeds an
    existing `client-tokens.json`, so it reaches only the restore half of the
    rollback; every other failure test raises before `written = True` and so
    reaches neither half. Nothing exercised the `prior is None` branch that
    UNLINKS the file, and deleting that branch left the suite green — a live
    `["*"]` credential in a fresh file, with the guard unconsumed, and no test
    saying so. Found in review of this PR (reviewer, round 1, PR #134).
    """
    real_connect_rw = _break_commit(monkeypatch)
    rc, out, _ = _run(capsys, NAME)
    assert rc == 1
    assert out == ""
    # The file the command created must be gone, not left holding a credential
    # nothing recorded.
    assert not identity.CLIENT_TOKENS.exists()
    assert _rows(first_install, "SELECT COUNT(*) FROM bootstrap_record") == [(0,)]

    # And the command is still runnable, which is the whole point of rolling the
    # record back.
    monkeypatch.setattr(store, "connect_rw", real_connect_rw)
    rc, out, err = _run(capsys, NAME)
    assert rc == 0, err
    assert out.strip() in identity._client_tokens()


def test_an_interruption_after_the_commit_keeps_the_credential(
        first_install, monkeypatch, capsys):
    """THE CASE A FLAG IN THE CALLING CODE CANNOT SEE.

    `commit()` blocks in fsync, so a Ctrl-C arriving during it is delivered the
    instant the call returns, at the call's own interrupt check — before any
    statement after it runs. The handler is therefore entered with the record
    already durable, and undoing the credential file there consumes the guard,
    erases the credential and never prints the token, which is the one state
    that cannot be recovered from: every retry refuses.

    So the handler reads `bootstrap_record` instead of a flag. This test pins
    that. It fails against a flag-based handler, which is how the fix was
    checked rather than assumed (reviewer, PR #134 round 2).
    """
    def interrupt():
        raise KeyboardInterrupt

    _break_commit(monkeypatch, after=interrupt)
    with pytest.raises(KeyboardInterrupt):
        bootstrap_library.main([NAME])
    capsys.readouterr()

    # The record committed, so the credential that matches it must still be there.
    assert _rows(first_install, "SELECT COUNT(*) FROM bootstrap_record") == [(1,)]
    assert identity.CLIENT_TOKENS.exists(), (
        "the credential was erased over a durable record — the Library is now "
        "unbootstrappable, since the guard refuses every retry")
    entries = identity._client_tokens()
    assert len(entries) == 1
    (token,) = entries
    digest = _rows(first_install, "SELECT credential_sha256 FROM bootstrap_record")
    assert digest == [(credential_binding.sha256_token(token),)], (
        "the surviving credential is not the one the record names")

    # And it authenticates, which is what makes the survival worth anything.
    ident = identity.resolve(f"Bearer {token}")
    assert ident is not None and ident.kind == "approved_client"


def test_a_connection_too_broken_to_answer_keeps_the_credential(
        first_install, monkeypatch, capsys):
    """THE FAIL-OPEN DIRECTION, WHICH IS THE ONE PROPERTY THE DOCSTRING SHOUTS.

    `_record_is_durable` cannot always tell whether the commit landed. Which way
    it errs is the whole decision: keeping a credential whose record did not
    commit leaves an inert entry in a 0600 file, while erasing one whose record
    DID commit locks the Library out for good. Nothing exercised that branch, so
    flipping it to `False` left the suite green — a capitalised promise with no
    check behind it, which is the shape this PR keeps finding
    (reviewer, PR #134 round 3).
    """
    real_connect_rw = store.connect_rw
    commit_error = sqlite3.OperationalError("disk I/O error")

    class ProbeFails:
        """Commit fails, and so does any query after it — a connection that
        cannot answer whether the record is there."""

        def __init__(self, conn):
            self._conn = conn
            self._commit_attempted = False

        def commit(self):
            self._commit_attempted = True
            raise commit_error

        def execute(self, *args, **kwargs):
            if self._commit_attempted:
                raise sqlite3.ProgrammingError("cannot operate on a closed database")
            return self._conn.execute(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    monkeypatch.setattr(store, "connect_rw", lambda: ProbeFails(real_connect_rw()))
    rc, out, err = _run(capsys, NAME)

    assert rc == 1
    assert out == "", "a failed bootstrap must not print a credential"
    assert "disk I/O error" in err, (
        "the probe's own error replaced the failure the operator needs to see")
    assert identity.CLIENT_TOKENS.exists(), (
        "the credential was erased on a connection that could not say whether "
        "the record committed; if it had, every retry would refuse")

    # The record did NOT commit here, so the kept entry is the inert residue the
    # module docstring describes, and the command is still runnable.
    assert _rows(first_install, "SELECT COUNT(*) FROM bootstrap_record") == [(0,)]
    monkeypatch.setattr(store, "connect_rw", real_connect_rw)
    rc, out, err = _run(capsys, NAME)
    assert rc == 0, err
    assert out.strip() in identity._client_tokens()


# --- refusals that are not the one-time guard --------------------------------


def test_a_second_bootstrap_is_refused_against_the_populated_record(
        first_install, capsys):
    rc, _, _ = _run(capsys, NAME)
    assert rc == 0
    tokens_after_first = identity.CLIENT_TOKENS.read_bytes()

    rc, out, err = _run(capsys, "Someone Else")
    assert rc == 1
    assert out == ""
    assert "already" in err
    assert _rows(first_install, "SELECT COUNT(*) FROM people") == [(1,)]
    assert identity.CLIENT_TOKENS.read_bytes() == tokens_after_first


def test_the_refusal_names_who_ran_it_and_when(first_install, capsys):
    _run(capsys, NAME)
    performed_at, person_id = _rows(
        first_install, "SELECT performed_at, person_id FROM bootstrap_record")[0]
    rc, _, err = _run(capsys, NAME)
    assert rc == 1
    assert str(performed_at) in err
    assert person_id in err


def test_it_refuses_while_writes_are_disabled(first_install, monkeypatch, capsys):
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    rc, _, _ = _run(capsys, NAME)
    assert rc == 1
    assert not identity.CLIENT_TOKENS.exists()


def test_it_refuses_a_database_nobody_has_migrated(tmp_path, monkeypatch, capsys):
    db = tmp_path / "unmigrated.db"
    sqlite3.connect(db).close()
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    monkeypatch.setattr(identity, "CLIENT_TOKENS", tmp_path / "client-tokens.json")
    rc, _, err = _run(capsys, NAME)
    assert rc == 1
    assert "alembic upgrade head" in err
    assert not identity.CLIENT_TOKENS.exists()


@pytest.mark.parametrize("name", ["", "   ", "x" * 301])
def test_it_refuses_a_name_the_people_table_cannot_hold(first_install, capsys, name):
    rc, _, _ = _run(capsys, name)
    assert rc == 2
    assert _rows(first_install, "SELECT COUNT(*) FROM bootstrap_record") == [(0,)]


def test_it_takes_no_secret_as_an_argument_or_from_stdin(first_install, monkeypatch, capsys):
    """The command mints its credential rather than being handed one, so there is
    no secret for argv to carry. Anything on stdin is ignored, not consumed as
    a token, and no option names one."""
    monkeypatch.setattr("sys.stdin", io.StringIO("a-token-someone-piped-in\n"))
    rc, out, _ = _run(capsys, NAME)
    assert rc == 0
    assert out.strip() != "a-token-someone-piped-in"
    with pytest.raises(SystemExit):
        bootstrap_library.main([NAME, "--token", "x"])


# --- the file itself ---------------------------------------------------------


def test_the_script_is_executable_and_names_the_deployed_interpreter():
    """`python3.11`, matching `bind_credential.py`. `python3.14` does not resolve
    on Ubuntu 22.04, and `enroll_machine.py` and `generate_csr.py` carry it as
    known drift that is not this command's to repeat."""
    path = REPO / "bootstrap_library.py"
    assert path.read_text().splitlines()[0] == "#!/usr/bin/env python3.11"
    assert os.access(path, os.X_OK)


def test_the_module_states_that_local_write_access_is_outside_the_guard():
    """The boundary is stated where the next reader will read it, and a rewrite
    that drops it fails here rather than silently."""
    doc = bootstrap_library.__doc__
    assert "local write access" in doc
    assert "DELETE FROM bootstrap_record" in doc
