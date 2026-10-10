"""The launchd job definition, checked against the values it claims to deploy.

**A COMMENT EXISTS SO A READER DOES NOT HAVE TO GO LOOK, WHICH IS WHAT MAKES A
STALE ONE WORSE THAN NONE.** ADR-0016 requires a file shipping a
configuration-gated control to name the value it deploys, and this plist does —
in prose, which nothing checked. The write cutover at about 04:00 on 2026-08-17
set `LIBRARY_DB` and `LIBRARY_WRITES_ENABLED` on the running job and left the
file saying both were UNSET, where it stayed for nine days
(issue_01M0XTPJVVRY6KR49MR7ZGWYCN). A convivy-deck co session read it, concluded
the Library refuses every write, and built a change around a 503 that does not
happen.

**These tests compare the PROSE against the DICT, which is the pair that drifted.**
Asserting only that the dict sets the two variables would have passed on the day
the comment went stale, and asserting only that the comment names them would have
passed on the day someone edited the dict. The claim and the thing claimed have
to be read together or the check is a ritual.

**What this CANNOT check is the loaded job**, which is the third party to the
disagreement and the only one that is actually serving. `launchctl print
gui/501/com.convivy.library` reports it; CI has no launchd and no such job. So
this file pins the checkout to its own statement about itself, and the plist's
own comment carries the command for the rest.
"""
import os
import plistlib
import re
import subprocess
from pathlib import Path

import pytest

import app

PLIST = Path(__file__).resolve().parent.parent / "launchd" / "com.convivy.library.plist"
INSTALLER = Path(__file__).resolve().parent.parent / "bin" / "install-library"

# A synthetic, fixed home — never this developer's own — that this module
# renders the checked-in template against, through install-library's own
# `--internal-render-plist` entry point (never a reimplementation of the
# substitution): launchd does not expand `$HOME` or `~`, so the file in the
# repo carries a placeholder, not a literal path, and this is what
# install-library would write to ~/Library/LaunchAgents on a machine whose
# home is this one.
FIXED_HOME = "/srv/fixture-home"

# The prose form ADR-0016 asks for, in a shape a regex can read back:
# `THE DEPLOYED VALUE OF <VARIABLE> IS "<value>"`.
CLAIM = re.compile(r'THE DEPLOYED VALUE OF ([A-Z_][A-Z0-9_]*) IS "([^"]*)"')


@pytest.fixture(scope="module")
def rendered_text():
    result = subprocess.run(
        [str(INSTALLER), "--internal-render-plist", str(PLIST)],
        env={**os.environ, "HOME": FIXED_HOME},
        capture_output=True, text=True, timeout=10, check=True)
    return result.stdout


@pytest.fixture(scope="module")
def plist(rendered_text):
    return plistlib.loads(rendered_text.encode())


@pytest.fixture(scope="module")
def claims(rendered_text):
    found = dict(CLAIM.findall(rendered_text))
    assert found, (
        "no `THE DEPLOYED VALUE OF X IS \"y\"` claim found — either ADR-0016's "
        "statement was dropped from this file or its wording changed, and this "
        "whole module now passes by finding nothing to check")
    return found


def test_the_job_sets_the_two_variables_the_service_has_no_default_for(plist):
    """`store.resolve_db` has no fallback for `LIBRARY_DB`, so a job that omits it
    starts a Library that refuses to serve rather than one that quietly picks a
    file. `LIBRARY_WRITES_ENABLED` alone would be the same refusal."""
    env = plist["EnvironmentVariables"]
    assert env["LIBRARY_DB"] == f"{FIXED_HOME}/.library/library.db"
    assert env["LIBRARY_WRITES_ENABLED"] == "1"


def test_every_deployed_value_the_comment_names_matches_the_dict(plist, claims):
    """**THE ASSERTION THAT WOULD HAVE CAUGHT THE NINE-DAY DRIFT.** The comment
    and the dict are two statements of one fact, and only one of them is what
    launchd reads."""
    env = plist["EnvironmentVariables"]
    for variable, claimed in claims.items():
        assert variable in env, (
            f"the comment names a deployed value for {variable} that the dict "
            f"does not set — the file contradicts itself")
        assert env[variable] == claimed, (
            f"the comment says {variable} deploys as {claimed!r}, the dict sets "
            f"{env[variable]!r}")


def test_the_comment_names_every_variable_the_dict_sets(plist, claims):
    """The other direction, and it is not symmetric with the test above. A
    variable added to the dict and never written into the comment is the drift
    running the other way: the deployed configuration changes and the statement
    a reader trusts stays true-but-incomplete, which reads as complete.

    `PATH` and `HOME` are exempt by name. They are process hygiene rather than a
    control gated by configuration, so ADR-0016 does not ask this file to state
    them, and requiring it would make the rule noise."""
    stated_elsewhere = {"PATH", "HOME"}
    for variable in plist["EnvironmentVariables"]:
        if variable in stated_elsewhere:
            continue
        assert variable in claims, (
            f"the dict sets {variable} and no comment states its deployed value "
            f"(ADR-0016)")


def test_exit_timeout_outlives_the_quiesce_drain_deadline(plist):
    """`bin/install-library`'s reload path sends this job a plain SIGTERM
    (`launchctl bootout`) to take it down before staging a new release.
    uvicorn's own graceful shutdown drains that exactly like the quiesce
    watcher's own drain does (`serve_dual._GRACEFUL_SHUTDOWN_SECONDS` is
    `app._DRAIN_DEADLINE_SECONDS` by reference), but launchd's own
    `ExitTimeOut` (capital O — `man launchd.plist`) is what actually enforces
    a ceiling on that wait, and a ceiling shorter than the drain it is
    supposed to outlive SIGKILLs a legitimate in-flight write before it
    finishes."""
    assert plist["ExitTimeOut"] > app._DRAIN_DEADLINE_SECONDS


def test_exit_timeout_does_not_exceed_the_observed_launchd_clamp(plist):
    """60 is the effective ceiling `launchctl print` reports for this
    gui-domain LaunchAgent on this machine (issue_01M3N7NB9218HXKQDPD3NF1BJH):
    a plist value of 75 was silently clamped down to 60 by launchd itself, so
    75 read correctly from this file while never being the number applied.
    A value above 60 here would reintroduce exactly that gap between what
    this file says and what launchd does — this test pins the plist to the
    number that is actually enforced, not a larger one that merely parses."""
    assert plist["ExitTimeOut"] <= 60


def test_no_misspelled_variant_of_exittimeout_key_survives(plist):
    """Round 4 finding #1, PR #157: this file spelled the key `ExitTimeout`
    (lowercase o) for three review rounds. launchd does not refuse an
    unrecognized key — it silently ignores it and falls back to its own
    default — so nothing short of checking the exact spelling would have
    caught this: `launchctl print gui/501/com.convivy.library` read `exit
    timeout = 5` the entire time, never the 75 this file claimed to set.
    Only the correctly-spelled key may appear under any casing of
    "exittimeout"."""
    keys_matching = [k for k in plist if re.match(r'(?i)^exittimeout$', k)]
    assert keys_matching == ["ExitTimeOut"], (
        f"expected only the correctly-spelled key ExitTimeOut, found: {keys_matching}")


def test_the_plist_still_carries_the_check_that_reads_the_loaded_job():
    """The one thing a test cannot do, so the file has to carry it. Without the
    command written down, a reader comparing the checkout against reality has to
    know that `launchctl print` is where the loaded environment lives — and the
    reader who needs it most is the one who does not."""
    text = PLIST.read_text()
    assert "launchctl print gui/501/com.convivy.library" in text
    assert "bootout" in text and "kickstart" in text, (
        "the file no longer says which restart preserves the loaded environment "
        "and which replaces it from this file, which is the difference between "
        "a redeploy that keeps writes on and one that silently turns them off")
