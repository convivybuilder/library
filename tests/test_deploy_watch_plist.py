"""`com.convivy.library.deploy-watch.plist` — CQ·94
(issue_01M1G7G14FMD7M30ZFQK9YZVST). Unlike every sibling plist this repo
ships (ci-result-fetch, cold-tier, cert-expiry-check — all deploy-pinned,
ADR-0059 rule 1), this one is CHECKOUT-pinned: its own job is comparing the
checkout's `origin/main` against the deploy area and, when they differ,
running the checkout's own `bin/library-install`, which needs a real `.git`
to fetch and compare against. `bin/library-deploy-watch`'s own module
docstring gives the reason, mirroring `convivy-orient/deploy_watch.py`'s
identical layout.
"""
import os
import plistlib
import subprocess
from pathlib import Path

LAUNCHD = Path(__file__).resolve().parent.parent / "launchd"
PLIST = LAUNCHD / "com.convivy.library.deploy-watch.plist"
INSTALLER = Path(__file__).resolve().parent.parent / "bin" / "install-library"

# A synthetic, fixed home — never this developer's own — matching every
# sibling plist test's own convention: install-library's `--internal-render-
# plist` is a read-only render (never touches real launchd or a real
# install), so running it directly against the checked-in template is safe.
FIXED_HOME = "/srv/fixture-home"


def _rendered(path: Path = PLIST) -> dict:
    result = subprocess.run(
        [str(INSTALLER), "--internal-render-plist", str(path)],
        env={**os.environ, "HOME": FIXED_HOME},
        capture_output=True, timeout=10, check=True)
    return plistlib.loads(result.stdout)


def test_the_plist_points_at_the_checkout_not_the_deploy_area():
    plist = _rendered()
    for arg in plist["ProgramArguments"]:
        assert arg.startswith(f"{FIXED_HOME}/Convivy/library/"), (
            f"{arg!r} points at the deploy area rather than the checkout — "
            f"this job's own git fetch/rev-parse needs a real .git, which "
            f"the deploy area (a git-archive export) never has")
    assert plist["WorkingDirectory"].startswith(f"{FIXED_HOME}/Convivy/library")


def test_the_plist_runs_the_script_under_bin():
    plist = _rendered()
    assert plist["ProgramArguments"][-1].endswith("/bin/library-deploy-watch")


def test_the_plist_runs_the_checkout_s_own_venv():
    plist = _rendered()
    assert plist["ProgramArguments"][0] == (
        f"{FIXED_HOME}/Convivy/library/.venv/bin/python3")


def test_the_plist_is_a_poll_not_a_kept_alive_server():
    plist = _rendered()
    assert "StartInterval" in plist
    assert "KeepAlive" not in plist, (
        "this job is expected to exit every run; KeepAlive would fight that")


def test_the_plist_s_interval_matches_orient_s_own_deploy_watcher():
    """CQ·94's own text: "StartInterval 300 like Orient's watcher"."""
    plist = _rendered()
    assert plist["StartInterval"] == 300


def test_the_plist_runs_at_load():
    """RunAtLoad picks up a merge that landed while the job was unloaded, as
    soon as it loads — matching ci-result-fetch's own plist, not Orient's
    (whose RunAtLoad is false for an unrelated reason that doesn't apply
    here)."""
    plist = _rendered()
    assert plist["RunAtLoad"] is True


def test_the_plist_s_state_paths_are_outside_the_deploy_area():
    plist = _rendered()
    for key in ("StandardOutPath", "StandardErrorPath"):
        assert plist[key].startswith(f"{FIXED_HOME}/.library/"), (
            f"{key} points inside the deploy area, which an atomic install "
            f"swap replaces wholesale and would destroy a log living in it")


def test_the_plist_sets_no_token_env_var():
    """The credential lives in ~/.library/deploy-watch-token (0600), read by
    bin/library-deploy-watch itself — never in the plist's own
    EnvironmentVariables, which install-library writes as a world-readable-
    by-default template and which `launchctl print` echoes back to any
    process that can ask for this job's environment."""
    env = _rendered()["EnvironmentVariables"]
    assert not any("TOKEN" in key for key in env)
