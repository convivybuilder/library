"""The cold-tier mover's launchd template — the same shape of check
`test_ci_result_fetch.py` runs for its own plist, since this one is a
poll of the same kind: deploy-pinned, not a kept-alive server, state paths
outside the deploy area.

`cold_tier.py` differs from `ci_result_fetch.py` in one load-bearing way:
it opens `LIBRARY_DB` and `LIBRARY_COLD_DB` directly through `store.py`
rather than posting over HTTP, so this plist also needs the same database
configuration the service's own plist deploys. A value that diverged from
`com.convivy.library.plist`'s would move a signal cold in one database
while the running service reads another.
"""
import os
import plistlib
import subprocess
from pathlib import Path

LAUNCHD = Path(__file__).resolve().parent.parent / "launchd"
PLIST = LAUNCHD / "com.convivy.library.cold-tier.plist"
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


def test_the_plist_is_a_poll_not_a_kept_alive_server():
    plist = _rendered(PLIST)
    assert "StartInterval" in plist
    assert "KeepAlive" not in plist, (
        "this job is expected to exit every run; KeepAlive would fight that")


def test_the_plist_s_state_paths_are_outside_the_deploy_area():
    plist = _rendered(PLIST)
    for key in ("StandardOutPath", "StandardErrorPath"):
        assert plist[key].startswith(f"{FIXED_HOME}/.library/"), (
            f"{key} points inside the deploy area, which an atomic install "
            f"swap replaces wholesale and would destroy a log living in it")


def test_the_plist_sets_the_database_variables_the_mover_opens_directly():
    """`cold_tier.py` has no HTTP client and no default for either path —
    `store.resolve_db` and `store.resolve_cold_db_path` both refuse an
    unset variable — so a job missing either one fails at import on every
    scheduled run rather than skipping silently."""
    plist = _rendered(PLIST)
    env = plist["EnvironmentVariables"]
    assert env["LIBRARY_DB"]
    assert env["LIBRARY_COLD_DB"]
    assert env["LIBRARY_WRITES_ENABLED"] == "1"


def test_the_plist_s_database_variables_match_the_service_s_own():
    """The one property that makes this job move the SAME vault the running
    service serves, rather than a differently-configured copy of it."""
    env = _rendered(PLIST)["EnvironmentVariables"]
    service_env = _rendered(SERVICE_PLIST)["EnvironmentVariables"]
    for variable in ("LIBRARY_DB", "LIBRARY_COLD_DB", "LIBRARY_VECTORS_DB",
                     "LIBRARY_WRITES_ENABLED"):
        assert env[variable] == service_env[variable], (
            f"{variable} differs between the mover's plist and the service's "
            f"own — the mover would act on a different database than the one "
            f"being served")


def test_library_cold_db_differs_from_library_db():
    """The one serious misconfiguration the build plan names and declines
    to add a runtime refusal for (archives-cold-tier.md, "Considered and
    not added") — checked here instead, once, on the one file that sets
    both. A copied line is the only way the two could end up equal."""
    env = _rendered(PLIST)["EnvironmentVariables"]
    assert env["LIBRARY_COLD_DB"] != env["LIBRARY_DB"]


def test_the_service_plist_sets_library_cold_db():
    """app.py resolves LIBRARY_COLD_DB at import with no default, so a
    service plist without it crash-loops on start. Library#181 shipped that
    requirement without the variable here, and the 2026-10-01 install of
    cd9693a went down until it was rolled back."""
    service_env = _rendered(SERVICE_PLIST)["EnvironmentVariables"]
    assert service_env.get("LIBRARY_COLD_DB")
    assert service_env["LIBRARY_COLD_DB"] != service_env["LIBRARY_DB"]
