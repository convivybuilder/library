"""`com.convivy.library.cert-expiry-check.plist` — the same shape of check
`test_cold_tier_plist.py` runs for its own sibling: deploy-pinned, not a
kept-alive server, state paths outside the deploy area.

`cert_expiry_check.py` differs from the retired `ci_result_fetch.py` in the same
load-bearing way `cold_tier.py` does: it opens `LIBRARY_DB` directly through
`store.py` and writes through `store.insert_signal` rather than posting over
HTTP, so this plist also needs the database configuration the service's own
plist deploys. Unlike `cold_tier.py` it never imports `vectors.py`, so it
carries no `LIBRARY_VECTORS_DB` — that omission is asserted below, not left
untested.
"""
import os
import plistlib
import subprocess
from pathlib import Path

LAUNCHD = Path(__file__).resolve().parent.parent / "launchd"
PLIST = LAUNCHD / "com.convivy.library.cert-expiry-check.plist"
SERVICE_PLIST = LAUNCHD / "com.convivy.library.plist"
INSTALLER = Path(__file__).resolve().parent.parent / "bin" / "install-library"

# A synthetic, fixed home — never this developer's own — that every case
# below renders the checked-in template against, through install-library's
# own `--internal-render-plist` entry point (never a reimplementation of the
# substitution): launchd does not expand `$HOME` or `~`, so the file in the
# repo carries a placeholder, not a literal path, and this is what
# install-library would write to ~/Library/LaunchAgents on a machine whose
# home is this one.
FIXED_HOME = "/srv/fixture-home"


def _rendered(path: Path) -> dict:
    result = subprocess.run(
        [str(INSTALLER), "--internal-render-plist", str(path)],
        env={**os.environ, "HOME": FIXED_HOME},
        capture_output=True, timeout=10, check=True)
    return plistlib.loads(result.stdout)


def test_the_plist_points_at_the_deploy_area_not_the_checkout():
    plist = _rendered(PLIST)
    for arg in plist["ProgramArguments"]:
        assert arg.startswith(f"{FIXED_HOME}/.local/deploy/library/"), (
            f"{arg!r} points at a working checkout rather than the deploy area "
            f"— a timer must never invoke the checkout")
    assert plist["WorkingDirectory"].startswith(
        f"{FIXED_HOME}/.local/deploy/library")


def test_the_plist_runs_the_script_under_bin():
    plist = _rendered(PLIST)
    assert plist["ProgramArguments"][-1].endswith("/bin/cert_expiry_check.py")


def test_the_plist_is_a_poll_not_a_kept_alive_server():
    plist = _rendered(PLIST)
    assert "StartInterval" in plist
    assert "KeepAlive" not in plist, (
        "this job is expected to exit every run; KeepAlive would fight that")


def test_the_plist_runs_at_least_daily():
    """platform/decisions/0066's Handoff names no interval of its own for
    the warning job, but the issue body states the requirement directly:
    "Run it at least daily"."""
    plist = _rendered(PLIST)
    assert plist["StartInterval"] <= 86400


def test_the_plist_s_state_paths_are_outside_the_deploy_area():
    plist = _rendered(PLIST)
    for key in ("StandardOutPath", "StandardErrorPath"):
        assert plist[key].startswith(f"{FIXED_HOME}/.library/"), (
            f"{key} points inside the deploy area, which an atomic install "
            f"swap replaces wholesale and would destroy a log living in it")


def test_the_plist_sets_the_database_variables_the_checker_opens_directly():
    """`cert_expiry_check.py` has no HTTP client and no default for either
    path — `store.resolve_db` refuses an unset `LIBRARY_DB`, and
    `store.insert_signal`'s dedup check attaches `LIBRARY_COLD_DB` on every
    write this job makes, since every alert carries a `dedup_key`."""
    plist = _rendered(PLIST)
    env = plist["EnvironmentVariables"]
    assert env["LIBRARY_DB"]
    assert env["LIBRARY_COLD_DB"]
    assert env["LIBRARY_WRITES_ENABLED"] == "1"


def test_the_plist_carries_no_vectors_db():
    """`cert_expiry_check.py` never imports `vectors.py` — `ops-alert` is
    already in `vectors.KEPT_NOT_EMBEDDED_SIGNAL_KINDS`, so there is nothing
    for this job to embed and no reason for it to require
    `LIBRARY_VECTORS_DB` the way `cold_tier.py`'s sibling plist does."""
    env = _rendered(PLIST)["EnvironmentVariables"]
    assert "LIBRARY_VECTORS_DB" not in env


def test_the_plist_s_database_variables_match_the_service_s_own():
    """The one property that makes this job check the SAME vault the
    running service serves, rather than a differently-configured copy."""
    env = _rendered(PLIST)["EnvironmentVariables"]
    service_env = _rendered(SERVICE_PLIST)["EnvironmentVariables"]
    for variable in ("LIBRARY_DB", "LIBRARY_COLD_DB", "LIBRARY_WRITES_ENABLED"):
        assert env[variable] == service_env[variable], (
            f"{variable} differs between the checker's plist and the "
            f"service's own — the checker would act on a different database "
            f"than the one being served")


def test_library_cold_db_differs_from_library_db():
    env = _rendered(PLIST)["EnvironmentVariables"]
    assert env["LIBRARY_COLD_DB"] != env["LIBRARY_DB"]
