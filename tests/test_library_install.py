"""bin/library-install — the one command (taking no argument but the
optional --no-migrate) an operator runs by hand to deploy the Library from
origin/main (library issue issue_01M3YHZHGXJ49HE5C6EEPK93M3).

**This file never executes the real bin/install-library, and never touches
this checkout.** Every case below runs against a from-scratch fixture repo
under `tmp_path`, with a stub `bin/install-library` the fixture itself
writes — real `install-library` runs `launchctl`, and running that for real
from a test is exactly the hazard library-install's own header warns about
(the 2026-10-02 incident: a fake `$HOME` does not isolate launchd from the
real gui domain, the real binary, or the real service label). `HOME` is
redirected to a scratch directory for every run, same reason, so nothing
here writes to a real `~/.local/deploy/library`.

Most cases run `bin/library-install` end to end as a subprocess — safe here
only because the stub it calls is not the real installer. A smaller group
sources the script instead (`LIBRARY_INSTALL_SOURCE_ONLY=1`, via a tiny
driver placed next to it so `$0` still resolves to the fixture repo) to
exercise its pure parsing functions directly, the way the module docstring's
own "prefer testing the pieces as sh functions sourced with a guard"
describes.
"""
from __future__ import annotations

import http.server
import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "library-install"

STUB_INSTALL_LIBRARY = """#!/bin/sh
# Stub for test_library_install.py — never the real installer. Logs every
# call it receives to STUB_INSTALL_LIBRARY_LOG (when set) so a case can
# assert it was never reached, then answers --status from, or writes, a
# DEPLOYED_COMMIT file shaped like the real one (only the "commit: " line
# matters to library-install).
set -eu
REPO="$(cd "$(dirname "$0")/.." && pwd)"
DEPLOY_DIR="$HOME/.local/deploy/library"
if [ -n "${STUB_INSTALL_LIBRARY_LOG:-}" ]; then
  echo "$*" >> "$STUB_INSTALL_LIBRARY_LOG"
fi
if [ "${1:-}" = "--status" ]; then
  if [ -f "$DEPLOY_DIR/DEPLOYED_COMMIT" ]; then
    cat "$DEPLOY_DIR/DEPLOYED_COMMIT"
    exit 0
  fi
  echo "install-library: nothing deployed yet" >&2
  exit 1
fi
if [ -f "$REPO/FAIL_INSTALL" ]; then
  echo "stub install-library: simulated failure" >&2
  exit 1
fi
SHA="$(git -C "$REPO" rev-parse main)"
mkdir -p "$DEPLOY_DIR"
{
  echo "ref: main"
  echo "commit: $SHA"
  echo "installed_at: 2026-01-01T00:00:00Z"
} > "$DEPLOY_DIR/DEPLOYED_COMMIT"
exit 0
"""

def _plist_text(env_vars: dict[str, str]) -> str:
    """A minimal, validly-shaped plist — one <key>/<string> pair per line,
    which is what _plist_env_value's line-oriented awk parser (like
    install-library's own _plist_env_pairs) assumes every real plist gives
    it."""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">',
        '<plist version="1.0">',
        "<dict>",
        "    <key>Label</key>",
        "    <string>com.convivy.library</string>",
        "    <key>ProgramArguments</key>",
        "    <array>",
        "        <string>/bin/sh</string>",
        "        <string>/srv/fixture-home/.local/deploy/library/serve.sh</string>",
        "    </array>",
        "    <key>EnvironmentVariables</key>",
        "    <dict>",
    ]
    for k, v in env_vars.items():
        lines.append(f"        <key>{k}</key>")
        lines.append(f"        <string>{v}</string>")
    lines += ["    </dict>", "</dict>", "</plist>", ""]
    return "\n".join(lines)


PLIST_TEMPLATE = _plist_text({"LIBRARY_DB": "/srv/fixture-home/.library/library.db"})

RUN_SH = """#!/bin/sh
# Test-only driver: sources library-install (never reaching main(), since
# LIBRARY_INSTALL_SOURCE_ONLY is set by the caller) from the exact directory
# it lives in, so $0 inside the sourced file still resolves to THIS repo —
# `.`/`source` never changes $0 on its own.
. "$(dirname "$0")/library-install"
"$@"
"""


def _git(repo, *args, check=True, env=None):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=check, capture_output=True, text=True, env=env)


def _write_repo_files(repo: Path):
    (repo / "bin").mkdir(parents=True, exist_ok=True)
    shutil.copy(SCRIPT, repo / "bin" / "library-install")
    (repo / "bin" / "library-install").chmod(0o755)
    (repo / "bin" / "install-library").write_text(STUB_INSTALL_LIBRARY)
    (repo / "bin" / "install-library").chmod(0o755)
    (repo / "bin" / "run.sh").write_text(RUN_SH)
    (repo / "bin" / "run.sh").chmod(0o755)
    (repo / "launchd").mkdir(exist_ok=True)
    (repo / "launchd" / "com.convivy.library.plist").write_text(PLIST_TEMPLATE)
    (repo / "alembic" / "versions").mkdir(parents=True, exist_ok=True)
    (repo / "alembic" / "versions" / ".gitkeep").write_text("")
    (repo / "README.md").write_text("fixture\n")


@pytest.fixture
def seed_repo(tmp_path):
    """Builds (and returns the path to) a from-scratch git repo on `main`,
    shaped like enough of the Library's own layout for library-install to
    operate on, with a real bare "origin" in sync with it at seed time."""
    repo = tmp_path / "src-repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")
    _write_repo_files(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    origin = tmp_path / "origin.git"
    _git(repo, "clone", "-q", "--bare", str(repo), str(origin))
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "fetch", "-q", "origin")
    _git(repo, "branch", "-q", "--set-upstream-to=origin/main", "main")
    return repo


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    return h


def run_library_install(repo: Path, home: Path, extra_env: dict[str, str] | None = None,
                         call_log: Path | None = None, timeout: int = 30,
                         args: list[str] | None = None):
    env = dict(os.environ, HOME=str(home))
    if call_log is not None:
        env["STUB_INSTALL_LIBRARY_LOG"] = str(call_log)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [str(repo / "bin" / "library-install"), *(args or [])], cwd=str(repo), env=env,
        capture_output=True, text=True, timeout=timeout, check=False)


def run_sourced(repo: Path, home: Path, *func_and_args: str, extra_env: dict[str, str] | None = None):
    """Invokes one function out of library-install directly, via the run.sh
    driver, with LIBRARY_INSTALL_SOURCE_ONLY set so sourcing it never reaches
    main()."""
    env = dict(os.environ, HOME=str(home), LIBRARY_INSTALL_SOURCE_ONLY="1")
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [str(repo / "bin" / "run.sh"), *func_and_args], cwd=str(repo), env=env,
        capture_output=True, text=True, timeout=10, check=False)


def source_through_path(path: Path, home: Path, shell_body: str, extra_env: dict[str, str] | None = None):
    """Sources `path` with $0 set to `path` itself (via `sh -c '...' "$path"`
    — the only way to make a sourced file see a given $0, since `.` never
    changes it) and then runs `shell_body`, with LIBRARY_INSTALL_SOURCE_ONLY
    set so sourcing never reaches main(). Used to put a symlink's own path
    in $0 without a wrapper script, which would need its own co-located
    library-install to source and so couldn't stand in for the symlink."""
    env = dict(os.environ, HOME=str(home), LIBRARY_INSTALL_SOURCE_ONLY="1")
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["sh", "-c", f'. "$0"; {shell_body}', str(path)], env=env,
        capture_output=True, text=True, timeout=10, check=False)


def deployed_sha(repo: Path, home: Path) -> str:
    return _git(repo, "rev-parse", "main").stdout.strip()


class _HealthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - stdlib's own method name
        body = self.server.health_body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence stdlib's default access log
        pass


@pytest.fixture
def health_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), _HealthHandler)
    server.health_body = '{"status":"ok","commit":"unset"}'
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    thread.join(timeout=5)


# --- static: the no-agent-no-test rule is stated in the header -------------
#
# CQ·94 gave this script exactly one automated caller, bin/library-deploy-
# watch, so "HUMAN-RUN ONLY" stopped being literally true — the header now
# states the invariant that actually still holds: no agent and no test may
# execute the deploy path, by hand, on this machine.


def test_header_states_the_no_agent_no_test_rule():
    text = SCRIPT.read_text()
    assert "No agent and no test may execute this script's deploy path" in text
    assert "2026-10-02" in text
    assert "fake HOME" in text or "fake `$HOME`" in text or "fake $HOME" in text


def test_header_names_the_one_automated_caller():
    text = SCRIPT.read_text()
    assert "bin/library-deploy-watch" in text
    assert "CQ·94" in text


def test_takes_only_no_migrate_documented():
    assert "Takes one optional argument, --no-migrate" in SCRIPT.read_text()


# --- sourcing guard: LIBRARY_INSTALL_SOURCE_ONLY never reaches main() -------


def test_sourcing_with_source_only_never_runs_main(seed_repo, home):
    # Calling a harmless pure function through the driver must succeed and
    # must not have fetched, merged, or touched DEPLOY_DIR — all of which
    # main() would do if the guard let it run.
    result = run_sourced(seed_repo, home, "_deployed_sha", "/nonexistent/DEPLOYED_COMMIT")
    assert result.returncode == 0, result.stderr
    assert not (home / ".local" / "deploy" / "library").exists()


# --- $0 resolution: REPO must follow a symlink back to the real checkout ---


def test_repo_resolves_through_a_symlink_in_a_temporary_directory(seed_repo, home, tmp_path):
    # The documented real-world install: a symlink to this file placed
    # somewhere else entirely. $0, when the symlink is invoked, is the
    # symlink's own path — never library-install's — so REPO must chase it
    # back to where this file actually lives, not to the symlink's own
    # directory.
    link_dir = tmp_path / "elsewhere"
    link_dir.mkdir()
    symlink = link_dir / "linked-library-install"
    symlink.symlink_to(seed_repo / "bin" / "library-install")
    result = source_through_path(symlink, home, 'printf "%s\\n" "$REPO"')
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(seed_repo.resolve())


def test_library_install_deploys_correctly_when_invoked_through_a_symlink(seed_repo, home, tmp_path):
    # Full end-to-end run through the symlink (not sourced): proves REPO
    # resolved correctly because execution reaches this script's own new-
    # baseline refusal (step (c)), rather than failing early with a git
    # "not a repository" error against the wrong directory.
    link_dir = tmp_path / "elsewhere"
    link_dir.mkdir()
    symlink = link_dir / "linked-library-install"
    symlink.symlink_to(seed_repo / "bin" / "library-install")
    env = dict(os.environ, HOME=str(home))
    result = subprocess.run([str(symlink)], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 1
    assert "not a git repository" not in (result.stdout + result.stderr).lower()
    assert "first install" in result.stderr


# --- pure functions, via the sourcing driver --------------------------------


def test_plist_env_value_reads_a_set_key(seed_repo, home, tmp_path):
    plist = tmp_path / "with-port.plist"
    plist.write_text(_plist_text({"LIBRARY_PORT": "9100", "LIBRARY_DB": "/x"}))
    result = run_sourced(seed_repo, home, "_plist_env_value", str(plist), "LIBRARY_PORT")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "9100"


def test_plist_env_value_fails_closed_on_a_missing_key(seed_repo, home):
    plist = seed_repo / "launchd" / "com.convivy.library.plist"
    result = run_sourced(seed_repo, home, "_plist_env_value", str(plist), "LIBRARY_PORT")
    assert result.returncode == 1
    assert result.stdout.strip() == ""


def test_plist_env_value_fails_closed_on_a_missing_file(seed_repo, home):
    result = run_sourced(seed_repo, home, "_plist_env_value", "/nonexistent.plist", "LIBRARY_PORT")
    assert result.returncode == 1


def test_service_port_falls_back_to_8100_when_unset(seed_repo, home):
    result = run_sourced(seed_repo, home, "_service_port")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "8100"


def test_service_port_reads_the_template_when_live_plist_is_absent(seed_repo, home):
    (seed_repo / "launchd" / "com.convivy.library.plist").write_text(
        _plist_text({"LIBRARY_PORT": "9200", "LIBRARY_DB": "/x"}))
    result = run_sourced(seed_repo, home, "_service_port")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "9200"


def test_service_port_prefers_the_live_plist_over_the_template(seed_repo, home):
    (seed_repo / "launchd" / "com.convivy.library.plist").write_text(
        _plist_text({"LIBRARY_PORT": "9200", "LIBRARY_DB": "/x"}))
    live_dir = home / "Library" / "LaunchAgents"
    live_dir.mkdir(parents=True)
    (live_dir / "com.convivy.library.plist").write_text(
        _plist_text({"LIBRARY_PORT": "9300", "LIBRARY_DB": "/x"}))
    result = run_sourced(seed_repo, home, "_service_port")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "9300"


def test_health_url_defaults_to_the_service_port(seed_repo, home):
    result = run_sourced(seed_repo, home, "_health_url")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "http://127.0.0.1:8100/health"


def test_health_url_honors_the_test_override(seed_repo, home):
    result = run_sourced(seed_repo, home, "_health_url",
                          extra_env={"LIBRARY_INSTALL_TEST_HEALTH_URL": "http://example.invalid/health"})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "http://example.invalid/health"


def test_health_field_extracts_commit_and_staged_commit(seed_repo, home):
    body = '{"status":"ok","commit":"abc123","staged_commit":"def456"}'
    result = run_sourced(seed_repo, home, "_health_field", body, "commit")
    assert result.stdout.strip() == "abc123"
    result = run_sourced(seed_repo, home, "_health_field", body, "staged_commit")
    assert result.stdout.strip() == "def456"


def test_health_field_is_empty_when_the_field_is_absent(seed_repo, home):
    body = '{"status":"ok","commit":"abc123"}'
    result = run_sourced(seed_repo, home, "_health_field", body, "staged_commit")
    assert result.stdout.strip() == ""


def test_deployed_sha_is_empty_before_any_install(seed_repo, home):
    result = run_sourced(seed_repo, home, "_deployed_sha", str(home / ".local" / "deploy" / "library" / "DEPLOYED_COMMIT"))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def test_deployed_sha_reads_the_commit_line(seed_repo, home):
    deploy_dir = home / ".local" / "deploy" / "library"
    deploy_dir.mkdir(parents=True)
    (deploy_dir / "DEPLOYED_COMMIT").write_text("ref: main\ncommit: cafef00d\ninstalled_at: x\n")
    result = run_sourced(seed_repo, home, "_deployed_sha", str(deploy_dir / "DEPLOYED_COMMIT"))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "cafef00d"


# --- the test-only install-library redirect, and its own safety check ------


def test_redirect_variable_pointed_at_the_real_path_is_refused(seed_repo, home):
    real = seed_repo / "bin" / "install-library"
    log = home.parent / "calls.log"
    result = run_library_install(
        seed_repo, home, call_log=log,
        extra_env={"LIBRARY_INSTALL_TEST_INSTALL_LIBRARY": str(real)})
    assert result.returncode == 1
    assert "must redirect to a stub" in result.stderr
    assert not log.exists()


def test_redirect_variable_sends_the_deploy_call_elsewhere(seed_repo, home, tmp_path):
    # Break the repo's own stub so a run that reached it would fail, and
    # point the redirect at a second, working stub placed outside the repo
    # entirely — if library-install still succeeds, it used the redirect.
    baseline = _git(seed_repo, "rev-parse", "main").stdout.strip()
    deploy_dir = home / ".local" / "deploy" / "library"
    deploy_dir.mkdir(parents=True)
    (deploy_dir / "DEPLOYED_COMMIT").write_text(f"ref: main\ncommit: {baseline}\n")
    (seed_repo / "bin" / "install-library").write_text("#!/bin/sh\nexit 97\n")
    (seed_repo / "bin" / "install-library").chmod(0o755)
    _git(seed_repo, "add", "-A")
    _git(seed_repo, "commit", "-q", "-m", "break the stub")
    _git(seed_repo, "push", "-q", "origin", "main")
    target = _git(seed_repo, "rev-parse", "main").stdout.strip()
    external = tmp_path / "external-stub"
    external.mkdir()
    # A minimal stub, deliberately not shaped like the repo's own — it has
    # no git repo of its own to read, so it just writes the known target sha
    # straight into DEPLOYED_COMMIT, proving only that THIS one ran instead
    # of the repo-local one above.
    (external / "install-library").write_text(f"""#!/bin/sh
set -eu
DEPLOY_DIR="$HOME/.local/deploy/library"
if [ "${{1:-}}" = "--status" ]; then
  if [ -f "$DEPLOY_DIR/DEPLOYED_COMMIT" ]; then cat "$DEPLOY_DIR/DEPLOYED_COMMIT"; exit 0; fi
  exit 1
fi
mkdir -p "$DEPLOY_DIR"
echo "commit: {target}" > "$DEPLOY_DIR/DEPLOYED_COMMIT"
exit 0
""")
    (external / "install-library").chmod(0o755)
    result = run_library_install(
        seed_repo, home,
        extra_env={"LIBRARY_INSTALL_TEST_INSTALL_LIBRARY": str(external / "install-library")})
    assert result.returncode == 0, result.stdout + result.stderr


def _assert_redirect_refused(seed_repo, home, redirect_value):
    log = home.parent / "calls.log"
    result = run_library_install(
        seed_repo, home, call_log=log,
        extra_env={"LIBRARY_INSTALL_TEST_INSTALL_LIBRARY": redirect_value})
    assert result.returncode == 1, result.stdout + result.stderr
    assert "stub outside this repository" in result.stderr
    assert not log.exists()


def test_redirect_as_a_symlink_to_the_real_installer_is_refused(seed_repo, home, tmp_path):
    link = tmp_path / "sym-install-library"
    link.symlink_to(seed_repo / "bin" / "install-library")
    _assert_redirect_refused(seed_repo, home, str(link))


def test_redirect_as_a_relative_path_to_the_real_installer_is_refused(seed_repo, home):
    _assert_redirect_refused(seed_repo, home, "bin/install-library")


def test_redirect_with_a_dot_slash_prefix_to_the_real_installer_is_refused(seed_repo, home):
    _assert_redirect_refused(seed_repo, home, "./bin/install-library")


def test_redirect_with_a_doubled_slash_to_the_real_installer_is_refused(seed_repo, home):
    _assert_redirect_refused(seed_repo, home, f"{seed_repo}//bin//install-library")


def test_redirect_with_a_dot_dot_segment_to_the_real_installer_is_refused(seed_repo, home):
    _assert_redirect_refused(seed_repo, home, f"{seed_repo}/bin/../bin/install-library")


def test_redirect_pointed_inside_this_repository_but_not_at_the_real_installer_is_refused(seed_repo, home):
    other = seed_repo / "bin" / "other-stub"
    other.write_text("#!/bin/sh\nexit 0\n")
    other.chmod(0o755)
    _assert_redirect_refused(seed_repo, home, str(other))


def test_redirect_pointed_at_a_nonexistent_path_is_refused(seed_repo, home):
    log = home.parent / "calls.log"
    result = run_library_install(
        seed_repo, home, call_log=log,
        extra_env={"LIBRARY_INSTALL_TEST_INSTALL_LIBRARY": str(seed_repo.parent / "does-not-exist")})
    assert result.returncode == 1
    assert "does not exist" in result.stderr
    assert not log.exists()


# --- (a)/(b): refusals that change nothing ----------------------------------


def test_a_dirty_tree_is_refused_and_the_stub_is_never_called(seed_repo, home):
    (seed_repo / "README.md").write_text("dirty\n")
    log = home.parent / "calls.log"
    result = run_library_install(seed_repo, home, call_log=log)
    assert result.returncode == 1
    assert "uncommitted changes" in result.stderr
    assert not log.exists()


def test_a_fetch_failure_names_refs_stash_when_present(seed_repo, home):
    """2026-10-08 incident: a dispatched run's `git stash` in a linked
    worktree left refs/stash — shared by every worktree of a repo —
    pointing at a commit whose objects were never completed, and `git
    fetch` then failed with "fatal: bad object refs/stash" in every
    worktree. This script's own step (a) fetch is the first fetch an
    operator running library-install by hand actually hits — the hint
    install-library's identical fetch failure already prints never reached
    them through this entry point before this test. Planting refs/stash
    directly (never a real `git stash`, which would touch the shared stash
    stack) reproduces the same shape without that risk: this names the ref
    and the removal command, and never removes it itself."""
    stash_sha = _git(seed_repo, "rev-parse", "main").stdout.strip()
    _git(seed_repo, "update-ref", "refs/stash", stash_sha)
    _git(seed_repo, "remote", "set-url", "origin", str(seed_repo.parent / "no-such-origin.git"))

    log = home.parent / "calls.log"
    result = run_library_install(seed_repo, home, call_log=log)
    assert result.returncode == 1
    combined = result.stdout + result.stderr
    assert "could not fetch origin" in combined
    assert "refs/stash" in combined
    assert "update-ref -d refs/stash" in combined
    assert not log.exists()
    assert _git(seed_repo, "show-ref", "--verify", "--quiet", "refs/stash", check=False).returncode == 0, (
        "this must never remove refs/stash itself")


def test_a_non_main_branch_is_refused_and_never_checked_out_for_the_operator(seed_repo, home):
    _git(seed_repo, "checkout", "-q", "-b", "side")
    log = home.parent / "calls.log"
    result = run_library_install(seed_repo, home, call_log=log)
    assert result.returncode == 1
    assert "not main" in result.stderr
    assert not log.exists()
    assert _git(seed_repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "side"


def test_diverged_main_is_refused_and_never_calls_the_stub(seed_repo, home, tmp_path):
    # Advance origin/main independently (a second clone, committing and
    # pushing on its own), then commit something unrelated on local main —
    # neither is an ancestor of the other, so `merge --ff-only` must fail.
    other = tmp_path / "other-clone"
    subprocess.run(["git", "clone", "-q", str(seed_repo.parent / "origin.git"), str(other)],
                    check=True, capture_output=True, text=True)
    _git(other, "config", "user.email", "t@t")
    _git(other, "config", "user.name", "t")
    _git(other, "config", "commit.gpgsign", "false")
    (other / "ELSEWHERE.md").write_text("elsewhere\n")
    _git(other, "add", "-A")
    _git(other, "commit", "-q", "-m", "elsewhere")
    _git(other, "push", "-q", "origin", "main")

    (seed_repo / "LOCAL.md").write_text("local\n")
    _git(seed_repo, "add", "-A")
    _git(seed_repo, "commit", "-q", "-m", "local")

    log = home.parent / "calls.log"
    result = run_library_install(seed_repo, home, call_log=log)
    assert result.returncode == 1
    assert "diverged" in result.stderr
    assert not log.exists()


def test_a_tag_named_main_does_not_change_the_target(seed_repo, home):
    # A lightweight tag literally named "main", pointing at a commit other
    # than the branch tip — git's own ref-disambiguation order resolves a
    # bare "main" to the tag before the branch, so the target must be read
    # from refs/heads/main explicitly, never a bare name.
    baseline = _git(seed_repo, "rev-parse", "main").stdout.strip()
    deploy_dir = home / ".local" / "deploy" / "library"
    deploy_dir.mkdir(parents=True)
    (deploy_dir / "DEPLOYED_COMMIT").write_text(f"ref: main\ncommit: {baseline}\n")

    (seed_repo / "DECOY.md").write_text("decoy\n")
    _git(seed_repo, "add", "-A")
    _git(seed_repo, "commit", "-q", "-m", "decoy commit for the tag")
    decoy = _git(seed_repo, "rev-parse", "HEAD").stdout.strip()
    _git(seed_repo, "tag", "main", decoy)
    _git(seed_repo, "reset", "-q", "--hard", baseline)

    result = run_library_install(seed_repo, home)
    assert result.returncode == 0, result.stdout + result.stderr
    assert baseline in result.stdout
    assert decoy not in result.stdout


# --- (c): the range report, the alembic refusal, the plist warning ---------


def test_nothing_to_deploy_skips_the_pr_listing_and_still_deploys(seed_repo, home):
    baseline = _git(seed_repo, "rev-parse", "main").stdout.strip()
    deploy_dir = home / ".local" / "deploy" / "library"
    deploy_dir.mkdir(parents=True)
    (deploy_dir / "DEPLOYED_COMMIT").write_text(f"ref: main\ncommit: {baseline}\n")
    result = run_library_install(seed_repo, home)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "nothing new to deploy" in result.stdout


def test_a_missing_deployed_commit_file_refuses_and_points_at_a_manual_first_install(seed_repo, home):
    log = home.parent / "calls.log"
    result = run_library_install(seed_repo, home, call_log=log)
    assert result.returncode == 1
    assert "first install" in result.stderr
    assert not log.exists()
    assert not (home / ".local" / "deploy" / "library").exists()


def test_a_deployed_commit_file_with_no_commit_line_is_refused(seed_repo, home):
    deploy_dir = home / ".local" / "deploy" / "library"
    deploy_dir.mkdir(parents=True)
    (deploy_dir / "DEPLOYED_COMMIT").write_text("ref: main\ninstalled_at: x\n")
    log = home.parent / "calls.log"
    result = run_library_install(seed_repo, home, call_log=log)
    assert result.returncode == 1
    assert "no 'commit:" in result.stderr
    assert not log.exists()


def test_an_unreadable_deployed_commit_file_is_refused(seed_repo, home):
    deploy_dir = home / ".local" / "deploy" / "library"
    deploy_dir.mkdir(parents=True)
    f = deploy_dir / "DEPLOYED_COMMIT"
    f.write_text("ref: main\ncommit: deadbeef\n")
    f.chmod(0o000)
    try:
        log = home.parent / "calls.log"
        result = run_library_install(seed_repo, home, call_log=log)
        assert result.returncode == 1
        assert "not readable" in result.stderr
        assert not log.exists()
    finally:
        f.chmod(0o644)


def test_a_deployed_commit_naming_an_unknown_commit_is_refused(seed_repo, home):
    deploy_dir = home / ".local" / "deploy" / "library"
    deploy_dir.mkdir(parents=True)
    (deploy_dir / "DEPLOYED_COMMIT").write_text("ref: main\ncommit: " + "f" * 40 + "\n")
    log = home.parent / "calls.log"
    result = run_library_install(seed_repo, home, call_log=log)
    assert result.returncode == 1
    assert "does not have" in result.stderr
    assert not log.exists()


def test_a_deployed_commit_naming_a_non_ancestor_commit_is_refused(seed_repo, home):
    # A real commit, present in the repo's object database (so git has it)
    # but reachable from no branch — not an ancestor of the target.
    _git(seed_repo, "checkout", "-q", "--orphan", "stray")
    (seed_repo / "STRAY.md").write_text("stray\n")
    _git(seed_repo, "add", "-A")
    _git(seed_repo, "commit", "-q", "-m", "stray commit")
    stray_sha = _git(seed_repo, "rev-parse", "stray").stdout.strip()
    _git(seed_repo, "checkout", "-q", "main")
    _git(seed_repo, "branch", "-q", "-D", "stray")

    deploy_dir = home / ".local" / "deploy" / "library"
    deploy_dir.mkdir(parents=True)
    (deploy_dir / "DEPLOYED_COMMIT").write_text(f"ref: main\ncommit: {stray_sha}\n")
    log = home.parent / "calls.log"
    result = run_library_install(seed_repo, home, call_log=log)
    assert result.returncode == 1
    assert "not an ancestor" in result.stderr
    assert not log.exists()


def test_an_alembic_change_in_range_is_reported_and_handed_to_install_library(seed_repo, home):
    # install-library's own auto-migrate path (not this script) now backs up
    # and migrates the vault, so library-install only reports the range here
    # and still lets the deploy through to the stub.
    deployed = _git(seed_repo, "rev-parse", "main").stdout.strip()
    deploy_dir = home / ".local" / "deploy" / "library"
    deploy_dir.mkdir(parents=True)
    (deploy_dir / "DEPLOYED_COMMIT").write_text(f"ref: main\ncommit: {deployed}\n")

    (seed_repo / "alembic" / "versions" / "0001_x.py").write_text("# migration\n")
    _git(seed_repo, "add", "-A")
    _git(seed_repo, "commit", "-q", "-m", "feat: add a migration")
    _git(seed_repo, "push", "-q", "origin", "main")
    target = _git(seed_repo, "rev-parse", "main").stdout.strip()

    log = home.parent / "calls.log"
    result = run_library_install(seed_repo, home, call_log=log)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "alembic/versions" in result.stdout
    assert "0001_x.py" in result.stdout
    assert log.exists()
    assert target in (deploy_dir / "DEPLOYED_COMMIT").read_text()


def test_no_migrate_flag_reaches_install_library(seed_repo, home):
    baseline = _git(seed_repo, "rev-parse", "main").stdout.strip()
    deploy_dir = home / ".local" / "deploy" / "library"
    deploy_dir.mkdir(parents=True)
    (deploy_dir / "DEPLOYED_COMMIT").write_text(f"ref: main\ncommit: {baseline}\n")

    (seed_repo / "NEW.md").write_text("new\n")
    _git(seed_repo, "add", "-A")
    _git(seed_repo, "commit", "-q", "-m", "feat: add a thing")
    _git(seed_repo, "push", "-q", "origin", "main")

    log = home.parent / "calls.log"
    result = run_library_install(seed_repo, home, call_log=log, args=["--no-migrate"])
    assert result.returncode == 0, result.stdout + result.stderr
    calls = log.read_text().splitlines()
    assert calls[0] == "--no-migrate"


def test_an_unknown_flag_is_refused_and_never_calls_the_stub(seed_repo, home):
    log = home.parent / "calls.log"
    result = run_library_install(seed_repo, home, call_log=log, args=["--bogus"])
    assert result.returncode == 1
    assert "unknown option" in result.stderr
    assert not log.exists()


def test_a_refusing_install_library_stops_before_the_verdict(seed_repo, home):
    # The stub's own FAIL_INSTALL sentinel simulates install-library itself
    # refusing (an unknown vault revision, a LIBRARY_AUTO_APPLY = False
    # revision, or a failed migration all surface the same way: a nonzero
    # exit under this script's `set -eu`, before step (e) ever runs).
    # Ignored by git so planting it never trips the dirty-tree refusal in
    # step (a).
    (seed_repo / ".gitignore").write_text("FAIL_INSTALL\n")
    baseline = _git(seed_repo, "rev-parse", "main").stdout.strip()
    deploy_dir = home / ".local" / "deploy" / "library"
    deploy_dir.mkdir(parents=True)
    (deploy_dir / "DEPLOYED_COMMIT").write_text(f"ref: main\ncommit: {baseline}\n")

    (seed_repo / "NEW.md").write_text("new\n")
    _git(seed_repo, "add", "-A")
    _git(seed_repo, "commit", "-q", "-m", "feat: add a thing")
    _git(seed_repo, "push", "-q", "origin", "main")

    (seed_repo / "FAIL_INSTALL").write_text("")

    result = run_library_install(seed_repo, home)
    assert result.returncode == 1
    assert "simulated failure" in result.stderr
    assert "Deploy confirmed" not in result.stdout
    assert "verdict:" not in result.stdout
    assert "verdict:" not in result.stderr


def test_a_launchd_change_other_than_the_managed_plist_warns_but_still_deploys(seed_repo, home):
    deployed = _git(seed_repo, "rev-parse", "main").stdout.strip()
    deploy_dir = home / ".local" / "deploy" / "library"
    deploy_dir.mkdir(parents=True)
    (deploy_dir / "DEPLOYED_COMMIT").write_text(f"ref: main\ncommit: {deployed}\n")

    (seed_repo / "launchd" / "com.convivy.library.cold-tier.plist").write_text("<plist/>\n")
    _git(seed_repo, "add", "-A")
    _git(seed_repo, "commit", "-q", "-m", "feat: add a mover job")
    _git(seed_repo, "push", "-q", "origin", "main")

    result = run_library_install(seed_repo, home)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "does not manage" in result.stderr
    assert "cold-tier.plist" in result.stderr


def test_merged_pr_titles_are_listed_between_deployed_and_target(seed_repo, home):
    deployed = _git(seed_repo, "rev-parse", "main").stdout.strip()
    deploy_dir = home / ".local" / "deploy" / "library"
    deploy_dir.mkdir(parents=True)
    (deploy_dir / "DEPLOYED_COMMIT").write_text(f"ref: main\ncommit: {deployed}\n")

    (seed_repo / "NEW.md").write_text("new\n")
    _git(seed_repo, "add", "-A")
    _git(seed_repo, "commit", "-q", "-m", "feat: add a thing (#7)")
    _git(seed_repo, "push", "-q", "origin", "main")

    result = run_library_install(seed_repo, home)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "feat: add a thing (#7)" in result.stdout


def test_a_malicious_commit_subject_is_sanitized_in_the_pr_title_listing(seed_repo, home):
    deployed = _git(seed_repo, "rev-parse", "main").stdout.strip()
    deploy_dir = home / ".local" / "deploy" / "library"
    deploy_dir.mkdir(parents=True)
    (deploy_dir / "DEPLOYED_COMMIT").write_text(f"ref: main\ncommit: {deployed}\n")

    (seed_repo / "NEW.md").write_text("new\n")
    _git(seed_repo, "add", "-A")
    subject = "evil\x1b]0;pwned\x07subject\x7f"
    subprocess.run(["git", "-C", str(seed_repo), "commit", "-q", "-m", subject],
                    check=True, capture_output=True, text=True)
    _git(seed_repo, "push", "-q", "origin", "main")

    result = run_library_install(seed_repo, home)
    assert result.returncode == 0, result.stdout + result.stderr
    # The script's own `say()` legitimately prints ANSI color codes (also
    # ESC-prefixed); only the injected OSC-looking sequence and the bare
    # BEL/DEL bytes from the commit subject must be gone.
    assert "\x1b]0;" not in result.stdout
    assert "\x07" not in result.stdout
    assert "\x7f" not in result.stdout
    assert "evil" in result.stdout
    assert "pwnedsubject" in result.stdout


def test_strip_controls_removes_c0_and_del_but_keeps_tab_and_newline(seed_repo, home):
    raw = "a\tb\nc\x01d\x1b]0;evil\x07e\x7ff\x0dg"
    result = subprocess.run(
        ["sh", "-c", '. "$0"; printf "%s" "$1" | _strip_controls',
         str(seed_repo / "bin" / "library-install"), raw],
        env=dict(os.environ, HOME=str(home), LIBRARY_INSTALL_SOURCE_ONLY="1"),
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "a\tb\ncd]0;evilefg"


# --- (e): the verdict --------------------------------------------------------


def _external_stub(repo: Path, out_dir: Path) -> Path:
    """A stub install-library placed OUTSIDE `repo` entirely (a valid
    LIBRARY_INSTALL_TEST_INSTALL_LIBRARY redirect target), with `repo`
    baked in rather than derived from its own $0 — it doesn't live next to
    `repo`'s bin/, so the co-location trick the real stub uses wouldn't
    resolve."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stub = out_dir / "install-library"
    stub.write_text(f"""#!/bin/sh
set -eu
REPO="{repo}"
DEPLOY_DIR="$HOME/.local/deploy/library"
if [ "${{1:-}}" = "--status" ]; then
  if [ -f "$DEPLOY_DIR/DEPLOYED_COMMIT" ]; then cat "$DEPLOY_DIR/DEPLOYED_COMMIT"; exit 0; fi
  echo "install-library: nothing deployed yet" >&2
  exit 1
fi
SHA="$(git -C "$REPO" rev-parse main)"
mkdir -p "$DEPLOY_DIR"
{{
  echo "ref: main"
  echo "commit: $SHA"
  echo "installed_at: 2026-01-01T00:00:00Z"
}} > "$DEPLOY_DIR/DEPLOYED_COMMIT"
exit 0
""")
    stub.chmod(0o755)
    return stub


def test_verdict_confirms_when_status_and_health_both_agree(seed_repo, home, health_server, tmp_path):
    target = _git(seed_repo, "rev-parse", "main").stdout.strip()
    deploy_dir = home / ".local" / "deploy" / "library"
    deploy_dir.mkdir(parents=True)
    (deploy_dir / "DEPLOYED_COMMIT").write_text(f"ref: main\ncommit: {target}\n")
    health_server.health_body = f'{{"status":"ok","commit":"{target}","staged_commit":"{target}"}}'
    port = health_server.server_address[1]
    stub = _external_stub(seed_repo, tmp_path / "ext")
    result = run_library_install(
        seed_repo, home,
        extra_env={"LIBRARY_INSTALL_TEST_HEALTH_URL": f"http://127.0.0.1:{port}/health",
                   "LIBRARY_INSTALL_TEST_INSTALL_LIBRARY": str(stub)})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Deploy confirmed" in result.stdout


def test_verdict_is_not_confirmed_when_health_disagrees(seed_repo, home, health_server, tmp_path):
    target = _git(seed_repo, "rev-parse", "main").stdout.strip()
    deploy_dir = home / ".local" / "deploy" / "library"
    deploy_dir.mkdir(parents=True)
    (deploy_dir / "DEPLOYED_COMMIT").write_text(f"ref: main\ncommit: {target}\n")
    health_server.health_body = '{"status":"ok","commit":"stale","staged_commit":"stale"}'
    port = health_server.server_address[1]
    stub = _external_stub(seed_repo, tmp_path / "ext")
    result = run_library_install(
        seed_repo, home,
        extra_env={"LIBRARY_INSTALL_TEST_HEALTH_URL": f"http://127.0.0.1:{port}/health",
                   "LIBRARY_INSTALL_TEST_INSTALL_LIBRARY": str(stub)})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "not yet confirmed" in result.stderr


def test_verdict_handles_health_being_unreachable(seed_repo, home, tmp_path):
    target = _git(seed_repo, "rev-parse", "main").stdout.strip()
    deploy_dir = home / ".local" / "deploy" / "library"
    deploy_dir.mkdir(parents=True)
    (deploy_dir / "DEPLOYED_COMMIT").write_text(f"ref: main\ncommit: {target}\n")
    stub = _external_stub(seed_repo, tmp_path / "ext")
    result = run_library_install(
        seed_repo, home,
        extra_env={"LIBRARY_INSTALL_TEST_HEALTH_URL": "http://127.0.0.1:1/health",
                   "LIBRARY_INSTALL_TEST_INSTALL_LIBRARY": str(stub)})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "not yet confirmed" in result.stderr


def test_health_url_override_is_refused_without_the_install_library_redirect(seed_repo, home, health_server):
    port = health_server.server_address[1]
    log = home.parent / "calls.log"
    result = run_library_install(
        seed_repo, home, call_log=log,
        extra_env={"LIBRARY_INSTALL_TEST_HEALTH_URL": f"http://127.0.0.1:{port}/health"})
    assert result.returncode == 1
    assert "LIBRARY_INSTALL_TEST_INSTALL_LIBRARY" in result.stderr
    assert not log.exists()
