"""Regression coverage for serve.sh's interpreter guard.

The guard decides which interpreter the Library is allowed to execute, and it has
shipped wrong five times: one permission digit, a symlinked venv stat'd instead of
its target, the venv root checked while a `bin/python` pointing elsewhere was
exec'd, the resolved target's directory checked instead of the one holding the
symlink, and mode bits read while a macOS ACL granted what they did not show.
Every one of those was found by a reviewer building the case by hand, and none of
them was visible to anybody reading the file. So the cases live here now, where
they run without somebody having to think of them again.

THE MODULE RUNS ON BOTH DARWIN AND LINUX, and CI runs it on ubuntu-latest. The
guard itself branches on `uname -s`: BSD `stat -f`, macOS ACL semantics (`ls -lde`,
`chmod +a`) and the `admin` group on Darwin; GNU `stat -c`, POSIX ACLs read with
`getfacl`, and no group carve-out beyond root's gid 0 on Linux. Cases that exercise
a platform-specific mechanism are gated to the platform that has it; every other
case runs on both, so a wrong version fails in CI on the very push that introduces
it, not only in a reviewer's local `pytest` on a Mac.

EVERY CASE DRIVES `serve.sh --check`, which runs the whole guard and the import
preflight and then stops short of the exec. Nothing here can import the app, read
the live credentials or the live control DB, or bind port 8100, whatever the
interpreter under test turns out to be. The first version of this file relied
instead on port 8100 already being held by the running Library — true on the
deployment machine and false on a fresh clone, where the case that points at the
real venv would have started a real service against real data for as long as the
test let it run.
"""

import os
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

SERVE = Path(__file__).resolve().parent.parent / "serve.sh"

DARWIN_ONLY = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="macOS ACL semantics (chmod +a, the admin group) only exist on Darwin",
)
LINUX_ONLY = pytest.mark.skipif(
    sys.platform != "linux",
    reason="POSIX ACL semantics (getfacl/setfacl) only exist on Linux",
)


# Exits the guard uses. 78 is EX_CONFIG, which is what every refusal returns.
REFUSED = 78

# A target that the guard will happily accept and that costs nothing to execute,
# so a passing case does not start a second Library on the production port.
HARMLESS = "/usr/bin/true"


def _fail_or_skip(reason: str):
    """Skip normally; fail hard under SERVE_GUARD_REQUIRED=1.

    Round 3 finding: 4 of this module's 7 CI skips were unfixed gaps (an
    unprivileged runner can't build their preconditions) rather than legitimate
    ones (a macOS-only mechanism), and nothing distinguished the two — a skip
    reads the same whether the gap is permanent or just never wired up. Under
    the same flag that turns a checkout-path refusal into a hard failure (see
    `_guard_refuses_this_checkout` below), a missing precondition here fails
    collection the same way, so a CI regression that silently drops one of
    these fixtures cannot go on reading green.
    """
    if os.environ.get("SERVE_GUARD_REQUIRED") == "1":
        pytest.fail(reason, pytrace=False)
    pytest.skip(reason)


def _guard_refuses_this_checkout() -> str | None:
    """The guard's own answer about the path this checkout sits on, or None.

    **ASKS `serve.sh`, RATHER THAN REIMPLEMENTING WHAT `serve.sh` WOULD SAY**
    (reviewer, PR #62). The first version of this walked the ancestors in Python
    and checked `S_IWOTH`, which is one of four things the real walk refuses on —
    it also refuses a non-owner, a group-writable directory whose group is not
    an administrator group, an ACL granting write the mode bits do not show
    (`ls -lde` on Darwin, `getfacl` on Linux), and any component it cannot
    stat. A checkout under one of those would have hit the confusing red
    this skip exists to remove, and a predicate that mirrors a guard is a
    predicate that can drift from it. Running the guard cannot drift, because it
    is the guard.

    `--check` is what makes this safe at collection time: the walk and the
    preflight run, the exec does not. `PY=/usr/bin/true` keeps the interpreter
    leg trivial, and `SERVE_ALLOW_BRANCH` bypasses the branch guard, which a
    worktree fails by construction and which is not what this is asking about.

    **MATCHES `serve: this script:` SPECIFICALLY.** `walk_checked "$0" "this
    script"` labels every refusal about the script's own path with that prefix,
    and that walk runs before argument parsing and before the interpreter is
    resolved — so it is exactly the set of refusals that preempt every case in
    this file. A refusal about anything else is a real answer to a real question
    and must not be skipped past.

    Returns None when `serve.sh` cannot be run at all, so a broken script fails
    these tests loudly instead of skipping them quietly.
    """
    try:
        result = subprocess.run(
            [str(SERVE), "--check"],
            env=dict(os.environ, PY=HARMLESS, SERVE_ALLOW_BRANCH="1"),
            capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in result.stderr.splitlines():
        if line.startswith("serve: this script:"):
            return line
    return None


# **SKIPPED WHERE THE CHECKOUT ITSELF SITS SOMEWHERE serve.sh REFUSES, and the
# reason is that the guard is WORKING there rather than failing.**
#
# Every case below drives `serve.sh --check` and asserts a specific exit for a
# specific refusal. The path walk runs first — before argument parsing, before
# `PY` is resolved — so a checkout under a directory the guard refuses gets that
# refusal instead, and 13 of these cases then compare it against the exit code
# their own case expects. It is the right answer to a different question.
#
# **A co session that puts a worktree in its scratchpad hits this**, because the
# scratchpad lives under `/private/tmp`, which is mode 1777. Measured 2026-08-26:
# a worktree at `/private/tmp/claude-501/.../scratchpad/lib-health` reported 13
# failed / 25 passed, an identical checkout of the same commit at
# `~/Convivy/worktrees/` reported 38 passed / 1 skipped, and
# `serve.sh --check` in the first printed
# `serve: this script: /private/tmp is world-writable (mode 777) — refusing`.
# A worktree `agentctl spawn` creates is under `~/Convivy/worktrees` and is NOT
# affected; that was checked rather than assumed
# (issue_01M0ZQT66XE1GZ4219H20TX156, whose diagnosis named a missing `.venv` —
# the cases pass `PY` explicitly and never need one).
#
# **The skip is on what the guard says, not on a missing `.venv`.** Skipping
# wherever `.venv` is absent would silence this file in every dispatched
# worker's worktree, which is exactly where the guard currently runs and passes
# — trading a misleading red for a silent green over the check that has shipped
# wrong five times.
_PATH_REFUSAL = _guard_refuses_this_checkout()

# **CI sets SERVE_GUARD_REQUIRED=1 so this skip can never again stand in for a
# passing run.** Round 1 of this PR shipped a `ubuntu-latest` job whose checkout
# sits under the runner user's home directory (`$HOME`), which carries a
# `default:user:runner:rwx` ACL —
# exactly what the guard's own ACL check refuses. Every one of this module's 50
# cases skipped via the reason below, silently, on every push, and the job read
# green throughout. Under this flag the same refusal is a collection error
# naming the path and the reason instead of a skip, so a CI checkout that lands
# somewhere the guard refuses fails loudly rather than reporting nothing was
# wrong. CI stages a copy of the checkout under a path the guard accepts
# (`/srv/library-ci`) specifically so this never fires there.
if _PATH_REFUSAL is not None and os.environ.get("SERVE_GUARD_REQUIRED") == "1":
    pytest.fail(
        "SERVE_GUARD_REQUIRED=1 and serve.sh refuses this checkout's own path "
        f"— {_PATH_REFUSAL} — refusing to silently skip the whole guard suite. "
        "Fix the checkout's ownership, mode or ACL instead of skipping it.",
        pytrace=False,
    )

pytestmark = [
    pytest.mark.skipif(
        _PATH_REFUSAL is not None,
        reason=(
            f"serve.sh refuses this checkout's own path — {_PATH_REFUSAL} — so it "
            "refuses before reaching any case here. The guard is working, not "
            "failing. Run the suite from a checkout under your home directory."
        ),
    ),
]


def run_guard(py, script=SERVE, allow_branch=True):
    """Run `serve.sh --check` with PY pointed at `py`.

    `--check` is what keeps this safe: the guard and the preflight run, the exec
    does not, so no case here can start a service no matter what `py` names.

    `allow_branch` defaults to bypassing the branch guard (SERVE_ALLOW_BRANCH=1).
    `script` defaults to this checkout's own serve.sh, and a checkout doing this
    work is a feature-branch worktree by construction — exactly what the branch
    guard exists to catch, and not what any of the interpreter-guard cases below
    are testing. The branch-guard tests further down point `script` at a
    purpose-built git repository instead and pass allow_branch=False, so they
    exercise the real check rather than a worktree that would fail it by
    definition.
    """
    env = dict(os.environ, PY=str(py))
    if allow_branch:
        env["SERVE_ALLOW_BRANCH"] = "1"
    return subprocess.run(
        [str(script), "--check"],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


def assert_refused(result, offending):
    """The guard refused, named the path, and got nowhere near starting."""
    assert result.returncode == REFUSED, (
        f"expected refusal ({REFUSED}), got {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "refusing" in result.stderr
    assert str(offending) in result.stderr, (
        f"refusal did not name {offending}\nstderr: {result.stderr}"
    )
    # A refusal must happen before the interpreter is ever run.
    assert "preflight" not in result.stderr


def assert_allowed(result):
    """The guard let the path through, whatever happened afterwards."""
    assert "refusing" not in result.stderr, (
        f"expected the guard to allow this path\nstderr: {result.stderr}"
    )


def drop_acls(root: Path):
    """Remove ACLs under `root` so pytest can delete the tree.

    A `deny delete` entry blocks removal by the owner too, so a test that sets one
    leaves an undeletable directory behind without this.
    """
    subprocess.run(["chmod", "-R", "-N", str(root)], capture_output=True)


@pytest.fixture
def tmp_path():
    """Overrides pytest's own `tmp_path` for this module.

    Pytest's built-in `tmp_path` defaults to a directory under `/tmp` on
    Linux — mode 1777, refused by the same sticky-world-writable check that
    already refuses `/private/tmp` on macOS (the comment on that check in
    `refuse_component` says as much). macOS never surfaces this because its
    default TMPDIR sits under `/private/var/folders/...` instead, which is
    not sticky.

    ROOTED UNDER THE HOME DIRECTORY BY DEFAULT, NOT NEXT TO `serve.sh`. A
    first version of this put the base inside this checkout, which passed
    every case except `test_unreadable_git_directory_is_refused`: that test
    chmods a fixture repo's own `.git` to 0o000, and git's repository
    discovery responds to an unreadable `.git` by walking up to the next one
    it can read — which, nested inside this checkout, was THIS repo's own
    `.git`, so the guard reported this worktree's real branch instead of the
    refusal the test expects. The home directory is not itself inside any
    git working tree, so there is no ancestor `.git` for that discovery to
    fall back to. It is also a directory this repo's own ACL comment above
    already treats as guard-safe on Darwin, and, unlike a nested checkout
    path, it does not raise the question of whether test-fixture churn
    belongs in a directory `serve.sh` itself walks.

    `SERVE_GUARD_TEST_ROOT` OVERRIDES THE BASE (Round 2 finding). `ubuntu-latest`
    puts a `default:user:runner:rwx` ACL on `/home` itself, not merely on this
    checkout's path — the same ACL the guard's own check refuses — so every
    case whose fixture tree walked up through `$HOME/.cache/...` refused at
    `/home` before reaching what the case meant to assert. CI sets this to a
    directory it stages alongside the checkout (`/srv/library-ci`, guard-clean
    the same way `serve.sh`'s own path is) instead of `$HOME`; everywhere else,
    the default above still applies.
    """
    override = os.environ.get("SERVE_GUARD_TEST_ROOT")
    base = Path(override) if override else Path.home() / ".cache" / "library-serve-guard-tests"
    base.mkdir(parents=True, exist_ok=True)
    base.chmod(0o755)
    path = Path(tempfile.mkdtemp(dir=base))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def tree(tmp_path):
    """A scratch directory that itself passes the guard."""
    root = tmp_path / "tree"
    root.mkdir()
    root.chmod(0o755)
    yield root
    drop_acls(tmp_path)


def test_world_writable_holding_directory_is_refused(tree):
    """Round 4: the directory holding the symlink decides what runs.

    The target is a perfectly good interpreter and the symlink is fine. What is
    wrong is the directory they sit in, which anybody can rewrite.
    """
    holder = tree / "bin"
    holder.mkdir()
    (holder / "python").symlink_to(HARMLESS)
    holder.chmod(0o777)

    assert_refused(run_guard(holder / "python"), holder)


def test_world_writable_directory_two_hops_out_is_refused(tree):
    """The general form of the same bug, away from the executable.

    Checking the holding directory alone would pass this: the hazard is two
    symlink hops upstream, which is why the guard walks the whole chain.
    """
    near = tree / "near"
    far = tree / "far"
    near.mkdir()
    far.mkdir()
    (far / "step").symlink_to(HARMLESS)
    (near / "python").symlink_to(far / "step")
    near.chmod(0o755)
    far.chmod(0o777)

    assert_refused(run_guard(near / "python"), far)


def test_world_writable_target_directory_is_refused(tree):
    """Round 3: a safe-looking symlink pointing into somewhere anybody can write."""
    safe = tree / "safe"
    evil = tree / "evil"
    safe.mkdir()
    evil.mkdir()
    (evil / "python").symlink_to(HARMLESS)
    (safe / "python").symlink_to(evil / "python")
    safe.chmod(0o755)
    evil.chmod(0o777)

    assert_refused(run_guard(safe / "python"), evil)


def find_third_party_dir():
    """A world-searchable directory owned by neither this user nor root.

    Creating one would need root, which the test runner does not have, so this
    first takes one an operator or CI already built with sudo
    (`SERVE_GUARD_THIRD_PARTY_DIR`, a directory chowned to a distinct account —
    an unprivileged process cannot chown to an account it isn't). Lacking that,
    it borrows one macOS already ships — the daemon state directories under
    /var/db are owned by their own service accounts. Linux has no such stock
    directory to borrow, so without the env var it reports none there. Returns
    None when nothing is available, so the caller decides whether that is a
    skip or a failure.
    """
    me = os.getuid()

    prepared = os.environ.get("SERVE_GUARD_THIRD_PARTY_DIR")
    if prepared:
        path = Path(prepared)
        try:
            st = path.stat()
        except OSError:
            return None
        if not stat.S_ISDIR(st.st_mode) or st.st_uid in (0, me):
            return None
        if not st.st_mode & stat.S_IXOTH:
            return None
        return Path(os.path.realpath(path))

    if sys.platform != "darwin":
        return None

    for base in ("/var/db", "/Library"):
        try:
            entries = list(os.scandir(base))
        except OSError:
            continue
        for entry in entries:
            try:
                st = os.stat(entry.path)
            except OSError:
                continue
            if not stat.S_ISDIR(st.st_mode):
                continue
            if st.st_uid in (0, me):
                continue
            if not st.st_mode & stat.S_IXOTH:
                continue        # the walk must be able to reach it
            return Path(os.path.realpath(entry.path))
    return None


def test_component_owned_by_a_third_user_is_refused():
    """The owner check, which had no coverage at all until both round-7 reviewers
    planted a no-op in it and watched all twelve tests stay green.

    It is the first thing `refuse_component` runs and it gates every later check
    in the same call, so a refactor quietly disabling it would ship green — the
    exact failure mode five previous versions of this guard shipped with.

    Reaching it without root takes a path that TRAVERSES a directory somebody
    else owns on its way to an interpreter this user can execute. The guard
    checks each component as it walks, so it refuses at that directory long
    before the `..` segments would have climbed back out of it.
    """
    owned_by_other = find_third_party_dir()
    if owned_by_other is None:
        _fail_or_skip(
            "no world-searchable directory owned by a third user here — set "
            "SERVE_GUARD_THIRD_PARTY_DIR to one sudo prepared"
        )

    climb = "/".join([".."] * (len(owned_by_other.parts) - 1))
    py = Path(f"{owned_by_other}/{climb}{HARMLESS}")
    if not os.access(py, os.X_OK):
        _fail_or_skip(f"{py} is not executable on this machine")

    result = run_guard(py)
    assert_refused(result, owned_by_other)
    assert "owned by uid" in result.stderr, (
        f"refused for the wrong reason\nstderr: {result.stderr}"
    )


def test_world_writable_without_group_write_is_refused(tree):
    """Mode 707: the world bit is the only thing wrong with this directory.

    The other world-writable cases here use 777, where the group bit is set too —
    so the group check fires first and they pass even with the world check
    disabled. Found by planting exactly that defect and watching every test stay
    green. This case is the one that fails when the world check is blinded.
    """
    holder = tree / "otheronly"
    holder.mkdir()
    (holder / "python").symlink_to(HARMLESS)
    holder.chmod(0o707)
    assert oct(holder.stat().st_mode)[-3:] == "707"

    assert_refused(run_guard(holder / "python"), holder)


def _group_writable_holder(tree, env_var, fallback_group):
    """A directory chgrp'd to a group this process may not belong to.

    An unprivileged CI runner cannot chgrp to a group it isn't a member of, so
    this prefers a directory the workflow already chgrp'd with sudo (`env_var`)
    and falls back to chgrp'ing one itself — which only works on a developer
    machine already carrying `fallback_group` membership.
    """
    prepared = os.environ.get(env_var)
    if prepared:
        holder = Path(prepared)
        if not holder.is_dir():
            _fail_or_skip(f"{env_var}={prepared} is not a directory")
        link = holder / "python"
        if link.is_symlink() or link.exists():
            link.unlink()
        return holder

    holder = tree / "grp"
    holder.mkdir()
    chgrp = subprocess.run(["chgrp", fallback_group, str(holder)], capture_output=True)
    if chgrp.returncode != 0:
        _fail_or_skip(
            f"this user cannot chgrp to {fallback_group} and {env_var} is unset"
        )
    return holder


def test_group_writable_by_non_admin_group_is_refused(tree):
    """Group write is only tolerated for groups that already administer the box."""
    holder = _group_writable_holder(tree, "SERVE_GUARD_NONADMIN_GROUP_DIR", "staff")
    (holder / "python").symlink_to(HARMLESS)
    holder.chmod(0o775)

    assert_refused(run_guard(holder / "python"), holder)


@DARWIN_ONLY
def test_group_writable_by_admin_is_allowed(tree):
    """The carve-out Homebrew's prefix depends on.

    /opt/homebrew/bin and /opt/homebrew/Cellar ship mode 775 group admin and the
    interpreter is reached through them, so refusing this would refuse to start on
    a stock install. Anybody who can write there administers the machine already.
    """
    holder = tree / "adm"
    holder.mkdir()
    (holder / "python").symlink_to(HARMLESS)
    chgrp = subprocess.run(["chgrp", "admin", str(holder)], capture_output=True)
    if chgrp.returncode != 0:
        pytest.skip("this user cannot chgrp to admin")
    holder.chmod(0o775)

    assert_allowed(run_guard(holder / "python"))


@DARWIN_ONLY
def test_acl_allowing_write_is_refused_though_the_mode_looks_safe(tree):
    """Round 5: macOS grants access outside the mode bits, and stat cannot see it.

    The directory reads 755 to any check built on permission bits alone while an
    ACL entry lets every local account add and delete files in it.
    """
    holder = tree / "acl"
    holder.mkdir()
    (holder / "python").symlink_to(HARMLESS)
    holder.chmod(0o755)
    subprocess.run(
        ["chmod", "+a", "everyone allow write,delete,add_file,delete_child", str(holder)],
        check=True,
    )
    assert oct(holder.stat().st_mode)[-3:] == "755", "the mode must still look safe"

    assert_refused(run_guard(holder / "python"), holder)


@DARWIN_ONLY
def test_deny_only_acl_is_allowed(tree):
    """A deny entry can only take access away, so it is not a reason to refuse.

    This is the shape macOS ships on every home directory (`group:everyone deny
    delete`), so refusing all ACLs outright would refuse to start on a stock Mac.
    """
    holder = tree / "denyonly"
    holder.mkdir()
    (holder / "python").symlink_to(HARMLESS)
    holder.chmod(0o755)
    subprocess.run(["chmod", "+a", "everyone deny delete", str(holder)], check=True)

    assert_allowed(run_guard(holder / "python"))


@LINUX_ONLY
def test_acl_named_user_entry_on_linux_is_refused(tree):
    """The Linux equivalent of round 5: a POSIX ACL grants access the mode
    bits do not show, and `getfacl` is what reads it.
    """
    holder = tree / "aclnamed"
    holder.mkdir()
    (holder / "python").symlink_to(HARMLESS)
    holder.chmod(0o755)
    setfacl = subprocess.run(["setfacl", "-m", "u:nobody:rwx", str(holder)], capture_output=True)
    if setfacl.returncode != 0:
        pytest.skip("setfacl unavailable or failed on this machine")

    assert_refused(run_guard(holder / "python"), holder)


@LINUX_ONLY
def test_acl_default_named_entry_on_linux_is_refused(tree):
    """A `default:` entry governs what a new file created here inherits, and
    naming a user or group in one is refused on the same terms as an
    ordinary named entry."""
    holder = tree / "acldefault"
    holder.mkdir()
    (holder / "python").symlink_to(HARMLESS)
    holder.chmod(0o755)
    setfacl = subprocess.run(["setfacl", "-d", "-m", "u:nobody:rwx", str(holder)], capture_output=True)
    if setfacl.returncode != 0:
        pytest.skip("setfacl unavailable or failed on this machine")

    assert_refused(run_guard(holder / "python"), holder)


@LINUX_ONLY
def test_acl_mask_only_is_allowed_on_linux(tree):
    """A bare `mask::` line caps what named entries are allowed; with no
    named entry present it grants nobody anything and must not refuse."""
    holder = tree / "aclmaskonly"
    holder.mkdir()
    (holder / "python").symlink_to(HARMLESS)
    holder.chmod(0o755)
    setfacl = subprocess.run(["setfacl", "-m", "u:nobody:rwx", str(holder)], capture_output=True)
    if setfacl.returncode != 0:
        pytest.skip("setfacl unavailable or failed on this machine")
    subprocess.run(["setfacl", "-x", "u:nobody", str(holder)], check=True)

    assert_allowed(run_guard(holder / "python"))


def _run_with_prepended_path(py, prepend_dir):
    """Run the guard with `prepend_dir` searched before the rest of PATH.

    Used to put a stub tool ahead of the real one without losing everything
    else the guard's own walk needs to run at all.
    """
    env = dict(os.environ, PY=str(py), SERVE_ALLOW_BRANCH="1")
    env["PATH"] = f"{prepend_dir}{os.pathsep}{env.get('PATH', '')}"
    return subprocess.run(
        [str(SERVE), "--check"], env=env, capture_output=True, text=True, timeout=180,
    )


@LINUX_ONLY
def test_stub_getfacl_printing_nothing_is_refused(tmp_path):
    """A `getfacl` that stops erroring without really reading ACLs must not
    silently pass every path it is asked about — refuses via the walk of the
    script's own path, which is checked before any test fixture is reached.
    """
    stub_dir = tmp_path / "stub_empty"
    stub_dir.mkdir()
    stub = stub_dir / "getfacl"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)

    result = _run_with_prepended_path(HARMLESS, stub_dir)
    assert result.returncode == REFUSED, result.stderr
    assert "did not report the expected" in result.stderr, result.stderr


@LINUX_ONLY
def test_stub_getfacl_exiting_nonzero_is_refused(tmp_path):
    """A `getfacl` that simply fails is exactly as unreadable as one that was
    never installed."""
    stub_dir = tmp_path / "stub_fail"
    stub_dir.mkdir()
    stub = stub_dir / "getfacl"
    stub.write_text("#!/bin/sh\nexit 1\n")
    stub.chmod(0o755)

    result = _run_with_prepended_path(HARMLESS, stub_dir)
    assert result.returncode == REFUSED, result.stderr
    assert "cannot read the ACL" in result.stderr, result.stderr


@LINUX_ONLY
def test_getfacl_missing_from_path_is_refused(tmp_path):
    """With no `getfacl` on PATH at all there is no way to read a POSIX ACL,
    and the guard must refuse rather than silently skip a check it cannot
    run — built from a PATH holding only the handful of tools the walk of
    the script's own path needs before it ever reaches the ACL check.
    """
    minimal = tmp_path / "minimal_path"
    minimal.mkdir()
    for name in ("id", "stat", "readlink", "dirname", "uname"):
        real = shutil.which(name)
        if real is None:
            pytest.skip(f"{name} not found on this machine to build a minimal PATH")
        (minimal / name).symlink_to(real)

    env = dict(os.environ, PY=HARMLESS, SERVE_ALLOW_BRANCH="1", PATH=str(minimal))
    result = subprocess.run(
        [str(SERVE), "--check"], env=env, capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == REFUSED, result.stderr
    assert "getfacl" in result.stderr, result.stderr


@LINUX_ONLY
def test_group_writable_by_gid_80_is_refused_on_linux(tree):
    """gid 80 is macOS's `admin` group; on Linux it can be any group at all,
    and nobody here knows who its members are. ADMIN_GIDS is " 0 " on Linux
    for exactly this reason — trusting gid 80 there would widen the guard
    rather than merely dodge a stock-install false refusal.
    """
    holder = _group_writable_holder(tree, "SERVE_GUARD_GID80_DIR", "80")
    (holder / "python").symlink_to(HARMLESS)
    holder.chmod(0o775)

    assert_refused(run_guard(holder / "python"), holder)


@LINUX_ONLY
def test_symlink_chain_passes_when_directory_and_target_are_safe_on_linux(tree):
    """Every symlink lstats as mode 777 on Linux, which is not a real signal
    of anything — the kernel ignores a symlink's own permission bits
    entirely. A safe symlinked directory, pointing at a safe target, must
    still pass, and this is the case a naive port would have broken by
    refusing on the link's own manufactured mode.
    """
    real_dir = tree / "real"
    real_dir.mkdir()
    real_dir.chmod(0o755)
    (real_dir / "python").symlink_to(HARMLESS)

    link_dir = tree / "link"
    link_dir.symlink_to(real_dir)

    result = run_guard(link_dir / "python")
    assert_allowed(result)
    assert result.returncode == 0, result.stderr


@LINUX_ONLY
def test_symlink_in_world_writable_directory_is_refused_on_linux(tree):
    """Skipping a symlink's own mode and ACL checks on Linux must not also
    skip the directory that holds it: whoever can write that directory can
    still replace the link, whatever the link itself lstats as. The refusal
    must name the directory, not the symlink lstat'd at mode 777.
    """
    holder = tree / "symlinkholder"
    holder.mkdir()
    (holder / "python").symlink_to(HARMLESS)
    holder.chmod(0o777)

    result = run_guard(holder / "python")
    assert result.returncode == REFUSED, result.stderr
    assert f"{holder} is world-writable" in result.stderr, result.stderr


def test_script_reached_through_a_world_writable_directory_is_refused(tree):
    """The script is code too, so its own path is walked on the same terms.

    A refusal here must survive the call site: the guard exits from inside a
    command substitution, where an `exit` leaves only the subshell. Nested one
    substitution deeper, the wrapper's own success is what survived and the script
    carried on with its directory set to the working directory.
    """
    launcher = tree / "launch"
    launcher.mkdir()
    (launcher / "serve.sh").symlink_to(SERVE)
    launcher.chmod(0o777)

    result = run_guard(HARMLESS, script=launcher / "serve.sh")
    assert_refused(result, launcher)


def test_a_clean_path_is_allowed(tree):
    """The positive control, which is the half four rewrites kept breaking.

    Every case above asserts a refusal, and a guard that refuses everything would
    pass all of them while taking the service down.
    """
    holder = tree / "clean"
    holder.mkdir()
    (holder / "python").symlink_to(HARMLESS)
    holder.chmod(0o755)

    result = run_guard(holder / "python")
    assert_allowed(result)
    assert result.returncode == 0


def test_the_real_interpreter_passes_the_guard():
    """The deployment chain itself passes, walked component by component.

    This is the assertion four previous rewrites would have failed: each tightened
    the check and broke the venv, the service, or both. It runs with no PY
    override, so it walks the real chain — home directory ACL, Homebrew's
    admin-group prefix, three symlink hops and all — and `--check` is what makes
    that safe to do.
    """
    real = SERVE.parent / ".venv" / "bin" / "python"
    if not real.exists():
        _fail_or_skip(
            f"no .venv in this checkout — CI stages one at {real} for exactly "
            "this positive control"
        )

    # SERVE_ALLOW_BRANCH: this repository's own checkout is a feature-branch
    # worktree while this test runs, same reasoning as run_guard() above — this
    # test is about the interpreter chain, not the branch guard.
    env = dict(os.environ, SERVE_ALLOW_BRANCH="1")
    result = subprocess.run(
        [str(SERVE), "--check"],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "refusing" not in result.stderr, result.stderr
    assert "whole path checked" in result.stdout, result.stdout
    assert "--check passed" in result.stdout, result.stdout

    # The app announces itself on startup, naming the protocol it serves and the
    # credentials it loaded, and it does so while importing — before any port is
    # bound. Their absence is what proves --check stopped short of importing it,
    # so assert it here rather than trusting the exit code.
    combined = result.stdout + result.stderr
    for started in ("serving protocol", "credential(s) loaded"):
        assert started not in combined, (
            f"--check reached the app: found {started!r}\n{combined}"
        )


def test_a_path_longer_than_the_step_budget_is_refused(tree):
    """The step budget, the one control between a symlink cycle and a boot loop.

    A real symlink cycle cannot reach it — the kernel's own ELOOP makes the `-x`
    test fail first, so the script never sees it. A long chain of ordinary nested
    directories does reach it, which is what makes the budget testable at all:
    widening the bound to a million left all thirteen other tests green.
    """
    deep = tree / "deep"
    deep.mkdir()
    walk = deep
    for i in range(70):
        walk = walk / f"d{i}"
    walk.mkdir(parents=True)
    (walk / "python").symlink_to(HARMLESS)

    result = run_guard(walk / "python")
    assert result.returncode == REFUSED, result.stderr
    assert "more than 64 components and symlink hops" in result.stderr, result.stderr


def run_git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def git_repo(tmp_path):
    """A scratch git repository holding its own copy of serve.sh, on `main`.

    DIR is derived from the RESOLVED path of the script itself, not from PY, so
    exercising the branch guard needs a real copy of serve.sh living inside a
    controlled repository — a symlink would resolve straight back through to
    this worktree's own checkout, which is not on `main` and is not what these
    tests are about.

    `refs/remotes/origin/main` is written directly with `update-ref` rather than
    by adding a real remote and fetching: the guard never fetches, it only
    resolves that ref locally, so nothing here needs network access or an
    actual origin to exist.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    repo.chmod(0o755)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True)
    run_git(repo, "config", "user.email", "test@example.com")
    run_git(repo, "config", "user.name", "Test")

    script = repo / "serve.sh"
    script.write_text(SERVE.read_text())
    script.chmod(0o755)
    run_git(repo, "add", "serve.sh")
    run_git(repo, "commit", "-q", "-m", "init")

    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    run_git(repo, "update-ref", "refs/remotes/origin/main", head)

    yield repo
    drop_acls(tmp_path)


def test_main_matching_origin_main_is_allowed(tree, git_repo):
    """The positive case: on `main`, HEAD equal to the local `origin/main` ref."""
    holder = tree / "onmain"
    holder.mkdir()
    (holder / "python").symlink_to(HARMLESS)
    holder.chmod(0o755)

    result = run_guard(holder / "python", script=git_repo / "serve.sh", allow_branch=False)
    assert_allowed(result)
    assert result.returncode == 0


def test_feature_branch_is_refused(tree, git_repo):
    """A feature branch is refused by name, not just refused."""
    run_git(git_repo, "checkout", "-q", "-b", "feature/x")

    holder = tree / "onfeature"
    holder.mkdir()
    (holder / "python").symlink_to(HARMLESS)
    holder.chmod(0o755)

    result = run_guard(holder / "python", script=git_repo / "serve.sh", allow_branch=False)
    assert result.returncode == REFUSED, result.stderr
    assert "feature/x" in result.stderr, result.stderr


def test_main_not_matching_origin_main_is_refused(tree, git_repo):
    """On `main`, but HEAD has an unpushed commit `origin/main` lacks.

    The branch name alone is not the whole check — a `main` whose HEAD is
    not an ancestor of origin's is exactly the state the guard exists to
    catch, whether that came from an unpushed local commit or a rebase.
    """
    (git_repo / "extra.txt").write_text("x")
    run_git(git_repo, "add", "extra.txt")
    run_git(git_repo, "commit", "-q", "-m", "extra")

    holder = tree / "divergedmain"
    holder.mkdir()
    (holder / "python").symlink_to(HARMLESS)
    holder.chmod(0o755)

    result = run_guard(holder / "python", script=git_repo / "serve.sh", allow_branch=False)
    assert result.returncode == REFUSED, result.stderr
    assert "not an ancestor of origin/main" in result.stderr, result.stderr


def test_main_behind_origin_main_is_allowed(tree, git_repo):
    """Fetched but not merged: `origin/main` has moved ahead of HEAD.

    No fetch happens in the guard, but a fetch can happen out of band —
    anything else with a shell open in this directory. HEAD is still an
    ancestor of the new `origin/main`, so the code on disk was reviewed; it
    is merely old. Refusing to boot here would crash-loop the service on
    every restart until somebody merges, which is a worse failure than the
    one the branch guard exists to prevent.
    """
    holder = tree / "behindmain"
    holder.mkdir()
    (holder / "python").symlink_to(HARMLESS)
    holder.chmod(0o755)

    run_git(git_repo, "checkout", "-q", "-b", "tmp-advance")
    (git_repo / "extra.txt").write_text("x")
    run_git(git_repo, "add", "extra.txt")
    run_git(git_repo, "commit", "-q", "-m", "advance")
    new_head = subprocess.run(
        ["git", "-C", str(git_repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    run_git(git_repo, "checkout", "-q", "main")
    run_git(git_repo, "update-ref", "refs/remotes/origin/main", new_head)

    result = run_guard(holder / "python", script=git_repo / "serve.sh", allow_branch=False)
    assert_allowed(result)
    assert result.returncode == 0, result.stderr


def test_branch_override_bypasses_and_announces(tree, git_repo):
    """SERVE_ALLOW_BRANCH starts anyway and says which branch it started from."""
    run_git(git_repo, "checkout", "-q", "-b", "feature/y")

    holder = tree / "override"
    holder.mkdir()
    (holder / "python").symlink_to(HARMLESS)
    holder.chmod(0o755)

    result = run_guard(holder / "python", script=git_repo / "serve.sh", allow_branch=True)
    assert result.returncode == 0, result.stderr
    assert "SERVE_ALLOW_BRANCH" in result.stderr, result.stderr
    assert "feature/y" in result.stderr, result.stderr


def test_dirty_tracked_file_is_refused(tree, git_repo):
    """The working tree is what gets served, not the commit HEAD names.

    A tracked file edited on disk and never committed is the most unreviewed
    code there is — this is the scenario finding 1 exists to close.
    """
    (git_repo / "serve.sh").write_text(
        (git_repo / "serve.sh").read_text() + "\n# a stray uncommitted edit\n"
    )

    holder = tree / "dirtytree"
    holder.mkdir()
    (holder / "python").symlink_to(HARMLESS)
    holder.chmod(0o755)

    result = run_guard(holder / "python", script=git_repo / "serve.sh", allow_branch=False)
    assert result.returncode == REFUSED, result.stderr
    assert "serve.sh modified on disk" in result.stderr, result.stderr
    assert "working tree does not match HEAD" in result.stderr, result.stderr


def test_staged_but_uncommitted_change_is_refused(tree, git_repo):
    """`git add` without `git commit` is still uncommitted, still refused.

    The check compares disk content directly to HEAD's blobs, bypassing the
    index entirely, so staging a change does not launder it past the guard.
    """
    (git_repo / "serve.sh").write_text(
        (git_repo / "serve.sh").read_text() + "\n# staged but not committed\n"
    )
    run_git(git_repo, "add", "serve.sh")

    holder = tree / "stagedtree"
    holder.mkdir()
    (holder / "python").symlink_to(HARMLESS)
    holder.chmod(0o755)

    result = run_guard(holder / "python", script=git_repo / "serve.sh", allow_branch=False)
    assert result.returncode == REFUSED, result.stderr
    assert "serve.sh modified on disk" in result.stderr, result.stderr


def test_deleted_tracked_file_is_refused(tree, git_repo):
    """A tracked file removed from disk without being committed as removed."""
    (git_repo / "extra.txt").write_text("x")
    run_git(git_repo, "add", "extra.txt")
    run_git(git_repo, "commit", "-q", "-m", "extra")
    # Keep origin/main at HEAD so the ancestor check passes and this test
    # exercises the working-tree check specifically, not the branch check.
    new_head = subprocess.run(
        ["git", "-C", str(git_repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    run_git(git_repo, "update-ref", "refs/remotes/origin/main", new_head)
    (git_repo / "extra.txt").unlink()

    holder = tree / "deletedtree"
    holder.mkdir()
    (holder / "python").symlink_to(HARMLESS)
    holder.chmod(0o755)

    result = run_guard(holder / "python", script=git_repo / "serve.sh", allow_branch=False)
    assert result.returncode == REFUSED, result.stderr
    assert "extra.txt removed from disk" in result.stderr, result.stderr


def test_tracked_path_with_embedded_newline_is_refused(tree, git_repo):
    """A tracked path holding a raw newline byte defeats NUL-to-newline
    reframing (`reframe_nul_records` in serve.sh): the byte the reframing
    would split records on is already inside a field, so this refuses rather
    than guess a record boundary — even though the working tree otherwise
    matches HEAD exactly and would pass `check_worktree_clean` cleanly.

    Confirmed reachable: git tracks a path with an embedded newline without
    complaint, and `git ls-tree -z` reports it with the byte intact.
    """
    (git_repo / "evil\npart.py").write_text("x = 1\n")
    run_git(git_repo, "add", "--", "evil\npart.py")
    run_git(git_repo, "commit", "-q", "-m", "add a path with a newline in it")
    new_head = subprocess.run(
        ["git", "-C", str(git_repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    run_git(git_repo, "update-ref", "refs/remotes/origin/main", new_head)

    holder = tree / "newlinepath"
    holder.mkdir()
    (holder / "python").symlink_to(HARMLESS)
    holder.chmod(0o755)

    result = run_guard(holder / "python", script=git_repo / "serve.sh", allow_branch=False)
    assert result.returncode == REFUSED, result.stderr
    assert "holds a path with a newline byte in it" in result.stderr, result.stderr


def test_unreadable_git_directory_is_refused(tree, git_repo):
    """`.git` present but unreadable must refuse, not fail open.

    Confirmed empirically that `chmod 000 .git` and no `.git` at all produce
    the identical exit code from `git rev-parse --git-dir`, which is why the
    guard now checks for the entry's existence before ever asking git
    anything.
    """
    holder = tree / "brokengit"
    holder.mkdir()
    (holder / "python").symlink_to(HARMLESS)
    holder.chmod(0o755)

    (git_repo / ".git").chmod(0o000)
    try:
        result = run_guard(holder / "python", script=git_repo / "serve.sh", allow_branch=False)
    finally:
        (git_repo / ".git").chmod(0o755)

    assert result.returncode == REFUSED, result.stderr
    assert "a .git entry that git cannot read" in result.stderr, result.stderr


def test_no_git_repository_is_allowed(tree, tmp_path):
    """No `.git` at all is a deployment shape this guard has nothing to check
    against, so it passes rather than bricking a tarball release."""
    plain = tmp_path / "plain"
    plain.mkdir()
    plain.chmod(0o755)
    script = plain / "serve.sh"
    script.write_text(SERVE.read_text())
    script.chmod(0o755)

    holder = tree / "nogit"
    holder.mkdir()
    (holder / "python").symlink_to(HARMLESS)
    holder.chmod(0o755)

    result = run_guard(holder / "python", script=script, allow_branch=False)
    assert_allowed(result)
    assert result.returncode == 0


def test_untracked_top_level_py_file_is_refused(tree, git_repo):
    """An untracked file at $DIR's top level shadows a real import.

    `$DIR` is on `sys.path[0]` ahead of the venv's `site-packages`, so an
    untracked `sqlite_vec.py` here is what `import sqlite_vec` resolves to —
    proved by planting exactly this and watching the app pick it up.
    """
    (git_repo / "sqlite_vec.py").write_text("raise ImportError('shadowed')\n")

    holder = tree / "shadowtop"
    holder.mkdir()
    (holder / "python").symlink_to(HARMLESS)
    holder.chmod(0o755)

    result = run_guard(holder / "python", script=git_repo / "serve.sh", allow_branch=False)
    assert result.returncode == REFUSED, result.stderr
    assert "sqlite_vec.py" in result.stderr, result.stderr
    assert "shadow an import" in result.stderr, result.stderr


def test_untracked_init_in_new_top_level_dir_is_refused(tree, git_repo):
    """An untracked __init__.py makes a brand-new top-level dir importable."""
    pkg = git_repo / "shadow_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("raise ImportError('shadowed')\n")

    holder = tree / "shadownewdir"
    holder.mkdir()
    (holder / "python").symlink_to(HARMLESS)
    holder.chmod(0o755)

    result = run_guard(holder / "python", script=git_repo / "serve.sh", allow_branch=False)
    assert result.returncode == REFUSED, result.stderr
    assert "shadow_pkg/__init__.py" in result.stderr, result.stderr


def test_untracked_init_in_existing_tracked_dir_is_refused(tree, git_repo):
    """The directory doesn't have to be new — an existing tracked directory
    gaining an untracked __init__.py becomes importable just the same."""
    existing = git_repo / "existing_pkg"
    existing.mkdir()
    (existing / "mod.py").write_text("x = 1\n")
    run_git(git_repo, "add", "existing_pkg/mod.py")
    run_git(git_repo, "commit", "-q", "-m", "existing pkg")
    new_head = subprocess.run(
        ["git", "-C", str(git_repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    run_git(git_repo, "update-ref", "refs/remotes/origin/main", new_head)

    (existing / "__init__.py").write_text("raise ImportError('shadowed')\n")

    holder = tree / "shadowexistingdir"
    holder.mkdir()
    (holder / "python").symlink_to(HARMLESS)
    holder.chmod(0o755)

    result = run_guard(holder / "python", script=git_repo / "serve.sh", allow_branch=False)
    assert result.returncode == REFUSED, result.stderr
    assert "existing_pkg/__init__.py" in result.stderr, result.stderr


def test_untracked_nested_non_init_file_is_allowed(tree, git_repo):
    """A file two levels down cannot be reached as a top-level import by
    anything on sys.path — it needs both `some_dir` AND `some_dir/sub` to
    already be importable packages, which this check does not verify past
    the first level, so it is not the hazard this check exists for."""
    sub = git_repo / "some_dir" / "sub"
    sub.mkdir(parents=True)
    (sub / "not_importable_directly.py").write_text("x = 1\n")

    holder = tree / "nestednonhazard"
    holder.mkdir()
    (holder / "python").symlink_to(HARMLESS)
    holder.chmod(0o755)

    result = run_guard(holder / "python", script=git_repo / "serve.sh", allow_branch=False)
    assert_allowed(result)
    assert result.returncode == 0, result.stderr


def test_untracked_py_one_level_inside_existing_tracked_dir_is_refused(tree, git_repo):
    """A non-`__init__.py` sibling planted inside a tracked package directory.

    `sqlite_vec/__init__.py` tracked and `sqlite_vec/real.py` tracked make
    `sqlite_vec` a real importable package; an untracked `sqlite_vec/evil.py`
    sitting beside them is importable as `sqlite_vec.evil` the moment tracked
    code does `import sqlite_vec`, which neither the top-level pathspec nor
    the `__init__.py`-only pathspec used to catch.
    """
    pkg = git_repo / "sqlite_vec"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("x = 1\n")
    (pkg / "real.py").write_text("x = 1\n")
    run_git(git_repo, "add", "sqlite_vec/__init__.py", "sqlite_vec/real.py")
    run_git(git_repo, "commit", "-q", "-m", "existing package")
    new_head = subprocess.run(
        ["git", "-C", str(git_repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    run_git(git_repo, "update-ref", "refs/remotes/origin/main", new_head)

    (pkg / "evil.py").write_text("raise ImportError('shadowed')\n")

    holder = tree / "shadowinpkg"
    holder.mkdir()
    (holder / "python").symlink_to(HARMLESS)
    holder.chmod(0o755)

    result = run_guard(holder / "python", script=git_repo / "serve.sh", allow_branch=False)
    assert result.returncode == REFUSED, result.stderr
    assert "sqlite_vec/evil.py" in result.stderr, result.stderr


def test_untracked_py_one_level_deep_is_refused_even_in_untracked_dir(tree, git_repo):
    """The pathspec is shape-based, not tracked-status-based: a one-level-deep
    `.py` file is refused whether or not its containing directory is tracked,
    since the guard has no cheap way to ask git "is this directory tracked"
    and a directory with no tracked entries today can gain one tomorrow
    without this check ever running again in between."""
    sub = git_repo / "some_dir"
    sub.mkdir()
    (sub / "not_a_package_yet.py").write_text("x = 1\n")

    holder = tree / "shadowonelevel"
    holder.mkdir()
    (holder / "python").symlink_to(HARMLESS)
    holder.chmod(0o755)

    result = run_guard(holder / "python", script=git_repo / "serve.sh", allow_branch=False)
    assert result.returncode == REFUSED, result.stderr
    assert "some_dir/not_a_package_yet.py" in result.stderr, result.stderr


def test_untracked_ordinary_byproducts_are_allowed(tree, git_repo):
    """.venv, __pycache__ and a log file are routine, not a shadowing hazard.

    Also proves the check does not trust .gitignore to tell them apart: none
    of these are excluded via `.gitignore`, they are excluded because none of
    them are a top-level `.py` file or a one-level-deep `__init__.py`.
    """
    venv_bin = git_repo / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "activate").write_text("#!/bin/sh\n")
    pycache = git_repo / "__pycache__"
    pycache.mkdir()
    (pycache / "app.cpython-314.pyc").write_bytes(b"\x00")
    (git_repo / "serve.log").write_text("boot ok\n")

    holder = tree / "ordinarybyproducts"
    holder.mkdir()
    (holder / "python").symlink_to(HARMLESS)
    holder.chmod(0o755)

    result = run_guard(holder / "python", script=git_repo / "serve.sh", allow_branch=False)
    assert_allowed(result)
    assert result.returncode == 0, result.stderr


def test_gitignored_shadow_file_is_still_refused(tree, git_repo):
    """--exclude-standard is deliberately not used: an untracked .gitignore
    naming the shadow file must not hide it, since an attacker who can write
    an untracked shadow file can just as easily write an untracked
    .gitignore pattern that hides it from a check built on --exclude-standard."""
    (git_repo / ".gitignore").write_text("sqlite_vec.py\n")
    (git_repo / "sqlite_vec.py").write_text("raise ImportError('shadowed')\n")

    holder = tree / "ignoredshadow"
    holder.mkdir()
    (holder / "python").symlink_to(HARMLESS)
    holder.chmod(0o755)

    result = run_guard(holder / "python", script=git_repo / "serve.sh", allow_branch=False)
    assert result.returncode == REFUSED, result.stderr
    assert "sqlite_vec.py" in result.stderr, result.stderr


def test_untracked_path_with_embedded_newline_is_refused(tree, git_repo):
    """An untracked top-level `.py` path holding a raw newline byte defeats
    NUL-to-newline reframing the same way the tracked case above does — see
    `reframe_nul_records` in serve.sh. Refused outright rather than silently
    misread, whether or not the name it splits into on either side of the
    newline would themselves have been a hazard.
    """
    (git_repo / "shadow\nfile.py").write_text("raise ImportError('shadowed')\n")

    holder = tree / "newlineshadow"
    holder.mkdir()
    (holder / "python").symlink_to(HARMLESS)
    holder.chmod(0o755)

    result = run_guard(holder / "python", script=git_repo / "serve.sh", allow_branch=False)
    assert result.returncode == REFUSED, result.stderr
    assert "holds a path with a newline byte in it" in result.stderr, result.stderr


def test_untouched_tracked_symlink_is_allowed(tree, git_repo):
    """A tracked symlink, exactly as HEAD recorded it, must not read as dirty.

    `hash-object --no-filters` follows a symlink and hashes the TARGET file's
    content, which is not what HEAD stores for a symlink — HEAD stores the
    target string. An untouched symlink compared the wrong way would refuse
    forever.
    """
    (git_repo / "target.txt").write_text("hello\n")
    (git_repo / "link.py").symlink_to("target.txt")
    run_git(git_repo, "add", "target.txt", "link.py")
    run_git(git_repo, "commit", "-q", "-m", "add symlink")
    new_head = subprocess.run(
        ["git", "-C", str(git_repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    run_git(git_repo, "update-ref", "refs/remotes/origin/main", new_head)

    holder = tree / "symlinkclean"
    holder.mkdir()
    (holder / "python").symlink_to(HARMLESS)
    holder.chmod(0o755)

    result = run_guard(holder / "python", script=git_repo / "serve.sh", allow_branch=False)
    assert_allowed(result)
    assert result.returncode == 0, result.stderr


def test_retargeted_tracked_symlink_is_refused(tree, git_repo):
    """The symlink's target string changed on disk without being committed."""
    (git_repo / "target.txt").write_text("hello\n")
    (git_repo / "other.txt").write_text("goodbye\n")
    (git_repo / "link.py").symlink_to("target.txt")
    run_git(git_repo, "add", "target.txt", "other.txt", "link.py")
    run_git(git_repo, "commit", "-q", "-m", "add symlink")
    new_head = subprocess.run(
        ["git", "-C", str(git_repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    run_git(git_repo, "update-ref", "refs/remotes/origin/main", new_head)

    (git_repo / "link.py").unlink()
    (git_repo / "link.py").symlink_to("other.txt")

    holder = tree / "symlinkretargeted"
    holder.mkdir()
    (holder / "python").symlink_to(HARMLESS)
    holder.chmod(0o755)

    result = run_guard(holder / "python", script=git_repo / "serve.sh", allow_branch=False)
    assert result.returncode == REFUSED, result.stderr
    assert "link.py's symlink target modified" in result.stderr, result.stderr


def test_tracked_symlink_replaced_by_regular_file_is_refused(tree, git_repo):
    """A tracked symlink swapped for a real file is unreviewed code too."""
    (git_repo / "target.txt").write_text("hello\n")
    (git_repo / "link.py").symlink_to("target.txt")
    run_git(git_repo, "add", "target.txt", "link.py")
    run_git(git_repo, "commit", "-q", "-m", "add symlink")
    new_head = subprocess.run(
        ["git", "-C", str(git_repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    run_git(git_repo, "update-ref", "refs/remotes/origin/main", new_head)

    (git_repo / "link.py").unlink()
    (git_repo / "link.py").write_text("raise ImportError('shadowed')\n")

    holder = tree / "symlinkreplaced"
    holder.mkdir()
    (holder / "python").symlink_to(HARMLESS)
    holder.chmod(0o755)

    result = run_guard(holder / "python", script=git_repo / "serve.sh", allow_branch=False)
    assert result.returncode == REFUSED, result.stderr
    assert "missing or replaced by a non-symlink" in result.stderr, result.stderr


def test_tracked_executable_losing_its_bit_is_refused(tree, git_repo):
    """A tracked 100755 file whose executable bit is stripped on disk is
    unreviewed content HEAD does not record, even though the file's bytes
    match — the same class of gap the symlink-target checks above close."""
    (git_repo / "run.py").write_text("#!/usr/bin/env python3\n")
    (git_repo / "run.py").chmod(0o755)
    run_git(git_repo, "add", "run.py")
    run_git(git_repo, "commit", "-q", "-m", "add executable")
    new_head = subprocess.run(
        ["git", "-C", str(git_repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    run_git(git_repo, "update-ref", "refs/remotes/origin/main", new_head)

    (git_repo / "run.py").chmod(0o644)

    holder = tree / "bitremoved"
    holder.mkdir()
    (holder / "python").symlink_to(HARMLESS)
    holder.chmod(0o755)

    result = run_guard(holder / "python", script=git_repo / "serve.sh", allow_branch=False)
    assert result.returncode == REFUSED, result.stderr
    assert "run.py's executable bit removed" in result.stderr, result.stderr


def test_tracked_regular_file_gaining_the_executable_bit_is_refused(tree, git_repo):
    """A tracked 100644 file that gains an executable bit on disk without
    that bit being committed is also a mismatch with what HEAD records."""
    (git_repo / "config.py").write_text("x = 1\n")
    run_git(git_repo, "add", "config.py")
    run_git(git_repo, "commit", "-q", "-m", "add plain file")
    new_head = subprocess.run(
        ["git", "-C", str(git_repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    run_git(git_repo, "update-ref", "refs/remotes/origin/main", new_head)

    (git_repo / "config.py").chmod(0o755)

    holder = tree / "bitadded"
    holder.mkdir()
    (holder / "python").symlink_to(HARMLESS)
    holder.chmod(0o755)

    result = run_guard(holder / "python", script=git_repo / "serve.sh", allow_branch=False)
    assert result.returncode == REFUSED, result.stderr
    assert "config.py's executable bit added" in result.stderr, result.stderr


def test_untracked_shadow_check_suppresses_fsmonitor(tree, git_repo):
    """`ls-files --others` can invoke a `core.fsmonitor` hook named in this
    checkout's own `.git/config`, exactly the hazard `check_worktree_clean`
    above names `git status` for — confirmed by pointing `core.fsmonitor` at
    a script that leaves a marker and watching it fire on an unsuppressed
    `ls-files --others` and never on the guard's own, `-c`-overridden call.
    """
    marker = git_repo / "fsmonitor-fired"
    hook = git_repo / "fsmonitor-hook.sh"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    run_git(git_repo, "config", "core.fsmonitor", str(hook))

    holder = tree / "fsmonitorsuppressed"
    holder.mkdir()
    (holder / "python").symlink_to(HARMLESS)
    holder.chmod(0o755)

    result = run_guard(holder / "python", script=git_repo / "serve.sh", allow_branch=False)
    assert_allowed(result)
    assert result.returncode == 0, result.stderr
    assert not marker.exists(), (
        "the fsmonitor hook fired during the guard's own ls-files calls — "
        "the -c core.fsmonitor= suppression did not hold\n"
        f"stderr: {result.stderr}"
    )


def test_check_mode_rejects_a_trailing_argument():
    """`--check extra` is a caller asking for something; refuse rather than ignore."""
    result = subprocess.run(
        [str(SERVE), "--check", "extra"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 64, result.stderr
    assert "too many arguments" in result.stderr


def test_check_mode_rejects_an_unknown_argument():
    """The argument parser refuses what it does not know rather than ignoring it.

    An unrecognised flag silently treated as "start normally" would make every
    test above pass while testing nothing.
    """
    result = subprocess.run(
        [str(SERVE), "--not-a-real-flag"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 64, result.stderr
    assert "unknown argument" in result.stderr


# --- LIBRARY_CHECK_PLIST: --check importing `app` under a staged plist's own
# environment, rather than never importing it at all --------------------------
#
# WHY THIS EXISTS. On 2026-10-01 `bin/install-library` installed cd9693a after
# its pre-swap `--check` passed, and every start then died at `app.py` import
# (`store.resolve_cold_db_path()` raised `MisconfiguredColdDB`) because the
# service plist lacked `LIBRARY_COLD_DB`. The preflight above
# (`test_the_real_interpreter_passes_the_guard`) only imports `fastapi`/
# `uvicorn`/`sqlite_vec`/`fastembed`, never `app` itself, so this import-time
# configuration refusal got past `--check` every time. These cases reproduce
# that outage as a fixture: a staged plist missing `LIBRARY_COLD_DB` must fail
# `--check` naming it, and the same plist with it set must pass.
#
# EVERY PATH IN THE FIXTURE PLIST IS A TEMP FILE, never `~/.library` — these
# cases drive the REAL venv's `app` import (see `test_the_real_interpreter_
# passes_the_guard` for why that is safe here: `--check` stops short of the
# exec), and the whole point is to exercise the real import-time refusals in
# `store.py`/`vectors.py`/`blobs.py` without touching anything live.
def _real_py_or_skip() -> Path:
    real = SERVE.parent / ".venv" / "bin" / "python"
    if not real.exists():
        _fail_or_skip(
            f"no .venv in this checkout — CI stages one at {real} for exactly "
            "this positive control"
        )
    return real


def _write_check_plist(tmp_path: Path, *, omit: tuple[str, ...] = ()) -> Path:
    """A plist naming every variable `app.py`'s import chain requires at
    import, each pointed at a temp file or directory under `tmp_path` — never
    `~/.library`. `omit` drops named keys entirely, the same shape a plist
    missing a variable has (not merely an empty value)."""
    blob_dir = tmp_path / "blobs"
    blob_dir.mkdir(exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "LIBRARY_DB": str(tmp_path / "library.db"),
        "LIBRARY_BLOB_DIR": str(blob_dir),
        "LIBRARY_VECTORS_DB": str(tmp_path / "vectors.db"),
        "LIBRARY_COLD_DB": str(tmp_path / "cold.db"),
    }
    for key in omit:
        env.pop(key, None)
    plist_path = tmp_path / "check.plist"
    plist_path.write_bytes(plistlib.dumps({
        "Label": "com.convivy.library",
        "ProgramArguments": ["/bin/sh", str(SERVE)],
        "EnvironmentVariables": env,
    }))
    return plist_path


def test_check_fails_naming_the_missing_cold_db_variable(tmp_path):
    """The cd9693a fixture: a staged plist missing LIBRARY_COLD_DB must fail
    `--check`, naming the variable, instead of passing and crash-looping the
    service the way the real install did."""
    _real_py_or_skip()
    plist_path = _write_check_plist(tmp_path, omit=("LIBRARY_COLD_DB",))

    result = subprocess.run(
        [str(SERVE), "--check"],
        env=dict(os.environ, SERVE_ALLOW_BRANCH="1",
                  LIBRARY_CHECK_PLIST=str(plist_path)),
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == REFUSED, (
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "LIBRARY_COLD_DB" in result.stderr, result.stderr


def test_check_passes_when_the_staged_plist_sets_every_variable(tmp_path):
    """The fixed case: the same plist, with LIBRARY_COLD_DB set, passes."""
    _real_py_or_skip()
    plist_path = _write_check_plist(tmp_path)

    result = subprocess.run(
        [str(SERVE), "--check"],
        env=dict(os.environ, SERVE_ALLOW_BRANCH="1",
                  LIBRARY_CHECK_PLIST=str(plist_path)),
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "refusing" not in result.stderr, result.stderr
    assert "app imports cleanly" in result.stdout, result.stdout


def test_check_passes_from_a_cwd_that_is_not_the_build_directory(tmp_path):
    """`bin/install-library` runs `"$STAGE/serve.sh" --check` from whatever
    directory the installer itself was started in, not from `$STAGE`. The
    `import app` step relies on the `cd "$DIR"` near the top of the script
    (before the branch guard) to make `sys.path[0]` — `''`, which Python
    resolves against the cwd at import time — resolve to the staged build's
    own directory rather than the caller's; this pins that so a refactor that
    drops or reorders that `cd` fails here instead of on the next install.
    `tmp_path` is never `SERVE`'s own directory (where `app.py` lives), so
    this is a cwd other than the build directory by construction.
    """
    _real_py_or_skip()
    plist_path = _write_check_plist(tmp_path)

    result = subprocess.run(
        [str(SERVE), "--check"],
        cwd=str(tmp_path),
        env=dict(os.environ, SERVE_ALLOW_BRANCH="1",
                  LIBRARY_CHECK_PLIST=str(plist_path)),
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "refusing" not in result.stderr, result.stderr
    assert "app imports cleanly" in result.stdout, result.stdout


def test_check_without_library_check_plist_does_not_import_the_app(tmp_path):
    """Unset — an ordinary `--check` with no plist to check against — skips
    the app-import step entirely rather than guessing at an environment
    nobody named. Without this, every case above `test_the_real_interpreter_
    passes_the_guard` asserting --check never reaches the app would need to
    change."""
    _real_py_or_skip()
    result = subprocess.run(
        [str(SERVE), "--check"],
        env=dict(os.environ, SERVE_ALLOW_BRANCH="1"),
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "importing app" not in result.stdout, result.stdout


def test_check_plist_naming_a_missing_file_is_refused(tmp_path):
    """LIBRARY_CHECK_PLIST naming a file that doesn't exist is a configuration
    mistake in the caller, refused with the path rather than silently
    skipping the check it was asked to run."""
    missing = tmp_path / "does-not-exist.plist"
    result = subprocess.run(
        [str(SERVE), "--check"],
        env=dict(os.environ, SERVE_ALLOW_BRANCH="1",
                  LIBRARY_CHECK_PLIST=str(missing)),
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == REFUSED, (
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert str(missing) in result.stderr, result.stderr


def test_check_plist_without_environment_variables_dict_is_refused(tmp_path):
    """Round 2 (security-reviewer): a plist with no literal
    `<key>EnvironmentVariables</key>` text at all is refused by the grep
    ahead of the awk parser — it never reaches the awk parser or the
    zero-pairs check after it, both of which `test_check_plist_with_an_
    empty_environment_variables_dict_is_refused` below covers instead. This
    must die naming the plist rather than importing app under an env nobody
    named; on the pre-fix head it instead reaches the app-import subprocess
    and dies on whichever variable app.py happens to require first, not on
    this message."""
    plist_path = tmp_path / "check.plist"
    plist_path.write_bytes(plistlib.dumps({
        "Label": "com.convivy.library",
        "ProgramArguments": ["/bin/sh", str(SERVE)],
    }))

    result = subprocess.run(
        [str(SERVE), "--check"],
        env=dict(os.environ, SERVE_ALLOW_BRANCH="1",
                  LIBRARY_CHECK_PLIST=str(plist_path)),
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == REFUSED, (
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "EnvironmentVariables" in result.stderr, result.stderr
    assert str(plist_path) in result.stderr, result.stderr


def test_check_plist_that_is_binary_format_is_refused(tmp_path):
    """Round 2 (security-reviewer): a binary-format plist — plutil can convert
    a hand-edited launchd plist to one — has no `<key>...</key>` text at all,
    so it is refused by the same grep as the missing-dict case above, before
    the awk parser ever runs, from a file that is otherwise a legitimate
    plist."""
    plist_path = tmp_path / "check.plist"
    plist_path.write_bytes(b"bplist00" + bytes(512))

    result = subprocess.run(
        [str(SERVE), "--check"],
        env=dict(os.environ, SERVE_ALLOW_BRANCH="1",
                  LIBRARY_CHECK_PLIST=str(plist_path)),
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == REFUSED, (
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "EnvironmentVariables" in result.stderr, result.stderr
    assert str(plist_path) in result.stderr, result.stderr


def test_check_plist_with_an_empty_environment_variables_dict_is_refused(tmp_path):
    """Round 5 (reviewer, run-bf6a9b11): neither test above actually reaches
    this check — both are refused earlier, by the grep for the literal
    `<key>EnvironmentVariables</key>` text, before the awk parser ever runs.
    This plist carries that text, so the grep passes, with a literal
    `<dict></dict>` right after it: the awk parser in `_plist_env_pairs`
    opens the EnvironmentVariables block on that `<key>` line and closes it
    again on the very next line without ever seeing a `<key>` inside, so it
    emits zero pairs — the shape this check exists to catch, distinct from
    the shape the grep catches above. Written by hand, not through
    `_write_check_plist`/`plistlib`: `plistlib.dumps({})` renders an empty
    dict as the self-closing `<dict/>` the KNOWN LIMIT paragraph above names,
    which this check does not catch — a non-self-closing `<dict></dict>` is
    what reaches it."""
    plist_path = tmp_path / "check.plist"
    plist_path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<plist version="1.0">\n'
        '<dict>\n'
        '\t<key>Label</key>\n'
        '\t<string>com.convivy.library</string>\n'
        '\t<key>EnvironmentVariables</key>\n'
        '\t<dict></dict>\n'
        '</dict>\n'
        '</plist>\n'
    )

    result = subprocess.run(
        [str(SERVE), "--check"],
        env=dict(os.environ, SERVE_ALLOW_BRANCH="1",
                  LIBRARY_CHECK_PLIST=str(plist_path)),
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == REFUSED, (
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "parsed to zero variables" in result.stderr, result.stderr
    assert str(plist_path) in result.stderr, result.stderr


def test_check_names_missing_app_py_when_app_itself_is_unreachable(tmp_path, git_repo):
    """Round 2 (reviewer): the `exc.name == "app"` branch — "cannot find
    app.py to import" — had no test; the only case exercising `import app`
    failing (missing LIBRARY_COLD_DB) hits the generic `except Exception`
    branch below it, not this one. This is the shape CI's own round-2
    failures took: app.py unreachable from where the staged build runs.
    `git_repo` stages a bare copy of serve.sh with no app.py beside it."""
    real = _real_py_or_skip()
    plist_path = _write_check_plist(tmp_path)

    result = subprocess.run(
        [str(git_repo / "serve.sh"), "--check"],
        env=dict(os.environ, SERVE_ALLOW_BRANCH="1", PY=str(real),
                  LIBRARY_CHECK_PLIST=str(plist_path)),
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == REFUSED, (
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "cannot find app.py to import" in result.stderr, result.stderr
