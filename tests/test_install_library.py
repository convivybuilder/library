"""bin/install-library — the deploy-pin installer for the Library
(platform/decisions/0059).

Every case here runs against a from-scratch fixture repository, never this
checkout, so the test controls exactly what "main" contains and does not
depend on which branch this suite happens to be running from. `HOME` is
redirected to a temp directory for every run, so nothing here writes to a
real `~/.local/deploy/library`, a real `~/Library/LaunchAgents`, or a real
`~/.library` — the last of which is the point of one test below.

The fixture repo's `requirements.txt` is deliberately empty: the installer
builds a real venv and runs a real `pip install -r requirements.txt` against
it, and a requirements file with nothing in it needs no network access to
satisfy, which is what keeps every case here hermetic and fast. What the
installer does with a real dependency set is not this file's concern — it is
the same `pip install` either way, and the mechanics under test are the
archive, the swap, the credential exclusion, and the DEPLOYED_COMMIT/plist
handling around it.

The fixture's `serve.sh` is a stub, not a real copy — `test_serve_guard.py`
already covers the real guard exhaustively, and re-running it here through a
fresh, dependency-free venv would make the installer's OWN success path
depend on a preflight this file has no reason to fail or pass on the real
guard's terms. The stub answers `--check` the way a real serve.sh would once
its dependencies are actually installed: 0 by default, and a controlled
failure when a marker file the fixture plants asks it to, which is what
`test_failed_install_leaves_the_previous_deploy_untouched` uses to exercise
the installer's own atomicity rather than serve.sh's. It also logs the
`LIBRARY_CHECK_PLIST` value each `--check` call received to
`STUB_SERVE_CHECK_ENV_LOG` (always set by `run_installer`, read back by
`serve_check_env_log`) — real `serve.sh`'s own handling of that variable is
`test_serve_guard.py`'s to cover; what this file tests is which plist
`bin/install-library` PASSES it, which this stub can observe without being a
real `serve.sh` at all.

**`launchctl` is stubbed the same way, and for a sharper reason: the real
one is not safe to invoke here at all.** The fixture plist's `Label` is the
real `com.convivy.library` (it has to be, to look like the template this
installer reads), so an installer that calls real `launchctl bootout`/
`bootstrap` against that label — even under a fake `$HOME` — would reach
past the test's sandbox and touch the actual running job. `run_installer`
therefore puts a stub `launchctl` ahead of the real one on `PATH` for every
case in this file, not only the ones that mention reload by name: once
`bin/install-library` reloads on any plist change, and every fresh install
here starts from no live plist at all, that is most of the cases in this
file. The stub logs each call it receives to `STUB_LAUNCHCTL_LOG`, and
persists two more things across calls within one test so the installer's own
reload-verification step has something real to check: `bootstrap`'s
environment (parsed from whatever plist path it was given, into
`STUB_LAUNCHCTL_ENV_STATE_FILE`, cleared by `bootout`) and a per-test call
count (`STUB_LAUNCHCTL_PRINT_COUNT_FILE`) that lets a case make `print`
report `waiting` for its first N calls and `running` after
(`STUB_LAUNCHCTL_WAITING_FOR_CALLS`). `STUB_LAUNCHCTL_DROP_ENV_KEY` drops one
key from what `print` reports back, simulating a bootstrap that "succeeded"
without actually applying that variable; `STUB_LAUNCHCTL_TRUNCATE_ENV_KEY`,
`STUB_LAUNCHCTL_EXTEND_ENV_KEY`/`_SUFFIX`, and `STUB_LAUNCHCTL_RENAME_ENV_KEY`/
`_TO` corrupt one key's reported value or name the same way, for the
exact-match cases below (round 2: a truncated value, a value with the
expected one as a prefix, and a different key whose name merely ends in the
expected one must all fail verification, not false-pass an unanchored
substring check). `STUB_LAUNCHCTL_BOOTSTRAP_FAIL_IF_CONTAINS` fails only the
`bootstrap` call whose plist file contains the given text, so a case can make
the FIRST bootstrap (the new plist) fail while a recovery bootstrap (the old,
backed-up one) still succeeds, or make both fail by matching text common to
both. `run_installer` always sets the reload-verify timeout and poll interval
to small values via `INSTALL_LIBRARY_RELOAD_VERIFY_*_SECONDS` so a case
exercising the retry bound does not spend real wall-clock time on it.
`STUB_LAUNCHCTL_BOOTOUT_FAIL` makes `bootout` itself exit nonzero, which the
installer is required to tolerate (its own exit status is never checked).
`STUB_LAUNCHCTL_EXIT_TIMEOUT` puts an `exit timeout = N` line into `print`'s
own output, defaulting to `60` — the effective-ExitTimeOut check now fails
closed on either an unreadable `exit timeout` or an unreadable
`_DRAIN_DEADLINE_SECONDS`, so `_write_fixture_files`'s stub `app.py` carries
`_DRAIN_DEADLINE_SECONDS = 50.0` by default too, and every case that reaches
`_verify_reload` without overriding either one verifies with margin (50 <
60) rather than skipping the comparison. The cases under "effective
ExitTimeOut" below override one or both to exercise the boundary and the
fail-closed paths themselves.

`print`'s stub output is shaped like the real thing (round 3 review, PR
#157): it always emits `inherited environment = { ... }` and `default
environment = { ... }` blocks ahead of the real `environment = { ... }`
one, exactly as `launchctl print` does — the earlier stub omitted both,
which is how an unanchored parser that pooled all three together passed
every case here. `STUB_LAUNCHCTL_INHERITED_INJECT`/`_DEFAULT_INJECT`
(`KEY=VALUE`) put one extra pair into the inherited/default block, for a
case that drops a key from the real block and proves the parser doesn't
pick up a same-named substitute from either of the other two.
`INSTALL_LIBRARY_TEST_FAIL_RECOVERY_SWAP=1` is a seam in the installer
itself, not the stub: it forces the restoring `mv "$OLD" "$DEPLOY_DIR"` to
fail, without needing a real filesystem-level failure to provoke it — the
same command appears twice in the installer (the primary swap's own
self-heal, and the recovery trap's pair-restore), and this seam covers
whichever one a given test actually reaches. `INSTALL_LIBRARY_TEST_FAIL_PRIMARY_SWAP=1`
forces the primary swap's first `mv "$STAGE" "$DEPLOY_DIR"` to fail, the
one that puts the restoring `mv` above in play at all.
`INSTALL_LIBRARY_TEST_FAIL_PLIST_WRITE=1` forces the final `mv
"$PLIST_CANDIDATE" "$PLIST_LIVE"` (the success-path plist write, after the
code swap) to fail. `INSTALL_LIBRARY_TEST_FAIL_TRAP_RM=1` forces every
previously-unguarded `rm` inside the recovery trap's own cleanup to fail —
round 4 review, PR #157: those calls used to be unchecked, reachable while
`set -eu` stays active inside a running EXIT trap on this bash 3.2.57
`/bin/sh`, where an abort mid-cleanup could skip the RECOVERY FAILED
message and the caller's own `$?` along with it.
`INSTALL_LIBRARY_TEST_FAIL_RECOVERY_CP=1` (security round 1, PR #158) forces
the recovery trap's own `cp "$PLIST_BACKUP" "$PLIST_LIVE"` to fail — the copy
meant to make the live plist FILE agree with the previous release once it's
back up and verified. All five are seams in the installer itself, not the
stub, and none needs a real filesystem-level failure to provoke it.

`tests/fixtures/launchctl_print_library.txt` is a real, once-captured
`launchctl print` of this exact job (checked for secrets before being
committed — its environment is paths and flags only) — used directly
against the installer's own `--internal-launchctl-env-pairs` hidden entry
point, so the anchoring fix is proven against real output, not just the
stub's approximation of it.

**`PATH` ordering alone used to be the only thing keeping the real
`launchctl` out of this suite** — on 2026-10-02, running install-library
directly from a shell under a fake `$HOME` (not through this harness)
reached the real `launchctl` anyway and booted out the real, live Library
service by its real label, because a fake `$HOME` changes none of the real
uid, the real `launchctl` binary, or the real domain it targets.
`install-library` now takes all three — the binary
(`INSTALL_LIBRARY_LAUNCHCTL`), the domain (`INSTALL_LIBRARY_LAUNCHD_DOMAIN`,
default `gui/<uid>`), and the LaunchAgents directory
(`INSTALL_LIBRARY_LAUNCH_AGENTS`) — from environment settings, and
`run_installer` below pins all three to the stub binary, `TEST_LAUNCHD_DOMAIN`
(a string shaped so it can never be a real `gui/<uid>` domain) and a scratch
directory under `home`, on every call. `STUB_LAUNCHCTL_EXPECTED_DOMAIN`,
always set alongside them, makes the stub itself refuse any call — bootout,
bootstrap or print — against any other domain, so a harness that forgets to
pin `INSTALL_LIBRARY_LAUNCHD_DOMAIN` gets the stub's own loud refusal
instead of silently reaching install-library's real-domain default; see the
"Part A: launchd isolation" cases near the end of this file.
"""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

INSTALLER = Path(__file__).resolve().parent.parent / "bin" / "install-library"
FIXTURE_LAUNCHCTL_PRINT = Path(__file__).resolve().parent / "fixtures" / "launchctl_print_library.txt"

# A domain string that can never be a real launchd domain (those are always
# "gui/<decimal-uid>") — passed to every installer run under test via
# INSTALL_LIBRARY_LAUNCHD_DOMAIN, and to the stub via
# STUB_LAUNCHCTL_EXPECTED_DOMAIN, so the stub can refuse any call that didn't
# go through this pinning (Part A, library issue
# issue_01M1VW2D48D8F4B6BM6AK28D7F round 3).
TEST_LAUNCHD_DOMAIN = f"gui/test-{os.getpid()}"

STUB_SERVE_SH = """#!/bin/sh
# Stub for install-library tests — see test_install_library.py's module
# docstring for why this is not a real copy of serve.sh.
#
# STUB_SERVE_CHECK_ENV_LOG, when set, appends the LIBRARY_CHECK_PLIST value
# this invocation received to that file — real serve.sh's own LIBRARY_CHECK_
# PLIST handling is test_serve_guard.py's, not this stub's; what this file
# tests is which plist install-library PASSES, which is observable without
# a real serve.sh at all.
if [ "$1" = "--check" ]; then
    if [ -n "${STUB_SERVE_CHECK_ENV_LOG:-}" ]; then
        echo "LIBRARY_CHECK_PLIST=${LIBRARY_CHECK_PLIST:-}" >> "$STUB_SERVE_CHECK_ENV_LOG"
    fi
    if [ -f "$(dirname "$0")/FAIL_CHECK" ]; then
        echo "stub serve.sh: simulated preflight failure" >&2
        exit 78
    fi
    echo "stub serve.sh: --check passed"
    exit 0
fi
exit 0
"""

STUB_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.convivy.library</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/sh</string>
        <string>/srv/fixture-home/.local/deploy/library/serve.sh</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>LIBRARY_ENV_MARKER</key>
        <string>stub-v1</string>
        {library_db_entry}
    </dict>
</dict>
</plist>
"""

# Every `seed_repo` fixture gitignores `.venv/` — a real checkout's own
# `.venv` is gitignored too (this repo's own .gitignore does the same) — so
# the stub interpreter below can sit there, untracked, without ever showing
# up as a dirty file on the new clean-tree check (bin/install-library's own
# `_assess_checkout`). A test that needs its OWN .gitignore entry (the
# credential test below) appends to this rather than overwriting it, for
# exactly that reason.
BASE_GITIGNORE = ".venv/\n"

# A stub `.venv/bin/python`, standing in for the checkout's own pinned
# interpreter that `bin/install-library` now runs `-m alembic upgrade head`
# with. Not a real alembic: this repo's fixture migration scripts are
# minimal fixtures of their own (`_write_alembic_versions`), so this reads
# the same revision/down_revision shape directly off disk (regex, not an
# import) and writes the tree's single head straight into `alembic_version`
# — enough to exercise the INSTALLER's own backup-then-migrate-then-reread
# sequence without needing a real alembic environment in every test here.
# A marker file at the repo root, `ALEMBIC_FAIL_MIGRATION` — committed, like
# `FAIL_CHECK` above, so its presence doesn't dirty the tree the new
# clean-tree check now looks at — makes this fail without touching the
# database, for the "a failing migration stops before the install" case.
# `sys.argv[1:]` is exactly `["-m", "alembic", "upgrade", "head"]`: the
# shebang line below makes the kernel re-exec this file itself as the
# script argument to a real `python3`, which never applies its OWN `-m`
# handling to flags that arrived as plain argv entries rather than its own
# command line.
STUB_ALEMBIC_PYTHON = r"""#!/usr/bin/env python3
import os
import re
import sqlite3
import sys


def head_revision(versions_dir):
    revs = {}
    downs = set()
    for name in sorted(os.listdir(versions_dir)):
        if not name.endswith(".py"):
            continue
        text = open(os.path.join(versions_dir, name)).read()
        m = re.search(r"^revision\s*=\s*['\"]([^'\"]+)['\"]", text, re.M)
        if not m:
            continue
        rev = m.group(1)
        dm = re.search(r"^down_revision\s*=\s*['\"]([^'\"]*)['\"]", text, re.M)
        down = dm.group(1) if dm else None
        revs[rev] = down
        if down:
            downs.add(down)
    heads = [r for r in revs if r not in downs]
    return heads[0] if len(heads) == 1 else None


def main():
    if sys.argv[1:] != ["-m", "alembic", "upgrade", "head"]:
        sys.stderr.write("stub alembic: only 'upgrade head' is supported\n")
        sys.exit(2)
    if os.path.exists("ALEMBIC_FAIL_MIGRATION"):
        sys.stderr.write("stub alembic: simulated migration failure\n")
        sys.exit(1)
    head = head_revision(os.path.join("alembic", "versions"))
    if head is None:
        sys.stderr.write("stub alembic: no single head revision found\n")
        sys.exit(1)
    conn = sqlite3.connect(os.environ["LIBRARY_DB"])
    try:
        conn.execute("UPDATE alembic_version SET version_num = ?", (head,))
        conn.commit()
    finally:
        conn.close()


main()
"""


def _write_stub_venv(repo: Path) -> None:
    venv_bin = repo / ".venv" / "bin"
    venv_bin.mkdir(parents=True, exist_ok=True)
    python_stub = venv_bin / "python"
    python_stub.write_text(STUB_ALEMBIC_PYTHON)
    python_stub.chmod(0o755)


# The migration-state check (library issue issue_01M1JKC0AC3ZBBA3CXC2DGSVYH)
# reads LIBRARY_DB out of the CURRENTLY LIVE plist, so every fixture's
# template needs that key present and resolvable by default — otherwise every
# fixture repo in this file would read as "LIBRARY_DB not set" on its SECOND
# install (the first install writes $PLIST_LIVE from this very template) and
# refuse, which is not what any of those pre-existing cases are testing.
# `seed_repo.build()` substitutes a real path here by default, already
# stamped to match the fixture's own alembic head — a case that wants a
# DIFFERENT migration state passes its own `library_db=` (a path it controls
# the contents of, or `None` to omit the key entirely) and the alembic chain
# it wants via `alembic_versions=`.
FIXTURE_ALEMBIC_REV = "fixturerev00a1"
DEFAULT_DB_NAME = "library-test.db"
_AUTO_LIBRARY_DB = object()  # sentinel: "auto-stamp a matching scratch vault"


def default_db_path(repo: Path) -> Path:
    """The scratch vault every `seed_repo.build()` call stamps by default —
    a sibling of `repo` (both children of the same per-test `tmp_path`), so
    a test that needs to re-render a plist for a SECOND commit (the
    `render_stub_plist(...).replace("stub-v1", "stub-v2")` cases below) can
    find it from `repo` alone, with no extra fixture parameter."""
    return repo.parent / DEFAULT_DB_NAME


def drifted_plist_text(repo: Path) -> str:
    """A "hand-edited, not the template" live plist for the drift cases
    below — byte-different from any real candidate (so drift detection
    still triggers) but carrying a LIBRARY_DB this file's migration check
    can resolve, matching `repo`'s own auto-stamped vault, so those cases
    exercise the DRIFT refusal rather than tripping an unrelated
    "LIBRARY_DB not set" refusal first (these tests hand-place this file
    at $PLIST_LIVE before any install has ever run). `<key>`/`<string>` are
    kept on separate lines — `_plist_env_pairs`'s own awk only looks for a
    `<string>` on the line AFTER a `<key>`, the same shape every real plist
    (and STUB_PLIST) uses."""
    return (
        "<!-- hand-edited, not the template -->\n"
        "<plist><dict>\n"
        "<key>EnvironmentVariables</key>\n"
        "<dict>\n"
        "<key>LIBRARY_DB</key>\n"
        f"<string>{default_db_path(repo)}</string>\n"
        "</dict>\n"
        "</dict></plist>\n"
    )


def render_stub_plist(library_db: str | None) -> str:
    """Renders STUB_PLIST's one substitution point: a LIBRARY_DB entry when
    `library_db` names a path, or nothing at all when it's None — the
    "LIBRARY_DB not set in the plist" state this file's migration-status
    cases exercise."""
    entry = (
        f"<key>LIBRARY_DB</key>\n        <string>{library_db}</string>"
        if library_db is not None else ""
    )
    return STUB_PLIST.format(library_db_entry=entry)


def _write_alembic_versions(repo: Path, versions: dict) -> None:
    """Writes one minimal alembic version script per `{revision:
    down_revision}` entry into `repo/alembic/versions/` — enough for
    install-library's own head-finding (`revision =` / `down_revision =`),
    nothing alembic itself would ever need to actually run (no `upgrade`/
    `downgrade` body is exercised by anything in this file)."""
    versions_dir = repo / "alembic" / "versions"
    versions_dir.mkdir(parents=True, exist_ok=True)
    for revision, down_revision in versions.items():
        (versions_dir / f"{revision}_fixture.py").write_text(
            f'"""fixture migration"""\n'
            f"revision = {revision!r}\n"
            f"down_revision = {down_revision!r}\n"
            f"def upgrade(): pass\n"
            f"def downgrade(): pass\n"
        )


def stamp_alembic_db(path: Path, revision: str | None) -> None:
    """Creates a fresh sqlite database at `path`, in WAL mode (the vault's
    own journal mode), stamped at `revision` — or, when `revision` is None,
    created with no `alembic_version` table at all, for a case exercising
    that state. Always a brand-new file: a case wanting "no database at
    this path" simply never calls this."""
    import sqlite3
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        if revision is not None:
            conn.execute(
                "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
            conn.execute("INSERT INTO alembic_version VALUES (?)", (revision,))
        else:
            conn.execute("CREATE TABLE not_alembic_version (x INTEGER)")
        conn.commit()
    finally:
        conn.close()

STUB_LAUNCHCTL = """#!/bin/sh
# Stub for install-library tests — see test_install_library.py's module
# docstring for why the real launchctl must never be reachable from this
# suite, and for what each of the env vars below controls.
#
# STUB_LAUNCHCTL_EXPECTED_DOMAIN is required on every call (Part A, library
# issue issue_01M1VW2D48D8F4B6BM6AK28D7F round 3): a harness that forgets to
# set it gets this stub's own refusal rather than a silent pass-through, and
# a domain argument that doesn't match it — including the real gui/<uid>
# domain install-library falls back to when its own
# INSTALL_LIBRARY_LAUNCHD_DOMAIN is left unset — is refused the same way
# rather than acted on, so a test that forgets to pin the domain can never
# silently reach a real launchd domain through this stub.
EXPECTED_DOMAIN="${STUB_LAUNCHCTL_EXPECTED_DOMAIN:?STUB_LAUNCHCTL_EXPECTED_DOMAIN must be set to the test-only launchd domain}"
LOG="${STUB_LAUNCHCTL_LOG:-/dev/null}"
ENV_STATE="${STUB_LAUNCHCTL_ENV_STATE_FILE:-}"
COUNT_FILE="${STUB_LAUNCHCTL_PRINT_COUNT_FILE:-}"
echo "$*" >> "$LOG"
case "$1" in
  bootout)
    domain="${2%/*}"
    if [ "$domain" != "$EXPECTED_DOMAIN" ]; then
      echo "stub launchctl: refusing bootout against domain '$domain', expected test domain '$EXPECTED_DOMAIN'" >&2
      exit 1
    fi
    [ -n "$ENV_STATE" ] && rm -f "$ENV_STATE"
    if [ "${STUB_LAUNCHCTL_BOOTOUT_FAIL:-0}" = "1" ]; then
      echo "stub launchctl: simulated bootout failure" >&2
      exit 1
    fi
    exit 0
    ;;
  bootstrap)
    domain="$2"
    if [ "$domain" != "$EXPECTED_DOMAIN" ]; then
      echo "stub launchctl: refusing bootstrap against domain '$domain', expected test domain '$EXPECTED_DOMAIN'" >&2
      exit 1
    fi
    if [ "${STUB_LAUNCHCTL_BOOTSTRAP_FAIL:-0}" = "1" ]; then
      echo "stub launchctl: simulated bootstrap failure" >&2
      exit 1
    fi
    plist_path="$3"
    match="${STUB_LAUNCHCTL_BOOTSTRAP_FAIL_IF_CONTAINS:-}"
    if [ -n "$match" ] && [ -f "$plist_path" ] && grep -qF -- "$match" "$plist_path"; then
      echo "stub launchctl: simulated bootstrap failure (matched plist content)" >&2
      exit 1
    fi
    if [ -n "$ENV_STATE" ] && [ -f "$plist_path" ]; then
      awk '
        /<key>EnvironmentVariables<\\/key>/ { in_env=1; next }
        in_env && /<\\/dict>/ { in_env=0 }
        in_env && /<key>/ {
          k=$0; sub(/.*<key>/,"",k); sub(/<\\/key>.*/,"",k); pending=k; next
        }
        in_env && pending != "" && /<string>/ {
          v=$0; sub(/.*<string>/,"",v); sub(/<\\/string>.*/,"",v)
          print pending "=" v
          pending=""
        }
      ' "$plist_path" > "$ENV_STATE"
    fi
    exit 0
    ;;
  print)
    domain="${2%/*}"
    if [ "$domain" != "$EXPECTED_DOMAIN" ]; then
      echo "stub launchctl: refusing print against domain '$domain', expected test domain '$EXPECTED_DOMAIN'" >&2
      exit 1
    fi
    n=0
    if [ -n "$COUNT_FILE" ]; then
      [ -f "$COUNT_FILE" ] && n=$(cat "$COUNT_FILE")
      n=$((n + 1))
      echo "$n" > "$COUNT_FILE"
    fi
    state="${STUB_LAUNCHCTL_STATE:-running}"
    waiting_for="${STUB_LAUNCHCTL_WAITING_FOR_CALLS:-0}"
    if [ "$waiting_for" -gt 0 ] && [ "$n" -le "$waiting_for" ]; then
      state="waiting"
    fi
    echo "gui/501/com.convivy.library = {"
    echo "	state = $state"
    exit_timeout="${STUB_LAUNCHCTL_EXIT_TIMEOUT-60}"
    if [ -n "$exit_timeout" ]; then
      echo "	exit timeout = $exit_timeout"
    fi
    echo "	inherited environment = {"
    inh="${STUB_LAUNCHCTL_INHERITED_INJECT:-}"
    if [ -n "$inh" ]; then
      echo "		${inh%%=*} => ${inh#*=}"
    fi
    echo "	}"
    echo "	default environment = {"
    def="${STUB_LAUNCHCTL_DEFAULT_INJECT:-}"
    if [ -n "$def" ]; then
      echo "		${def%%=*} => ${def#*=}"
    fi
    echo "	}"
    echo "	environment = {"
    if [ -n "$ENV_STATE" ] && [ -f "$ENV_STATE" ]; then
      while IFS='=' read -r k v; do
        [ -n "$k" ] || continue
        [ "$k" = "${STUB_LAUNCHCTL_DROP_ENV_KEY:-}" ] && continue
        if [ "$k" = "${STUB_LAUNCHCTL_TRUNCATE_ENV_KEY:-}" ]; then
          v="${v%?}"
        fi
        if [ "$k" = "${STUB_LAUNCHCTL_EXTEND_ENV_KEY:-}" ]; then
          v="${v}${STUB_LAUNCHCTL_EXTEND_ENV_SUFFIX:-EXTRA}"
        fi
        if [ "$k" = "${STUB_LAUNCHCTL_RENAME_ENV_KEY:-}" ]; then
          k="${STUB_LAUNCHCTL_RENAME_ENV_KEY_TO:-${k}_RENAMED}"
        fi
        echo "		$k => $v"
      done < "$ENV_STATE"
    fi
    echo "	}"
    echo "}"
    exit 0
    ;;
  *)
    exit 0
    ;;
esac
"""


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=check, capture_output=True, text=True)


def _write_fixture_files(repo: Path, version_marker: str, extra: dict[str, str] | None = None,
                          library_db: str | None = None, alembic_versions: dict | None = None):
    (repo / "serve.sh").write_text(STUB_SERVE_SH)
    (repo / "serve.sh").chmod(0o755)
    (repo / "requirements.txt").write_text("")
    (repo / "app.py").write_text(f"VERSION = {version_marker!r}\n_DRAIN_DEADLINE_SECONDS = 50.0\n")
    (repo / "launchd").mkdir(exist_ok=True)
    (repo / "launchd" / "com.convivy.library.plist").write_text(render_stub_plist(library_db))
    _write_alembic_versions(repo, alembic_versions or {FIXTURE_ALEMBIC_REV: None})
    (repo / ".gitignore").write_text(BASE_GITIGNORE)
    _write_stub_venv(repo)
    if extra:
        for relpath, content in extra.items():
            p = repo / relpath
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)


@pytest.fixture
def seed_repo(tmp_path):
    """Build (and return the path to) a from-scratch git repo on `main`,
    shaped like the Library, with the fixture files above committed, and
    with a real "origin" remote (a bare clone, in sync with `main` at seed
    time) — every real deploy checkout has one, and the stale-branch guard
    this file exercises now refuses a branch ref with none configured at
    all (security review, PR #202 round 1), so a repo built with none here
    would stop being representative of anything this installer actually
    runs against. `run_installer`'s own `sync_origin=True` default keeps
    this origin caught up with whatever a case commits on top of this seed,
    so only the cases under "stale local branch vs. origin" below, which
    pass `sync_origin=False`, ever see it fall behind.

    `library_db` and `alembic_versions` default to a MATCHING migration
    state — a scratch vault, stamped at `FIXTURE_ALEMBIC_REV`, referenced by
    the plist this repo's `launchd/com.convivy.library.plist` template sets
    LIBRARY_DB to — so a case that doesn't care about the migration-state
    check (almost all of this file) is unaffected by it. A case that does
    care passes its own `alembic_versions` (a `{revision: down_revision}`
    chain) and either lets `library_db` auto-stamp at `stamp_revision` (a
    revision from that chain, or any other string to simulate "unknown to
    the tree") or passes `library_db=None` / a path of its own choosing to
    exercise the other states directly."""
    def build(version_marker="v1", extra=None, alembic_versions=None,
              library_db=_AUTO_LIBRARY_DB, stamp_revision=None):
        repo = tmp_path / "src-repo"
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "config", "user.email", "t@t")
        _git(repo, "config", "user.name", "t")
        _git(repo, "config", "commit.gpgsign", "false")
        resolved_db = library_db
        if resolved_db is _AUTO_LIBRARY_DB:
            resolved_db = str(default_db_path(repo))
            stamp_alembic_db(default_db_path(repo), stamp_revision or FIXTURE_ALEMBIC_REV)
        _write_fixture_files(repo, version_marker, extra,
                              library_db=resolved_db, alembic_versions=alembic_versions)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "seed")
        origin = tmp_path / "origin.git"
        _git(repo, "clone", "-q", "--bare", str(repo), str(origin))
        _git(repo, "remote", "add", "origin", str(origin))
        _git(repo, "fetch", "-q", "origin")
        return repo
    return build


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


def _stub_launchctl_dir(home: Path) -> Path:
    """A sibling of `home`, not a child of it — `test_never_writes_outside_...`
    below asserts on `home`'s own contents, and the stub bin/log have nothing
    to do with what the installer itself is allowed to write."""
    stub_dir = home.parent / "stubbin"
    stub_dir.mkdir(exist_ok=True)
    launchctl = stub_dir / "launchctl"
    if not launchctl.exists():
        launchctl.write_text(STUB_LAUNCHCTL)
        launchctl.chmod(0o755)
    return stub_dir


def launchctl_log(home: Path) -> list[str]:
    """Every stub `launchctl` invocation this run made, oldest first."""
    log = home.parent / "launchctl-calls.log"
    if not log.exists():
        return []
    return [line for line in log.read_text().splitlines() if line]


def serve_check_env_log(home: Path) -> list[str]:
    """Every `LIBRARY_CHECK_PLIST=...` value the stub `serve.sh --check`
    received, oldest first — see `run_installer`'s own
    `STUB_SERVE_CHECK_ENV_LOG` for how this gets populated."""
    log = home.parent / "serve-check-env.log"
    if not log.exists():
        return []
    return [line for line in log.read_text().splitlines() if line]


def stub_print_environment(home: Path) -> dict[str, str]:
    """Queries the SAME stub state an install run just left behind — the
    "loaded" environment as `launchctl print` would report it right now — by
    invoking the stub binary directly, outside the installer. Used to check
    the installer's own invariant from the outside: what's loaded must match
    $PLIST_LIVE's own content, not just whatever the installer's exit message
    claims."""
    stub_dir = _stub_launchctl_dir(home)
    env = dict(os.environ)
    env["STUB_LAUNCHCTL_LOG"] = str(home.parent / "launchctl-calls.log")
    env["STUB_LAUNCHCTL_ENV_STATE_FILE"] = str(home.parent / "launchctl-env-state")
    env["STUB_LAUNCHCTL_EXPECTED_DOMAIN"] = TEST_LAUNCHD_DOMAIN
    result = subprocess.run(
        [str(stub_dir / "launchctl"), "print", f"{TEST_LAUNCHD_DOMAIN}/com.convivy.library"],
        env=env, capture_output=True, text=True, check=True)
    pairs = {}
    in_env = False
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("environment"):
            in_env = True
            continue
        if not in_env:
            continue
        if stripped == "}":
            in_env = False
            continue
        if " => " in stripped:
            k, v = stripped.split(" => ", 1)
            pairs[k] = v
    return pairs


def plist_environment(path: Path) -> dict[str, str]:
    """The real EnvironmentVariables dict out of a plist file, parsed the same
    way launchd itself would read it."""
    return dict(plistlib.loads(path.read_bytes()).get("EnvironmentVariables", {}))


def run_installer(*args, home, timeout=120, launchctl_env=None, sync_origin=True,
                   set_launchd_domain=True):
    """Every case here passes --python=sys.executable, the interpreter this
    test suite is already running under, instead of install-library's
    hardcoded macOS/Homebrew search — that search finds nothing on CI's
    Linux runners, and the point of this suite is to exercise the real
    install path, not skip it. This only picks which interpreter builds the
    staged venv; it does not touch serve.sh's own ownership walk, which
    still runs unmodified against the staged build before the swap.

    `sync_origin=True` (the default) pushes whatever local branch --ref
    names (or "main" if --ref is absent) to `seed_repo`'s own "origin"
    before invoking the installer, so a case that commits more changes on
    top of the seed and then installs with no --ref (most of this file)
    never trips the stale-branch guard that guard is this file's own
    subject of. The guard's own cases, under "stale local branch vs.
    origin" below, pass `sync_origin=False` and manage that divergence by
    hand instead — a --ref that isn't a bare local branch name (a sha, a
    tag, HEAD, or a refs/... spelling) is left alone here either way, since
    none of those match `refs/heads/<ref>` for this to push.

    `launchctl` is stubbed for every call (see the module docstring for why
    that isn't optional); `launchctl_env` overrides the stub's behavior for
    one call, e.g. `{"STUB_LAUNCHCTL_BOOTSTRAP_FAIL": "1"}`. The reload
    verification step's own timeout/poll are always overridden to small
    values here — every case that exercises retries controls the outcome via
    `STUB_LAUNCHCTL_WAITING_FOR_CALLS` / `STUB_LAUNCHCTL_STATE`, not via wall
    clock time, so this suite stays fast whether or not a case cares.

    `INSTALL_LIBRARY_LAUNCHCTL` and `INSTALL_LIBRARY_LAUNCH_AGENTS` are
    always pointed at the stub binary and a scratch directory under `home`;
    `INSTALL_LIBRARY_LAUNCHD_DOMAIN` is always pinned to `TEST_LAUNCHD_DOMAIN`
    too, UNLESS `set_launchd_domain=False` — the one case that exercises a
    harness forgetting to pin it, which the stub's own
    `STUB_LAUNCHCTL_EXPECTED_DOMAIN` (always set here, never conditional)
    then refuses rather than silently accepting (Part A, library issue
    issue_01M1VW2D48D8F4B6BM6AK28D7F round 3).

    `--status` with no explicit `--repo=` of its own gets one anyway, always
    pointed at a plain (non-git) scratch directory nested inside `home` —
    `--status` now reads $REPO too (whether main is behind origin), and with
    no override here that would default to THIS SUITE'S OWN checkout, the
    real worktree every other test in this file is careful never to touch.
    Pointed at a non-git directory rather than a throwaway repo so the new
    check fails closed ("not a git checkout") with no git call of its own,
    for every one of this file's many pre-existing `--status` cases that
    never cared about $REPO at all. Nested inside `home` rather than placed
    as its sibling: `db_path.parent` (`home`'s own parent) is what the
    "a status read creates no new files" cases below list before and after a
    `--status` call, so a sibling scratch directory lazily created on that
    call's first use would read as a file the READ created, when it is this
    harness's own fixture, not `install-library`, creating it."""
    args = list(args)
    if "--status" in args and not any(a.startswith("--repo=") for a in args):
        safe_repo = home / "status-default-repo-never-the-real-checkout"
        safe_repo.mkdir(exist_ok=True)
        args.append(f"--repo={safe_repo}")
    stub_dir = _stub_launchctl_dir(home)
    if sync_origin:
        repo_arg = next((a for a in args if a.startswith("--repo=")), None)
        if repo_arg:
            sync_repo = Path(repo_arg[len("--repo="):])
            ref_arg = next((a for a in args if a.startswith("--ref=")), None)
            sync_ref = ref_arg[len("--ref="):] if ref_arg else "main"
            has_origin = _git(
                sync_repo, "remote", "get-url", "origin", check=False).returncode == 0
            is_branch = _git(
                sync_repo, "show-ref", "--verify", "--quiet", f"refs/heads/{sync_ref}",
                check=False).returncode == 0
            if has_origin and is_branch:
                _git(sync_repo, "push", "-q", "--force", "origin",
                     f"refs/heads/{sync_ref}:refs/heads/{sync_ref}", check=False)
    env = dict(os.environ, HOME=str(home))
    env["PATH"] = os.pathsep.join([str(stub_dir), env.get("PATH", "")])
    env["STUB_LAUNCHCTL_LOG"] = str(home.parent / "launchctl-calls.log")
    env["STUB_LAUNCHCTL_ENV_STATE_FILE"] = str(home.parent / "launchctl-env-state")
    env["STUB_LAUNCHCTL_PRINT_COUNT_FILE"] = str(home.parent / "launchctl-print-count")
    env["STUB_SERVE_CHECK_ENV_LOG"] = str(home.parent / "serve-check-env.log")
    env["INSTALL_LIBRARY_RELOAD_VERIFY_TIMEOUT_SECONDS"] = "2"
    env["INSTALL_LIBRARY_RELOAD_VERIFY_POLL_SECONDS"] = "0.2"
    # Part A isolation (library issue issue_01M1VW2D48D8F4B6BM6AK28D7F round
    # 3): point every launchd-facing operation at the stub binary, a
    # scratch LaunchAgents directory and a domain that can never be real —
    # STUB_LAUNCHCTL_EXPECTED_DOMAIN is set unconditionally so the stub
    # itself refuses any call against any other domain, including the real
    # one install-library falls back to when set_launchd_domain=False leaves
    # INSTALL_LIBRARY_LAUNCHD_DOMAIN unset.
    env["INSTALL_LIBRARY_LAUNCHCTL"] = str(stub_dir / "launchctl")
    env["INSTALL_LIBRARY_LAUNCH_AGENTS"] = str(home / "Library" / "LaunchAgents")
    env["STUB_LAUNCHCTL_EXPECTED_DOMAIN"] = TEST_LAUNCHD_DOMAIN
    if set_launchd_domain:
        env["INSTALL_LIBRARY_LAUNCHD_DOMAIN"] = TEST_LAUNCHD_DOMAIN
    if launchctl_env:
        env.update(launchctl_env)
    return subprocess.run(
        [str(INSTALLER), f"--python={sys.executable}", *args], env=env,
        capture_output=True, text=True, timeout=timeout, check=False)


DEPLOY = "/.local/deploy/library"
PLIST_RECORD = "/.local/deploy/library-plist.sha256"


def plist_record_sha(home: Path) -> str | None:
    record = home / PLIST_RECORD.lstrip("/")
    return record.read_text().strip() if record.is_file() else None


def sha256_of(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- the success path ---------------------------------------------------------


def test_fresh_install_copies_code_builds_venv_and_records_commit(seed_repo, home):
    repo = seed_repo("v1")
    sha = _git(repo, "rev-parse", "main").stdout.strip()

    result = run_installer("--repo=" + str(repo), home=home)
    assert result.returncode == 0, result.stdout + result.stderr

    deploy_dir = home / DEPLOY.lstrip("/")
    app_py = (deploy_dir / "app.py").read_text()
    assert "v1" in app_py

    assert (deploy_dir / "app.py").is_file()
    assert not (deploy_dir / "app.py").is_symlink()
    assert (deploy_dir / ".venv" / "bin" / "python3.11").exists() or \
           (deploy_dir / ".venv" / "bin" / "python3").exists(), \
        "a pinned interpreter must be built inside the deploy area"
    assert not (deploy_dir / ".venv").is_symlink()

    deployed_commit = (deploy_dir / "DEPLOYED_COMMIT").read_text()
    assert f"commit: {sha}" in deployed_commit

    plist = home / "Library" / "LaunchAgents" / "com.convivy.library.plist"
    assert plist.is_file()
    assert not plist.is_symlink()
    assert "deploy/library/serve.sh" in plist.read_text()


def test_fresh_install_rewrites_console_script_shebangs_to_the_deployed_path(seed_repo, home):
    """pip writes each console script in the staged venv ($DEPLOY_DIR.new/
    .venv/bin) with an absolute shebang naming the interpreter AT THE STAGE
    PATH, which the installer's own swap renames away from under it
    (CQ-111). This checks every regular file in the DEPLOYED venv's bin/:
    none of them may still name the ".new" staging directory, and every one
    that is a shebang script naming a venv's bin/ must name this one."""
    repo = seed_repo("v1")
    result = run_installer("--repo=" + str(repo), home=home)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "did not name" not in result.stderr
    assert "failed after the swap" not in result.stderr

    deploy_dir = home / DEPLOY.lstrip("/")
    venv_bin = deploy_dir / ".venv" / "bin"
    checked = []
    for script in venv_bin.iterdir():
        if script.is_symlink() or not script.is_file():
            continue
        first_line = script.read_bytes().split(b"\n", 1)[0]
        if not first_line.startswith(b"#!") or b"/.venv/bin/" not in first_line:
            continue
        shebang = first_line[2:].decode()
        assert ".new" not in shebang, \
            f"{script.name}'s shebang still names the staging directory: {shebang}"
        assert shebang.startswith(str(venv_bin) + "/"), \
            f"{script.name}'s shebang does not name the deployed venv: {shebang}"
        checked.append(script.name)
    assert checked, "no console script shebang was found to check — pip itself always writes one"


def test_status_reports_the_deployed_commit_with_no_side_effects(seed_repo, home):
    repo = seed_repo("v1")
    sha = _git(repo, "rev-parse", "main").stdout.strip()
    run_installer("--repo=" + str(repo), home=home)

    deploy_dir = home / DEPLOY.lstrip("/")
    before = sorted(str(p) for p in deploy_dir.rglob("*"))

    result = run_installer("--status", home=home)
    assert result.returncode == 0
    assert sha in result.stdout

    after = sorted(str(p) for p in deploy_dir.rglob("*"))
    assert before == after, "--status must not modify the deploy area"


def test_status_before_any_install_fails_clearly(home):
    result = run_installer("--status", home=home)
    assert result.returncode != 0
    assert "nothing deployed" in (result.stdout + result.stderr)


# --- reviewer round 1: --status's three plist-record branches, each asserted
# on its own printed text rather than just its exit code -------------------


def test_status_reports_no_record_when_none_was_ever_written(home):
    """Absent branch: no install has ever run, so there is no
    $PLIST_RECORD file at all — distinct from either matching or mismatching
    a live plist, and the wording has to say so rather than silently falling
    into one of the other two branches."""
    result = run_installer("--status", home=home)
    assert "plist record: none (no install has recorded one yet)" in result.stdout


def test_status_reports_a_matching_record_after_a_fresh_install(seed_repo, home):
    """Matching branch: a fresh install both writes the plist and records its
    sha256, so an immediate --status must read the two back as equal."""
    repo = seed_repo("v1")
    run_installer("--repo=" + str(repo), home=home)
    live_plist = home / "Library" / "LaunchAgents" / "com.convivy.library.plist"
    recorded = plist_record_sha(home)
    assert recorded is not None

    result = run_installer("--status", home=home)
    assert result.returncode == 0
    assert f"plist record: {recorded}" in result.stdout
    assert "plist record matches the live plist" in result.stdout
    assert recorded == sha256_of(live_plist)


def test_status_reports_a_stale_record_after_a_hand_edit(seed_repo, home):
    """Stale branch: an operator hand-edits the live plist after a successful
    install, so the file on disk no longer hashes to what the record still
    holds — --status has to say so, not just report the (now outdated)
    recorded sha as if nothing changed."""
    repo = seed_repo("v1")
    run_installer("--repo=" + str(repo), home=home)
    live_plist = home / "Library" / "LaunchAgents" / "com.convivy.library.plist"
    recorded = plist_record_sha(home)
    assert recorded is not None

    live_plist.write_text(live_plist.read_text() + "<!-- hand-edited after install -->\n")
    assert sha256_of(live_plist) != recorded

    result = run_installer("--status", home=home)
    assert result.returncode == 0
    assert f"plist record: {recorded}" in result.stdout
    assert "plist record does NOT match the live plist" in result.stdout


# --- migration-state check (library issue issue_01M1JKC0AC3ZBBA3CXC2DGSVYH):
# --status's three new lines, every state they distinguish, and the plain
# install's own pre-stage refusal. `seed_repo.build()`'s defaults already put
# every OTHER case in this file in the "matching" state (see its own
# docstring); the cases below are the ones that deliberately leave it.

UNKNOWN_REV = "nottreehead00"  # never a revision any fixture's own chain uses


def test_status_reports_matching_migration_state(seed_repo, home):
    repo = seed_repo("v1")
    run_installer("--repo=" + str(repo), home=home)

    result = run_installer("--status", home=home)
    assert result.returncode == 0
    assert f"alembic db revision: {FIXTURE_ALEMBIC_REV}" in result.stdout
    assert f"alembic tree head: {FIXTURE_ALEMBIC_REV}" in result.stdout
    assert "alembic: matching" in result.stdout


def test_status_reports_behind_migration_state(seed_repo, home):
    old, new = "oldrev000001", "newrev000002"
    repo = seed_repo("v1", alembic_versions={old: None, new: old}, stamp_revision=old)
    run_installer("--repo=" + str(repo), home=home)

    result = run_installer("--repo=" + str(repo), "--status", home=home)
    assert result.returncode == 0
    assert f"alembic db revision: {old}" in result.stdout
    assert f"alembic tree head: {new}" in result.stdout
    assert "alembic: BEHIND" in result.stdout
    assert "pending migrations" in result.stdout
    assert new in result.stdout.split("pending migrations", 1)[1]


def test_status_reports_main_behind_origin_and_changes_nothing(seed_repo, home, tmp_path):
    """--status's own new line about $REPO's local main vs. origin/main —
    and, since it fetches to answer that, this also confirms the fetch
    itself never fast-forwards or otherwise changes the checkout, unlike
    an ordinary install's own --ref=main."""
    repo = seed_repo("v1")
    origin = _add_bare_origin(repo, tmp_path)
    local_sha = _git(repo, "rev-parse", "main").stdout.strip()
    run_installer("--repo=" + str(repo), home=home)

    advanced = tmp_path / "advance-clone-status"
    _git(repo, "clone", "-q", str(origin), str(advanced))
    _git(advanced, "config", "user.email", "t@t")
    _git(advanced, "config", "user.name", "t")
    (advanced / "app.py").write_text("VERSION = 'v2'\n_DRAIN_DEADLINE_SECONDS = 50.0\n")
    _git(advanced, "add", "-A")
    _git(advanced, "commit", "-q", "-m", "v2, pushed to origin only")
    _git(advanced, "push", "-q", "origin", "main")
    origin_sha = _git(advanced, "rev-parse", "main").stdout.strip()

    result = run_installer("--repo=" + str(repo), "--status", home=home, sync_origin=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"main: behind origin/main (local {local_sha}, origin {origin_sha})" in result.stdout
    assert _git(repo, "rev-parse", "main").stdout.strip() == local_sha, (
        "--status must never fast-forward the checkout")


def test_status_reports_main_up_to_date_with_origin(seed_repo, home, tmp_path):
    repo = seed_repo("v1")
    _add_bare_origin(repo, tmp_path)
    sha = _git(repo, "rev-parse", "main").stdout.strip()

    result = run_installer("--repo=" + str(repo), "--status", home=home)
    assert result.returncode != 0  # nothing deployed yet — still prints the main: line
    assert f"main: up to date with origin/main ({sha})" in result.stdout


def test_status_reports_no_library_db(seed_repo, home):
    repo = seed_repo("v1", library_db=None)
    run_installer("--repo=" + str(repo), home=home)

    result = run_installer("--status", home=home)
    assert result.returncode == 0
    assert "alembic db revision: unknown (LIBRARY_DB is not set" in result.stdout
    assert "alembic: could not be determined" in result.stdout


def test_status_reports_db_missing(seed_repo, home, tmp_path):
    missing = tmp_path / "nowhere" / "library.db"
    repo = seed_repo("v1", library_db=str(missing))
    run_installer("--repo=" + str(repo), home=home)

    result = run_installer("--status", home=home)
    assert result.returncode == 0
    assert f"alembic db revision: unknown (no database file at {missing})" in result.stdout
    assert "alembic: could not be determined" in result.stdout


def test_status_reports_no_version_table(seed_repo, home, tmp_path):
    db_path = tmp_path / "no-table.db"
    stamp_alembic_db(db_path, None)
    repo = seed_repo("v1", library_db=str(db_path))
    run_installer("--repo=" + str(repo), home=home)

    result = run_installer("--status", home=home)
    assert result.returncode == 0
    assert "has no alembic_version table" in result.stdout
    assert "alembic: could not be determined" in result.stdout


def test_status_reports_revision_unknown_to_the_tree(seed_repo, home, tmp_path):
    db_path = tmp_path / "unknown-rev.db"
    stamp_alembic_db(db_path, UNKNOWN_REV)
    repo = seed_repo("v1", library_db=str(db_path))
    run_installer("--repo=" + str(repo), home=home)

    result = run_installer("--status", home=home)
    assert result.returncode == 0
    assert f"alembic db revision: {UNKNOWN_REV}" in result.stdout
    assert f"the database's revision ({UNKNOWN_REV}) is unknown to this tree" in result.stdout


def test_status_reports_unreadable_db(seed_repo, home, tmp_path):
    """A read FAILURE — a file that is not a sqlite database at all — must
    never print as though the vault were up to date."""
    db_path = tmp_path / "corrupt.db"
    db_path.write_bytes(b"not a sqlite database at all")
    repo = seed_repo("v1", library_db=str(db_path))
    run_installer("--repo=" + str(repo), home=home)

    result = run_installer("--status", home=home)
    assert result.returncode == 0
    assert "alembic db revision: could not be read" in result.stdout
    assert "alembic: could not be determined" in result.stdout
    assert "matching" not in result.stdout


# --- round 4 review (PR #207): the WAL read always reads `immutable=1`, so
# it creates no file in any WAL state, and the "behind" retry that fact
# requires near the vault's own migration-state check, below -----------------


def test_status_read_creates_no_new_files_when_the_vault_has_no_wal(seed_repo, home):
    """The fixture vault is a clean WAL-mode close (no -wal/-shm on disk) —
    the state every OTHER case in this file reads through. A plain `mode=ro`
    read of that exact shape creates an empty -wal/-shm pair on at least one
    real python3 build (confirmed directly against /opt/homebrew's); this
    read must never do that to the vault's own directory, on either build."""
    repo = seed_repo("v1")
    run_installer("--repo=" + str(repo), home=home)
    db_path = default_db_path(repo)
    assert not Path(str(db_path) + "-wal").exists()
    before = sorted(p.name for p in db_path.parent.iterdir())

    result = run_installer("--status", home=home)
    assert result.returncode == 0

    after = sorted(p.name for p in db_path.parent.iterdir())
    assert after == before, "reading the vault's revision must create no new files"


def test_status_read_creates_no_new_files_when_wal_and_shm_are_present(seed_repo, home):
    """A live writer mid-transaction leaves both sidecars on disk. Round 3
    security review confirmed round 2's plain `mode=ro` branch regenerates a
    missing -shm against this shape; this read is always `immutable=1`,
    which performs no locking/journal probe at all, so it must create
    nothing here either."""
    repo = seed_repo("v1")
    run_installer("--repo=" + str(repo), home=home)
    db_path = default_db_path(repo)
    wal_ready = threading.Event()
    release_writer = threading.Event()

    def hold_wal_and_shm_open():
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("BEGIN")
            conn.execute(
                "UPDATE alembic_version SET version_num = ?", (FIXTURE_ALEMBIC_REV,))
            wal_ready.set()
            release_writer.wait(timeout=10)
            conn.rollback()
        finally:
            conn.close()

    writer = threading.Thread(target=hold_wal_and_shm_open)
    writer.start()
    try:
        assert wal_ready.wait(timeout=5), "the writer thread never opened the WAL"
        assert Path(str(db_path) + "-wal").exists()
        assert Path(str(db_path) + "-shm").exists()
        before = sorted(p.name for p in db_path.parent.iterdir())

        result = run_installer("--status", home=home)
        assert result.returncode == 0
        assert "alembic: matching" in result.stdout

        after = sorted(p.name for p in db_path.parent.iterdir())
        assert after == before, "reading wal+shm present must create no new files"
    finally:
        release_writer.set()
        writer.join(timeout=10)


def test_status_read_creates_no_new_files_when_wal_present_and_shm_absent(seed_repo, home):
    """Round 3 security review's own reproduction: a crash
    (`os._exit()`, no `close()`) leaves both -wal and -shm; removing -shm by
    hand reproduces the exact precondition that round's finding named —
    plain `mode=ro` regenerates it. This read must create neither."""
    repo = seed_repo("v1")
    run_installer("--repo=" + str(repo), home=home)
    db_path = default_db_path(repo)

    crash_script = (
        "import sqlite3, os\n"
        f"conn = sqlite3.connect({str(db_path)!r})\n"
        "conn.execute('PRAGMA journal_mode=WAL')\n"
        "conn.execute('UPDATE alembic_version SET version_num = ?', "
        f"({FIXTURE_ALEMBIC_REV!r},))\n"
        "conn.commit()\n"
        "os._exit(0)\n"
    )
    subprocess.run([sys.executable, "-c", crash_script], check=True, timeout=10)
    assert Path(str(db_path) + "-wal").exists()
    assert Path(str(db_path) + "-shm").exists()
    Path(str(db_path) + "-shm").unlink()

    before = sorted(p.name for p in db_path.parent.iterdir())
    result = run_installer("--status", home=home)
    assert result.returncode == 0
    assert "alembic: matching" in result.stdout

    after = sorted(p.name for p in db_path.parent.iterdir())
    assert after == before, "reading wal-present/shm-absent must create no new files"


# --- round 4: the "behind" retry. _db_alembic_revision's `immutable=1` never
# consults -wal, so a migration the live service has committed but not yet
# checkpointed into the main file still reads as the OLD revision;
# _compute_migration_state re-reads a "behind" result for a couple of
# seconds, but only when a -wal file is there to explain it, before this
# file's own refusal below ever sees it. ------------------------------------


def test_install_installs_when_a_behind_wal_checkpoints_within_the_retry_bound(seed_repo, home):
    """The live service checkpoints on every write request's own connection
    close, not just at shutdown — this holds a transaction open just long
    enough for the install's first read to land inside the retry bound,
    then commits and closes (the checkpoint), and asserts the install
    proceeds once that lands, reading the now-current revision."""
    old, new = "oldrev000001", "newrev000002"
    repo = seed_repo("v1", alembic_versions={old: None, new: old}, stamp_revision=old)
    first = run_installer("--repo=" + str(repo), home=home)
    assert first.returncode == 0, first.stdout + first.stderr

    (repo / "app.py").write_text("VERSION = 'v2'\n_DRAIN_DEADLINE_SECONDS = 50.0\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "v2, migration mid-checkpoint")

    db_path = default_db_path(repo)
    wal_ready = threading.Event()

    def apply_migration_then_checkpoint():
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("BEGIN")
            conn.execute("UPDATE alembic_version SET version_num = ?", (new,))
            wal_ready.set()
            time.sleep(0.4)
            conn.commit()
        finally:
            conn.close()

    writer = threading.Thread(target=apply_migration_then_checkpoint)
    writer.start()
    try:
        assert wal_ready.wait(timeout=5), "the writer thread never opened the WAL"
        assert Path(str(db_path) + "-wal").exists()
        result = run_installer("--repo=" + str(repo), home=home)
    finally:
        writer.join(timeout=10)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "'v2'" in (home / DEPLOY.lstrip("/") / "app.py").read_text()


def test_install_refuses_after_the_retry_bound_when_a_behind_wal_never_checkpoints(
        seed_repo, home):
    """A -wal file that stays open the whole bound without ever committing
    the pending migration is still genuinely behind once the bound elapses
    — the refusal message has to say a -wal file was there, distinct from
    the plain "behind, no -wal at all" refusal below, since an operator
    reading it needs to know the check raced a real write rather than found
    a stale vault outright. Passes --no-migrate so this still exercises the
    refusal rather than the new auto-migrate path — the retry bound this
    case is about runs inside _compute_migration_state regardless of that
    flag, ahead of the auto-migrate/refuse branch the flag does control."""
    old, new = "oldrev000001", "newrev000002"
    repo = seed_repo("v1", alembic_versions={old: None, new: old}, stamp_revision=old)
    first = run_installer("--repo=" + str(repo), home=home)
    assert first.returncode == 0, first.stdout + first.stderr

    (repo / "app.py").write_text("VERSION = 'v2'\n_DRAIN_DEADLINE_SECONDS = 50.0\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "v2, wal never checkpoints")

    db_path = default_db_path(repo)
    wal_ready = threading.Event()
    release_writer = threading.Event()

    def hold_wal_open_without_checkpointing():
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("BEGIN")
            conn.execute("UPDATE alembic_version SET version_num = ?", (new,))
            wal_ready.set()
            release_writer.wait(timeout=15)
            conn.rollback()
        finally:
            conn.close()

    writer = threading.Thread(target=hold_wal_open_without_checkpointing)
    writer.start()
    try:
        assert wal_ready.wait(timeout=5), "the writer thread never opened the WAL"
        assert Path(str(db_path) + "-wal").exists()
        result = run_installer("--repo=" + str(repo), "--no-migrate", home=home)
    finally:
        release_writer.set()
        writer.join(timeout=15)

    assert result.returncode != 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert f"the vault is at revision {old}" in combined
    assert "-wal" in combined
    assert "re-run" in combined


def test_install_refuses_immediately_when_behind_with_no_wal(seed_repo, home):
    """No -wal file means there is nothing pending to checkpoint, so this
    refuses at once rather than spending the retry bound's wall-clock time
    on a database that was never going to change. Passes --no-migrate for
    the same reason as the retry-bound case above: this is a test of the
    refusal path, not of whether the new auto-migrate path would also
    reach the same vault."""
    old, new = "oldrev000001", "newrev000002"
    repo = seed_repo("v1", alembic_versions={old: None, new: old}, stamp_revision=old)
    first = run_installer("--repo=" + str(repo), home=home)
    assert first.returncode == 0, first.stdout + first.stderr

    (repo / "app.py").write_text("VERSION = 'v2'\n_DRAIN_DEADLINE_SECONDS = 50.0\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "v2, no wal at all")

    db_path = default_db_path(repo)
    assert not Path(str(db_path) + "-wal").exists()

    started = time.monotonic()
    result = run_installer("--repo=" + str(repo), "--no-migrate", home=home)
    elapsed = time.monotonic() - started

    assert result.returncode != 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert f"the vault is at revision {old}" in combined
    assert "-wal" not in combined
    assert elapsed < 1.8, (
        "a behind database with no -wal must refuse without waiting out the retry bound")


# --- the plain install's own refusal, before anything is staged or launchd
# is touched. Every case below installs once with NO live plist yet (which
# this check skips — see install-library's own comment on why) to establish
# one, then installs again over that now-live service, which is what the
# check protects.


def test_first_install_skips_the_check_with_no_live_plist(seed_repo, home):
    """Nothing is live yet to protect, so a vault that would otherwise refuse
    (missing entirely, here) does not block the very first install onto a
    machine."""
    repo = seed_repo("v1", library_db="/definitely/does/not/exist/library.db")
    result = run_installer("--repo=" + str(repo), home=home)
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_behind_vault_is_backed_up_migrated_and_installed(seed_repo, home):
    """The core new behaviour (2026-10-08 ruling): a second install over an
    already-live service whose vault is behind the tree's migration head now
    backs it up with `sqlite3 .backup`, runs `alembic upgrade head` against
    it from the checkout, re-reads the vault's revision to confirm it now
    matches, and proceeds to install — rather than refusing and handing the
    operator a manual command, which is what this used to do and what
    `test_no_migrate_keeps_todays_schema_refusal` below still covers."""
    old, new = "oldrev000001", "newrev000002"
    repo = seed_repo("v1", alembic_versions={old: None, new: old}, stamp_revision=old)
    first = run_installer("--repo=" + str(repo), home=home)
    assert first.returncode == 0, first.stdout + first.stderr
    db_path = default_db_path(repo)

    (repo / "app.py").write_text("VERSION = 'v2'\n_DRAIN_DEADLINE_SECONDS = 50.0\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "v2, vault still at the old revision")

    result = run_installer("--repo=" + str(repo), home=home)
    assert result.returncode == 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "backing it up" in combined
    assert "Migrated" in combined

    backups = sorted(db_path.parent.glob(db_path.name + ".pre-deploy-*"))
    assert len(backups) == 1, f"expected exactly one backup, found {backups}"
    check = subprocess.run(
        ["sqlite3", f"file:{backups[0]}?immutable=1", "pragma quick_check"],
        capture_output=True, text=True, check=True)
    assert check.stdout.strip() == "ok"

    deploy_dir = home / DEPLOY.lstrip("/")
    assert "'v2'" in (deploy_dir / "app.py").read_text()

    import sqlite3
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT version_num FROM alembic_version").fetchall()
    finally:
        conn.close()
    assert rows == [(new,)]


def test_install_proceeds_when_the_migration_leaves_its_revision_only_in_the_wal(
        seed_repo, home):
    """alembic's own write can commit to the WAL and never reach the main
    file by the time this script re-reads it: a reader with an older open
    snapshot (started before alembic's own commit, held here for the whole
    install call) blocks the writer's close-time checkpoint from moving that
    frame into the main file — reproduced directly, the same mechanism the
    "status read" cases above hold open to prove the opposite direction
    (that an immutable read sees nothing new). The post-migration check has
    to read through a connection that sees the WAL (round 4 review) or this
    refuses on a migration that in fact succeeded."""
    old, new = "oldrev000001", "newrev000002"
    repo = seed_repo("v1", alembic_versions={old: None, new: old}, stamp_revision=old)
    first = run_installer("--repo=" + str(repo), home=home)
    assert first.returncode == 0, first.stdout + first.stderr
    db_path = default_db_path(repo)

    (repo / "app.py").write_text("VERSION = 'v2'\n_DRAIN_DEADLINE_SECONDS = 50.0\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "v2, migration lands only in the WAL")

    import sqlite3
    reader = sqlite3.connect(str(db_path))
    reader.execute("BEGIN")
    reader.execute("SELECT 1 FROM alembic_version").fetchall()
    try:
        result = run_installer("--repo=" + str(repo), home=home)
        assert result.returncode == 0, result.stdout + result.stderr
        combined = result.stdout + result.stderr
        assert "Migrated" in combined

        # Checked with the reader's snapshot still open: closing it first
        # would itself be the last-connection close that checkpoints and
        # removes the WAL, which is the gap this test exists to catch, not
        # a property of the install that already finished.
        assert Path(str(db_path) + "-wal").exists(), (
            "the reader's open snapshot must have kept the migration's "
            "write out of the main file, or this test isn't exercising "
            "the gap it's named for")
    finally:
        reader.rollback()
        reader.close()

    deploy_dir = home / DEPLOY.lstrip("/")
    assert "'v2'" in (deploy_dir / "app.py").read_text()


@pytest.mark.parametrize("marker_line", [
    pytest.param("LIBRARY_AUTO_APPLY = False\n", id="plain-false"),
    pytest.param("LIBRARY_AUTO_APPLY: bool = False\n", id="annotated-false"),
    pytest.param("    LIBRARY_AUTO_APPLY = True\n", id="indented"),
    pytest.param("LIBRARY_AUTO_APPLY = bool(os.environ.get('X'))\n", id="computed"),
])
def test_a_pending_revision_mentioning_auto_apply_without_a_plain_true_stops(
        seed_repo, home, marker_line):
    """The check fails closed on the identifier, not on one literal spelling
    of `False`: an annotated False, an indented assignment (not
    module-level), and a computed value all stop this exactly as the plain
    `LIBRARY_AUTO_APPLY = False` alembic/README documents does — before it
    backs up or touches anything, naming the file and the manual command.
    Distinct from `test_no_migrate_keeps_todays_schema_refusal` below, which
    is an operator's own choice rather than the revision's (security
    review, PR #231 round 1)."""
    old, new = "oldrev000001", "newrev000002"
    repo = seed_repo("v1", alembic_versions={old: None, new: old}, stamp_revision=old)
    first = run_installer("--repo=" + str(repo), home=home)
    assert first.returncode == 0, first.stdout + first.stderr
    db_path = default_db_path(repo)

    new_script = repo / "alembic" / "versions" / f"{new}_fixture.py"
    new_script.write_text(marker_line + new_script.read_text())
    (repo / "app.py").write_text("VERSION = 'v2'\n_DRAIN_DEADLINE_SECONDS = 50.0\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "v2, pending revision mentions LIBRARY_AUTO_APPLY")

    result = run_installer("--repo=" + str(repo), home=home)
    assert result.returncode != 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "LIBRARY_AUTO_APPLY" in combined
    assert f"{new}_fixture.py" in combined
    assert "alembic upgrade head" in combined

    backups = list(db_path.parent.glob(db_path.name + ".pre-deploy-*"))
    assert backups == [], "a revision that opts out must never be backed up or migrated"

    import sqlite3
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT version_num FROM alembic_version").fetchall()
    finally:
        conn.close()
    assert rows == [(old,)], "the vault must be untouched"


def test_a_pending_revision_with_auto_apply_true_migrates(seed_repo, home):
    """The one spelling that lets a revision carry the identifier and still
    auto-apply: a plain, untyped, module-level `LIBRARY_AUTO_APPLY = True`."""
    old, new = "oldrev000001", "newrev000002"
    repo = seed_repo("v1", alembic_versions={old: None, new: old}, stamp_revision=old)
    first = run_installer("--repo=" + str(repo), home=home)
    assert first.returncode == 0, first.stdout + first.stderr
    db_path = default_db_path(repo)

    new_script = repo / "alembic" / "versions" / f"{new}_fixture.py"
    new_script.write_text("LIBRARY_AUTO_APPLY = True\n" + new_script.read_text())
    (repo / "app.py").write_text("VERSION = 'v2'\n_DRAIN_DEADLINE_SECONDS = 50.0\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "v2, pending revision explicitly opts in")

    result = run_installer("--repo=" + str(repo), home=home)
    assert result.returncode == 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "Migrated" in combined

    import sqlite3
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT version_num FROM alembic_version").fetchall()
    finally:
        conn.close()
    assert rows == [(new,)]


def test_a_failing_migration_stops_before_the_install_and_names_the_backup(seed_repo, home):
    old, new = "oldrev000001", "newrev000002"
    repo = seed_repo("v1", alembic_versions={old: None, new: old}, stamp_revision=old)
    first = run_installer("--repo=" + str(repo), home=home)
    assert first.returncode == 0, first.stdout + first.stderr
    db_path = default_db_path(repo)

    # Committed, like the serve.sh stub's own FAIL_CHECK marker above — the
    # stub alembic interpreter reads this relative to $REPO (its own cwd
    # when invoked), and a committed file keeps the tree clean for the new
    # assess-the-checkout step this install also has to pass first.
    (repo / "ALEMBIC_FAIL_MIGRATION").write_text("simulate a broken migration\n")
    (repo / "app.py").write_text("VERSION = 'v2'\n_DRAIN_DEADLINE_SECONDS = 50.0\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "v2, migration will fail")

    result = run_installer("--repo=" + str(repo), home=home)
    assert result.returncode != 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "alembic upgrade head failed" in combined

    backups = sorted(db_path.parent.glob(db_path.name + ".pre-deploy-*"))
    assert len(backups) == 1, f"expected exactly one backup, found {backups}"
    assert str(backups[0]) in combined, "the failure message must name the backup's path"

    import sqlite3
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT version_num FROM alembic_version").fetchall()
    finally:
        conn.close()
    assert rows == [(old,)], "a failed migration must leave the live vault untouched"

    deploy_dir = home / DEPLOY.lstrip("/")
    assert "'v2'" not in (deploy_dir / "app.py").read_text()


def test_no_migrate_keeps_todays_schema_refusal(seed_repo, home):
    old, new = "oldrev000001", "newrev000002"
    repo = seed_repo("v1", alembic_versions={old: None, new: old}, stamp_revision=old)
    first = run_installer("--repo=" + str(repo), home=home)
    assert first.returncode == 0, first.stdout + first.stderr
    db_path = default_db_path(repo)

    (repo / "app.py").write_text("VERSION = 'v2'\n_DRAIN_DEADLINE_SECONDS = 50.0\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "v2, --no-migrate")

    result = run_installer("--repo=" + str(repo), "--no-migrate", home=home)
    assert result.returncode != 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "REFUSING to install" in combined
    assert old in combined and new in combined
    assert "--no-migrate was given" in combined

    backups = list(db_path.parent.glob(db_path.name + ".pre-deploy-*"))
    assert backups == [], "--no-migrate must never back up or migrate"
    deploy_dir = home / DEPLOY.lstrip("/")
    assert "'v2'" not in (deploy_dir / "app.py").read_text()


def test_allow_schema_behind_proceeds_past_the_refusal(seed_repo, home):
    old, new = "oldrev000001", "newrev000002"
    repo = seed_repo("v1", alembic_versions={old: None, new: old}, stamp_revision=old)
    first = run_installer("--repo=" + str(repo), home=home)
    assert first.returncode == 0, first.stdout + first.stderr

    (repo / "app.py").write_text("VERSION = 'v2'\n_DRAIN_DEADLINE_SECONDS = 50.0\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "v2, installing anyway")

    result = run_installer("--repo=" + str(repo), "--allow-schema-behind", home=home)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "'v2'" in (home / DEPLOY.lstrip("/") / "app.py").read_text()
    assert "installing with the vault's schema state as 'behind'" in (
        result.stdout + result.stderr)


def test_install_refuses_when_no_version_table(seed_repo, home, tmp_path):
    db_path = tmp_path / "no-table.db"
    stamp_alembic_db(db_path, None)
    repo = seed_repo("v1", library_db=str(db_path))
    first = run_installer("--repo=" + str(repo), home=home)
    assert first.returncode == 0, first.stdout + first.stderr

    (repo / "app.py").write_text("VERSION = 'v2'\n_DRAIN_DEADLINE_SECONDS = 50.0\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "v2")

    result = run_installer("--repo=" + str(repo), home=home)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "no alembic_version table" in (result.stdout + result.stderr)


def test_install_refuses_when_revision_unknown_to_the_tree(seed_repo, home, tmp_path):
    """An unknown revision is never migrated, even by default — it usually
    means the vault is AHEAD of the tree (a rollback), which running
    `alembic upgrade head` against would make worse, not better."""
    db_path = tmp_path / "unknown.db"
    stamp_alembic_db(db_path, UNKNOWN_REV)
    repo = seed_repo("v1", library_db=str(db_path))
    first = run_installer("--repo=" + str(repo), home=home)
    assert first.returncode == 0, first.stdout + first.stderr

    (repo / "app.py").write_text("VERSION = 'v2'\n_DRAIN_DEADLINE_SECONDS = 50.0\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "v2")

    result = run_installer("--repo=" + str(repo), home=home)
    assert result.returncode != 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert UNKNOWN_REV in combined
    assert "do not recognize" in combined

    backups = list(db_path.parent.glob(db_path.name + ".pre-deploy-*"))
    assert backups == [], "an unknown vault revision must never be backed up or migrated"


def test_install_refuses_when_db_file_is_missing(seed_repo, home, tmp_path):
    missing = tmp_path / "nowhere" / "library.db"
    repo = seed_repo("v1", library_db=str(missing))
    first = run_installer("--repo=" + str(repo), home=home)
    assert first.returncode == 0, first.stdout + first.stderr

    (repo / "app.py").write_text("VERSION = 'v2'\n_DRAIN_DEADLINE_SECONDS = 50.0\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "v2")

    result = run_installer("--repo=" + str(repo), home=home)
    assert result.returncode != 0, result.stdout + result.stderr
    assert f"no database file at {missing}" in (result.stdout + result.stderr)


def test_install_refuses_when_db_is_unreadable(seed_repo, home, tmp_path):
    db_path = tmp_path / "corrupt.db"
    db_path.write_bytes(b"not a sqlite database at all")
    repo = seed_repo("v1", library_db=str(db_path))
    first = run_installer("--repo=" + str(repo), home=home)
    assert first.returncode == 0, first.stdout + first.stderr

    (repo / "app.py").write_text("VERSION = 'v2'\n_DRAIN_DEADLINE_SECONDS = 50.0\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "v2")

    result = run_installer("--repo=" + str(repo), home=home)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "could not be read" in (result.stdout + result.stderr)


def test_install_refuses_when_live_plist_has_no_library_db(seed_repo, home):
    repo = seed_repo("v1", library_db=None)
    first = run_installer("--repo=" + str(repo), home=home)
    assert first.returncode == 0, first.stdout + first.stderr

    (repo / "app.py").write_text("VERSION = 'v2'\n_DRAIN_DEADLINE_SECONDS = 50.0\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "v2")

    result = run_installer("--repo=" + str(repo), home=home)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "LIBRARY_DB is not set" in (result.stdout + result.stderr)


# --- finding 3, round 2 review (PR #207): the TREE side of the check can be
# unreadable too, not just the vault's — _alembic_head_in_dir fails the same
# way whether the versions directory is ambiguous or missing outright, and
# either must refuse before anything is staged, exactly like the vault-side
# cases above.


def test_install_refuses_before_staging_when_tree_alembic_heads_are_ambiguous(seed_repo, home):
    rev_a, rev_b = "branchrevaaaa", "branchrevbbbb"
    repo = seed_repo("v1")
    first = run_installer("--repo=" + str(repo), home=home)
    assert first.returncode == 0, first.stdout + first.stderr
    deploy_dir = home / DEPLOY.lstrip("/")
    deployed_before = (deploy_dir / "DEPLOYED_COMMIT").read_text()
    (home.parent / "launchctl-calls.log").unlink(missing_ok=True)

    _write_alembic_versions(repo, {rev_a: None, rev_b: None})
    (repo / "app.py").write_text("VERSION = 'v2'\n_DRAIN_DEADLINE_SECONDS = 50.0\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "ambiguous heads")

    result = run_installer("--repo=" + str(repo), home=home)
    assert result.returncode != 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "REFUSING to install" in combined
    assert "could not be read" in combined

    assert launchctl_log(home) == [], (
        "the refusal must happen before any launchctl call")
    assert (deploy_dir / "DEPLOYED_COMMIT").read_text() == deployed_before, (
        "the refusal must happen before anything is staged — the previous "
        "release must be exactly as it was")
    assert not Path(str(deploy_dir) + ".new").exists()


def test_install_refuses_before_staging_when_tree_has_no_alembic_versions(seed_repo, home):
    repo = seed_repo("v1")
    first = run_installer("--repo=" + str(repo), home=home)
    assert first.returncode == 0, first.stdout + first.stderr
    deploy_dir = home / DEPLOY.lstrip("/")
    deployed_before = (deploy_dir / "DEPLOYED_COMMIT").read_text()
    (home.parent / "launchctl-calls.log").unlink(missing_ok=True)

    _git(repo, "rm", "-r", "-q", "alembic")
    (repo / "app.py").write_text("VERSION = 'v2'\n_DRAIN_DEADLINE_SECONDS = 50.0\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "drop alembic entirely")

    result = run_installer("--repo=" + str(repo), home=home)
    assert result.returncode != 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "REFUSING to install" in combined
    assert "could not be read" in combined

    assert launchctl_log(home) == [], (
        "the refusal must happen before any launchctl call")
    assert (deploy_dir / "DEPLOYED_COMMIT").read_text() == deployed_before, (
        "the refusal must happen before anything is staged — the previous "
        "release must be exactly as it was")
    assert not Path(str(deploy_dir) + ".new").exists()


# --- rule 7: git archive, never the working tree -----------------------------


def test_git_archive_excludes_an_untracked_gitignored_credential_file(seed_repo, home):
    """The whole reason this installer exists rather than a `cp -r`: a
    gitignored credential file sitting in the checkout must never reach the
    deploy area."""
    repo = seed_repo("v1")
    # Appended, not overwritten — `seed_repo`'s own .gitignore already
    # ignores .venv/ (the fixture's stub alembic interpreter), and
    # overwriting it here would make that untracked directory show up as
    # dirty on the very next install's own clean-tree check.
    gitignore = repo / ".gitignore"
    gitignore.write_text(gitignore.read_text() + "client-tokens.json\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-q", "-m", "gitignore")
    (repo / "client-tokens.json").write_text('{"super": "secret"}')

    result = run_installer("--repo=" + str(repo), home=home)
    assert result.returncode == 0, result.stdout + result.stderr

    deploy_dir = home / DEPLOY.lstrip("/")
    leaked = list(deploy_dir.rglob("client-tokens.json"))
    assert leaked == [], f"a gitignored credential file reached the deploy area: {leaked}"


def test_an_uncommitted_edit_is_never_what_gets_deployed(seed_repo, home):
    """rule 7 again, from the other direction: a dirty working tree must not
    leak into the deploy area either — only the committed blob. Installs an
    explicit sha (bypassing the branch arm's own new clean-tree assessment,
    which now refuses a dirty default-branch install outright — see
    `test_a_dirty_tree_stops_before_any_change` below for that case) so this
    stays focused on rule 7's own property: `git archive` reads the commit
    object, never the working tree, whatever is sitting dirty in it."""
    repo = seed_repo("v1")
    sha = _git(repo, "rev-parse", "main").stdout.strip()
    (repo / "app.py").write_text("VERSION = 'DIRTY — MUST NOT DEPLOY'\n")

    result = run_installer(
        "--repo=" + str(repo), f"--ref={sha}", "--allow-non-main", home=home)
    assert result.returncode == 0, result.stdout + result.stderr

    deployed = (home / DEPLOY.lstrip("/") / "app.py").read_text()
    assert "DIRTY" not in deployed
    assert "v1" in deployed


# --- atomicity ----------------------------------------------------------------


def test_failed_install_leaves_the_previous_deploy_untouched(seed_repo, home):
    """The installer validates the staged build with the deployed serve.sh's
    own `--check` BEFORE the swap — this plants a marker that makes the stub
    fail that check, and confirms the swap never happens: the prior good
    release stays exactly as it was, and nothing partial is left behind."""
    repo = seed_repo("v1")
    good = run_installer("--repo=" + str(repo), home=home)
    assert good.returncode == 0, good.stdout + good.stderr
    deploy_dir = home / DEPLOY.lstrip("/")
    good_sha = _git(repo, "rev-parse", "main").stdout.strip()

    (repo / "FAIL_CHECK").write_text("simulate a broken staged build\n")
    (repo / "app.py").write_text("VERSION = 'v2-broken'\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "v2, broken preflight")

    bad = run_installer("--repo=" + str(repo), home=home)
    assert bad.returncode != 0, bad.stdout + bad.stderr
    assert "preflight" in (bad.stdout + bad.stderr)

    assert (deploy_dir / "DEPLOYED_COMMIT").read_text().count(f"commit: {good_sha}") == 1
    assert "v1" in (deploy_dir / "app.py").read_text()
    assert "v2-broken" not in (deploy_dir / "app.py").read_text()

    leftovers = list(deploy_dir.parent.glob("library.new")) + \
        list(deploy_dir.parent.glob("library.old.*"))
    assert leftovers == [], f"a staging/backup directory survived: {leftovers}"


def test_reinstall_atomically_replaces_the_prior_release_with_no_leftovers(seed_repo, home):
    repo = seed_repo("v1")
    run_installer("--repo=" + str(repo), home=home)

    (repo / "app.py").write_text("VERSION = 'v3'\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "v3")

    result = run_installer("--repo=" + str(repo), home=home)
    assert result.returncode == 0, result.stdout + result.stderr

    deploy_dir = home / DEPLOY.lstrip("/")
    assert "v3" in (deploy_dir / "app.py").read_text()

    leftovers = list(deploy_dir.parent.glob("library.new")) + \
        list(deploy_dir.parent.glob("library.old.*"))
    assert leftovers == [], f"a staging/backup directory survived: {leftovers}"


# --- ref handling ---------------------------------------------------------


def test_a_ref_that_does_not_resolve_is_refused(seed_repo, home):
    repo = seed_repo("v1")
    result = run_installer("--repo=" + str(repo), "--ref=not-a-real-ref", home=home)
    assert result.returncode != 0
    assert "does not resolve" in (result.stdout + result.stderr)
    assert not (home / DEPLOY.lstrip("/")).exists()


def test_an_abbreviated_sha_is_refused(seed_repo, home):
    """Round 3 review (reviewer, non-blocking — substance): the full-commit-
    sha gate (`_looks_like_full_commit_sha`) only accepts a 40- or 64-char
    hex string. A real commit named by an abbreviated sha RESOLVES via plain
    `git rev-parse` — unlike `not-a-real-ref` above, which exercises a
    different path (the ambiguity loop's zero-candidate count, and the
    `set -eu`/`grep -c` fix) — but `--symbolic-full-name` returns empty for
    it exactly as for a relative expression, landing in the `''|HEAD` arm,
    where the length gate refuses it rather than deploying an under-
    specified commit."""
    repo = seed_repo("v1")
    sha = _git(repo, "rev-parse", "main").stdout.strip()
    short = sha[:12]

    # --allow-non-main: a short sha is not the literal string "main", so
    # without this the ADR-0059 rule 6 gate (REF != "main") would refuse it
    # first and this test would pass for the wrong reason — it has to reach
    # the classification arm's own length gate to test what it claims.
    result = run_installer(
        "--repo=" + str(repo), f"--ref={short}", "--allow-non-main", home=home)
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert short in combined
    assert "does not know how to classify" not in combined, (
        "an abbreviated sha should hit the ''|HEAD arm's own length gate, "
        "not the final classify-refusal arm")
    assert "REFUSING to install ref" not in combined, (
        "an abbreviated sha should be refused by the classification arm's "
        "own length gate, not by the --allow-non-main/ADR-0059 rule 6 gate")
    assert not (home / DEPLOY.lstrip("/")).exists()


def test_a_relative_expression_is_refused(seed_repo, home):
    """The same gate as `test_an_abbreviated_sha_is_refused` above, for a
    relative revision expression (`main~1`) instead of an abbreviated sha —
    both resolve via plain `git rev-parse` but are not a full-length hex
    object name, so both must be refused rather than silently deploying an
    unverified ancestor commit."""
    repo = seed_repo("v1")
    (repo / "app.py").write_text("VERSION = 'v2'\n_DRAIN_DEADLINE_SECONDS = 50.0\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "v2")

    # --allow-non-main: "main~1" is not the literal string "main", so
    # without this the ADR-0059 rule 6 gate (REF != "main") would refuse it
    # first and this test would pass for the wrong reason — see the
    # abbreviated-sha test above for the same hazard.
    result = run_installer(
        "--repo=" + str(repo), "--ref=main~1", "--allow-non-main", home=home)
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "main~1" in combined
    assert "REFUSING to install ref" not in combined, (
        "main~1 should be refused by the classification arm's own length "
        "gate, not by the --allow-non-main/ADR-0059 rule 6 gate")
    assert not (home / DEPLOY.lstrip("/")).exists()


def test_a_ref_outside_the_recognized_namespaces_hits_the_classify_refusal(
        seed_repo, home, tmp_path):
    """Round 3 review (reviewer, non-blocking — substance): the final `*)`
    catch-all (a `FULL_REF` outside refs/heads, refs/remotes/origin and
    refs/tags) had no direct test. A second remote's own tracking ref is
    exactly this: it resolves to a real commit, fully-qualified, but this
    installer only knows how to verify origin's branches and refresh
    origin's tracking refs, so anything else is refused rather than
    deployed unchecked."""
    repo = seed_repo("v1")
    _add_bare_origin(repo, tmp_path)
    upstream = tmp_path / "upstream.git"
    _git(repo, "clone", "-q", "--bare", str(repo), str(upstream))
    _git(repo, "remote", "add", "upstream", str(upstream))
    _git(repo, "fetch", "-q", "upstream")
    assert _git(repo, "show-ref", "--verify", "--quiet",
                 "refs/remotes/upstream/main", check=False).returncode == 0

    result = run_installer(
        "--repo=" + str(repo), "--ref=refs/remotes/upstream/main", home=home)
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "refs/remotes/upstream/main" in combined
    assert "does not know how to classify" in combined
    assert not (home / DEPLOY.lstrip("/")).exists()


def test_a_tag_named_like_the_branch_is_refused_as_ambiguous(seed_repo, home, tmp_path):
    """Security review, round 2: a tag with the same name as the branch being
    deployed makes plain `git rev-parse main` silently resolve to the TAG's
    commit (git's own ref precedence puts refs/tags/<name> ahead of
    refs/heads/<name>), while `git rev-parse --symbolic-full-name main`
    separately fails to disambiguate and returns nothing — neither one
    reaches this installer's actual classification with a diagnostic, so the
    collision is refused outright, before either call runs, naming every ref
    it could mean."""
    repo = seed_repo("v1")
    _add_bare_origin(repo, tmp_path)
    _git(repo, "tag", "main")

    result = run_installer("--repo=" + str(repo), home=home)
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "ambiguous" in combined
    assert "refs/tags/main" in combined
    assert "refs/heads/main" in combined
    assert not (home / DEPLOY.lstrip("/")).exists()


def test_a_tag_named_like_a_non_main_branch_is_refused_as_ambiguous(seed_repo, home, tmp_path):
    """The same collision for a branch reached through --allow-non-main — the
    ambiguity check runs before that gate (and before any resolution at
    all), so a tag reusing a feature branch's name is refused the same way
    regardless of --allow-non-main."""
    repo = seed_repo("v1")
    _add_bare_origin(repo, tmp_path)
    _git(repo, "checkout", "-q", "-b", "feat/x")
    _git(repo, "tag", "feat/x")
    _git(repo, "checkout", "-q", "main")

    result = run_installer(
        "--repo=" + str(repo), "--ref=feat/x", "--allow-non-main", home=home)
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "ambiguous" in combined
    assert "refs/tags/feat/x" in combined
    assert "refs/heads/feat/x" in combined
    assert not (home / DEPLOY.lstrip("/")).exists()


def test_a_branch_named_like_the_remote_is_refused_as_ambiguous_via_its_head(
        seed_repo, home, tmp_path):
    """Round 3 reviews (reviewer and security-reviewer, both non-blocking —
    style/substance): gitrevisions(7)'s sixth fallback form,
    refs/remotes/<name>/HEAD, is what a bare --ref actually collides with
    when a local branch shares a configured remote's own name — `git clone`
    auto-creates refs/remotes/origin/HEAD, but never a bare
    refs/remotes/origin ref, so the four original candidates (refs/<name>,
    refs/tags/<name>, refs/heads/<name>, refs/remotes/<name>) all miss this
    collision and only the /HEAD form this round adds catches it. Reproduced
    directly before this fix: the installer fell through to the generic
    classify-refusal message instead of the dedicated ambiguous one — still
    a refusal, never an unverified deploy, which is why neither round-3
    review raised this as blocking."""
    repo = seed_repo("v1")
    _add_bare_origin(repo, tmp_path)
    # `seed_repo` adds origin with `remote add` + `fetch`, which creates
    # refs/remotes/origin/HEAD only where fetch follows the remote's HEAD
    # (`remote.<name>.followRemoteHEAD`, added in git 2.48; setting it to
    # `never` on a newer git reproduces the older behaviour). ci1's job image
    # carries 2.43, where the ref never exists, there is no collision, and
    # the installer refuses through its non-main gate instead. Set it
    # outright so the case holds on any git.
    _git(repo, "remote", "set-head", "origin", "main")
    _git(repo, "branch", "origin")

    result = run_installer("--repo=" + str(repo), "--ref=origin", home=home)
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "ambiguous" in combined
    assert "refs/heads/origin" in combined
    assert "refs/remotes/origin/HEAD" in combined
    assert not (home / DEPLOY.lstrip("/")).exists()


def test_a_non_main_ref_is_refused_without_the_override(seed_repo, home):
    repo = seed_repo("v1")
    _git(repo, "checkout", "-q", "-b", "feat/x")
    (repo / "app.py").write_text("VERSION = 'v2-branch'\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "branch change")
    _git(repo, "checkout", "-q", "main")

    result = run_installer("--repo=" + str(repo), "--ref=feat/x", home=home)
    assert result.returncode != 0
    assert "REFUSING" in (result.stdout + result.stderr)
    assert not (home / DEPLOY.lstrip("/")).exists()


def test_allow_non_main_installs_the_named_ref(seed_repo, home):
    repo = seed_repo("v1")
    _git(repo, "checkout", "-q", "-b", "feat/x")
    (repo / "app.py").write_text("VERSION = 'v2-branch'\n_DRAIN_DEADLINE_SECONDS = 50.0\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "branch change")
    branch_sha = _git(repo, "rev-parse", "feat/x").stdout.strip()
    _git(repo, "checkout", "-q", "main")

    result = run_installer(
        "--repo=" + str(repo), "--ref=feat/x", "--allow-non-main", home=home)
    assert result.returncode == 0, result.stdout + result.stderr

    deploy_dir = home / DEPLOY.lstrip("/")
    assert "v2-branch" in (deploy_dir / "app.py").read_text()
    assert f"commit: {branch_sha}" in (deploy_dir / "DEPLOYED_COMMIT").read_text()


# --- stale local branch vs. origin (issue_01M1VW2D48D8F4B6BM6AK28D7F) --------
#
# Every `seed_repo` repo already has an "origin" remote (see that fixture),
# synced to its own `main` at seed time. These are the cases that pass
# `run_installer(..., sync_origin=False)` to turn off that fixture's own
# keep-it-synced default and manage origin's divergence from `repo` by
# hand — the thing under test here.


def _add_bare_origin(repo: Path, tmp_path: Path) -> Path:
    """Returns the path to the bare "origin" `seed_repo` already created for
    `repo` (see that fixture) — kept under its own name here so the cases
    below, which advance origin independently of `repo` by cloning and
    pushing to it, still have a path to clone from."""
    return tmp_path / "origin.git"


def test_a_local_branch_behind_origin_fast_forwards(seed_repo, home, tmp_path):
    """The core case ADR-0059's own fast-forward rule is about: main is
    checked out, clean, and strictly behind origin/main — this merges
    `--ff-only` to catch it up and installs the result, rather than
    refusing (library issue issue_01M1VW2D48D8F4B6BM6AK28D7F, extended
    2026-10-08: "assesses git, pulls -ff if necessary ... then does the
    install")."""
    repo = seed_repo("v1")
    origin = _add_bare_origin(repo, tmp_path)
    local_sha = _git(repo, "rev-parse", "main").stdout.strip()

    # Advance origin's main past what the local checkout has, without
    # fast-forwarding the local branch — the exact shape of both occurrences
    # in the issue.
    advanced = tmp_path / "advance-clone"
    _git(repo, "clone", "-q", str(origin), str(advanced))
    _git(advanced, "config", "user.email", "t@t")
    _git(advanced, "config", "user.name", "t")
    (advanced / "app.py").write_text("VERSION = 'v2'\n_DRAIN_DEADLINE_SECONDS = 50.0\n")
    _git(advanced, "add", "-A")
    _git(advanced, "commit", "-q", "-m", "v2, pushed to origin only")
    _git(advanced, "push", "-q", "origin", "main")
    origin_sha = _git(advanced, "rev-parse", "main").stdout.strip()
    assert origin_sha != local_sha

    result = run_installer("--repo=" + str(repo), home=home, sync_origin=False)
    assert result.returncode == 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "fast-forwarding" in combined.lower()
    assert local_sha in combined
    assert origin_sha in combined
    assert _git(repo, "rev-parse", "main").stdout.strip() == origin_sha, (
        "the local checkout itself must be fast-forwarded, not just the deploy")
    deploy_dir = home / DEPLOY.lstrip("/")
    assert f"commit: {origin_sha}" in (deploy_dir / "DEPLOYED_COMMIT").read_text()
    assert "'v2'" in (deploy_dir / "app.py").read_text()


def test_no_pull_keeps_todays_refusal_when_behind_origin(seed_repo, home, tmp_path):
    """The exact scenario above, with `--no-pull` — this never fast-forwards
    and never touches the checkout, refusing exactly as this installer
    always did before it could pull at all."""
    repo = seed_repo("v1")
    origin = _add_bare_origin(repo, tmp_path)
    local_sha = _git(repo, "rev-parse", "main").stdout.strip()

    advanced = tmp_path / "advance-clone-no-pull"
    _git(repo, "clone", "-q", str(origin), str(advanced))
    _git(advanced, "config", "user.email", "t@t")
    _git(advanced, "config", "user.name", "t")
    (advanced / "app.py").write_text("VERSION = 'v2'\n_DRAIN_DEADLINE_SECONDS = 50.0\n")
    _git(advanced, "add", "-A")
    _git(advanced, "commit", "-q", "-m", "v2, pushed to origin only")
    _git(advanced, "push", "-q", "origin", "main")
    origin_sha = _git(advanced, "rev-parse", "main").stdout.strip()
    assert origin_sha != local_sha

    result = run_installer(
        "--repo=" + str(repo), "--no-pull", home=home, sync_origin=False)
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "REFUSING" in combined
    assert local_sha in combined
    assert origin_sha in combined
    assert "pull --ff-only" in combined
    assert _git(repo, "rev-parse", "main").stdout.strip() == local_sha, (
        "--no-pull must never touch the checkout")
    assert not (home / DEPLOY.lstrip("/")).exists()


def test_a_local_branch_matching_origin_proceeds(seed_repo, home, tmp_path):
    repo = seed_repo("v1")
    _add_bare_origin(repo, tmp_path)
    sha = _git(repo, "rev-parse", "main").stdout.strip()

    result = run_installer("--repo=" + str(repo), home=home)
    assert result.returncode == 0, result.stdout + result.stderr
    deploy_dir = home / DEPLOY.lstrip("/")
    assert f"commit: {sha}" in (deploy_dir / "DEPLOYED_COMMIT").read_text()


def test_an_explicit_sha_deploys_as_given_even_behind_a_moved_origin(seed_repo, home, tmp_path):
    """A full sha matches no `refs/heads/<sha>`, so it is not "a branch" by
    this check's own test and is never compared to origin at all — the
    fixture's `main` need not even still point at it. `--allow-non-main` is
    needed here only because the sha string isn't literally "main" (ADR-0059
    rule 6), unrelated to the freshness check under test."""
    repo = seed_repo("v1")
    origin = _add_bare_origin(repo, tmp_path)
    target_sha = _git(repo, "rev-parse", "main").stdout.strip()

    advanced = tmp_path / "advance-clone-2"
    _git(repo, "clone", "-q", str(origin), str(advanced))
    _git(advanced, "config", "user.email", "t@t")
    _git(advanced, "config", "user.name", "t")
    (advanced / "app.py").write_text("VERSION = 'v2'\n_DRAIN_DEADLINE_SECONDS = 50.0\n")
    _git(advanced, "add", "-A")
    _git(advanced, "commit", "-q", "-m", "v2, pushed to origin only")
    _git(advanced, "push", "-q", "origin", "main")

    result = run_installer(
        "--repo=" + str(repo), f"--ref={target_sha}", "--allow-non-main", home=home)
    assert result.returncode == 0, result.stdout + result.stderr
    deploy_dir = home / DEPLOY.lstrip("/")
    assert f"commit: {target_sha}" in (deploy_dir / "DEPLOYED_COMMIT").read_text()


def test_a_tag_ref_deploys_without_the_staleness_check(seed_repo, home, tmp_path):
    """A tag resolves to refs/tags/<name>, not refs/heads/<name> — this
    installer cannot fetch and compare a tag against a branch, so it deploys
    exactly as given, same as an explicit sha, even with origin configured
    and behind."""
    repo = seed_repo("v1")
    origin = _add_bare_origin(repo, tmp_path)
    _git(repo, "tag", "deploy-v1")
    target_sha = _git(repo, "rev-parse", "deploy-v1").stdout.strip()

    advanced = tmp_path / "advance-clone-tag"
    _git(repo, "clone", "-q", str(origin), str(advanced))
    _git(advanced, "config", "user.email", "t@t")
    _git(advanced, "config", "user.name", "t")
    (advanced / "app.py").write_text("VERSION = 'v2'\n_DRAIN_DEADLINE_SECONDS = 50.0\n")
    _git(advanced, "add", "-A")
    _git(advanced, "commit", "-q", "-m", "v2, pushed to origin only")
    _git(advanced, "push", "-q", "origin", "main")

    result = run_installer(
        "--repo=" + str(repo), "--ref=deploy-v1", "--allow-non-main", home=home,
        sync_origin=False)
    assert result.returncode == 0, result.stdout + result.stderr
    deploy_dir = home / DEPLOY.lstrip("/")
    assert f"commit: {target_sha}" in (deploy_dir / "DEPLOYED_COMMIT").read_text()


def test_a_ref_spelled_refs_heads_main_behind_origin_fast_forwards_too(seed_repo, home, tmp_path):
    """Round 1 matched "is $REF a branch" against the literal --ref string,
    so --ref=refs/heads/main reached the unchecked `git rev-parse` path that
    was the exact gap this issue reports, under a different spelling of the
    same ref `test_a_local_branch_behind_origin_fast_forwards` above already
    covers bare (round 2 review) — and resolves to the SAME checked-out
    branch, so it fast-forwards too, not just a bare `main`. `--no-pull`
    here, rather than duplicating the ff assertions, is what proves this
    spelling reaches the identical code path as the bare one: the refusal
    text is unchanged from before this installer could pull at all."""
    repo = seed_repo("v1")
    origin = _add_bare_origin(repo, tmp_path)
    local_sha = _git(repo, "rev-parse", "main").stdout.strip()

    advanced = tmp_path / "advance-clone-refspelled"
    _git(repo, "clone", "-q", str(origin), str(advanced))
    _git(advanced, "config", "user.email", "t@t")
    _git(advanced, "config", "user.name", "t")
    (advanced / "app.py").write_text("VERSION = 'v2'\n_DRAIN_DEADLINE_SECONDS = 50.0\n")
    _git(advanced, "add", "-A")
    _git(advanced, "commit", "-q", "-m", "v2, pushed to origin only")
    _git(advanced, "push", "-q", "origin", "main")
    origin_sha = _git(advanced, "rev-parse", "main").stdout.strip()
    assert origin_sha != local_sha

    result = run_installer(
        "--repo=" + str(repo), "--ref=refs/heads/main", "--no-pull", home=home,
        sync_origin=False)
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "REFUSING" in combined
    assert local_sha in combined
    assert origin_sha in combined
    assert not (home / DEPLOY.lstrip("/")).exists()


def test_ref_HEAD_on_a_branch_behind_origin_fast_forwards_too(seed_repo, home, tmp_path):
    """--ref=HEAD resolves to a local branch exactly as "main" does — the
    checkout's HEAD points at whatever branch it's on — which `git rev-parse
    --symbolic-full-name HEAD` follows to see (round 2 review). `--no-pull`
    here keeps this a refusal test, same reasoning as the refs/heads/main
    spelling above."""
    repo = seed_repo("v1")
    origin = _add_bare_origin(repo, tmp_path)
    local_sha = _git(repo, "rev-parse", "main").stdout.strip()

    advanced = tmp_path / "advance-clone-head"
    _git(repo, "clone", "-q", str(origin), str(advanced))
    _git(advanced, "config", "user.email", "t@t")
    _git(advanced, "config", "user.name", "t")
    (advanced / "app.py").write_text("VERSION = 'v2'\n_DRAIN_DEADLINE_SECONDS = 50.0\n")
    _git(advanced, "add", "-A")
    _git(advanced, "commit", "-q", "-m", "v2, pushed to origin only")
    _git(advanced, "push", "-q", "origin", "main")
    origin_sha = _git(advanced, "rev-parse", "main").stdout.strip()
    assert origin_sha != local_sha

    result = run_installer(
        "--repo=" + str(repo), "--ref=HEAD", "--no-pull", home=home, sync_origin=False)
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "REFUSING" in combined
    assert local_sha in combined
    assert origin_sha in combined
    assert not (home / DEPLOY.lstrip("/")).exists()


def test_origin_slash_main_is_fetched_and_deploys_at_origins_current_commit(
        seed_repo, home, tmp_path):
    """--ref=origin/main resolves to refs/remotes/origin/main — fetched
    fresh before use, so what deploys is origin's CURRENT commit, not
    whatever this checkout's own remote-tracking ref last happened to hold
    (reviewer, round 2: this classification arm had zero test coverage)."""
    repo = seed_repo("v1")
    origin = _add_bare_origin(repo, tmp_path)

    advanced = tmp_path / "advance-clone-origin-slash-main"
    _git(repo, "clone", "-q", str(origin), str(advanced))
    _git(advanced, "config", "user.email", "t@t")
    _git(advanced, "config", "user.name", "t")
    (advanced / "app.py").write_text("VERSION = 'v2'\n_DRAIN_DEADLINE_SECONDS = 50.0\n")
    _git(advanced, "add", "-A")
    _git(advanced, "commit", "-q", "-m", "v2, pushed to origin only")
    _git(advanced, "push", "-q", "origin", "main")
    origin_sha = _git(advanced, "rev-parse", "main").stdout.strip()

    result = run_installer(
        "--repo=" + str(repo), "--ref=origin/main", "--allow-non-main", home=home,
        sync_origin=False)
    assert result.returncode == 0, result.stdout + result.stderr
    deploy_dir = home / DEPLOY.lstrip("/")
    assert f"commit: {origin_sha}" in (deploy_dir / "DEPLOYED_COMMIT").read_text()


def test_refs_remotes_origin_main_is_fetched_and_deploys_at_origins_current_commit(
        seed_repo, home, tmp_path):
    """The fully-qualified spelling of the same ref — same fetch-then-deploy
    behavior as the bare origin/main spelling above."""
    repo = seed_repo("v1")
    origin = _add_bare_origin(repo, tmp_path)

    advanced = tmp_path / "advance-clone-refs-remotes-origin-main"
    _git(repo, "clone", "-q", str(origin), str(advanced))
    _git(advanced, "config", "user.email", "t@t")
    _git(advanced, "config", "user.name", "t")
    (advanced / "app.py").write_text("VERSION = 'v2'\n_DRAIN_DEADLINE_SECONDS = 50.0\n")
    _git(advanced, "add", "-A")
    _git(advanced, "commit", "-q", "-m", "v2, pushed to origin only")
    _git(advanced, "push", "-q", "origin", "main")
    origin_sha = _git(advanced, "rev-parse", "main").stdout.strip()

    result = run_installer(
        "--repo=" + str(repo), "--ref=refs/remotes/origin/main", "--allow-non-main",
        home=home, sync_origin=False)
    assert result.returncode == 0, result.stdout + result.stderr
    deploy_dir = home / DEPLOY.lstrip("/")
    assert f"commit: {origin_sha}" in (deploy_dir / "DEPLOYED_COMMIT").read_text()


def test_a_non_main_branch_spelled_refs_heads_through_allow_non_main_behind_origin_is_refused(
        seed_repo, home, tmp_path):
    """The exact scenario the usage text's own --allow-non-main example names
    ("to verify a branch before merge"), reached through the one spelling
    round 1 actually missed: for a BARE branch name, round 1's own
    `refs/heads/$REF` string match already happened to equal this check, so
    a bare `--ref=feat/x` is not a regression test for round 1's gap — only
    a fully-qualified `--ref=refs/heads/<branch>` combined with
    --allow-non-main is (reviewer, round 2: the three prior spelling-specific
    tests each paired a problematic spelling with "main", which needs no
    --allow-non-main, leaving this exact combination untested)."""
    repo = seed_repo("v1")
    origin = _add_bare_origin(repo, tmp_path)
    _git(repo, "checkout", "-q", "-b", "feat/x")
    (repo / "app.py").write_text("VERSION = 'v2-branch'\n_DRAIN_DEADLINE_SECONDS = 50.0\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "branch change")
    _git(repo, "push", "-q", "origin", "feat/x")
    local_sha = _git(repo, "rev-parse", "feat/x").stdout.strip()
    _git(repo, "checkout", "-q", "main")

    advanced = tmp_path / "advance-clone-featx"
    _git(repo, "clone", "-q", str(origin), str(advanced))
    _git(advanced, "config", "user.email", "t@t")
    _git(advanced, "config", "user.name", "t")
    _git(advanced, "checkout", "-q", "feat/x")
    (advanced / "app.py").write_text("VERSION = 'v3-branch'\n_DRAIN_DEADLINE_SECONDS = 50.0\n")
    _git(advanced, "add", "-A")
    _git(advanced, "commit", "-q", "-m", "v3, pushed to origin only")
    _git(advanced, "push", "-q", "origin", "feat/x")
    origin_sha = _git(advanced, "rev-parse", "feat/x").stdout.strip()
    assert origin_sha != local_sha

    result = run_installer(
        "--repo=" + str(repo), "--ref=refs/heads/feat/x", "--allow-non-main", home=home,
        sync_origin=False)
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "REFUSING" in combined
    assert local_sha in combined
    assert origin_sha in combined
    assert not (home / DEPLOY.lstrip("/")).exists()


def test_a_local_branch_ahead_of_origin_is_refused(seed_repo, home, tmp_path):
    """The direction the pre-fix comparison already refused but no case
    named: an unpushed local commit, with origin never advanced at all."""
    repo = seed_repo("v1")
    _add_bare_origin(repo, tmp_path)
    origin_sha = _git(repo, "rev-parse", "main").stdout.strip()

    (repo / "app.py").write_text("VERSION = 'v2'\n_DRAIN_DEADLINE_SECONDS = 50.0\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "v2, never pushed")
    local_sha = _git(repo, "rev-parse", "main").stdout.strip()
    assert local_sha != origin_sha

    result = run_installer("--repo=" + str(repo), home=home, sync_origin=False)
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "REFUSING" in combined
    assert "ahead of" in combined
    assert local_sha in combined
    assert origin_sha in combined
    assert not (home / DEPLOY.lstrip("/")).exists()


def test_a_local_branch_diverged_from_origin_is_refused(seed_repo, home, tmp_path):
    """Neither a pull nor a push would fix this: local and origin each have a
    commit the other lacks. Named separately from "ahead"/"behind" so a
    future edit that simplifies this to a single ancestry check would be
    caught here even if it still passed the easier single-direction cases."""
    repo = seed_repo("v1")
    origin = _add_bare_origin(repo, tmp_path)

    advanced = tmp_path / "advance-clone-diverge"
    _git(repo, "clone", "-q", str(origin), str(advanced))
    _git(advanced, "config", "user.email", "t@t")
    _git(advanced, "config", "user.name", "t")
    (advanced / "app.py").write_text("VERSION = 'origin-side'\n_DRAIN_DEADLINE_SECONDS = 50.0\n")
    _git(advanced, "add", "-A")
    _git(advanced, "commit", "-q", "-m", "origin-only commit")
    _git(advanced, "push", "-q", "origin", "main")
    origin_sha = _git(advanced, "rev-parse", "main").stdout.strip()

    (repo / "app.py").write_text("VERSION = 'local-side'\n_DRAIN_DEADLINE_SECONDS = 50.0\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "local-only commit")
    local_sha = _git(repo, "rev-parse", "main").stdout.strip()
    assert local_sha != origin_sha

    result = run_installer("--repo=" + str(repo), home=home, sync_origin=False)
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "REFUSING" in combined
    assert "diverged from" in combined
    assert local_sha in combined
    assert origin_sha in combined
    assert not (home / DEPLOY.lstrip("/")).exists()


def test_no_origin_configured_with_a_branch_ref_is_refused(seed_repo, home):
    """Security review, round 1: a checkout with no "origin" remote at all
    used to skip this guard silently and deploy the local branch unverified
    — exactly the pre-fix exposure the guard exists to close. This repo has
    `seed_repo`'s own "origin" removed, leaving none at all."""
    repo = seed_repo("v1")
    _git(repo, "remote", "remove", "origin")

    result = run_installer("--repo=" + str(repo), home=home, sync_origin=False)
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "REFUSING" in combined
    assert "no 'origin' remote configured" in combined
    assert "commit sha" in combined
    assert not (home / DEPLOY.lstrip("/")).exists()


def test_a_fetch_failure_refuses_the_install(seed_repo, home, tmp_path):
    """origin is configured but unreachable (a bad URL stands in for a
    network failure or a revoked credential) — the fetch this guard needs
    fails, and that refuses rather than falling through to deploy the local
    branch unverified."""
    repo = seed_repo("v1")
    _add_bare_origin(repo, tmp_path)
    _git(repo, "remote", "set-url", "origin", str(tmp_path / "no-such-origin.git"))

    result = run_installer("--repo=" + str(repo), home=home, sync_origin=False)
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "could not fetch origin" in combined
    assert not (home / DEPLOY.lstrip("/")).exists()


def test_a_fetch_failure_names_refs_stash_when_present(seed_repo, home, tmp_path):
    """library issue, 2026-10-08: a dispatched run's `git stash` in a linked
    worktree left refs/stash — shared by every worktree of a repo — pointing
    at a commit whose objects were never completed, and `git fetch` then
    failed with "fatal: bad object refs/stash" in every worktree, this one
    included. Planting refs/stash directly (never a real `git stash`, which
    would touch the shared stash stack) reproduces the same shape without
    that risk: this names the ref and the removal command, and never removes
    it itself."""
    repo = seed_repo("v1")
    _add_bare_origin(repo, tmp_path)
    _git(repo, "remote", "set-url", "origin", str(tmp_path / "no-such-origin.git"))
    stash_sha = _git(repo, "rev-parse", "main").stdout.strip()
    _git(repo, "update-ref", "refs/stash", stash_sha)

    result = run_installer("--repo=" + str(repo), home=home, sync_origin=False)
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "could not fetch origin" in combined
    assert "refs/stash" in combined
    assert "update-ref -d refs/stash" in combined
    assert _git(repo, "show-ref", "--verify", "--quiet", "refs/stash", check=False).returncode == 0, (
        "this must never remove refs/stash itself")


# --- assess the checkout before pulling (2026-10-08 ruling: "assesses git,
# pulls -ff if necessary, runs a migration if needed, then does the
# install") — every case below is on the default ref (main, checked out),
# so each would otherwise reach the fast-forward path above; these stop
# before that path's own fetch ever runs. -------------------------------


def test_a_dirty_tree_stops_before_any_change(seed_repo, home):
    """An uncommitted edit to a tracked file on main stops before anything
    is fetched, staged, or touched — this never stashes, resets, checks
    out, or cleans the dirty file itself."""
    repo = seed_repo("v1")
    dirty_content = "VERSION = 'v2-uncommitted'\n_DRAIN_DEADLINE_SECONDS = 50.0\n"
    (repo / "app.py").write_text(dirty_content)

    result = run_installer("--repo=" + str(repo), home=home)
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "REFUSING to pull" in combined
    assert "not clean" in combined
    assert "app.py" in combined
    assert not (home / DEPLOY.lstrip("/")).exists()
    assert (repo / "app.py").read_text() == dirty_content


def test_a_dirty_tree_with_no_pull_is_unaffected(seed_repo, home):
    """--no-pull never attempts the fast-forward at all, so the new
    clean-tree assessment that guards it never runs either — an uncommitted
    edit is deployed-around exactly as it always was (rule 7), not refused."""
    repo = seed_repo("v1")
    (repo / "app.py").write_text("VERSION = 'v2-uncommitted'\n_DRAIN_DEADLINE_SECONDS = 50.0\n")

    result = run_installer("--repo=" + str(repo), "--no-pull", home=home)
    assert result.returncode == 0, result.stdout + result.stderr
    deployed = (home / DEPLOY.lstrip("/") / "app.py").read_text()
    assert "v2-uncommitted" not in deployed
    assert "v1" in deployed


def test_a_merge_in_progress_stops_before_any_change(seed_repo, home):
    """MERGE_HEAD existing — an unresolved conflicted merge — stops the same
    way a dirty tree does, before any fetch. Planted directly rather than
    provoked with a real conflicting merge, which this fixture's single-
    commit history has no second branch to produce."""
    repo = seed_repo("v1")
    git_dir = Path(_git(repo, "rev-parse", "--absolute-git-dir").stdout.strip())
    main_sha = _git(repo, "rev-parse", "main").stdout.strip()
    (git_dir / "MERGE_HEAD").write_text(main_sha + "\n")

    result = run_installer("--repo=" + str(repo), home=home)
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "REFUSING to pull" in combined
    assert "merge in progress" in combined
    assert not (home / DEPLOY.lstrip("/")).exists()


def test_a_rebase_merge_in_progress_stops_before_any_change(seed_repo, home):
    """rebase-merge/ existing (an interactive or merge-based rebase stopped
    at a conflict) stops the same way MERGE_HEAD does, before any fetch —
    the parallel branch of `_assess_checkout`'s own rebase check, planted
    directly since this fixture's single-commit history has nothing to
    rebase for real."""
    repo = seed_repo("v1")
    git_dir = Path(_git(repo, "rev-parse", "--absolute-git-dir").stdout.strip())
    (git_dir / "rebase-merge").mkdir()

    result = run_installer("--repo=" + str(repo), home=home)
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "REFUSING to pull" in combined
    assert "rebase in progress" in combined
    assert not (home / DEPLOY.lstrip("/")).exists()


def test_a_rebase_apply_in_progress_stops_before_any_change(seed_repo, home):
    """rebase-apply/ existing (an `am`-based rebase, or a plain `git am`,
    stopped at a conflict) stops the same way, before any fetch — the
    other half of the `||` in `_assess_checkout`'s rebase check."""
    repo = seed_repo("v1")
    git_dir = Path(_git(repo, "rev-parse", "--absolute-git-dir").stdout.strip())
    (git_dir / "rebase-apply").mkdir()

    result = run_installer("--repo=" + str(repo), home=home)
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "REFUSING to pull" in combined
    assert "rebase in progress" in combined
    assert not (home / DEPLOY.lstrip("/")).exists()


# --- plist handling -------------------------------------------------------


def test_a_drifted_live_plist_is_not_clobbered_without_force(seed_repo, home):
    """No record exists yet (no prior install), so a differing template is
    refused exactly as before the record was added."""
    repo = seed_repo("v1")
    launch_agents = home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    live_plist = launch_agents / "com.convivy.library.plist"
    drifted = drifted_plist_text(repo)
    live_plist.write_text(drifted)

    result = run_installer("--repo=" + str(repo), home=home)
    assert result.returncode == 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "NOT overwritten" in combined
    assert "what install-library last installed" in combined, (
        "the refusal must name what it's actually comparing against, not the template")
    assert "recorded sha256: none" in combined, (
        "no record exists yet, and the message must say so rather than print a sha")
    assert f"live sha256:     {sha256_of(live_plist)}" in combined
    assert live_plist.read_text() == drifted
    assert launchctl_log(home) == [], (
        "a plist refused as drifted must never trigger a reload")
    assert plist_record_sha(home) is None, (
        "a refused write must never record a plist that was never installed")


def test_force_overwrites_a_drifted_live_plist(seed_repo, home):
    """With no record present, a --force run overwrites the drifted plist as
    before, and also writes the record for the next install to check against."""
    repo = seed_repo("v1")
    launch_agents = home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    live_plist = launch_agents / "com.convivy.library.plist"
    live_plist.write_text(drifted_plist_text(repo))

    result = run_installer("--repo=" + str(repo), "--force", home=home)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "deploy/library/serve.sh" in live_plist.read_text()
    assert any(call.startswith("bootstrap ") for call in launchctl_log(home)), (
        "--force replacing a drifted plist changes what's live and must reload")
    assert plist_record_sha(home) == sha256_of(live_plist), (
        "a --force write must record what it just installed, for the next "
        "install's drift decision")


def test_a_template_change_over_an_unedited_live_plist_reloads_without_force(
        seed_repo, home):
    """The bug this issue is about: once a first install has recorded what it
    put at $PLIST_LIVE, a later template change over that SAME, unedited live
    plist is a legitimate change, not drift — no --force needed, and the
    reload path runs."""
    repo = seed_repo("v1")
    first = run_installer("--repo=" + str(repo), home=home)
    assert first.returncode == 0, first.stdout + first.stderr
    live_plist = home / "Library" / "LaunchAgents" / "com.convivy.library.plist"
    assert plist_record_sha(home) == sha256_of(live_plist)

    (repo / "launchd" / "com.convivy.library.plist").write_text(
        render_stub_plist(str(default_db_path(repo))).replace("stub-v1", "stub-v2"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "v2, template changed, live plist untouched")
    (home.parent / "launchctl-calls.log").unlink(missing_ok=True)

    second = run_installer("--repo=" + str(repo), home=home)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "not overwritten" not in (second.stdout + second.stderr).lower(), (
        "a template change over an unedited live plist must never be refused as drift")
    assert "stub-v2" in live_plist.read_text()
    assert any(call.startswith("bootstrap ") for call in launchctl_log(home)), (
        "a legitimate template change still has to reload the job"
    )
    assert plist_record_sha(home) == sha256_of(live_plist), (
        "the record must be updated to the newly installed plist")


def test_an_edited_live_plist_is_refused_even_with_a_record_on_file(
        seed_repo, home):
    """A first install records what it put at $PLIST_LIVE; an operator then
    hand-edits the live file (so it no longer matches the record); a template
    change on top of THAT must still be refused as drift, same as if no
    record existed at all."""
    repo = seed_repo("v1")
    first = run_installer("--repo=" + str(repo), home=home)
    assert first.returncode == 0, first.stdout + first.stderr
    live_plist = home / "Library" / "LaunchAgents" / "com.convivy.library.plist"
    recorded_before = plist_record_sha(home)
    assert recorded_before == sha256_of(live_plist)

    live_plist.write_text(live_plist.read_text() + "<!-- hand-edited after install -->\n")

    (repo / "launchd" / "com.convivy.library.plist").write_text(
        render_stub_plist(str(default_db_path(repo))).replace("stub-v1", "stub-v2"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "v2, template changed, live plist hand-edited")
    (home.parent / "launchctl-calls.log").unlink(missing_ok=True)
    edited_live = live_plist.read_text()

    second = run_installer("--repo=" + str(repo), home=home)
    assert second.returncode == 0, second.stdout + second.stderr
    combined = second.stdout + second.stderr
    assert "NOT overwritten" in combined
    assert f"recorded sha256: {recorded_before}" in combined, (
        "the refusal must print the recorded sha it's comparing against")
    assert f"live sha256:     {sha256_of(live_plist)}" in combined, (
        "the refusal must print the live plist's own sha, not just the recorded one")
    assert live_plist.read_text() == edited_live
    assert launchctl_log(home) == [], (
        "a plist refused as drifted must never trigger a reload")
    assert plist_record_sha(home) == recorded_before, (
        "a refused write must leave the record exactly as it was")


# --- LIBRARY_CHECK_PLIST: which plist --check is judged against -------------
#
# WHY THIS EXISTS (library issue issue_01M3W62VVYJPQ6N9Y85DF3ZQYP). `serve.sh
# --check` now imports `app` itself, under whatever plist `LIBRARY_CHECK_
# PLIST` names (test_serve_guard.py covers that half); these two cases cover
# the OTHER half, that `bin/install-library` itself passes the right plist —
# the one this install is actually about to make live, never its own shell
# environment and never the live plist when a legitimate new one is staged.


def test_check_is_passed_the_staged_candidate_plist(seed_repo, home):
    """The ordinary case: a fresh install has no drift to refuse, so the
    plist `--check` is judged against is the staged candidate — the one
    about to go live — not the live plist path (there is nothing live yet
    here) and not a path under the installer's own repo checkout.

    `checked_path` itself is gone by the time this assertion runs — the
    candidate is `mv`'d onto `$PLIST_LIVE` later in the same install, which
    is exactly why `--check` had to read it while it still existed rather
    than this test re-reading it afterward; the stub logged the path AT
    `--check` time, which is the moment that matters."""
    repo = seed_repo("v1")
    result = run_installer("--repo=" + str(repo), home=home)
    assert result.returncode == 0, result.stdout + result.stderr

    log = serve_check_env_log(home)
    assert len(log) == 1, log
    assert log[0].startswith("LIBRARY_CHECK_PLIST="), log
    checked_path = Path(log[0][len("LIBRARY_CHECK_PLIST="):])
    live_plist = home / "Library" / "LaunchAgents" / "com.convivy.library.plist"
    assert checked_path != live_plist, (
        "a fresh install has nothing live to fall back to — this must be "
        "the staged candidate, not the (nonexistent) live plist path"
    )
    assert ".candidate." in checked_path.name, (
        f"expected the staged candidate plist, got {checked_path}"
    )
    assert checked_path.parent == live_plist.parent


def test_check_is_passed_the_live_plist_when_the_candidate_is_refused_as_drift(
        seed_repo, home):
    """The install-library#157-shaped case: an operator's hand-edited live
    plist refuses the candidate write, but the code swap still happens —
    `serve.sh --check` must be judged against the plist that will actually
    stay loaded (the drifted live one), not the candidate that was just
    refused and removed."""
    repo = seed_repo("v1")
    launch_agents = home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    live_plist = launch_agents / "com.convivy.library.plist"
    drifted = drifted_plist_text(repo)
    live_plist.write_text(drifted)

    result = run_installer("--repo=" + str(repo), home=home)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "NOT overwritten" in (result.stdout + result.stderr), (
        "this case is only testing what it means to — the drift refusal"
    )

    log = serve_check_env_log(home)
    assert len(log) == 1, log
    checked_path = Path(log[0][len("LIBRARY_CHECK_PLIST="):])
    assert checked_path.read_text() == drifted, (
        "a refused candidate must not be what --check is judged against; "
        "the live (drifted) plist is what actually stays loaded"
    )
    assert live_plist.read_text() == drifted


def test_a_matching_template_with_no_record_writes_the_record(seed_repo, home):
    """The live plist already holds exactly the template's bytes (e.g. placed
    by hand, or left over from before this record existed), and no record is
    on file yet. This is not drift — nothing differs from the candidate — so
    no reload is needed, but the record has to be written now so a LATER
    template change is judged against it rather than refused."""
    repo = seed_repo("v1")
    sha = _git(repo, "rev-parse", "main").stdout.strip()
    launch_agents = home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    live_plist = launch_agents / "com.convivy.library.plist"
    candidate = subprocess.run(
        ["git", "-C", str(repo), "show", f"{sha}:launchd/com.convivy.library.plist"],
        capture_output=True, text=True, check=True).stdout
    live_plist.write_text(candidate)
    assert plist_record_sha(home) is None

    result = run_installer("--repo=" + str(repo), home=home)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "no reload needed" in result.stdout
    assert launchctl_log(home) == []
    assert plist_record_sha(home) == sha256_of(live_plist)


# --- home-directory rendering: every shipped launchd/*.plist carries
# `@@LIBRARY_HOME@@` in place of a literal home directory, because launchd
# does not expand `$HOME` or `~` in a plist it loads. The two cases below are
# this file's one exception to "never this checkout" in the module
# docstring: they read the real, checked-in templates directly (same as
# test_launchd_plist.py, test_cold_tier_plist.py and
# test_cert_expiry_check_plist.py already do), because the thing under test
# is the renderer's behavior on those specific three files, not the
# installer's deploy-from-a-repo path a fixture repo exists to isolate. -----

LAUNCHD_DIR = Path(__file__).resolve().parent.parent / "launchd"
PLIST_HOME_PLACEHOLDER = "@@LIBRARY_HOME@@"


@pytest.mark.parametrize("plist_name", [
    "com.convivy.library.plist",
    "com.convivy.library.cold-tier.plist",
    "com.convivy.library.cert-expiry-check.plist",
])
def test_render_plist_matches_a_literal_substitution_of_the_home_placeholder(plist_name):
    """`--internal-render-plist` is install-library's own hidden entry point
    for the substitution the real install path runs on every plist it
    writes (the real install compares and writes rendered content only,
    never the raw template). Here it is run, against a fixed,
    synthetic home, directly on each of the three checked-in templates and
    checked against the simplest possible independent oracle: a plain
    `str.replace` of that same template's own text, for that same home —
    what the current file's content literally is for that home, with no
    shell or awk involved. Byte-for-byte agreement is the whole claim: the
    installer's renderer does nothing but swap the placeholder for $HOME."""
    template_path = LAUNCHD_DIR / plist_name
    template_text = template_path.read_text()
    assert PLIST_HOME_PLACEHOLDER in template_text, (
        f"{plist_name} no longer carries the home placeholder — either this "
        f"test or the template has drifted")
    fixed_home = "/srv/fixture-home"
    expected = template_text.replace(PLIST_HOME_PLACEHOLDER, fixed_home)
    result = subprocess.run(
        [str(INSTALLER), "--internal-render-plist", str(template_path)],
        env={**os.environ, "HOME": fixed_home},
        capture_output=True, text=True, timeout=10, check=True)
    assert result.stdout == expected


def test_render_plist_leaves_a_placeholder_free_template_untouched(tmp_path):
    """The renderer is a literal substitution, not a template language — a
    file with no occurrence of the placeholder at all (every fixture plist
    in this suite, none of which carry it) must come back byte-identical,
    never erroring and never stripping or altering anything else."""
    plain = tmp_path / "plain.plist"
    plain.write_text(STUB_PLIST.format(library_db_entry=""))
    result = subprocess.run(
        [str(INSTALLER), "--internal-render-plist", str(plain)],
        env={**os.environ, "HOME": "/srv/fixture-home"},
        capture_output=True, text=True, timeout=10, check=True)
    assert result.stdout == plain.read_text()


@pytest.mark.parametrize("home", [
    r"/srv/back\tslash",
    r"/srv/trailing\\",
    "/srv/amp&ersand",
    "/srv/with space",
])
def test_render_plist_copies_a_home_with_special_characters_verbatim(
        tmp_path, home):
    """`awk -v` processes escape sequences in its value, so a renderer that
    passed $HOME that way would turn `\\t` into a tab; awk's gsub would give
    `&` a meaning. The renderer must copy every character of $HOME as is."""
    template = tmp_path / "t.plist"
    template.write_text(
        f"<string>{PLIST_HOME_PLACEHOLDER}/.library/x</string>\n"
        f"<string>{PLIST_HOME_PLACEHOLDER}</string>\n")
    result = subprocess.run(
        [str(INSTALLER), "--internal-render-plist", str(template)],
        env={**os.environ, "HOME": home},
        capture_output=True, text=True, timeout=10, check=True)
    assert result.stdout == template.read_text().replace(
        PLIST_HOME_PLACEHOLDER, home)


def test_a_full_install_renders_the_home_placeholder_from_this_run_s_own_home(
        seed_repo, home, tmp_path):
    """The real call site, end to end: `git show "$SHA:launchd/$PLIST_NAME"`
    piped straight into the same substitution the two cases above exercise
    directly. The fixture's own template is given one occurrence of the
    placeholder (every other case in this file uses the placeholder-free
    stub, which is also what proves the renderer is a no-op on it); the live
    plist this install writes must hold THIS run's own `home` fixture, never
    the placeholder and never some other machine's `$HOME`."""
    db_path = tmp_path / DEFAULT_DB_NAME
    placeholder_plist = render_stub_plist(str(db_path)).replace(
        "/srv/fixture-home/.local/deploy/library/serve.sh",
        f"{PLIST_HOME_PLACEHOLDER}/.local/deploy/library/serve.sh")
    repo = seed_repo("v1", extra={"launchd/com.convivy.library.plist": placeholder_plist})
    result = run_installer("--repo=" + str(repo), home=home)
    assert result.returncode == 0, result.stdout + result.stderr

    live_plist = home / "Library" / "LaunchAgents" / "com.convivy.library.plist"
    assert PLIST_HOME_PLACEHOLDER not in live_plist.read_text()
    rendered = plistlib.loads(live_plist.read_bytes())
    assert rendered["ProgramArguments"][-1] == f"{home}/.local/deploy/library/serve.sh"


def test_a_ref_with_no_service_plist_stops_the_install_and_keeps_the_live_plist(
        seed_repo, home):
    """`git show` fails when the ref carries no service plist. Piped into the
    renderer under a /bin/sh with no pipefail, that failure was masked and an
    empty candidate replaced the live plist (PR #227 round 2). The install
    must stop, leaving the live plist as it was."""
    repo = seed_repo("v1")
    first = run_installer("--repo=" + str(repo), home=home)
    assert first.returncode == 0, first.stdout + first.stderr
    live_plist = home / "Library" / "LaunchAgents" / "com.convivy.library.plist"
    before = live_plist.read_bytes()

    _git(repo, "rm", "-q", "launchd/com.convivy.library.plist")
    _git(repo, "commit", "-q", "-m", "drop the service plist")
    second = run_installer("--repo=" + str(repo), home=home)

    assert second.returncode != 0, second.stdout + second.stderr
    assert live_plist.read_bytes() == before


# --- round 3: the environment parser must read only the real `environment =
# { ... }` block, never `inherited` or `default` — both of which real
# `launchctl print` output always emits alongside it, and both of which
# contain the literal substring "environment = {" an unanchored regex would
# also match. ------------------------------------------------------------


def test_env_parser_reads_only_the_real_environment_block_from_a_captured_fixture():
    """Round 3 finding #1. The fixture is a real, once-captured `launchctl
    print` of this exact job (checked for secrets before being committed —
    its environment is paths and flags only), with PATH set to THREE
    different values across the inherited/default/real blocks — the
    sharpest case for catching a merge. This calls install-library's own
    parser through its hidden --internal-launchctl-env-pairs entry point,
    not a reimplementation of it."""
    result = subprocess.run(
        [str(INSTALLER), "--internal-launchctl-env-pairs"],
        input=FIXTURE_LAUNCHCTL_PRINT.read_text(),
        capture_output=True, text=True, timeout=10, check=True)
    pairs = dict(line.split("=", 1) for line in result.stdout.splitlines() if line)

    assert pairs == {
        "OSLogRateLimit": "64",
        "LIBRARY_API_TOKENS": "/srv/fixture-home/Convivy/convivy-orient/api-tokens.json",
        "LIBRARY_WRITES_ENABLED": "1",
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": "/srv/fixture-home",
        "LIBRARY_DB": "/srv/fixture-home/.library/library.db",
        "XPC_SERVICE_NAME": "com.convivy.library",
    }, (
        "must be exactly the real `environment` block's pairs — PATH here must "
        "be the real block's value, not `default`'s /usr/bin:/bin:/usr/sbin:/sbin, "
        "and SSH_AUTH_SOCK (inherited-only) must not appear at all")


def test_a_key_injected_into_the_inherited_or_default_block_does_not_pass_a_missing_key(
        seed_repo, home):
    """Round 3 finding #1, from the full install's side: the key the new
    plist expects is dropped from the stub's real `environment` block AND
    injected, with the SAME expected value, into the `inherited` block. An
    unanchored parser pooling all three blocks together would have found it
    there and false-passed; the anchored one must still report it missing."""
    repo = seed_repo("v1")
    result = run_installer(
        "--repo=" + str(repo), home=home,
        launchctl_env={
            "STUB_LAUNCHCTL_DROP_ENV_KEY": "LIBRARY_ENV_MARKER",
            "STUB_LAUNCHCTL_INHERITED_INJECT": "LIBRARY_ENV_MARKER=stub-v1",
        })
    assert result.returncode != 0
    assert "LIBRARY_ENV_MARKER" in (result.stdout + result.stderr)


# --- reload: platform/decisions/0059 stays quiesce-and-restart for code;
# this is the part of that same install that has to reach launchd itself,
# because nothing else replaces a LOADED job's environment. ------------------


def test_reload_invoked_when_the_plist_changed(seed_repo, home):
    """The ordinary case this whole issue is about: nothing was ever loaded
    before, so the plist this install writes always differs from that
    (vacuously) prior state, and the job gets bootstrapped with it — in the
    required order: taken down BEFORE the reload, brought back up (and
    confirmed with `launchctl print`) after."""
    repo = seed_repo("v1")
    result = run_installer("--repo=" + str(repo), home=home)
    assert result.returncode == 0, result.stdout + result.stderr

    calls = launchctl_log(home)
    verbs = [call.split()[0] for call in calls]
    assert "bootout" in verbs and "bootstrap" in verbs and "print" in verbs
    assert verbs.index("bootout") < verbs.index("bootstrap") < verbs.index("print"), (
        f"expected bootout, then bootstrap, then print — got {verbs}")
    assert "Reloaded" in result.stdout


def test_bootout_happens_before_staging_not_after(seed_repo, home):
    """The security-review requirement this whole reorder exists for: the job
    must come down BEFORE the new release is staged, not after, so neither
    old code under the new environment nor new code under the old one is ever
    live. Proven here from the failure side, since that's the side that can
    tell the two orders apart: a plist that will change, staged code planted
    to fail its own preflight, and no prior install to recover to. If bootout
    ran after staging (the old order), a preflight failure would leave the
    job never touched at all, with no `bootout` call in the log; here it must
    appear, and `bootstrap` must not — the staged build never got far enough
    for this install to reach it."""
    repo = seed_repo("v1", extra={"FAIL_CHECK": "simulate a broken staged build\n"})

    result = run_installer("--repo=" + str(repo), home=home)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "preflight" in (result.stdout + result.stderr)

    calls = launchctl_log(home)
    verbs = [call.split()[0] for call in calls]
    assert "bootout" in verbs, "bootout must happen before staging, even when staging then fails"
    assert "bootstrap" not in verbs, (
        "bootstrap must never be reached when staging fails after bootout")

    deploy_dir = home / DEPLOY.lstrip("/")
    assert not deploy_dir.exists(), "a fresh install's failed stage must leave nothing deployed"
    live_plist = home / "Library" / "LaunchAgents" / "com.convivy.library.plist"
    assert not live_plist.exists(), (
        "the plist is written only after a successful stage — it must not exist here")


def test_a_preflight_failure_after_bootout_restores_the_previous_plist(seed_repo, home):
    """Extending atomicity to the job itself, not just the code: a good v1 is
    already live (bootstrapped, per the test above); a v2 that both changes
    the plist and fails its own preflight takes the job down for the reload
    (as it must), then has to bring the OLD plist back up rather than leaving
    the job down for a release that never shipped."""
    repo = seed_repo("v1")
    first = run_installer("--repo=" + str(repo), home=home)
    assert first.returncode == 0, first.stdout + first.stderr
    live_plist = home / "Library" / "LaunchAgents" / "com.convivy.library.plist"
    assert "stub-v1" in live_plist.read_text()

    (repo / "launchd" / "com.convivy.library.plist").write_text(
        render_stub_plist(str(default_db_path(repo))).replace("stub-v1", "stub-v2"))
    (repo / "FAIL_CHECK").write_text("simulate a broken staged build\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "v2, changed plist, broken preflight")
    (home.parent / "launchctl-calls.log").unlink(missing_ok=True)

    second = run_installer("--repo=" + str(repo), "--force", home=home)
    assert second.returncode != 0, second.stdout + second.stderr
    assert "preflight" in (second.stdout + second.stderr)

    calls = launchctl_log(home)
    verbs = [call.split()[0] for call in calls]
    assert verbs.count("bootout") == 1
    recovery_bootstraps = [c for c in calls if c.startswith("bootstrap ") and "pre-reload" in c]
    assert recovery_bootstraps, (
        f"expected a recovery bootstrap of the pre-reload backup — got {calls}")

    # The live plist file was never overwritten (the write happens only after
    # a successful stage), so it still reads v1 — the recovery bootstrap
    # reloads the job from a separate backup file, not from this one.
    assert "stub-v1" in live_plist.read_text()
    assert "stub-v2" not in live_plist.read_text()
    deploy_dir = home / DEPLOY.lstrip("/")
    assert "v1" in (deploy_dir / "app.py").read_text()
    assert plist_record_sha(home) == sha256_of(live_plist), (
        "the failed reload's pair-restore must leave the record matching "
        "what was actually restored and loaded, not the v2 that never shipped")


def test_reload_skipped_when_the_plist_did_not_change(seed_repo, home):
    """A second install of the same commit writes the identical plist bytes
    over the identical live file — nothing for a reload to pick up, and
    forcing one anyway would be an unnecessary restart on every re-run."""
    repo = seed_repo("v1")
    first = run_installer("--repo=" + str(repo), home=home)
    assert first.returncode == 0, first.stdout + first.stderr

    (home.parent / "launchctl-calls.log").unlink(missing_ok=True)

    second = run_installer("--repo=" + str(repo), home=home)
    assert second.returncode == 0, second.stdout + second.stderr
    assert launchctl_log(home) == [], (
        "an unchanged plist must not trigger any launchctl call, bootout included")
    assert "no reload needed" in second.stdout


def test_no_reload_closing_message_describes_the_real_self_restart(seed_repo, home):
    """issue_01M335GK1ATAAE97J3GGMBFQP2: the closing message printed when the
    plist did not change (so this script itself reloaded nothing) used to
    claim "Nothing was loaded, unloaded, kickstarted, or restarted" and hand
    the operator `launchctl bootout`/`bootstrap` as "how to pick up this
    release" — both false. The service's own `_restart_watcher` (app.py)
    quiesces and restarts itself onto the staged commit unconditionally, on
    a timer, with no launchctl call from this script or the operator. The
    second install here is the one that takes the no-reload branch (the
    first always reloads, since there is no live plist yet to compare
    against) — see test_reload_skipped_when_the_plist_did_not_change above."""
    repo = seed_repo("v1")
    first = run_installer("--repo=" + str(repo), home=home)
    assert first.returncode == 0, first.stdout + first.stderr

    second = run_installer("--repo=" + str(repo), home=home)
    assert second.returncode == 0, second.stdout + second.stderr

    assert "Nothing was loaded, unloaded, kickstarted, or restarted" not in second.stdout
    assert "To pick up this release" not in second.stdout
    assert "gui/$(id -u)" not in second.stdout, (
        "no launchctl invocation must be handed to the operator as the way "
        "to pick up a release — the self-restart needs none")

    assert "restarts" in second.stdout and "ITSELF" in second.stdout, (
        "the message must say the running process restarts itself, not that "
        "nothing restarts")
    assert "/health" in second.stdout
    assert "staged_commit" in second.stdout
    assert "50s" in second.stdout, (
        "the drain bound named must come from the deployed app.py's own "
        "_DRAIN_DEADLINE_SECONDS (50.0 in the fixture), not a hardcoded guess")


def test_reload_skipped_when_drift_was_refused(seed_repo, home):
    """Same scenario as test_a_drifted_live_plist_is_not_clobbered_without_force,
    named for the reload property specifically: a refused plist write must
    leave the loaded job exactly as it was, drifted or not — no bootout
    either, since the job is never coming down for a write that never
    happens."""
    repo = seed_repo("v1")
    launch_agents = home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    (launch_agents / "com.convivy.library.plist").write_text(
        drifted_plist_text(repo))

    result = run_installer("--repo=" + str(repo), home=home)
    assert result.returncode == 0, result.stdout + result.stderr
    assert launchctl_log(home) == []


def test_a_failed_bootstrap_exits_non_zero(seed_repo, home):
    """A staged build that is already swapped into $DEPLOY_DIR by this point
    (the reload runs after the swap) but whose environment failed to load is
    not a successful install — the operator has to know the job isn't
    serving the code this run just deployed."""
    repo = seed_repo("v1")
    result = run_installer(
        "--repo=" + str(repo), home=home,
        launchctl_env={"STUB_LAUNCHCTL_BOOTSTRAP_FAIL": "1"})
    assert result.returncode != 0
    assert "bootstrap failed" in (result.stdout + result.stderr)


def test_a_reload_that_leaves_the_job_not_running_exits_non_zero(seed_repo, home):
    """`launchctl bootstrap` exiting 0 only means launchd accepted the
    definition, not that the program stayed up — ThrottleInterval exists
    for exactly the case where it didn't, so the outcome check has to read
    the job's actual state back, not the bootstrap exit code alone. The state
    stays `waiting` for every retry within the bound, so this also covers
    "running never within the bound fails"."""
    repo = seed_repo("v1")
    result = run_installer(
        "--repo=" + str(repo), home=home,
        launchctl_env={"STUB_LAUNCHCTL_STATE": "waiting"})
    assert result.returncode != 0
    assert "not running" in (result.stdout + result.stderr)


def test_reload_verification_passes_once_the_job_is_running_after_retries(seed_repo, home):
    """`print` reports `waiting` for the first two calls and `running`
    thereafter — a real bootstrap that took a moment to come up, not a
    permanently wedged one. The verification loop has to retry rather than
    fail on the first read."""
    repo = seed_repo("v1")
    result = run_installer(
        "--repo=" + str(repo), home=home,
        launchctl_env={"STUB_LAUNCHCTL_WAITING_FOR_CALLS": "2"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Reloaded" in result.stdout

    calls = launchctl_log(home)
    assert len([c for c in calls if c.startswith("print ")]) >= 3, (
        "expected at least the two waiting reads plus the one that finally saw running")


def test_missing_env_key_after_bootstrap_exits_non_zero(seed_repo, home):
    """`bootstrap` exiting 0 and the job reading `running` are both true here
    — and still not enough. `launchctl print`'s own environment block is
    missing the one variable this plist sets, simulating a bootstrap that
    "succeeded" without actually applying it (security review, PR #157 round
    1: the prior check confirmed only `state = running`)."""
    repo = seed_repo("v1")
    result = run_installer(
        "--repo=" + str(repo), home=home,
        launchctl_env={"STUB_LAUNCHCTL_DROP_ENV_KEY": "LIBRARY_ENV_MARKER"})
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "LIBRARY_ENV_MARKER" in combined, (
        "the failure must name the specific variable that never took effect")
    assert "launchctl bootstrap" in combined and "launchctl bootout" in combined, (
        "the failure must give the exact recovery commands, not just report the mismatch")


def test_exit_timeout_with_margin_over_the_drain_deadline_verifies(seed_repo, home):
    """issue_01M3N7NB9218HXKQDPD3NF1BJH: `_verify_reload` reads the EFFECTIVE
    `exit timeout` `launchctl print` reports back, not the plist's own
    number, and checks it against the deployed app.py's own
    `_DRAIN_DEADLINE_SECONDS` — 60 over a 50s drain deadline has margin, so
    the install completes."""
    repo = seed_repo("v1", extra={
        "app.py": "VERSION = 'v1'\n_DRAIN_DEADLINE_SECONDS = 50.0\n"})
    result = run_installer(
        "--repo=" + str(repo), home=home,
        launchctl_env={"STUB_LAUNCHCTL_EXIT_TIMEOUT": "60"})
    assert result.returncode == 0, result.stdout + result.stderr


def test_exit_timeout_equal_to_the_drain_deadline_fails_verification(seed_repo, home):
    """The failure side of the same check, and the exact shape of the bug it
    fixes: an effective `exit timeout` equal to the drain deadline gives
    launchd's SIGKILL nothing to wait for — it lands at the same instant the
    graceful drain gives up, rather than after it."""
    repo = seed_repo("v1", extra={
        "app.py": "VERSION = 'v1'\n_DRAIN_DEADLINE_SECONDS = 60.0\n"})
    result = run_installer(
        "--repo=" + str(repo), home=home,
        launchctl_env={"STUB_LAUNCHCTL_EXIT_TIMEOUT": "60"})
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "ExitTimeOut" in combined and "_DRAIN_DEADLINE_SECONDS" in combined, (
        "the failure must name both the effective value and the constant it fell short of")


def test_drain_deadline_too_large_for_shell_comparison_fails_verification(seed_repo, home):
    """Round 2 review, PR #159: a `_DRAIN_DEADLINE_SECONDS` so large that
    `[ ... -gt ... ]` cannot compare it (all digits, so it clears the
    unreadable check, but past what `[` can hold as an integer) must fail
    the reload — not verify it because the comparison itself errored rather
    than reading false."""
    repo = seed_repo("v1", extra={
        "app.py": "VERSION = 'v1'\n_DRAIN_DEADLINE_SECONDS = 99999999999999999999999.0\n"})
    result = run_installer(
        "--repo=" + str(repo), home=home,
        launchctl_env={"STUB_LAUNCHCTL_EXIT_TIMEOUT": "60"})
    assert result.returncode != 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "ExitTimeOut" in combined and "_DRAIN_DEADLINE_SECONDS" in combined, (
        "the failure must name both values even when they could not be compared")


def test_default_drain_deadline_and_exit_timeout_verify_by_default(seed_repo, home):
    """The happy path a fail-closed effective-ExitTimeOut check must not
    break: with no per-test override, the fixture's own default
    `_DRAIN_DEADLINE_SECONDS = 50.0` and the stub launchctl's own default
    `exit timeout = 60` line still leave margin, so the reload verifies."""
    repo = seed_repo("v1")
    result = run_installer("--repo=" + str(repo), home=home)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Reloaded" in result.stdout


def test_annotated_drain_deadline_constant_parses(seed_repo, home):
    """PR #159 round 1: `_drain_deadline_seconds` must accept the annotated
    assignment form (`NAME: TYPE = VALUE`), not only the plain one."""
    repo = seed_repo("v1", extra={
        "app.py": "VERSION = 'v1'\n_DRAIN_DEADLINE_SECONDS: float = 50.0\n"})
    result = run_installer(
        "--repo=" + str(repo), home=home,
        launchctl_env={"STUB_LAUNCHCTL_EXIT_TIMEOUT": "60"})
    assert result.returncode == 0, result.stdout + result.stderr


def test_drain_deadline_with_trailing_comment_parses(seed_repo, home):
    """A trailing `#` comment must be stripped before the number is parsed —
    the prior awk passed `50  # note` through as the unparseable `50#note`,
    which breaks the shell `-le` comparison."""
    repo = seed_repo("v1", extra={
        "app.py": "VERSION = 'v1'\n_DRAIN_DEADLINE_SECONDS = 50  # seconds\n"})
    result = run_installer(
        "--repo=" + str(repo), home=home,
        launchctl_env={"STUB_LAUNCHCTL_EXIT_TIMEOUT": "60"})
    assert result.returncode == 0, result.stdout + result.stderr


def test_unparseable_drain_deadline_fails_verification(seed_repo, home):
    """A `_DRAIN_DEADLINE_SECONDS` whose value isn't a plain integer once
    parsed (here, a string literal) must fail the reload rather than being
    skipped as "not applicable" — the round 1 finding this fixes."""
    repo = seed_repo("v1", extra={
        "app.py": "VERSION = 'v1'\n_DRAIN_DEADLINE_SECONDS = 'fifty'\n"})
    result = run_installer(
        "--repo=" + str(repo), home=home,
        launchctl_env={"STUB_LAUNCHCTL_EXIT_TIMEOUT": "60"})
    assert result.returncode != 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "app.py" in combined and "_DRAIN_DEADLINE_SECONDS" in combined
    assert "the live environment is missing" not in combined


def test_missing_exit_timeout_line_fails_verification(seed_repo, home):
    """A running job whose `launchctl print` carries no `exit timeout` line
    at all must fail the reload the same way an insufficient one does, not
    verify silently because there was nothing to compare against."""
    repo = seed_repo("v1")
    result = run_installer(
        "--repo=" + str(repo), home=home,
        launchctl_env={"STUB_LAUNCHCTL_EXIT_TIMEOUT": ""})
    assert result.returncode != 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "launchctl print" in combined and "exit timeout" in combined
    assert "the live environment is missing" not in combined


def test_decoy_drain_deadline_in_comment_or_string_does_not_supply_the_value(seed_repo, home):
    """`_drain_deadline_seconds` only takes a line starting in column 0 with
    the exact name — a commented-out assignment and a string literal that
    merely contains the name, both carrying a value small enough to pass the
    ExitTimeOut check if they were read, must not supply it: the real
    constant is absent here, so the reload must fail closed, not pass on the
    decoy."""
    repo = seed_repo("v1", extra={
        "app.py": (
            "VERSION = 'v1'\n"
            "# _DRAIN_DEADLINE_SECONDS = 1\n"
            "MSG = '_DRAIN_DEADLINE_SECONDS = 1'\n"
        )})
    result = run_installer(
        "--repo=" + str(repo), home=home,
        launchctl_env={"STUB_LAUNCHCTL_EXIT_TIMEOUT": "60"})
    assert result.returncode != 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "app.py" in combined and "_DRAIN_DEADLINE_SECONDS" in combined


# --- round 2: exact-match environment verification, and pairing the code
# rollback with the plist rollback in recovery -------------------------------


def test_a_truncated_reported_value_fails_verification(seed_repo, home):
    """Round 2 finding #3: the unanchored `grep -qF` this replaces would have
    passed if the reported value were merely a truncation of the expected
    one, since the shorter string still can't appear as a substring — this
    checks the failure direction is still caught by the exact-match parser
    when the reported value is a prefix of what the plist actually sets."""
    repo = seed_repo("v1")
    result = run_installer(
        "--repo=" + str(repo), home=home,
        launchctl_env={"STUB_LAUNCHCTL_TRUNCATE_ENV_KEY": "LIBRARY_ENV_MARKER"})
    assert result.returncode != 0
    assert "LIBRARY_ENV_MARKER" in (result.stdout + result.stderr)


def test_a_prefix_extended_reported_value_fails_verification(seed_repo, home):
    """Round 2 finding #3: the old `grep -qF -- "$k => $v"` matched as long as
    the expected pair appeared ANYWHERE in `launchctl print`'s text — so a
    reported value with the expected one as a PREFIX (extra characters
    appended) still contained the exact substring being searched for and
    would have false-passed. The exact-match parser must not."""
    repo = seed_repo("v1")
    result = run_installer(
        "--repo=" + str(repo), home=home,
        launchctl_env={"STUB_LAUNCHCTL_EXTEND_ENV_KEY": "LIBRARY_ENV_MARKER"})
    assert result.returncode != 0
    assert "LIBRARY_ENV_MARKER" in (result.stdout + result.stderr)


def test_a_suffix_key_collision_fails_verification(seed_repo, home):
    """Round 2 finding #3: the old check would also have false-passed on a
    DIFFERENT key whose name merely ends in the expected one — e.g. a
    reported `FOO_LIBRARY_ENV_MARKER => stub-v1` contains the exact substring
    `LIBRARY_ENV_MARKER => stub-v1` the old grep searched for, even though the
    plist's own `LIBRARY_ENV_MARKER` key never actually appears. The
    exact-match parser compares whole KEY=VALUE lines and must not confuse
    the two keys."""
    repo = seed_repo("v1")
    result = run_installer(
        "--repo=" + str(repo), home=home,
        launchctl_env={
            "STUB_LAUNCHCTL_RENAME_ENV_KEY": "LIBRARY_ENV_MARKER",
            "STUB_LAUNCHCTL_RENAME_ENV_KEY_TO": "FOO_LIBRARY_ENV_MARKER",
        })
    assert result.returncode != 0
    assert "LIBRARY_ENV_MARKER" in (result.stdout + result.stderr)


def test_a_bootstrap_failure_rolls_back_the_previous_tree_and_plist_as_a_pair(seed_repo, home):
    """Round 2 finding #1: a `launchctl bootstrap` failure after the code
    swap has already landed must roll back BOTH the deploy tree and the
    plist, not the plist alone — bootstrapping the old plist over the
    already-swapped new code would be exactly the new-code/old-environment
    hazard this script exists to close. Confirms the invariant directly: once
    recovery is done, what `launchctl print` reports as loaded matches
    $PLIST_LIVE byte for byte, and the deploy tree is back on the previous
    release, not left on the broken one."""
    repo = seed_repo("v1")
    first = run_installer("--repo=" + str(repo), home=home)
    assert first.returncode == 0, first.stdout + first.stderr
    live_plist = home / "Library" / "LaunchAgents" / "com.convivy.library.plist"
    assert "stub-v1" in live_plist.read_text()

    (repo / "launchd" / "com.convivy.library.plist").write_text(
        render_stub_plist(str(default_db_path(repo))).replace("stub-v1", "stub-v2"))
    (repo / "app.py").write_text("VERSION = 'v2'\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "v2, changed plist, bootstrap will fail")
    (home.parent / "launchctl-calls.log").unlink(missing_ok=True)

    second = run_installer(
        "--repo=" + str(repo), "--force", home=home,
        launchctl_env={"STUB_LAUNCHCTL_BOOTSTRAP_FAIL_IF_CONTAINS": "stub-v2"})
    assert second.returncode != 0, second.stdout + second.stderr

    deploy_dir = home / DEPLOY.lstrip("/")
    assert "v1" in (deploy_dir / "app.py").read_text(), (
        "the deploy tree must be rolled back to the previous release, not left on the new one")
    assert "v2" not in (deploy_dir / "app.py").read_text()

    assert "stub-v1" in live_plist.read_text()
    assert "stub-v2" not in live_plist.read_text(), (
        "the live plist FILE must be rolled back too, not just what's loaded")

    calls = launchctl_log(home)
    recovery_bootstraps = [c for c in calls if c.startswith("bootstrap ") and "pre-reload" in c]
    assert recovery_bootstraps, f"expected a recovery bootstrap of the pre-reload plist — got {calls}"

    assert stub_print_environment(home) == plist_environment(live_plist), (
        "what launchd reports as loaded must match $PLIST_LIVE's own content")

    leftovers = list(deploy_dir.parent.glob("library.old.*"))
    assert leftovers == [], f"the retained-for-rollback tree must be consumed, not left behind: {leftovers}"


def test_a_pair_restore_failure_leaves_the_job_unloaded_with_a_valid_message(seed_repo, home):
    """Round 2 finding #2: if the recovery bootstrap ALSO fails (both the new
    plist and the backed-up old one fail to load), the installer must not
    print a "RECOVERY FAILED" message naming a file it then deletes — every
    path the message names must still exist afterward — and must leave the
    job unloaded rather than ever bootstrapping a mismatched pair."""
    repo = seed_repo("v1")
    first = run_installer("--repo=" + str(repo), home=home)
    assert first.returncode == 0, first.stdout + first.stderr

    (repo / "launchd" / "com.convivy.library.plist").write_text(
        render_stub_plist(str(default_db_path(repo))).replace("stub-v1", "stub-v2"))
    (repo / "app.py").write_text("VERSION = 'v2'\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "v2, both bootstraps will fail")
    (home.parent / "launchctl-calls.log").unlink(missing_ok=True)

    result = run_installer(
        "--repo=" + str(repo), "--force", home=home,
        launchctl_env={"STUB_LAUNCHCTL_BOOTSTRAP_FAIL_IF_CONTAINS": "com.convivy.library"})
    assert result.returncode != 0

    combined = result.stdout + result.stderr
    assert "RECOVERY FAILED" in combined

    referenced_paths = [
        line.strip().split()[-1] for line in combined.splitlines()
        if "pre-reload" in line]
    assert referenced_paths, f"expected the failure message to name a plist path — got: {combined}"
    for path in referenced_paths:
        assert Path(path).exists(), (
            f"recovery message referenced a path it then deleted: {path}")

    assert "is down" in combined


def test_a_failed_pair_restore_cp_still_leaves_the_record_matching_the_live_file(
        seed_repo, home):
    """Security round 1 finding #4: the recovery bootstrap of the previous
    (pre-reload) plist succeeds, but the `cp` that's meant to copy its bytes
    onto $PLIST_LIVE fails — so $PLIST_LIVE is left holding the NEW plist's
    bytes (never overwritten) while the OLD one is what's actually loaded.
    The record must still equal the sha of whatever is actually at
    $PLIST_LIVE right now (the new plist's bytes) rather than being left at
    its pre-reload value, which is what a bare `_write_plist_record` call
    only inside the `cp` success branch used to do."""
    repo = seed_repo("v1")
    first = run_installer("--repo=" + str(repo), home=home)
    assert first.returncode == 0, first.stdout + first.stderr
    live_plist = home / "Library" / "LaunchAgents" / "com.convivy.library.plist"
    assert "stub-v1" in live_plist.read_text()

    (repo / "launchd" / "com.convivy.library.plist").write_text(
        render_stub_plist(str(default_db_path(repo))).replace("stub-v1", "stub-v2"))
    (repo / "app.py").write_text("VERSION = 'v2'\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "v2, new bootstrap fails, recovery cp also fails")
    (home.parent / "launchctl-calls.log").unlink(missing_ok=True)

    result = run_installer(
        "--repo=" + str(repo), "--force", home=home,
        launchctl_env={
            "STUB_LAUNCHCTL_BOOTSTRAP_FAIL_IF_CONTAINS": "stub-v2",
            "INSTALL_LIBRARY_TEST_FAIL_RECOVERY_CP": "1",
        })
    assert result.returncode != 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "could not be rewritten to match" in combined
    assert "fix the file by hand" in combined

    deploy_dir = home / DEPLOY.lstrip("/")
    assert "v1" in (deploy_dir / "app.py").read_text(), (
        "the code must still roll back to the previous release even though the plist cp failed")

    assert "stub-v2" in live_plist.read_text(), (
        "the cp failed, so $PLIST_LIVE must still hold the bytes this install wrote — "
        "never silently reverted by a code path other than the cp itself")

    assert plist_record_sha(home) == sha256_of(live_plist), (
        "the record must equal the sha of whatever is actually at $PLIST_LIVE, "
        "never stale, even when the cp that would have made them agree on "
        "content failed")


# --- round 3: one test per step of the reload table (bin/install-library's
# own "THE RELOADING INSTALL, STEP BY STEP" comment) whose failure isn't
# already covered above. ------------------------------------------------


def test_step1_a_failed_bootout_is_tolerated(seed_repo, home):
    """Step 1: `bootout`'s own exit status is ignored (`|| true`) — a job
    that isn't loaded yet exits nonzero from a real `launchctl bootout` too,
    and nothing downstream depends on this call having succeeded."""
    repo = seed_repo("v1")
    result = run_installer(
        "--repo=" + str(repo), home=home,
        launchctl_env={"STUB_LAUNCHCTL_BOOTOUT_FAIL": "1"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Reloaded" in result.stdout


def test_step3_a_failure_in_the_recovery_swap_fails_closed_with_files_named(seed_repo, home):
    """Step 3 / round 3 finding #2: the pair-restore's own `mv` of the
    previous release back into $DEPLOY_DIR is guarded explicitly rather
    than relying on `set -e` inside the trap. With
    INSTALL_LIBRARY_TEST_FAIL_RECOVERY_SWAP forcing that one `mv` to fail
    (bootstrap of the new plist also fails, so recovery is attempted at
    all): $DEPLOY_DIR must never be left missing or empty, the previous
    release must survive intact at the path the message names, and neither
    it nor the plist backup the message names may be deleted."""
    repo = seed_repo("v1")
    first = run_installer("--repo=" + str(repo), home=home)
    assert first.returncode == 0, first.stdout + first.stderr

    (repo / "launchd" / "com.convivy.library.plist").write_text(
        render_stub_plist(str(default_db_path(repo))).replace("stub-v1", "stub-v2"))
    (repo / "app.py").write_text("VERSION = 'v2'\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "v2, bootstrap and the recovery mv both fail")
    (home.parent / "launchctl-calls.log").unlink(missing_ok=True)

    result = run_installer(
        "--repo=" + str(repo), "--force", home=home,
        launchctl_env={
            "STUB_LAUNCHCTL_BOOTSTRAP_FAIL_IF_CONTAINS": "stub-v2",
            "INSTALL_LIBRARY_TEST_FAIL_RECOVERY_SWAP": "1",
        })
    assert result.returncode != 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "RECOVERY FAILED" in combined

    deploy_dir = home / DEPLOY.lstrip("/")
    assert deploy_dir.is_dir() and any(deploy_dir.iterdir()), (
        "the deploy dir must never be left missing or empty by a failed recovery swap")
    assert "v2" in (deploy_dir / "app.py").read_text(), (
        "the swap-back failed, so the untested new release is what's left on disk")

    old_dirs = list(deploy_dir.parent.glob("library.old.*"))
    assert len(old_dirs) == 1, f"expected the previous release to survive intact, got {old_dirs}"
    assert "v1" in (old_dirs[0] / "app.py").read_text()
    assert str(old_dirs[0]) in combined, "the failure message must name the intact previous release"

    broken_dirs = list(deploy_dir.parent.glob("library.broken.*"))
    assert broken_dirs == [], f"the scratch rename used during recovery must not survive: {broken_dirs}"

    backups = list((home / "Library" / "LaunchAgents").glob("com.convivy.library.plist.pre-reload.*"))
    assert len(backups) == 1, f"expected exactly one preserved plist backup, got {backups}"
    assert str(backups[0]) in combined, "the failure message must name the preserved plist backup"


def test_step6_a_verify_timeout_after_a_successful_bootstrap_keeps_the_previous_pair(
        seed_repo, home):
    """Step 6 / round 3 finding #4: once `launchctl bootstrap` has actually
    returned 0 for the NEW plist, a verify that never reads back `running`
    must not auto-roll-back (round 1: the new process may already be up) —
    but it must not silently discard the only way back to the previous
    release either. Both the previous release tree and the previous plist
    backup must survive on disk, and the exit message must name both.
    Round 2 review: $PLIST_LIVE already holds the new plist's bytes by this
    point, so the record must be rewritten to match it here too, the same
    as every other recovery outcome — not left describing the previous
    plist this install has already moved past."""
    repo = seed_repo("v1")
    first = run_installer("--repo=" + str(repo), home=home)
    assert first.returncode == 0, first.stdout + first.stderr

    (repo / "launchd" / "com.convivy.library.plist").write_text(
        render_stub_plist(str(default_db_path(repo))).replace("stub-v1", "stub-v2"))
    (repo / "app.py").write_text("VERSION = 'v2'\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "v2, bootstrap succeeds but verify never reads running")
    (home.parent / "launchctl-calls.log").unlink(missing_ok=True)

    result = run_installer(
        "--repo=" + str(repo), "--force", home=home,
        launchctl_env={"STUB_LAUNCHCTL_STATE": "waiting"})
    assert result.returncode != 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "not running" in combined

    deploy_dir = home / DEPLOY.lstrip("/")
    assert "v2" in (deploy_dir / "app.py").read_text(), (
        "no auto-rollback once bootstrap issued — the new, unverified pair stays installed")

    old_dirs = list(deploy_dir.parent.glob("library.old.*"))
    assert len(old_dirs) == 1, f"expected the previous release to survive as exactly one dir, got {old_dirs}"
    assert "v1" in (old_dirs[0] / "app.py").read_text()
    assert str(old_dirs[0]) in combined, "the message must name the kept previous release"

    backups = list((home / "Library" / "LaunchAgents").glob("com.convivy.library.plist.pre-reload.*"))
    assert len(backups) == 1, f"expected exactly one preserved plist backup, got {backups}"
    assert "stub-v1" in backups[0].read_text()
    assert str(backups[0]) in combined, "the message must name the kept previous plist"

    live_plist = home / "Library" / "LaunchAgents" / "com.convivy.library.plist"
    assert "stub-v2" in live_plist.read_text()
    assert plist_record_sha(home) == sha256_of(live_plist), (
        "the record must be rewritten to match $PLIST_LIVE on the "
        "verify-timeout path too, not left at its pre-reload value")


# --- round 4: the trap's own cleanup rm calls checked explicitly, the
# primary swap's double-mv-failure named rather than swallowed, and the
# success-path plist write checked rather than left to `set -e`. ------------


def test_round4_trap_cleanup_rm_failure_still_reaches_recovery_failed_message(
        seed_repo, home):
    """Round 4 finding #2: the recovery trap's own cleanup `rm` calls are
    checked explicitly now, never left for `set -e` to abort the trap on
    mid-message — round 3 already made this the rule for the trap's `mv`
    calls, but four `rm` calls were still bare. INSTALL_LIBRARY_TEST_FAIL_TRAP_RM
    forces the final cleanup of the stale new plist to fail without touching
    the filesystem; under the old, unguarded code, `set -eu` staying active
    inside a running EXIT trap on this bash 3.2.57 `/bin/sh` could have
    aborted the trap right there, before it ever printed RECOVERY FAILED or
    reached its own `exit "$st"` — losing the exit status the caller
    depends on along with it."""
    repo = seed_repo("v1")
    first = run_installer("--repo=" + str(repo), home=home)
    assert first.returncode == 0, first.stdout + first.stderr

    (repo / "launchd" / "com.convivy.library.plist").write_text(
        render_stub_plist(str(default_db_path(repo))).replace("stub-v1", "stub-v2"))
    (repo / "app.py").write_text("VERSION = 'v2'\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "v2, both bootstraps fail, trap cleanup rm forced to fail too")
    (home.parent / "launchctl-calls.log").unlink(missing_ok=True)

    result = run_installer(
        "--repo=" + str(repo), "--force", home=home,
        launchctl_env={
            "STUB_LAUNCHCTL_BOOTSTRAP_FAIL_IF_CONTAINS": "com.convivy.library",
            "INSTALL_LIBRARY_TEST_FAIL_TRAP_RM": "1",
        })
    combined = result.stdout + result.stderr
    assert result.returncode == 1, (
        f"expected the ORIGINAL triggering exit status (1), not something "
        f"else left behind by a failing rm: {combined}")
    assert "RECOVERY FAILED" in combined and "is down" in combined, (
        "the trap must still reach its full fail-closed message even when "
        "its own cleanup rm fails")
    assert "could not remove stale" in combined

    live_plist = home / "Library" / "LaunchAgents" / "com.convivy.library.plist"
    assert "stub-v2" in live_plist.read_text(), (
        "the forced-failing rm must actually have been skipped, leaving the "
        "stale new plist in place — proof the failure was real, not silently "
        "absorbed")


def test_round4_double_mv_failure_in_primary_swap_fails_closed_naming_old(
        seed_repo, home):
    """Round 4 finding #3: the primary swap's self-heal — when its second
    `mv` (staging the new build into $DEPLOY_DIR) fails, it tries to move
    $OLD back — must not claim the previous release was restored, and must
    not clear $OLD, when that restoring `mv` ALSO fails: $OLD is the only
    path back to the intact previous release at that point, and the message
    has to name it rather than a variable the script has already forgotten.
    Reached with the plist unchanged, so RELOADING is 0 and none of the
    trap's own recovery branches (which share the identical `mv "$OLD"
    "$DEPLOY_DIR"` command) run at all — this isolates the primary swap
    block on its own."""
    repo = seed_repo("v1")
    first = run_installer("--repo=" + str(repo), home=home)
    assert first.returncode == 0, first.stdout + first.stderr

    (repo / "app.py").write_text("VERSION = 'v2'\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "v2, plist unchanged, both swap mv's forced to fail")
    (home.parent / "launchctl-calls.log").unlink(missing_ok=True)

    result = run_installer(
        "--repo=" + str(repo), home=home,
        launchctl_env={
            "INSTALL_LIBRARY_TEST_FAIL_PRIMARY_SWAP": "1",
            "INSTALL_LIBRARY_TEST_FAIL_RECOVERY_SWAP": "1",
        })
    assert result.returncode != 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr

    assert "could not restore the previous release from" in combined
    assert "the previous release was restored" not in combined, (
        "a failed restoring mv must never be reported as a restoration")
    assert launchctl_log(home) == [], (
        "the plist never changed, so this must never even reach a bootout/reload")

    deploy_dir = home / DEPLOY.lstrip("/")
    assert not deploy_dir.exists(), (
        "both mv's were forced to fail, so nothing was ever moved into $DEPLOY_DIR")

    old_dirs = list(deploy_dir.parent.glob("library.old.*"))
    assert len(old_dirs) == 1, f"expected the previous release to survive intact, got {old_dirs}"
    assert "v1" in (old_dirs[0] / "app.py").read_text()
    assert str(old_dirs[0]) in combined, "the failure message must name the intact previous release"


def test_round4_plist_write_failure_fails_closed_and_recovers_previous_pair(
        seed_repo, home):
    """Round 4 finding #4: the final `mv` that installs the staged plist
    candidate onto $PLIST_LIVE — the success-path write, after the code has
    already swapped in — is checked explicitly rather than left to an
    implicit `set -e` abort. Reached on a reinstall with a changed plist, so
    the trap's ordinary pair-restore still gets to run afterward (this is a
    plain write failure, not a recovery-swap failure) and confirms the job
    ends up exactly where it started: on the previous release and plist."""
    repo = seed_repo("v1")
    first = run_installer("--repo=" + str(repo), home=home)
    assert first.returncode == 0, first.stdout + first.stderr
    live_plist = home / "Library" / "LaunchAgents" / "com.convivy.library.plist"

    (repo / "launchd" / "com.convivy.library.plist").write_text(
        render_stub_plist(str(default_db_path(repo))).replace("stub-v1", "stub-v2"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "v2, changed plist, plist write will fail")
    (home.parent / "launchctl-calls.log").unlink(missing_ok=True)

    result = run_installer(
        "--repo=" + str(repo), "--force", home=home,
        launchctl_env={"INSTALL_LIBRARY_TEST_FAIL_PLIST_WRITE": "1"})
    assert result.returncode != 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "could not write" in combined and str(live_plist) in combined

    assert "stub-v1" in live_plist.read_text(), (
        "the trap's ordinary pair-restore must still bring the previous "
        "plist back after a plain write failure")
    assert "stub-v2" not in live_plist.read_text()


# --- rule 3: never touches the Chart House / Archives' external state area --


def test_never_writes_outside_the_deploy_area_and_launch_agents(seed_repo, home):
    """The Chart House and the Archives live at ~/.library, outside any repo,
    read by absolute path — this installer has no line that opens or writes
    anything there, and this asserts it stays that way: nothing under $HOME
    changes except the deploy area and LaunchAgents."""
    (home / ".library").mkdir()
    (home / ".library" / "library.db").write_text("not a real db, just a sentinel\n")
    before = (home / ".library" / "library.db").read_text()
    before_listing = sorted(str(p.relative_to(home)) for p in (home / ".library").rglob("*"))

    repo = seed_repo("v1")
    result = run_installer("--repo=" + str(repo), home=home)
    assert result.returncode == 0, result.stdout + result.stderr

    after = (home / ".library" / "library.db").read_text()
    after_listing = sorted(str(p.relative_to(home)) for p in (home / ".library").rglob("*"))
    assert before == after
    assert before_listing == after_listing

    allowed_prefixes = (".local", "Library")
    for entry in home.iterdir():
        if entry.name == ".library":
            continue
        assert entry.name in allowed_prefixes, (
            f"install-library wrote something unexpected under $HOME: {entry}")


# --- Part A: launchd isolation is pinned by env var, not just by $HOME -----
# (library issue issue_01M1VW2D48D8F4B6BM6AK28D7F round 3). On 2026-10-02
# the previous attempt at this round ran install-library directly from a
# shell under a fake $HOME, to reproduce a bug — $HOME isolates the plist
# path (above), but the installer still booted out gui/$(id -u)/
# com.convivy.library BY LABEL, taking down the real, live Library service,
# because nothing about a fake $HOME changes the real uid or the real
# launchctl binary. These three cases prove the independent fix:
# INSTALL_LIBRARY_LAUNCHCTL, INSTALL_LIBRARY_LAUNCHD_DOMAIN and
# INSTALL_LIBRARY_LAUNCH_AGENTS (all three set by `run_installer` above) put
# every launchd-facing call behind a stub binary, a domain that can never be
# a real one, and a scratch directory, with no dependence on $HOME alone.

REAL_LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"


def _real_launch_agents_listing() -> dict[str, tuple[int, float]]:
    """name -> (size, mtime) of every entry in the REAL LaunchAgents
    directory, never faked by any `home` fixture here since this reads it by
    its own absolute path — used only to prove a test run left it alone."""
    if not REAL_LAUNCH_AGENTS.is_dir():
        return {}
    return {p.name: (p.stat().st_size, p.stat().st_mtime) for p in REAL_LAUNCH_AGENTS.iterdir()}


def test_every_launchctl_call_uses_the_stub_and_the_test_domain(seed_repo, home):
    """A fresh install reloads (bootout, bootstrap, print all run). Every
    line the stub logged names `TEST_LAUNCHD_DOMAIN` as its domain — proving
    install-library actually read `INSTALL_LIBRARY_LAUNCHD_DOMAIN` rather
    than falling back to the real `gui/<uid>` — and the log existing at all
    proves the STUB ran: the real `launchctl` has never heard of
    `STUB_LAUNCHCTL_LOG` and would never write to it."""
    repo = seed_repo("v1")
    result = run_installer("--repo=" + str(repo), home=home)
    assert result.returncode == 0, result.stdout + result.stderr

    calls = launchctl_log(home)
    assert calls, "expected at least one launchctl call on a fresh (reloading) install"
    assert any(c.startswith("bootout ") for c in calls)
    assert any(c.startswith("bootstrap ") for c in calls)
    assert any(c.startswith("print ") for c in calls)

    for call in calls:
        cmd, arg2 = call.split(" ", 2)[0], call.split(" ", 2)[1]
        domain = arg2 if cmd == "bootstrap" else arg2.rsplit("/", 1)[0]
        assert domain == TEST_LAUNCHD_DOMAIN, (
            f"call {call!r} used domain {domain!r}, not the pinned test domain "
            f"{TEST_LAUNCHD_DOMAIN!r} — this would be a real launchd domain "
            "in an unpinned run")


def test_launch_agents_override_is_used_to_the_exclusion_of_the_default_path(
        seed_repo, home):
    """Platform-independent proof that INSTALL_LIBRARY_LAUNCH_AGENTS is what
    isolates a test run, not just a fake `$HOME` (round 3 review: the
    previous version of this test hardcoded this developer's own
    Library/LaunchAgents path, which doesn't exist on CI's ubuntu runner, so
    the before/after listing was {} on both sides there and the assertion
    passed without checking anything). This run points
    INSTALL_LIBRARY_LAUNCH_AGENTS at a scratch directory that is NOT
    $HOME/Library/LaunchAgents — install-library's own default for an
    unpinned run, still reachable under this same fake, temporary `home` —
    so a plist landing in the scratch directory while that default path
    stays absent proves the override is read and used exclusively, on any
    platform, with no dependence on a real machine's own LaunchAgents
    directory existing."""
    scratch = home.parent / "scratch-launch-agents"
    default_under_fake_home = home / "Library" / "LaunchAgents"

    repo = seed_repo("v1")
    result = run_installer(
        "--repo=" + str(repo), home=home,
        launchctl_env={"INSTALL_LIBRARY_LAUNCH_AGENTS": str(scratch)})
    assert result.returncode == 0, result.stdout + result.stderr

    assert (scratch / "com.convivy.library.plist").is_file(), (
        "the plist did not land in the INSTALL_LIBRARY_LAUNCH_AGENTS "
        "override directory")
    assert not default_under_fake_home.exists(), (
        f"install-library wrote to its own default LaunchAgents path "
        f"{default_under_fake_home} even though INSTALL_LIBRARY_LAUNCH_AGENTS "
        f"pointed it at {scratch} instead")


def test_real_launch_agents_directory_is_never_touched(seed_repo, home):
    """Guarded to run only where this developer's own ~/Library/LaunchAgents
    exists — this developer's own Mac. On any other machine, including this
    repo's own CI (ubuntu-latest), that directory doesn't exist, so a
    before/after listing would be {} on both sides and the assertion would
    pass whether or not isolation actually worked (round 3 review); the
    platform-independent proof of that mechanism is
    test_launch_agents_override_is_used_to_the_exclusion_of_the_default_path
    above. This test adds one thing that one can't: it reads the REAL path
    by its real absolute name, so it is the direct regression test for the
    2026-10-02 incident this round's hard rule exists to prevent, on the one
    machine where that incident happened."""
    if not REAL_LAUNCH_AGENTS.is_dir():
        pytest.skip(f"{REAL_LAUNCH_AGENTS} does not exist on this machine")
    before = _real_launch_agents_listing()

    repo = seed_repo("v1")
    result = run_installer("--repo=" + str(repo), home=home)
    assert result.returncode == 0, result.stdout + result.stderr

    after = _real_launch_agents_listing()
    assert after == before, (
        "a test run changed the REAL LaunchAgents directory — "
        "INSTALL_LIBRARY_LAUNCH_AGENTS isolation failed")


def test_forgetting_to_pin_the_launchd_domain_makes_the_stub_refuse(seed_repo, home):
    """The harness-forgets-the-domain case: with
    INSTALL_LIBRARY_LAUNCHD_DOMAIN left unset, install-library falls back to
    its own documented default, the REAL `gui/<uid>` domain — and the
    stub's `STUB_LAUNCHCTL_EXPECTED_DOMAIN` (still pinned to the test
    domain by `run_installer` regardless of `set_launchd_domain`) refuses
    that call loudly rather than silently accepting it, so the installer
    run fails rather than reaching a real domain even when the harness
    itself forgot to pin one."""
    repo = seed_repo("v1")
    result = run_installer(
        "--repo=" + str(repo), home=home, set_launchd_domain=False)

    output = result.stdout + result.stderr
    assert result.returncode != 0, (
        "a run with no pinned test domain must fail, not silently proceed "
        f"against install-library's own real-domain default — got: {output}")
    assert "refusing bootstrap against domain" in output, output
    real_domain = f"gui/{os.getuid()}"
    assert real_domain in output, (
        f"expected the refused call to name the real domain {real_domain!r} "
        f"install-library falls back to — got: {output}")
