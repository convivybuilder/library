"""Where the Library finds the project registry when nothing names it directly.

Mirrors `root()` in agent-platform's `lib/agent-platform/builder_root.py`
(read 2026-10-02), the one place that resolves `CREWS_ROOT`. The Library must
not import agent-platform at runtime — it is its own repository with its own
venv and deploy, and agent-platform's `lib/` is a symlink farm that has gone
missing or pointed at a feature-branch worktree before (`projects.py`'s own
module docstring). So this is a small, separate copy of the same logic rather
than a dependency on it.

**A change to one must be made to the other.** `root()` there and `root()`
here read the same config key from the same file with the same fail-safe
rules, including the default-parameter wiring (round-1 review on PR #205:
this module used to recompute `fallback` and `config` from a per-call
`home` rather than from module-level constants, which could disagree with
`builder_root.root()` under a partial-override call even though no caller
in either repo makes one today) — a drift between them is exactly the
registry-divergence bug this module exists to close, moved one level up.
"""
from __future__ import annotations

import os
import pwd
from pathlib import Path


def real_account_home() -> Path:
    """The account's home from the passwd database, not `$HOME`.

    `$HOME` is whatever invoked this process carries — an ssh session,
    launchd, cron — and a different `$HOME` would read a different config
    file. Every home-derived value here comes from the system's own record of
    the account instead.
    """
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def fallback_for(home: Path) -> Path:
    """The compiled-in root for an account whose home is `home`, used when
    nothing is configured."""
    return home / "builder"


def config_for(home: Path) -> Path:
    """The per-machine config file for an account whose home is `home`."""
    return home / ".config" / "convivy" / "session-agent.env"


# Computed once, at import time, from the real passwd home — matching
# `builder_root.py`'s `FALLBACK` and `CONFIG` exactly, including the
# asymmetry with `home` below: `root()` recomputes `home` fresh on every
# call, but a `fallback` or `config` default comes from here rather than
# from whatever `home` that call happened to pass.
FALLBACK = fallback_for(real_account_home())
CONFIG = config_for(real_account_home())

# The only key the config file may set, read as DATA, never sourced or
# executed: the file is operator-editable, and a bad line in it must never do
# more than leave the setting unset.
KEY = "CREWS_ROOT"


def configured_raw(config: Path | None = None) -> str | None:
    """The raw `CREWS_ROOT` value from `config` (default CONFIG), or `None`
    when nothing is set.

    An absent file, an absent key, and a line this cannot parse all read the
    same. Surrounding quotes are tolerated, for a hand-edited value.
    """
    try:
        text = (CONFIG if config is None else config).read_text()
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() != KEY:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        return value
    return None


def expanded(raw: str, home: Path | None = None) -> Path:
    """`raw` with a leading `~` expanded against the PASSWD home.

    `Path.expanduser()` resolves `~` from `$HOME` when it is set, which would
    reintroduce the environment dependency `real_account_home` closes
    everywhere else. Only `~` and `~/...` are handled here; `~user` goes to
    `expanduser()`, which reads it from passwd.
    """
    home = real_account_home() if home is None else home
    if raw == "~":
        return home
    if raw.startswith("~/"):
        return home / raw[2:]
    return Path(raw).expanduser()


def root(config: Path | None = None, fallback: Path | None = None,
         home: Path | None = None) -> Path:
    """The machine's root: the configured `CREWS_ROOT`, or `fallback`.

    `config`, `fallback` and `home` default to the real config file, the
    real fallback, and the real passwd home; a caller passes its own only so
    a test can point those at a temporary directory. Matching
    `builder_root.root()` exactly, `fallback` and `config` default from the
    module-level FALLBACK and CONFIG (computed once at import time), while
    `home` is recomputed fresh on every call.

    FAILS SAFE, NEVER OPEN, matching agent-platform's `root()` exactly: a
    configured value that is relative, empty, unparseable, or names something
    that is not a directory falls back rather than being trusted or raising.
    So does a value too WIDE to act as a root — the filesystem root, the
    account's home, or any ancestor of home, or a symlink resolving to one —
    because each of those passes every other check while making any
    containment test against it vacuous. A non-home root that is merely wide
    (`/tmp`, `/var`) is accepted as given; see agent-platform's `root()` for
    why that is a limit rather than an oversight.

    Returned unresolved, as the caller wrote it, so a message quoting it shows
    the value actually in force.
    """
    fallback = FALLBACK if fallback is None else fallback
    home = real_account_home() if home is None else home
    raw = configured_raw(config)
    if not raw:
        return fallback
    try:
        candidate = expanded(raw, home)
        if not candidate.is_absolute():
            return fallback
        resolved = candidate.resolve()
    except (OSError, RuntimeError, ValueError):
        # ValueError is an embedded NUL (Path.resolve() on POSIX); RuntimeError
        # is a symlink loop. Either is a value this machine cannot make sense
        # of.
        return fallback
    if resolved.exists() and not resolved.is_dir():
        return fallback
    home = home.resolve()
    if resolved == Path(resolved.anchor) or resolved == home or resolved in home.parents:
        return fallback
    return candidate
