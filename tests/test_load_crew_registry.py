"""`load_crew_registry.py`: the one-time operator load of the roster and
per-project profiles (issue_01M19QP8S0QPKM0MN7XHYWNGKY).

**Against small fixture trees, not the real `~/Convivy/knowledge` or
`~/Convivy/agent-platform` checkouts.** Neither exists in CI — only this
repository is checked out there — which is exactly why the migration
itself does not read them (see the migration's own docstring). What this
tests is the parsing and the call into `crew_registry`, which is real work
this script does regardless of which checkout it points at.
"""
from __future__ import annotations

import sqlite3
import textwrap

import pytest

import load_crew_registry
import store

ACTOR = "test, 2026-09-06"


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "control.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE roster_agents (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            created_by TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            updated_by TEXT NOT NULL
        );
        CREATE TABLE profiles (
            id INTEGER PRIMARY KEY,
            project TEXT NOT NULL,
            variant TEXT NOT NULL,
            include TEXT NOT NULL,
            claude_md_extras TEXT NOT NULL,
            created_at TEXT NOT NULL,
            created_by TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            updated_by TEXT NOT NULL,
            UNIQUE (project, variant)
        );
    """)
    conn.commit()
    conn.close()
    monkeypatch.setattr(store, "DB_PATH", path)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    return path


def _rows(db, table):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in
                conn.execute(f"SELECT * FROM {table} ORDER BY id")]
    finally:
        conn.close()


# --- roster: reading ALLOWED_AUDIENCE ---------------------------------------


def _write_knowledge_py(tmp_path, names):
    path = tmp_path / "knowledge.py"
    literal = "{\n" + "\n".join(f'    "{n}",' for n in names) + "\n}"
    path.write_text(f"ALLOWED_AUDIENCE = {literal}\n")
    return path


def test_roster_from_knowledge_py_reads_the_set_literal(tmp_path):
    path = _write_knowledge_py(tmp_path, ["coder", "reviewer", "architect"])
    assert load_crew_registry._roster_from_knowledge_py(str(path)) == [
        "architect", "coder", "reviewer"]


def test_roster_from_knowledge_py_does_not_execute_the_file(tmp_path):
    """`ast.literal_eval`, not `import` or `exec` — a sibling repository's
    module must not run inside this process just to read one set out of
    it."""
    path = tmp_path / "knowledge.py"
    path.write_text(
        "import sys\nsys.exit(1)\nALLOWED_AUDIENCE = {'coder'}\n")
    assert load_crew_registry._roster_from_knowledge_py(str(path)) == [
        "coder"]


def test_roster_from_knowledge_py_refuses_a_file_with_no_such_assignment(
        tmp_path):
    path = tmp_path / "knowledge.py"
    path.write_text("SOMETHING_ELSE = {'coder'}\n")
    with pytest.raises(SystemExit, match="ALLOWED_AUDIENCE"):
        load_crew_registry._roster_from_knowledge_py(str(path))


def test_roster_subcommand_loads_every_name(db, tmp_path):
    path = _write_knowledge_py(tmp_path, ["coder", "reviewer"])
    rc = load_crew_registry.main(["roster", str(path), "--actor", ACTOR])
    assert rc == 0
    assert {r["name"] for r in _rows(db, "roster_agents")} == \
        {"coder", "reviewer"}
    assert all(r["created_by"] == ACTOR for r in _rows(db, "roster_agents"))


def test_roster_subcommand_refuses_a_second_run_against_a_populated_table(
        db, tmp_path, capsys):
    """Not idempotent, on purpose (issue_01M34STNFA2CSD78PW5Y9XHX1M):
    `PUT`/`DELETE /roster/{name}` are the roster's write surface now, so a
    populated table may hold an HTTP caller's edit, and a re-run must not
    upsert `ALLOWED_AUDIENCE` back over it. The second run writes nothing
    and exits non-zero."""
    path = _write_knowledge_py(tmp_path, ["coder"])
    rc1 = load_crew_registry.main(["roster", str(path), "--actor", ACTOR])
    assert rc1 == 0
    assert {r["name"] for r in _rows(db, "roster_agents")} == {"coder"}

    rc2 = load_crew_registry.main(["roster", str(path), "--actor", ACTOR])
    assert rc2 != 0
    assert {r["name"] for r in _rows(db, "roster_agents")} == {"coder"}
    err = capsys.readouterr().err
    assert "roster_agents already holds" in err
    assert "--reseed" in err


def test_roster_subcommand_reseed_overwrites_a_populated_table(
        db, tmp_path):
    """`--reseed` is the explicit override: a populated table upserts from
    `ALLOWED_AUDIENCE` anyway, the one legitimate reason being restoring the
    table after it was lost or rebuilt."""
    path = _write_knowledge_py(tmp_path, ["coder"])
    load_crew_registry.main(["roster", str(path), "--actor", ACTOR])

    path2 = _write_knowledge_py(tmp_path, ["coder", "reviewer"])
    rc = load_crew_registry.main(
        ["roster", str(path2), "--actor", ACTOR, "--reseed"])
    assert rc == 0
    assert {r["name"] for r in _rows(db, "roster_agents")} == \
        {"coder", "reviewer"}


# --- profiles: reading the YAML tree ----------------------------------------


def _write_profile(root, project, variant, body):
    d = root / "projects" / project / "profiles"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{variant}.yaml").write_text(textwrap.dedent(body))


def test_profiles_from_knowledge_repo_reads_every_yaml_file(tmp_path):
    _write_profile(tmp_path, "hollis", "daytime", """\
        profile: hollis/daytime
        include:
          - kind: agent
            audience_contains: [hollis-coder]
        claude_md_extras:
          - "You are the hollis crew."
        """)
    _write_profile(tmp_path, "hollis", "overnight", """\
        profile: hollis/overnight
        include:
          - kind: agent
            audience_contains: [hollis-overnight-orchestrator]
        """)
    rows = load_crew_registry._profiles_from_knowledge_repo(str(tmp_path))
    by_variant = {r["variant"]: r for r in rows}
    assert set(by_variant) == {"daytime", "overnight"}
    assert by_variant["daytime"]["project"] == "hollis"
    assert by_variant["daytime"]["include"] == [
        {"kind": "agent", "audience_contains": ["hollis-coder"]}]
    assert by_variant["daytime"]["claude_md_extras"] == \
        ["You are the hollis crew."]
    assert by_variant["overnight"]["claude_md_extras"] == []


def test_profiles_from_knowledge_repo_refuses_a_profile_key_mismatch(
        tmp_path):
    """The path (`projects/hollis/profiles/daytime.yaml`) says
    `hollis/daytime`; a YAML `profile:` key claiming something else is a
    fact about the file worth stopping over, not something to silently
    resolve one way or the other."""
    _write_profile(tmp_path, "hollis", "daytime", """\
        profile: hollis/overnight
        include: []
        """)
    with pytest.raises(SystemExit, match="does not match"):
        load_crew_registry._profiles_from_knowledge_repo(str(tmp_path))


def test_profiles_subcommand_loads_every_file(db, tmp_path):
    _write_profile(tmp_path, "hollis", "daytime", """\
        profile: hollis/daytime
        include:
          - kind: agent
            audience_contains: [hollis-coder]
        """)
    _write_profile(tmp_path, "vivy", "daytime", """\
        profile: vivy/daytime
        include:
          - kind: agent
            audience_contains: [reviewer]
        """)
    rc = load_crew_registry.main(
        ["profiles", str(tmp_path), "--actor", ACTOR])
    assert rc == 0
    projects = {r["project"] for r in _rows(db, "profiles")}
    assert projects == {"hollis", "vivy"}


def test_profiles_subcommand_is_idempotent(db, tmp_path):
    _write_profile(tmp_path, "hollis", "daytime", """\
        profile: hollis/daytime
        include: []
        """)
    load_crew_registry.main(["profiles", str(tmp_path), "--actor", ACTOR])
    load_crew_registry.main(["profiles", str(tmp_path), "--actor", ACTOR])
    assert len(_rows(db, "profiles")) == 1


# --- profiles: reconciling deleted files ------------------------------------


def test_profiles_subcommand_removes_a_row_whose_file_is_gone(
        db, tmp_path, capsys):
    daytime = tmp_path / "projects" / "hollis" / "profiles" / "daytime.yaml"
    overnight = \
        tmp_path / "projects" / "hollis" / "profiles" / "overnight.yaml"
    _write_profile(tmp_path, "hollis", "daytime", """\
        profile: hollis/daytime
        include: []
        """)
    _write_profile(tmp_path, "hollis", "overnight", """\
        profile: hollis/overnight
        include: []
        """)
    load_crew_registry.main(["profiles", str(tmp_path), "--actor", ACTOR])
    assert {(r["project"], r["variant"]) for r in _rows(db, "profiles")} == \
        {("hollis", "daytime"), ("hollis", "overnight")}

    overnight.unlink()
    assert daytime.exists()
    rc = load_crew_registry.main(
        ["profiles", str(tmp_path), "--actor", ACTOR])
    assert rc == 0

    # **THE DATABASE IS THE ASSERTION; THE PRINTED TEXT IS NOT** (reviewer, PR
    # #131 round 1). Checking only stdout passes with the `profile_delete` call
    # deleted, because the `print` beside it is unconditional — the test would
    # confirm that the command SAYS it removed a row while the row is still
    # being served.
    assert {(r["project"], r["variant"]) for r in _rows(db, "profiles")} == \
        {("hollis", "daytime")}

    out = capsys.readouterr().out
    assert "removed profile: hollis/overnight" in out
    assert "removed 1 profile(s)" in out


def test_profiles_subcommand_keeps_a_row_whose_file_still_exists(
        db, tmp_path):
    _write_profile(tmp_path, "hollis", "daytime", """\
        profile: hollis/daytime
        include: []
        """)
    load_crew_registry.main(["profiles", str(tmp_path), "--actor", ACTOR])
    load_crew_registry.main(["profiles", str(tmp_path), "--actor", ACTOR])
    assert {(r["project"], r["variant"]) for r in _rows(db, "profiles")} == \
        {("hollis", "daytime")}


def test_profiles_subcommand_dry_run_writes_nothing(db, tmp_path, capsys):
    overnight = \
        tmp_path / "projects" / "hollis" / "profiles" / "overnight.yaml"
    _write_profile(tmp_path, "hollis", "overnight", """\
        profile: hollis/overnight
        include: []
        """)
    load_crew_registry.main(["profiles", str(tmp_path), "--actor", ACTOR])
    overnight.unlink()
    # A second file, so the dry run has something to LOAD as well as something
    # to remove. Without it the "would load" half of the flag goes unexercised,
    # and that half is where the flag was writing.
    _write_profile(tmp_path, "hollis", "daytime", """\
        profile: hollis/daytime
        include: []
        """)

    # The whole table, captured before the dry run, values included. Asserting
    # only on the (project, variant) SET would pass while the dry run rewrote
    # every row's `updated_at`, and it would count a row the dry run itself
    # INSERTED as evidence that nothing changed — `hollis/daytime` has no row
    # at this point, so a dry run that upserts leaves the set looking correct.
    before = sorted(tuple(sorted(r.items())) for r in _rows(db, "profiles"))
    assert {(r["project"], r["variant"]) for r in _rows(db, "profiles")} == \
        {("hollis", "overnight")}

    rc = load_crew_registry.main(
        ["profiles", str(tmp_path), "--actor", ACTOR, "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "would remove profile: hollis/overnight" in out
    assert "would remove 1 profile(s)" in out
    assert "would load 1 profile(s)" in out

    after = sorted(tuple(sorted(r.items())) for r in _rows(db, "profiles"))
    assert after == before


def test_profiles_subcommand_announces_a_no_op_reconcile(
        db, tmp_path, capsys):
    _write_profile(tmp_path, "hollis", "daytime", """\
        profile: hollis/daytime
        include: []
        """)
    rc = load_crew_registry.main(
        ["profiles", str(tmp_path), "--actor", ACTOR])
    assert rc == 0
    assert "removed 0 profile(s)" in capsys.readouterr().out


def test_profiles_subcommand_refuses_a_root_with_no_projects_dir(
        db, tmp_path):
    empty_root = tmp_path / "not-a-checkout"
    empty_root.mkdir()
    with pytest.raises(SystemExit, match="does not look like a knowledge"):
        load_crew_registry.main(
            ["profiles", str(empty_root), "--actor", ACTOR])
    assert _rows(db, "profiles") == []


def test_profiles_subcommand_refuses_an_empty_projects_dir(db, tmp_path):
    (tmp_path / "projects").mkdir()
    with pytest.raises(SystemExit, match="does not look like a knowledge"):
        load_crew_registry.main(["profiles", str(tmp_path), "--actor", ACTOR])
    assert _rows(db, "profiles") == []


def test_profiles_subcommand_refuses_a_missing_root(db, tmp_path):
    missing = tmp_path / "does-not-exist"
    with pytest.raises(SystemExit, match="does not look like a knowledge"):
        load_crew_registry.main(["profiles", str(missing), "--actor", ACTOR])
    assert _rows(db, "profiles") == []


def test_profiles_subcommand_refuses_a_root_that_globs_no_profiles(
        db, tmp_path):
    """The case `_validate_knowledge_root` alone lets through: a `projects/`
    directory that is not empty, so the structural check passes, but with no
    `profiles/*.yaml` under any of it. A worktree checked out before those
    files existed looks exactly like this, and without the refusal every row
    in the table is stale by construction and gets deleted."""
    _write_profile(tmp_path, "hollis", "daytime", """\
        profile: hollis/daytime
        include: []
        """)
    load_crew_registry.main(["profiles", str(tmp_path), "--actor", ACTOR])
    assert len(_rows(db, "profiles")) == 1

    # Remove the profile but leave `projects/hollis/profiles/` standing, so
    # the structural check still passes and only the glob comes back empty.
    (tmp_path / "projects" / "hollis" / "profiles" / "daytime.yaml").unlink()

    with pytest.raises(SystemExit, match="refusing to reconcile"):
        load_crew_registry.main(["profiles", str(tmp_path), "--actor", ACTOR])
    assert len(_rows(db, "profiles")) == 1


def test_profiles_dry_run_validates_profile_shape_the_real_run_would_reject(
        db, tmp_path, capsys):
    """A dry run skips `profile_put`, which is where shape validation lived, so
    without validating on both paths a clean dry run would report "would load"
    for a profile the real run raises on before it writes anything."""
    _write_profile(tmp_path, "hollis", "daytime", """\
        profile: hollis/daytime
        include:
          - kind: agent
            gateway: nope
        """)
    # `main` catches `InvalidRosterRow` and returns 1 with the message on
    # stderr, the same way a real run's failure surfaces, rather than letting
    # it escape — so the dry run reports the rejection an operator would hit.
    rc = load_crew_registry.main(
        ["profiles", str(tmp_path), "--actor", ACTOR, "--dry-run"])
    assert rc == 1
    assert "ARCH-134" in capsys.readouterr().err
    assert _rows(db, "profiles") == []


# --- the shadow gate still refuses through this entry point ----------------


def test_the_roster_subcommand_refuses_while_writes_are_disabled(
        db, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    path = _write_knowledge_py(tmp_path, ["coder"])
    rc = load_crew_registry.main(["roster", str(path), "--actor", ACTOR])
    assert rc == 1
    assert "read-only shadow" in capsys.readouterr().err
