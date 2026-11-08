"""store_owner — runs a store-opening step as the account that owns the
database file, for a scheduled job whose own account can no longer open it
once that store belongs to the Library's own service account
(company/decisions/0025 point 5, library issue CQ-225).

cold_tier.py's own move/restore pass and cert_expiry_check.py's own read of
`machine_certificates` and write through `store.insert_signal` each run as
the invoking user by default — the single-account install's own path,
unchanged. On a two- or three-account install, LIBRARY_DB belongs to a
no-login service account (`_library` on macOS, a `library` system user on
Linux) that the operator's own LaunchAgent account cannot open directly, so
each of these has to run AS that account instead.

**The owner is read from the database file itself, never assumed.** A
store's owner is whichever account created it, and `file_owner` below
never guesses otherwise.

**`reexec_as_db_owner` re-runs this same script through
`$LIBRARY_SWITCH_USER_CMD` (default `sudo`)**, in two steps: first a
complete no-op probe (`-n -u <owner> -- true`) that decides whether the
switch is even possible, then — only once that has succeeded — the real
re-exec, through the standalone `env` command rather than `subprocess`'s
own `env=` kwarg, because sudo resets the environment for the account it
switches to regardless of what this process's own environment holds
(sudo(8), `env_reset`); `env KEY=VALUE ... cmd` sets `cmd`'s own
environment directly, which sudo's reset cannot touch, so only the Library's
own settings are passed that way (`_forwarded_env`). `sudo -n` refuses
before running anything at all whenever becoming that account would need a
password (sudo(8): "-n ... if a password is required ... sudo will display
an error message and exit"), which is what makes a refused probe leave
nothing run. `$LIBRARY_SWITCH_USER_CMD` lets a test stand in a stub that
logs its own argv and then execs onward itself, rather than a real account
switch — `bin/install-library`'s own `INSTALL_LIBRARY_SWITCH_USER_CMD` seam
does the identical thing, in shell, for the identical reason."""
from __future__ import annotations

import os
import pwd
import subprocess
import sys
from pathlib import Path


class OwnerSwitchRefused(Exception):
    """The invoking user is neither `db_path`'s owner nor able to become it
    without a password prompt — nothing has been run."""

    def __init__(self, db_path, owner: str, current_user: str):
        self.db_path = db_path
        self.owner = owner
        self.current_user = current_user
        super().__init__(
            f"{db_path} is owned by {owner!r}, and {current_user!r} cannot "
            f"switch to it without a password prompt")


def file_owner(path: str | Path) -> str | None:
    """`path`'s owning username, read from the file itself — or `None` when
    it doesn't exist yet or its owning uid has no passwd entry.

    `LIBRARY_TEST_DB_OWNER` overrides this, for a test to simulate a store
    owned by an account that doesn't really exist on this machine, and only
    while `_switch_is_stubbed()`: with the real `sudo` the owner always comes
    from the file, so a caller cannot choose which account to switch to."""
    override = os.environ.get("LIBRARY_TEST_DB_OWNER")
    if override and _switch_is_stubbed():
        return override
    try:
        uid = os.stat(path).st_uid
    except OSError:
        return None
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return None


def current_username() -> str:
    return pwd.getpwuid(os.geteuid()).pw_name


def _switch_cmd() -> str:
    return os.environ.get("LIBRARY_SWITCH_USER_CMD", "sudo")


def _switch_is_stubbed() -> bool:
    """Whether a test has replaced the switch command with a stub that is
    not `sudo`. A symlink to `sudo` under another name defeats this, but
    then the switch is bounded by what sudoers already lets the caller run
    as that account."""
    cmd = os.environ.get("LIBRARY_SWITCH_USER_CMD")
    return bool(cmd) and os.path.basename(cmd) != "sudo"


# What crosses the switch. Only the Library's own settings, none of which
# loads code, and a fixed PATH. Never the whole environment: `env K=V` sets
# the child's environment past sudo's `env_reset`, so forwarding everything
# would hand PYTHONPATH, LD_PRELOAD and the like to the service account's
# interpreter. The two variables that steer the switch itself stay behind.
_FORWARDED_PREFIX = "LIBRARY_"
_NEVER_FORWARDED = frozenset({"LIBRARY_SWITCH_USER_CMD", "LIBRARY_TEST_DB_OWNER"})
_SWITCHED_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


def _forwarded_env() -> list[str]:
    pairs = [f"PATH={_SWITCHED_PATH}"]
    for key, value in sorted(os.environ.items()):
        if key.startswith(_FORWARDED_PREFIX) and key not in _NEVER_FORWARDED:
            pairs.append(f"{key}={value}")
    return pairs


def can_switch_to(owner: str) -> bool:
    """Whether the invoking user can become `owner` without a password
    prompt — probed with a complete no-op (`true`), never any part of a
    real store-opening command, so a failed probe guarantees nothing real
    has run."""
    result = subprocess.run([_switch_cmd(), "-n", "-u", owner, "--", "true"])
    return result.returncode == 0


def run_as_owner(owner: str, argv: list[str]) -> int:
    """Runs `argv` as `owner`, passing the Library's own settings along
    explicitly through `env` (`_forwarded_env`; see the module docstring for
    why `env`). Never probes first — the caller is expected to have already
    confirmed `can_switch_to`."""
    full_argv = [_switch_cmd(), "-n", "-u", owner, "--", "env",
                 *_forwarded_env(), *argv]
    return subprocess.run(full_argv).returncode


def reexec_as_db_owner(db_path: str | Path, script_path: str | Path,
                       args: list[str]) -> int | None:
    """Re-executes `script_path` (`sys.executable script_path *args`) as
    `db_path`'s owning account, when that account isn't already the
    invoking user. `script_path` and `args` are the caller's own — never
    read from `sys.argv` here, so a caller invoked programmatically with
    its own argument list (every test in this file) re-execs exactly that
    list, rather than whatever this interpreter's REAL command line
    happens to be (pytest's own, under a test).

    Returns `None` when no switch was needed — `db_path` doesn't exist yet,
    its owner can't be read, or its owner already is the invoking user —
    which is the caller's signal to proceed in-process exactly as a
    single-account install always has. Returns an exit code when a switch
    happened and the real work already ran, in the child; the caller must
    propagate it rather than redoing any of that work itself.

    Raises `OwnerSwitchRefused`, naming the owner, when `can_switch_to`
    fails — before anything else has run."""
    owner = file_owner(db_path)
    me = current_username()
    if owner is None or owner == me:
        return None
    if not can_switch_to(owner):
        raise OwnerSwitchRefused(db_path, owner, me)
    # -E and -s: the switched interpreter ignores every PYTHON* variable and
    # the user site directory, whatever sudo's env_keep lets through.
    return run_as_owner(
        owner, [sys.executable, "-E", "-s", str(script_path), *args])
