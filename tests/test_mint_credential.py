"""`mint_credential.py`: the local command that adds one credential to
`identity.TOKENS` or `identity.CLIENT_TOKENS` and prints it once.

**Every fixture builds the adopter's first-install condition**: neither
token file exists yet, and the directory that would hold them does not
either — the same shape `tests/test_bootstrap_library.py` builds for
`bootstrap_library.py`.
"""
from __future__ import annotations

import json
import re
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as libapp
import identity
import mint_credential
import projects
import protocol
import store

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def fresh_files(tmp_path, monkeypatch):
    """No credential of any kind on disk, in a directory that does not exist
    yet either."""
    state = tmp_path / "state" / ".library"
    monkeypatch.setattr(identity, "TOKENS", state / "api-tokens.json")
    monkeypatch.setattr(identity, "CLIENT_TOKENS", state / "client-tokens.json")
    assert not state.exists()
    return state


def _run(capsys, *argv):
    """Run the command; return `(exit_code, stdout, stderr)`."""
    rc = mint_credential.main(list(argv))
    out, err = capsys.readouterr()
    return rc, out, err


def _without_created_at(entry: dict) -> dict:
    """The entry minus its `created_at`, after checking that field is a
    well-formed UTC stamp; the tests that compare a whole entry compare the
    rest of it."""
    rest = dict(entry)
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", rest.pop("created_at"))
    return rest


@pytest.mark.parametrize("file", ["agents", "clients"])
def test_a_minted_entry_records_when_it_was_minted(fresh_files, capsys, file):
    """CQ·140: each entry carries its own minting time, so a credential's age
    is no longer read off the file's mtime, which is one value for every
    entry in the file."""
    before = identity.created_at_stamp()
    rc, out, _ = _run(capsys, "--file", file, "--name", "orient")
    after = identity.created_at_stamp()
    assert rc == 0
    path = identity.TOKENS if file == "agents" else identity.CLIENT_TOKENS
    entry = json.loads(path.read_text())[out.strip()]
    assert before <= entry["created_at"] <= after


def test_resolve_ignores_created_at_however_it_is_written(fresh_files):
    """`resolve()` never consults the field, so an entry with a malformed one
    authenticates exactly as an entry without one."""
    fresh_files.mkdir(parents=True)
    identity.TOKENS.write_text(json.dumps({
        "tok-none": {"name": "a", "products": ["*"]},
        "tok-junk": {"name": "b", "products": ["*"], "created_at": {"not": "a date"}},
    }))
    for token, actor in (("tok-none", "a"), ("tok-junk", "b")):
        ident = identity.resolve(f"Bearer {token}")
        assert ident is not None and ident.actor == actor


# --- the import-time trap `library#155`'s review caught ----------------------


def test_a_fresh_shell_with_neither_db_variable_set_does_not_crash(tmp_path):
    """`import identity` must not let `store`'s module-level `resolve_db()`
    raise before this command's own logic ever runs — the exact shape that
    made `init_vectors.py` crash with a raw `MisconfiguredDatabase`
    traceback instead of its own message (reviewer, library#155).

    A monkeypatch cannot exercise this: `store` is imported once per
    process, so a test running in the same session as every other test here
    would only ever see the placeholder `tests/conftest.py` already set.
    This spawns a real subprocess with neither `LIBRARY_DB` nor
    `ORIENT_CONTROL_DB` exported — the literal shell an adopter runs this
    command from for the first time.
    """
    result = subprocess.run(
        [sys.executable, str(REPO / "mint_credential.py"),
         "--file", "agents", "--name", "orient"],
        cwd=str(REPO), capture_output=True, text=True, check=False,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    assert result.returncode == 0, result.stderr
    assert "MisconfiguredDatabase" not in result.stderr
    assert "Traceback" not in result.stderr
    token = result.stdout.strip()
    assert token
    loaded = json.loads((tmp_path / ".library" / "api-tokens.json").read_text())
    assert _without_created_at(loaded[token]) == {"name": "orient", "products": ["*"]}


# --- minting into each file, read back through identity's own resolver -------


def test_minted_into_agents_resolves_as_agent(fresh_files, capsys):
    rc, out, _ = _run(capsys, "--file", "agents", "--name", "orient")
    assert rc == 0
    token = out.strip()
    ident = identity.resolve(f"Bearer {token}")
    assert ident is not None
    assert ident.kind == "agent"
    assert ident.actor == "orient"
    assert identity.in_scope(ident, "any-product-at-all")


def test_minted_into_agents_with_kind_system_resolves_as_system(fresh_files, capsys):
    rc, out, _ = _run(capsys, "--file", "agents", "--name", "the-poller",
                      "--kind", "system", "--products", "library,orient")
    assert rc == 0
    token = out.strip()
    ident = identity.resolve(f"Bearer {token}")
    assert ident is not None
    assert ident.kind == "system"
    assert ident.actor == "the-poller"
    assert ident.products == frozenset({"library", "orient"})
    assert not identity.in_scope(ident, "some-other-product")


def test_minted_into_clients_resolves_as_approved_client(fresh_files, capsys):
    rc, out, _ = _run(capsys, "--file", "clients", "--name", "orient")
    assert rc == 0
    token = out.strip()
    ident = identity.resolve(f"Bearer {token}")
    assert ident is not None
    assert ident.kind == "approved_client"
    assert ident.actor == "orient"
    assert identity.authorized(ident, "person.read")  # human-reserved, held by default


def test_clients_withholds_and_holds_reserved_are_honoured(fresh_files, capsys):
    rc, out, _ = _run(capsys, "--file", "clients", "--name", "the-bosun",
                      "--holds-reserved", "signal.ack",
                      "--withholds", "item.create")
    assert rc == 0
    token = out.strip()
    ident = identity.resolve(f"Bearer {token}")
    assert ident is not None
    assert identity.authorized(ident, "signal.ack")
    assert not identity.authorized(ident, "policy.write")  # excluded by holds_reserved
    assert not identity.authorized(ident, "item.create")  # withheld


# --- a --kind for the agents file cannot name approved_client ----------------


def test_kind_approved_client_for_the_agents_file_is_refused(fresh_files, capsys):
    with pytest.raises(SystemExit) as exc_info:
        mint_credential.main(["--file", "agents", "--name", "orient",
                              "--kind", "approved_client"])
    assert exc_info.value.code == 2
    out, _ = capsys.readouterr()
    assert out == "", "a refusal must not print a credential"
    assert not identity.TOKENS.exists()


def test_holds_reserved_for_the_agents_file_is_refused(fresh_files, capsys):
    rc, out, err = _run(capsys, "--file", "agents", "--name", "orient",
                        "--holds-reserved", "signal.ack")
    assert rc == 2
    assert out == ""
    assert "--file clients" in err
    assert not identity.TOKENS.exists()


def test_an_unknown_capability_in_withholds_is_written_and_resolves(
        fresh_files, capsys):
    """Shares `identity._entry_capabilities` with `identity._client_tokens()`
    (the exact function the load path calls), so this command's behaviour here
    tracks the load path's: a `withholds` entry naming a capability no role
    defines is written rather than refused (issue_01M339339P1WA80Q218SSTYKMT) —
    nobody can hold a capability no role defines, so withholding it is a no-op,
    not an error. Refusing it here would make minting a credential fail for
    exactly the entry the load path now accepts."""
    rc, out, err = _run(capsys, "--file", "clients", "--name", "orient",
                        "--withholds", "not-a-real-capability")
    assert rc == 0, err
    token = out.strip()
    ident = identity.resolve(f"Bearer {token}")
    assert ident is not None
    assert identity.capability_report(ident)["dead_withholds"] == [
        "not-a-real-capability"]


def test_holds_reserved_naming_a_non_reserved_capability_is_refused(
        fresh_files, capsys):
    rc, out, err = _run(capsys, "--file", "clients", "--name", "orient",
                        "--holds-reserved", "item.read")
    assert rc == 1
    assert out == ""
    assert "item.read" in err
    assert not identity.CLIENT_TOKENS.exists()


# --- --grants: --file agents only --------------------------------------------


def test_grants_is_written_and_resolves_as_the_agent_role_plus_the_grant(
        fresh_files, capsys):
    rc, out, err = _run(capsys, "--file", "agents", "--name", "review-post",
                        "--kind", "agent",
                        "--grants", "review_record.create")
    assert rc == 0, err
    token = out.strip()
    loaded = identity._tokens()
    assert loaded[token]["grants"] == ["review_record.create"]

    ident = identity.resolve(f"Bearer {token}")
    assert ident is not None
    resolved = identity.resolve_capabilities(ident)
    assert resolved.caps == identity.ROLE_CAPS["agent"] | {"review_record.create"}


def test_grants_an_unknown_capability_is_refused_and_writes_nothing(
        fresh_files, capsys):
    identity.TOKENS.parent.mkdir(parents=True)
    identity.TOKENS.write_text(json.dumps(
        {"an-existing-token": {"name": "deck", "products": ["*"]}}))
    identity.TOKENS.chmod(0o600)
    before = identity.TOKENS.read_bytes()

    rc, out, err = _run(capsys, "--file", "agents", "--name", "orient",
                        "--grants", "not-a-real-capability")
    assert rc == 1
    assert out == ""
    assert "not-a-real-capability" in err
    assert identity.TOKENS.read_bytes() == before


def test_grants_a_human_reserved_capability_is_refused_and_writes_nothing(
        fresh_files, capsys):
    identity.TOKENS.parent.mkdir(parents=True)
    identity.TOKENS.write_text(json.dumps(
        {"an-existing-token": {"name": "deck", "products": ["*"]}}))
    identity.TOKENS.chmod(0o600)
    before = identity.TOKENS.read_bytes()

    rc, out, err = _run(capsys, "--file", "agents", "--name", "orient",
                        "--grants", "signal.ack")
    assert rc == 1
    assert out == ""
    assert "signal.ack" in err
    assert identity.TOKENS.read_bytes() == before


def test_grants_roster_write_on_a_non_orchestrator_kind_is_refused_and_writes_nothing(
        fresh_files, capsys):
    identity.TOKENS.parent.mkdir(parents=True)
    identity.TOKENS.write_text(json.dumps(
        {"an-existing-token": {"name": "deck", "products": ["*"]}}))
    identity.TOKENS.chmod(0o600)
    before = identity.TOKENS.read_bytes()

    rc, out, err = _run(capsys, "--file", "agents", "--name", "the-poller",
                        "--kind", "system", "--grants", "roster.write")
    assert rc == 1
    assert out == ""
    assert "roster.write" in err
    assert identity.TOKENS.read_bytes() == before


def test_grants_for_the_clients_file_is_refused_and_writes_nothing(
        fresh_files, capsys):
    rc, out, err = _run(capsys, "--file", "clients", "--name", "orient",
                        "--grants", "review_record.create")
    assert rc == 2
    assert out == ""
    assert "--file agents" in err
    assert not identity.CLIENT_TOKENS.exists()


def test_empty_grants_writes_no_grants_key(fresh_files, capsys):
    rc, out, _ = _run(capsys, "--file", "agents", "--name", "orient",
                      "--grants", "")
    assert rc == 0
    token = out.strip()
    loaded = identity._tokens()
    assert "grants" not in loaded[token]


# --- --profile: --file agents only (issue_01M35QG0VANA5P1XE3PH6A7PS4) --------


def test_profile_is_written_and_resolves(fresh_files, capsys):
    rc, out, err = _run(capsys, "--file", "agents", "--name", "shipwright",
                        "--profile", "shipwright/daytime")
    assert rc == 0, err
    token = out.strip()
    loaded = identity._tokens()
    assert loaded[token]["profile"] == "shipwright/daytime"

    ident = identity.resolve(f"Bearer {token}")
    assert ident is not None
    assert ident.profile == "shipwright/daytime"


def test_no_profile_writes_no_profile_key(fresh_files, capsys):
    rc, out, _ = _run(capsys, "--file", "agents", "--name", "orient")
    assert rc == 0
    token = out.strip()
    loaded = identity._tokens()
    assert "profile" not in loaded[token]


def test_a_malformed_profile_is_refused_and_writes_nothing(fresh_files, capsys):
    identity.TOKENS.parent.mkdir(parents=True)
    identity.TOKENS.write_text(json.dumps(
        {"an-existing-token": {"name": "deck", "products": ["*"]}}))
    identity.TOKENS.chmod(0o600)
    before = identity.TOKENS.read_bytes()

    rc, out, err = _run(capsys, "--file", "agents", "--name", "orient",
                        "--profile", "no-slash-here")
    assert rc == 1
    assert out == ""
    assert "profile" in err
    assert identity.TOKENS.read_bytes() == before


def test_profile_for_the_clients_file_is_refused(fresh_files, capsys):
    rc, out, err = _run(capsys, "--file", "clients", "--name", "orient",
                        "--profile", "shipwright/daytime")
    assert rc == 2
    assert out == ""
    assert "--file agents" in err
    assert not identity.CLIENT_TOKENS.exists()


# --- --crew: --file agents only (CQ·10, the operator's ruling of 2026-10-15) ----------


def test_crew_is_written_and_resolves(fresh_files, capsys):
    rc, out, err = _run(capsys, "--file", "agents", "--name", "shipwright",
                        "--crew", "shipwright")
    assert rc == 0, err
    token = out.strip()
    loaded = identity._tokens()
    assert loaded[token]["crew"] == "shipwright"

    ident = identity.resolve(f"Bearer {token}")
    assert ident is not None
    assert ident.crew == "shipwright"


def test_no_crew_writes_no_crew_key(fresh_files, capsys):
    rc, out, _ = _run(capsys, "--file", "agents", "--name", "orient")
    assert rc == 0
    token = out.strip()
    loaded = identity._tokens()
    assert "crew" not in loaded[token]


def test_a_malformed_crew_is_refused_and_writes_nothing(fresh_files, capsys):
    identity.TOKENS.parent.mkdir(parents=True)
    identity.TOKENS.write_text(json.dumps(
        {"an-existing-token": {"name": "deck", "products": ["*"]}}))
    identity.TOKENS.chmod(0o600)
    before = identity.TOKENS.read_bytes()

    rc, out, err = _run(capsys, "--file", "agents", "--name", "orient",
                        "--crew", "Has-Upper")
    assert rc == 1
    assert out == ""
    assert "crew" in err
    assert identity.TOKENS.read_bytes() == before


def test_crew_for_the_clients_file_is_refused(fresh_files, capsys):
    rc, out, err = _run(capsys, "--file", "clients", "--name", "orient",
                        "--crew", "shipwright")
    assert rc == 2
    assert out == ""
    assert "--file agents" in err
    assert not identity.CLIENT_TOKENS.exists()


# --- round 2 (reviewer): an explicit --products "" must still be refused ----
#
# `--products` defaulting to `None` (so `--revoke` can tell it apart from an
# explicit empty value) must not change what an explicit empty value does on
# the mint path: `--products ""` used to be refused before `--revoke`
# existed, because `"".split(",")` filters to `[]`. Falling back to `"*"` on
# falsiness rather than on `is None` would silently widen scope instead.


@pytest.mark.parametrize("products", ["", ","])
def test_products_naming_nothing_is_refused_and_writes_nothing(
        fresh_files, capsys, products):
    rc, out, err = _run(capsys, "--file", "agents", "--name", "orient",
                        "--products", products)
    assert rc == 2
    assert out == ""
    assert "--products must name at least one product" in err
    assert not identity.TOKENS.exists()


# --- duplicate --name, and --replace -----------------------------------------


def test_a_duplicate_name_is_refused(fresh_files, capsys):
    rc, first_out, _ = _run(capsys, "--file", "agents", "--name", "orient")
    assert rc == 0
    first_token = first_out.strip()

    rc, out, err = _run(capsys, "--file", "agents", "--name", "orient")
    assert rc == 1
    assert out == "", "a refusal must not print a credential"
    assert "already has a credential" in err
    assert first_token not in err, "a refusal must not print another entry's token"
    loaded = identity._tokens()
    assert list(loaded.keys()) == [first_token]


def test_replace_swaps_the_token_and_keeps_the_name(fresh_files, capsys):
    rc, first_out, _ = _run(capsys, "--file", "agents", "--name", "orient")
    assert rc == 0
    first_token = first_out.strip()

    rc, second_out, _ = _run(capsys, "--file", "agents", "--name", "orient",
                             "--replace", "--kind", "system")
    assert rc == 0
    second_token = second_out.strip()
    assert second_token != first_token

    loaded = identity._tokens()
    assert list(loaded.keys()) == [second_token]
    assert _without_created_at(loaded[second_token]) == {
        "name": "orient", "products": ["*"], "kind": "system"}


# --- file safety: mode, atomicity, and other entries -------------------------


def test_the_file_and_its_new_directory_are_private(fresh_files, capsys):
    rc, _, _ = _run(capsys, "--file", "clients", "--name", "orient")
    assert rc == 0
    file_mode = stat.S_IMODE(identity.CLIENT_TOKENS.stat().st_mode)
    dir_mode = stat.S_IMODE(identity.CLIENT_TOKENS.parent.stat().st_mode)
    assert file_mode == 0o600, oct(file_mode)
    assert dir_mode == 0o700, oct(dir_mode)
    leftovers = [p.name for p in identity.CLIENT_TOKENS.parent.iterdir()
                 if p != identity.CLIENT_TOKENS]
    assert leftovers == [], f"a temporary file was left behind: {leftovers}"


def test_an_existing_wider_mode_file_is_refused(fresh_files, capsys):
    identity.TOKENS.parent.mkdir(parents=True)
    identity.TOKENS.write_text(json.dumps({}))
    identity.TOKENS.chmod(0o644)

    rc, out, err = _run(capsys, "--file", "agents", "--name", "orient")
    assert rc == 1
    assert out == ""
    assert "0o644" in err
    assert "wider than 0600" in err
    assert identity.TOKENS.read_text() == json.dumps({})


def test_invalid_json_in_the_target_file_is_refused(fresh_files, capsys):
    identity.TOKENS.parent.mkdir(parents=True)
    identity.TOKENS.write_text("not json at all {")
    identity.TOKENS.chmod(0o600)
    before = identity.TOKENS.read_bytes()

    rc, out, err = _run(capsys, "--file", "agents", "--name", "orient")
    assert rc == 1
    assert out == ""
    assert "not valid JSON" in err
    assert identity.TOKENS.read_bytes() == before


def test_non_object_json_in_the_target_file_is_refused(fresh_files, capsys):
    identity.TOKENS.parent.mkdir(parents=True)
    identity.TOKENS.write_text(json.dumps(["not", "an", "object"]))
    identity.TOKENS.chmod(0o600)
    before = identity.TOKENS.read_bytes()

    rc, out, err = _run(capsys, "--file", "agents", "--name", "orient")
    assert rc == 1
    assert out == ""
    assert "not a JSON object" in err
    assert identity.TOKENS.read_bytes() == before


def test_other_entries_survive_byte_for_byte_in_value(fresh_files, capsys):
    identity.TOKENS.parent.mkdir(parents=True)
    existing_entry = {"name": "deck", "products": ["library", "orient"],
                      "kind": "system"}
    identity.TOKENS.write_text(json.dumps({"an-existing-token": existing_entry}))
    identity.TOKENS.chmod(0o600)

    rc, out, _ = _run(capsys, "--file", "agents", "--name", "orient")
    assert rc == 0
    loaded = identity._tokens()
    assert loaded["an-existing-token"] == existing_entry
    assert out.strip() in loaded
    assert len(loaded) == 2


# --- --out writes the token to a file instead of stdout ---------------------


def test_out_writes_the_token_to_a_file_and_stdout_stays_empty(
        fresh_files, tmp_path, capsys):
    out = tmp_path / "orient-control" / "library-token"
    rc, out_text, err = _run(capsys, "--file", "agents", "--name", "orient",
                             "--out", str(out))
    assert rc == 0
    assert out_text == "", "stdout must stay empty when --out is given"
    assert str(out) in err
    token = out.read_text().rstrip("\n")
    ident = identity.resolve(f"Bearer {token}")
    assert ident is not None
    assert ident.actor == "orient"


def test_out_file_and_its_new_directory_are_private(fresh_files, tmp_path, capsys):
    out = tmp_path / "orient-control" / "library-token"
    rc, _, _ = _run(capsys, "--file", "agents", "--name", "orient",
                    "--out", str(out))
    assert rc == 0
    file_mode = stat.S_IMODE(out.stat().st_mode)
    dir_mode = stat.S_IMODE(out.parent.stat().st_mode)
    assert file_mode == 0o600, oct(file_mode)
    assert dir_mode == 0o700, oct(dir_mode)
    leftovers = [p.name for p in out.parent.iterdir() if p != out]
    assert leftovers == [], f"a temporary file was left behind: {leftovers}"


def test_out_refuses_an_existing_file_and_mints_nothing(
        fresh_files, tmp_path, capsys):
    out = tmp_path / "library-token"
    out.write_text("pre-existing content")

    rc, out_text, err = _run(capsys, "--file", "agents", "--name", "orient",
                             "--out", str(out))
    assert rc == 1
    assert out_text == ""
    assert "already exists" in err
    assert out.read_text() == "pre-existing content"
    assert not identity.TOKENS.exists(), "a refused --out must refuse minting too"


def test_out_replace_overwrites_the_existing_file(fresh_files, tmp_path, capsys):
    out = tmp_path / "library-token"
    out.write_text("stale-token\n")
    out.chmod(0o600)

    rc, out_text, err = _run(capsys, "--file", "agents", "--name", "orient",
                             "--out", str(out), "--replace")
    assert rc == 0
    assert out_text == ""
    token = out.read_text().rstrip("\n")
    assert token != "stale-token"
    ident = identity.resolve(f"Bearer {token}")
    assert ident is not None
    assert ident.actor == "orient"


# --- --out: ordering, store collision, dangling symlink, ancestor modes -----
#
# Every test below is a real subprocess against a temp HOME, like the
# import-time regression test above: the ordering and rollback guarantees
# these findings are about only bind at the process boundary, and a
# monkeypatch inside this test process can't exercise "the store commit runs
# after --out, and rolls it back on failure" the way a fresh interpreter can.


def _run_sub(tmp_path, *argv):
    """Run mint_credential.py as a subprocess with HOME pointed at
    `tmp_path` and neither token-file env var set, so both stores resolve
    under `tmp_path/.library` — the same shape
    `test_a_fresh_shell_with_neither_db_variable_set_does_not_crash` uses."""
    return subprocess.run(
        [sys.executable, str(REPO / "mint_credential.py"), *argv],
        cwd=str(REPO), capture_output=True, text=True, check=False,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})


def test_out_a_directory_at_path_is_refused_and_the_store_is_untouched(tmp_path):
    store_path = tmp_path / ".library" / "api-tokens.json"
    store_path.parent.mkdir(mode=0o700)
    store_path.write_text(json.dumps({"an-existing-token":
                                      {"name": "deck", "products": ["*"]}}))
    store_path.chmod(0o600)
    before = store_path.read_bytes()

    out_dir = tmp_path / "out-is-a-directory"
    out_dir.mkdir()

    result = _run_sub(tmp_path, "--file", "agents", "--name", "orient",
                      "--out", str(out_dir))

    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    assert store_path.read_bytes() == before
    assert out_dir.is_dir()


def test_out_equal_to_the_store_path_is_refused_and_the_store_is_unchanged(
        tmp_path):
    store_path = tmp_path / ".library" / "api-tokens.json"
    store_path.parent.mkdir(mode=0o700)
    store_path.write_text(json.dumps({"an-existing-token":
                                      {"name": "deck", "products": ["*"]}}))
    store_path.chmod(0o600)
    before = store_path.read_bytes()

    result = _run_sub(tmp_path, "--file", "agents", "--name", "orient",
                      "--out", str(store_path), "--replace")

    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    assert "credential store" in result.stderr
    assert store_path.read_bytes() == before


def test_out_a_dangling_symlink_at_path_is_refused(tmp_path):
    link = tmp_path / "dangling-link"
    link.symlink_to(tmp_path / "nowhere")

    result = _run_sub(tmp_path, "--file", "agents", "--name", "orient",
                      "--out", str(link))

    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    assert "symlink" in result.stderr
    assert not (tmp_path / ".library" / "api-tokens.json").exists()
    assert link.is_symlink() and not link.exists()


def test_out_missing_ancestor_directories_end_up_0700(tmp_path):
    out = tmp_path / "a" / "b" / "c" / "library-token"

    result = _run_sub(tmp_path, "--file", "agents", "--name", "orient",
                      "--out", str(out))

    assert result.returncode == 0, result.stderr
    for ancestor in (tmp_path / "a", tmp_path / "a" / "b",
                     tmp_path / "a" / "b" / "c"):
        assert stat.S_IMODE(ancestor.stat().st_mode) == 0o700, ancestor


def test_out_a_successful_mint_leaves_both_files_private_and_authenticates(
        tmp_path, monkeypatch):
    out = tmp_path / "orient-control" / "library-token"

    result = _run_sub(tmp_path, "--file", "agents", "--name", "orient",
                      "--out", str(out))
    assert result.returncode == 0, result.stderr

    store_path = tmp_path / ".library" / "api-tokens.json"
    assert store_path.exists()
    assert out.exists()
    assert stat.S_IMODE(store_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(out.stat().st_mode) == 0o600

    token = out.read_text().rstrip("\n")
    monkeypatch.setattr(identity, "TOKENS", store_path)
    ident = identity.resolve(f"Bearer {token}")
    assert ident is not None
    assert ident.actor == "orient"


# --- --out --replace: a store-commit failure must not cost the old file ----
#
# round 3 regression (reviewer, library#156): a --replace whose --out file
# already held a working credential, followed by a store-commit failure,
# deleted that file instead of restoring it — the store still served the old
# credential, so a working deployment lost its token file. Both tests here
# force the store commit to fail by revoking write permission on the store's
# own directory, so `_commit`'s `tempfile.mkstemp` fails closed; they are
# subprocess tests, like the section above, since the ordering and rollback
# guarantees only bind at the process boundary.


def test_out_replace_store_commit_failure_restores_the_previous_out_file(
        tmp_path):
    store_dir = tmp_path / ".library"
    store_dir.mkdir(mode=0o700)
    store_path = store_dir / "api-tokens.json"
    store_path.write_text(json.dumps({"an-existing-token":
                                      {"name": "deck", "products": ["*"]}}))
    store_path.chmod(0o600)
    before_store = store_path.read_bytes()

    out = tmp_path / "library-token"
    out.write_text("stale-but-valid-token\n")
    out.chmod(0o600)
    before_out = out.read_bytes()

    store_dir.chmod(0o500)  # _commit's mkstemp cannot create a file here
    try:
        result = _run_sub(tmp_path, "--file", "agents", "--name", "orient",
                          "--out", str(out), "--replace")
    finally:
        store_dir.chmod(0o700)

    assert result.returncode != 0, result.stdout
    assert "Traceback" not in result.stderr
    assert out.read_bytes() == before_out, (
        "a store-commit failure must restore --out, not delete it")
    assert store_path.read_bytes() == before_store
    leftovers = [p.name for p in tmp_path.iterdir()
                 if p.name not in (".library", "library-token")]
    assert leftovers == [], f"a temporary file was left behind: {leftovers}"
    leftovers_in_store = [p.name for p in store_dir.iterdir()
                          if p.name != "api-tokens.json"]
    assert leftovers_in_store == [], (
        f"a temporary file was left behind: {leftovers_in_store}")


def test_out_replace_store_commit_success_removes_backup_and_authenticates(
        tmp_path, monkeypatch):
    store_dir = tmp_path / ".library"
    store_dir.mkdir(mode=0o700)
    store_path = store_dir / "api-tokens.json"
    store_path.write_text(json.dumps({}))
    store_path.chmod(0o600)

    out = tmp_path / "library-token"
    out.write_text("stale-token\n")
    out.chmod(0o600)

    result = _run_sub(tmp_path, "--file", "agents", "--name", "orient",
                      "--out", str(out), "--replace")

    assert result.returncode == 0, result.stderr
    token = out.read_text().rstrip("\n")
    assert token != "stale-token"
    leftovers = [p.name for p in tmp_path.iterdir()
                 if p.name not in (".library", "library-token")]
    assert leftovers == [], f"a temporary file was left behind: {leftovers}"
    leftovers_in_store = [p.name for p in store_dir.iterdir()
                          if p.name != "api-tokens.json"]
    assert leftovers_in_store == [], (
        f"a temporary file was left behind: {leftovers_in_store}")

    monkeypatch.setattr(identity, "TOKENS", store_path)
    ident = identity.resolve(f"Bearer {token}")
    assert ident is not None
    assert ident.actor == "orient"


# --- --out --replace: no chmod on the backup, ever -------------------------
#
# round 4 findings (both reviewers, library#156): `_write_out` used to
# `os.chmod(backup, 0o600)` after renaming PATH's previous contents aside.
# `chmod` follows a symlink to its target, so a symlinked PATH under
# --replace silently relocked the target's permissions rather than the
# link's. The fix removes that chmod entirely by requiring PATH to already
# be a private regular file before any of this runs — these three tests
# reproduce the two ways it could have been wider, plus the double-fault
# case the security finding raised, and are subprocess tests like the
# section above for the same reason.


def test_out_replace_with_a_symlink_at_path_is_refused_and_the_target_is_unchanged(
        tmp_path):
    target = tmp_path / "real-target"
    target.write_text("target-token\n")
    target.chmod(0o644)  # deliberately wide: proves this is never chmod'd
    before_target = target.read_bytes()

    link = tmp_path / "library-token"
    link.symlink_to(target)

    result = _run_sub(tmp_path, "--file", "agents", "--name", "orient",
                      "--out", str(link), "--replace")

    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    assert "symlink" in result.stderr
    assert not (tmp_path / ".library" / "api-tokens.json").exists()
    assert link.is_symlink()
    assert target.read_bytes() == before_target
    assert stat.S_IMODE(target.stat().st_mode) == 0o644, (
        "a symlinked --out under --replace must never touch the target's mode")


def test_out_replace_with_path_wider_than_0600_is_refused_and_unchanged(
        tmp_path):
    out = tmp_path / "library-token"
    out.write_text("stale-token\n")
    out.chmod(0o644)
    before = out.read_bytes()

    result = _run_sub(tmp_path, "--file", "agents", "--name", "orient",
                      "--out", str(out), "--replace")

    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    assert "0o644" in result.stderr
    assert "wider than 0600" in result.stderr
    assert out.read_bytes() == before
    assert stat.S_IMODE(out.stat().st_mode) == 0o644
    assert not (tmp_path / ".library" / "api-tokens.json").exists()


def test_out_replace_double_fault_names_the_backup_and_prints_no_traceback(
        tmp_path):
    """A second fault — the rename-back after the mint write itself failed
    also fails — must exit non-zero, name the backup's full path, and never
    print a bare traceback. Forced by monkeypatching `_atomic_write` (in a
    subprocess, since the ordering only binds at the process boundary) to
    make --out's directory unwritable and then fail, so `_write_out`'s own
    `os.replace(backup, path)` restore fails the same way."""
    store_dir = tmp_path / ".library"
    store_dir.mkdir(mode=0o700)
    (store_dir / "api-tokens.json").write_text(json.dumps({}))
    (store_dir / "api-tokens.json").chmod(0o600)

    out_dir = tmp_path / "out-dir"
    out_dir.mkdir(mode=0o700)
    out = out_dir / "library-token"
    out.write_text("stale-but-valid-token\n")
    out.chmod(0o600)
    before_out = out.read_bytes()

    script = f"""
import os, sys
sys.path.insert(0, {str(REPO)!r})
os.environ.setdefault("LIBRARY_DB", "/nonexistent/mint-credential-placeholder.db")
import mint_credential

real_atomic_write = mint_credential._atomic_write
out_path = {str(out)!r}
out_dir = {str(out_dir)!r}

def failing_atomic_write(path, data):
    if str(path) == out_path:
        os.chmod(out_dir, 0o500)  # PATH's directory: revoke write now
        raise OSError(28, "simulated disk full")
    return real_atomic_write(path, data)

mint_credential._atomic_write = failing_atomic_write
rc = mint_credential.main(["--file", "agents", "--name", "orient",
                           "--out", out_path, "--replace"])
os.chmod(out_dir, 0o700)
sys.exit(rc)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(REPO), capture_output=True, text=True, check=False,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})

    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    backup = out_dir / "library-token.mint-backup"
    assert str(backup) in result.stderr
    assert "still-valid" in result.stderr
    assert backup.read_bytes() == before_out
    assert json.loads((store_dir / "api-tokens.json").read_text()) == {}


# --- the round-4 success and failure tests still pass ------------------------
#
# `test_out_replace_store_commit_failure_restores_the_previous_out_file` and
# `test_out_replace_store_commit_success_removes_backup_and_authenticates`,
# above, exercise the ordinary (non-double-fault) restore-succeeds and
# no-restore-needed paths; both already chmod --out to 0600 before running,
# so the new `_check_out_replace_target` precondition does not change their
# outcome, and they are left as they were.


# --- the token is the one thing on stdout ------------------------------------


def test_stdout_is_the_token_alone_and_status_goes_to_stderr(fresh_files, capsys):
    rc, out, err = _run(capsys, "--file", "agents", "--name", "orient")
    assert rc == 0
    assert out.count("\n") == 1 and out.strip() == out.rstrip("\n")
    assert "orient" in err
    assert str(identity.TOKENS) in err
    assert out.strip() not in err, "the credential belongs on stdout only"


# --- --revoke: removes exactly the named entry -------------------------------


def test_revoke_removes_exactly_that_entry_and_keeps_others(fresh_files, capsys):
    rc, kept_out, _ = _run(capsys, "--file", "agents", "--name", "deck")
    assert rc == 0
    kept_token = kept_out.strip()

    rc, gone_out, _ = _run(capsys, "--file", "agents", "--name", "orient")
    assert rc == 0
    gone_token = gone_out.strip()

    before = identity.TOKENS.read_bytes()

    rc, out, err = _run(capsys, "--file", "agents", "--revoke", "--name", "orient")
    assert rc == 0, err
    assert out == ""
    assert "orient" in err

    loaded = identity._tokens()
    assert list(loaded.keys()) == [kept_token]
    assert _without_created_at(loaded[kept_token]) == {"name": "deck", "products": ["*"]}
    assert gone_token not in loaded
    assert identity.resolve(f"Bearer {gone_token}") is None
    assert identity.resolve(f"Bearer {kept_token}") is not None
    # The surviving entry's bytes are untouched, not just its parsed value.
    after = json.loads(identity.TOKENS.read_bytes())
    assert after[kept_token] == json.loads(before)[kept_token]


def test_revoke_from_the_clients_file_removes_the_entry_there(fresh_files, capsys):
    rc, out, _ = _run(capsys, "--file", "clients", "--name", "the-bosun")
    assert rc == 0
    token = out.strip()

    rc, out, err = _run(capsys, "--file", "clients", "--revoke",
                        "--name", "the-bosun")
    assert rc == 0, err
    assert out == ""
    loaded = json.loads(identity.CLIENT_TOKENS.read_text())
    assert loaded == {}
    assert identity.resolve(f"Bearer {token}") is None


# --- --revoke: refused when the name is absent or ambiguous -------------------


def test_revoke_an_absent_name_is_refused_and_writes_nothing(fresh_files, capsys):
    rc, out, _ = _run(capsys, "--file", "agents", "--name", "orient")
    assert rc == 0
    before = identity.TOKENS.read_bytes()

    rc, out, err = _run(capsys, "--file", "agents", "--revoke",
                        "--name", "no-such-project")
    assert rc != 0
    assert out == ""
    assert "no-such-project" in err
    assert identity.TOKENS.read_bytes() == before


def test_revoke_an_existing_wider_mode_file_is_refused_and_unchanged(
        fresh_files, capsys):
    """`_revoke_entry` must run the same `_check_mode` guard the mint path
    runs (round 2, reviewer): a store file already wider than 0600 halts
    revoke too, rather than being silently rewritten at 0600 by
    `_atomic_write`."""
    identity.TOKENS.parent.mkdir(parents=True)
    identity.TOKENS.write_text(json.dumps(
        {"an-existing-token": {"name": "orient", "products": ["*"]}}))
    identity.TOKENS.chmod(0o644)
    before = identity.TOKENS.read_bytes()

    rc, out, err = _run(capsys, "--file", "agents", "--revoke",
                        "--name", "orient")
    assert rc == 1
    assert out == ""
    assert "0o644" in err
    assert "wider than 0600" in err
    assert identity.TOKENS.read_bytes() == before


def test_revoke_with_no_file_at_all_is_refused_and_writes_nothing(
        fresh_files, capsys):
    assert not identity.TOKENS.exists()
    rc, out, err = _run(capsys, "--file", "agents", "--revoke",
                        "--name", "orient")
    assert rc != 0
    assert out == ""
    assert "orient" in err
    assert not identity.TOKENS.exists()


def test_revoke_a_duplicated_name_is_refused_and_writes_nothing_and_names_neither_token(
        fresh_files, capsys):
    identity.TOKENS.parent.mkdir(parents=True)
    duplicated = {
        "token-one": {"name": "orient", "products": ["*"]},
        "token-two": {"name": "orient", "products": ["vivy"]},
    }
    identity.TOKENS.write_text(json.dumps(duplicated))
    identity.TOKENS.chmod(0o600)
    before = identity.TOKENS.read_bytes()

    rc, out, err = _run(capsys, "--file", "agents", "--revoke",
                        "--name", "orient")
    assert rc != 0
    assert out == ""
    assert "2" in err
    assert "orient" in err
    assert "token-one" not in err and "token-two" not in err
    assert identity.TOKENS.read_bytes() == before


# --- --revoke: incompatible with every mint-only option -----------------------


@pytest.mark.parametrize("extra", [
    ["--out", "/tmp/wherever"],
    ["--replace"],
    ["--products", "vivy"],
    ["--kind", "system"],
    ["--grants", "review_record.create"],
    ["--profile", "shipwright/daytime"],
    ["--crew", "shipwright"],
    ["--holds-reserved", "signal.ack"],
    ["--withholds", "item.create"],
])
def test_revoke_rejects_every_mint_only_option_at_parse_time(
        fresh_files, capsys, extra):
    rc, out, err = _run(capsys, "--file", "agents", "--revoke",
                        "--name", "orient", *extra)
    assert rc == 2
    assert out == ""
    assert extra[0] in err
    assert not identity.TOKENS.exists()


# --- --revoke: an OSError from the write is a clean refusal, not a traceback -


def test_revoke_an_oserror_from_the_write_is_a_clean_refusal(
        fresh_files, monkeypatch, capsys):
    """round 2 (reviewer): the revoke branch of `main()` must catch an
    `OSError` from `_atomic_write` the way the mint path's `_commit` call
    does, rather than letting it escape as a raw traceback."""
    rc, out, _ = _run(capsys, "--file", "agents", "--name", "orient")
    assert rc == 0
    token = out.strip()
    before = identity.TOKENS.read_bytes()

    def failing_atomic_write(path, data):
        raise OSError(28, "simulated disk full")

    monkeypatch.setattr(mint_credential, "_atomic_write", failing_atomic_write)

    rc, out, err = _run(capsys, "--file", "agents", "--revoke",
                        "--name", "orient")
    assert rc == 1
    assert out == ""
    assert "Traceback" not in err
    assert token not in err
    assert identity.TOKENS.read_bytes() == before


# --- --revoke: no token ever reaches stdout, stderr, or a raised message -----


def test_revoke_never_prints_any_token_on_any_path(fresh_files, capsys):
    rc, survivor_out, _ = _run(capsys, "--file", "agents", "--name", "deck")
    survivor_token = survivor_out.strip()
    rc, gone_out, _ = _run(capsys, "--file", "agents", "--name", "orient")
    gone_token = gone_out.strip()

    # the successful revoke
    rc, out, err = _run(capsys, "--file", "agents", "--revoke", "--name", "orient")
    assert rc == 0
    assert survivor_token not in out and survivor_token not in err
    assert gone_token not in out and gone_token not in err

    # the absent-name refusal
    rc, out, err = _run(capsys, "--file", "agents", "--revoke",
                        "--name", "no-such-name")
    assert survivor_token not in out and survivor_token not in err

    # the duplicated-name refusal
    duplicated = {
        "token-dup-one": {"name": "dup", "products": ["*"]},
        "token-dup-two": {"name": "dup", "products": ["*"]},
    }
    merged = {**json.loads(identity.TOKENS.read_text()), **duplicated}
    identity.TOKENS.write_text(json.dumps(merged))
    rc, out, err = _run(capsys, "--file", "agents", "--revoke", "--name", "dup")
    assert rc != 0
    assert "token-dup-one" not in out and "token-dup-one" not in err
    assert "token-dup-two" not in out and "token-dup-two" not in err
    assert survivor_token not in out and survivor_token not in err

    # the mutually-exclusive-option refusal
    rc, out, err = _run(capsys, "--file", "agents", "--revoke",
                        "--name", "deck", "--replace")
    assert survivor_token not in out and survivor_token not in err


# --- --revoke end to end: a revoked credential is refused on its next request
#
# The test the operator named (knowledge#800): within ONE running app instance, with
# no restart and no module reload, a credential that authenticated once is
# refused 401 on the very next request after `--revoke` removes its entry —
# `identity._tokens()` re-reads the file per request. The fixture here is the
# same shape `tests/test_write_surface.py`'s `client` fixture builds.


@pytest.fixture
def app_with_writes(fresh_files, tmp_path, monkeypatch, vector_index):
    db = tmp_path / "control.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE signals (
            id TEXT PRIMARY KEY, source TEXT, stream TEXT, kind TEXT, actor TEXT,
            ingested_by TEXT, product TEXT, refs TEXT, body TEXT, meta TEXT,
            privacy_class TEXT, produced_at TEXT, ingested_at TEXT,
            embedded_at TEXT, dedup_key TEXT UNIQUE, acting_human TEXT);
        CREATE TABLE black_box_registrations (
            id INTEGER PRIMARY KEY, crew TEXT NOT NULL UNIQUE,
            model TEXT NOT NULL, gateway TEXT NOT NULL,
            created_by TEXT NOT NULL, created_at TEXT NOT NULL,
            updated_by TEXT NOT NULL, updated_at TEXT NOT NULL);
    """)
    conn.commit()
    conn.close()
    vector_index(db)
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)

    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({"_meta": {}, "vivy": {"repos": []}}))
    monkeypatch.setattr(projects, "REGISTRY_PATH", registry)
    projects.reload()
    yield TestClient(libapp.app)
    projects.reload()


def _signal_headers(token):
    return {"Authorization": f"Bearer {token}",
            protocol.HEADER: str(protocol.CURRENT)}


def test_a_revoked_credential_is_refused_401_on_its_next_request(
        app_with_writes, capsys):
    client = app_with_writes
    rc, out, _ = _run(capsys, "--file", "agents", "--name", "a-builder-project")
    assert rc == 0
    token = out.strip()

    body = {"source": "github", "kind": "note", "actor": "a-builder-project",
            "product": "vivy", "body": "a thing happened"}
    first = client.post("/signals", json=body, headers=_signal_headers(token))
    assert first.status_code == 201, first.text

    rc, revoke_out, revoke_err = _run(
        capsys, "--file", "agents", "--revoke", "--name", "a-builder-project")
    assert rc == 0, revoke_err
    assert revoke_out == ""

    second = client.post("/signals", json=body, headers=_signal_headers(token))
    assert second.status_code == 401, second.text
