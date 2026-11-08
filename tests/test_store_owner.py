"""store_owner.py — running a store-opening step as LIBRARY_DB's own owner
once that store belongs to the Library's own service account
(company/decisions/0025 point 5, library issue CQ-225).

**The switch command is always a stub here, never the real `sudo`** — the
same reason `tests/test_install_library.py` never lets the real `launchctl`
near its own suite: a stub logs the exact argv `run_as_owner`/`can_switch_to`
built for it (one argument per line, a `---` separator between calls) and
either execs the real command onward (no real account switch, which is what
lets a case prove the real work still ran) or refuses before running
anything, the same shape a real `sudo -n` takes when becoming the target
account would need a password.
"""
from __future__ import annotations

import subprocess
import os
import sys

import pytest

import store_owner

STUB = """#!/bin/sh
printf '%s\\n' "$@" >> "${STUB_SWITCH_LOG:-/dev/null}"
echo '---' >> "${STUB_SWITCH_LOG:-/dev/null}"
if [ "${STUB_SWITCH_FAIL:-0}" = "1" ]; then
    echo "stub switch command: a password would be required" >&2
    exit 1
fi
shift 4
exec "$@"
"""


@pytest.fixture
def stub(tmp_path, monkeypatch):
    """Installs the stub as $LIBRARY_SWITCH_USER_CMD and points
    STUB_SWITCH_LOG at a scratch file; returns a function that reads back
    every call made so far, oldest first, each as its own argv list."""
    path = tmp_path / "switch-user-cmd"
    path.write_text(STUB)
    path.chmod(0o755)
    log = tmp_path / "switch-calls.log"
    monkeypatch.setenv("LIBRARY_SWITCH_USER_CMD", str(path))
    monkeypatch.setenv("STUB_SWITCH_LOG", str(log))

    def calls():
        if not log.exists():
            return []
        lines = log.read_text().splitlines()
        out, current = [], []
        for line in lines:
            if line == "---":
                out.append(current)
                current = []
            else:
                current.append(line)
        return out

    return calls


# --- file_owner ----------------------------------------------------------


def test_file_owner_reads_the_real_owner_of_an_existing_file(tmp_path):
    f = tmp_path / "library.db"
    f.write_text("x")
    assert store_owner.file_owner(f) == store_owner.current_username()


def test_file_owner_is_none_for_a_missing_file(tmp_path):
    assert store_owner.file_owner(tmp_path / "does-not-exist.db") is None


def test_file_owner_override_wins_over_the_real_stat_under_a_stub(
        stub, tmp_path, monkeypatch):
    f = tmp_path / "library.db"
    f.write_text("x")
    monkeypatch.setenv("LIBRARY_TEST_DB_OWNER", "otheruser")
    assert store_owner.file_owner(f) == "otheruser"


def test_file_owner_override_is_ignored_with_the_real_switch(tmp_path, monkeypatch):
    """With no stub, or a switch command named sudo, the owner comes from the
    file: a caller cannot choose the account the real sudo switches to."""
    f = tmp_path / "library.db"
    f.write_text("x")
    me = store_owner.current_username()
    monkeypatch.setenv("LIBRARY_TEST_DB_OWNER", "otheruser")
    monkeypatch.delenv("LIBRARY_SWITCH_USER_CMD", raising=False)
    assert store_owner.file_owner(f) == me
    monkeypatch.setenv("LIBRARY_SWITCH_USER_CMD", "/usr/bin/sudo")
    assert store_owner.file_owner(f) == me


# --- can_switch_to ---------------------------------------------------------


def test_can_switch_to_probes_with_a_plain_no_op(stub):
    assert store_owner.can_switch_to("otheruser") is True
    assert stub() == [["-n", "-u", "otheruser", "--", "true"]]


def test_can_switch_to_is_false_when_the_stub_refuses(stub, monkeypatch):
    monkeypatch.setenv("STUB_SWITCH_FAIL", "1")
    assert store_owner.can_switch_to("otheruser") is False
    assert stub() == [["-n", "-u", "otheruser", "--", "true"]]


# --- run_as_owner ----------------------------------------------------------


def test_run_as_owner_forwards_only_the_librarys_own_settings(
        stub, tmp_path, monkeypatch):
    """`env K=V` sets the child's environment past sudo's env_reset, so only
    LIBRARY_* settings and a fixed PATH may cross; PYTHONPATH, LD_PRELOAD,
    an arbitrary variable and the two that steer the switch may not. Read
    from the argv the switch command receives: the stub passes its own
    environment to the child, which real sudo would not."""
    marker = tmp_path / "ran"
    monkeypatch.setenv("LIBRARY_DB", "/srv/library/library.db")
    monkeypatch.setenv("LIBRARY_TEST_DB_OWNER", "otheruser")
    monkeypatch.setenv("PYTHONPATH", "/tmp/planted")
    monkeypatch.setenv("LD_PRELOAD", "/tmp/planted.so")
    monkeypatch.setenv("SOME_VAR", "some-value")
    monkeypatch.setenv("PATH", "/tmp/planted-bin:" + os.environ["PATH"])
    rc = store_owner.run_as_owner(
        "otheruser", [sys.executable, "-c",
                      f"open({str(marker)!r}, 'w').write('ran')"])
    assert rc == 0
    assert marker.read_text() == "ran"

    calls = stub()
    assert len(calls) == 1
    argv = calls[0]
    assert argv[:5] == ["-n", "-u", "otheruser", "--", "env"]
    exe = argv.index(sys.executable)
    pairs = argv[5:exe]
    assert pairs[0] == "PATH=/usr/bin:/bin:/usr/sbin:/sbin"
    keys = [p.split("=", 1)[0] for p in pairs[1:]]
    assert all(k.startswith("LIBRARY_") for k in keys), keys
    assert "LIBRARY_DB=/srv/library/library.db" in pairs
    for planted in ("PYTHONPATH", "LD_PRELOAD", "SOME_VAR",
                    "LIBRARY_TEST_DB_OWNER", "LIBRARY_SWITCH_USER_CMD"):
        assert planted not in keys


def test_run_as_owner_propagates_a_failing_exit_code(stub):
    rc = store_owner.run_as_owner("otheruser", [sys.executable, "-c", "import sys; sys.exit(7)"])
    assert rc == 7


# --- reexec_as_db_owner -----------------------------------------------------


def test_reexec_as_db_owner_is_a_no_op_when_the_owner_is_the_invoking_user(
        tmp_path, monkeypatch):
    """The single-account install's own path: with no override, a file this
    test process itself created is owned by this test process's own user,
    so nothing is switched and the switch command is never invoked at
    all."""
    db = tmp_path / "library.db"
    db.write_text("x")
    script = tmp_path / "worker.py"
    script.write_text("pass\n")

    def _must_not_run(*_a, **_k):
        raise AssertionError("subprocess.run must never be called")

    monkeypatch.setattr(subprocess, "run", _must_not_run)
    assert store_owner.reexec_as_db_owner(db, script, []) is None


def test_reexec_as_db_owner_is_a_no_op_when_the_file_does_not_exist_yet(tmp_path):
    script = tmp_path / "worker.py"
    script.write_text("pass\n")
    assert store_owner.reexec_as_db_owner(tmp_path / "nope.db", script, []) is None


def test_reexec_as_db_owner_switches_and_runs_the_real_work(stub, tmp_path, monkeypatch):
    db = tmp_path / "library.db"
    db.write_text("x")
    marker = tmp_path / "ran"
    script = tmp_path / "worker.py"
    script.write_text(f"open({str(marker)!r}, 'w').write('ran')\n")
    monkeypatch.setenv("LIBRARY_TEST_DB_OWNER", "otheruser")

    rc = store_owner.reexec_as_db_owner(db, script, [])
    assert rc == 0
    assert marker.read_text() == "ran"

    calls = stub()
    assert calls[0] == ["-n", "-u", "otheruser", "--", "true"]
    assert calls[1][:4] == ["-n", "-u", "otheruser", "--"]
    exe = calls[1].index(sys.executable)
    # -E -s: the switched interpreter ignores PYTHON* and the user site.
    assert calls[1][exe:exe + 4] == [sys.executable, "-E", "-s", str(script)]


def test_reexec_as_db_owner_refuses_and_runs_nothing(stub, tmp_path, monkeypatch):
    db = tmp_path / "library.db"
    db.write_text("x")
    marker = tmp_path / "ran"
    script = tmp_path / "worker.py"
    script.write_text(f"open({str(marker)!r}, 'w').write('ran')\n")
    monkeypatch.setenv("LIBRARY_TEST_DB_OWNER", "otheruser")
    monkeypatch.setenv("STUB_SWITCH_FAIL", "1")

    with pytest.raises(store_owner.OwnerSwitchRefused) as excinfo:
        store_owner.reexec_as_db_owner(db, script, [])
    assert "otheruser" in str(excinfo.value)
    assert not marker.exists()
    assert stub() == [["-n", "-u", "otheruser", "--", "true"]]
