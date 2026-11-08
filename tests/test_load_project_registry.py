"""`load_project_registry.py`: the operator load of the registry file into
the Chart House's `project_registry` table (CQ·241).

Fixture slugs are synthetic (`oldmill`, `harrow`) — no real crew name.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

import load_project_registry
import project_registry
import store

ACTOR = "test, 2026-10-25"


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "control.db"
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE project_registry (
            id INTEGER PRIMARY KEY,
            slug TEXT NOT NULL UNIQUE,
            entry TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            updated_by TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()
    monkeypatch.setattr(store, "DB_PATH", path)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    return path


def _rows(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in
                conn.execute("SELECT * FROM project_registry ORDER BY slug")]
    finally:
        conn.close()


def _write(tmp_path, document):
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(document))
    return path


def test_load_subcommand_loads_every_entry(db, tmp_path):
    path = _write(tmp_path, {"oldmill": {"repos": []}, "_meta": {}})
    rc = load_project_registry.main([str(path), "--actor", ACTOR])
    assert rc == 0
    assert {r["slug"] for r in _rows(db)} == {"oldmill", "_meta"}
    assert all(r["updated_by"] == ACTOR for r in _rows(db))


def test_replace_prints_added_and_removed_slugs(db, tmp_path, capsys):
    path1 = _write(tmp_path, {"oldmill": {"repos": []},
                              "harrow": {"repos": []}})
    load_project_registry.main([str(path1), "--actor", ACTOR])
    capsys.readouterr()

    path2 = _write(tmp_path, {"oldmill": {"repos": []}, "fenwick": {}})
    rc = load_project_registry.main([str(path2), "--actor", ACTOR, "--replace",
                                     "--remove", "harrow"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "added: fenwick" in out
    assert "removed: harrow" in out
    assert "added 1 slug(s), removed 1 slug(s)" in out
    assert {r["slug"] for r in _rows(db)} == {"oldmill", "fenwick"}


def test_load_refuses_a_populated_table_with_exit_3_and_writes_nothing(
        db, tmp_path):
    """A second plain run must not erase a crew registered since the first:
    the row standing in for that crew survives, and the exit code is the
    one an install reads as "already loaded"."""
    load_project_registry.main(
        [str(_write(tmp_path, {"oldmill": {"repos": []}})), "--actor", ACTOR])
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO project_registry (slug, entry, updated_at, "
                 "updated_by) VALUES ('harrow', '{}', '2026-10-25', 'reg')")
    conn.commit()
    conn.close()
    before = _rows(db)

    rc = load_project_registry.main(
        [str(_write(tmp_path, {"oldmill": {"repos": []}})), "--actor", ACTOR])
    assert rc == load_project_registry.EXIT_POPULATED == 3
    assert _rows(db) == before


def test_a_usage_error_exits_2_and_writes_nothing(db, tmp_path):
    path = _write(tmp_path, {"oldmill": {"repos": []}})
    with pytest.raises(SystemExit) as e:
        load_project_registry.main([str(path)])
    assert e.value.code == 2
    assert _rows(db) == []


def test_load_refuses_a_missing_file_with_exit_1_and_writes_nothing(
        db, tmp_path):
    missing = tmp_path / "does-not-exist.json"
    with pytest.raises(SystemExit, match="not found") as e:
        load_project_registry.main([str(missing), "--actor", ACTOR])
    # SystemExit with a message string exits 1 when it reaches the
    # interpreter; assert that rather than the string alone.
    assert isinstance(e.value.code, str)
    assert _rows(db) == []


def test_replace_refuses_an_invalid_file_and_keeps_the_table(db, tmp_path):
    load_project_registry.main(
        [str(_write(tmp_path, {"oldmill": {"repos": []}})), "--actor", ACTOR])
    before = _rows(db)
    bad = tmp_path / "bad.json"
    bad.write_text("not json")
    with pytest.raises(SystemExit, match="cannot read"):
        load_project_registry.main([str(bad), "--actor", ACTOR, "--replace"])
    assert _rows(db) == before


def test_load_refuses_unparsable_json_and_writes_nothing(db, tmp_path):
    path = tmp_path / "registry.json"
    path.write_text("not json")
    with pytest.raises(SystemExit, match="cannot read"):
        load_project_registry.main([str(path), "--actor", ACTOR])
    assert _rows(db) == []


def test_load_refuses_a_non_object_top_level_and_writes_nothing(db, tmp_path):
    path = _write(tmp_path, ["oldmill"])
    with pytest.raises(SystemExit, match="JSON object"):
        load_project_registry.main([str(path), "--actor", ACTOR])
    assert _rows(db) == []


def test_load_refuses_a_non_object_project_entry_and_writes_nothing(
        db, tmp_path):
    path = _write(tmp_path, {"oldmill": "not an object",
                             "harrow": {"repos": []}})
    with pytest.raises(SystemExit, match="oldmill"):
        load_project_registry.main([str(path), "--actor", ACTOR])
    assert _rows(db) == []


def test_load_refuses_while_writes_are_disabled(db, tmp_path, monkeypatch):
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    path = _write(tmp_path, {"oldmill": {"repos": []}})
    rc = load_project_registry.main([str(path), "--actor", ACTOR])
    assert rc == 1
    assert _rows(db) == []


def test_load_round_trips_through_registry_document(db, tmp_path):
    document = {"oldmill": {"repos": ["oldmill-app"], "private": True},
               "_scopes": {"platform": {}}}
    path = _write(tmp_path, document)
    load_project_registry.main([str(path), "--actor", ACTOR])
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        assert project_registry.registry_document(conn) == document
    finally:
        conn.close()


# --- a replace removes only what --remove names (CQ·247) ----------------------


def _seed_then_register(db, tmp_path):
    """The table after a seed load from a file, plus one crew registered
    the way `POST /registry/crews` writes it: in the table and not in the
    file."""
    path = _write(tmp_path, {"oldmill": {"repos": ["oldmill"]}})
    assert load_project_registry.main([str(path), "--actor", ACTOR]) == 0
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO project_registry (slug, entry, updated_at, updated_by) "
        "VALUES ('harrow', '{\"repos\": [\"harrow\"]}', "
        "'2026-10-26 23:00:00', 'agent-platform-registrar')")
    conn.commit()
    conn.close()
    return path


def test_a_row_edit_replace_refuses_to_erase_a_registered_crew(
        db, tmp_path, capsys):
    """The CQ·247 occurrence: a field edit made in the file and loaded with
    --replace, from a file that never held the crew registered over the
    wire, exits 4 and leaves both rows exactly as they were."""
    _seed_then_register(db, tmp_path)
    before = _rows(db)
    edited = _write(tmp_path, {"oldmill": {"repos": ["oldmill"], "note": "x"}})
    capsys.readouterr()
    rc = load_project_registry.main([str(edited), "--actor", ACTOR,
                                     "--replace"])
    assert rc == load_project_registry.EXIT_WOULD_REMOVE == 4
    err = capsys.readouterr().err
    assert "harrow" in err and "nothing was written" in err
    assert _rows(db) == before


def test_naming_the_slug_with_remove_lets_the_replace_remove_it(
        db, tmp_path, capsys):
    _seed_then_register(db, tmp_path)
    edited = _write(tmp_path, {"oldmill": {"repos": ["oldmill"], "note": "x"}})
    capsys.readouterr()
    rc = load_project_registry.main([str(edited), "--actor", ACTOR,
                                     "--replace", "--remove", "harrow"])
    assert rc == 0
    assert "removed: harrow" in capsys.readouterr().out
    rows = {r["slug"]: r for r in _rows(db)}
    assert set(rows) == {"oldmill"}
    assert json.loads(rows["oldmill"]["entry"])["note"] == "x"


@pytest.mark.parametrize("named", [["oldmill"], ["harow"]])
def test_remove_naming_a_slug_the_replace_keeps_or_never_held_refuses(
        db, tmp_path, capsys, named):
    """`oldmill` stays in the file, and `harow` is a typo for a slug the
    table holds; either way the named set is not the removed set."""
    _seed_then_register(db, tmp_path)
    before = _rows(db)
    path = _write(tmp_path, {"oldmill": {"repos": ["oldmill"]},
                             "harrow": {"repos": ["harrow"]}})
    capsys.readouterr()
    args = [str(path), "--actor", ACTOR, "--replace"]
    for slug in named:
        args += ["--remove", slug]
    assert load_project_registry.main(args) == 4
    assert named[0] in capsys.readouterr().err
    assert _rows(db) == before


def test_a_replace_that_adds_and_edits_needs_no_remove(db, tmp_path):
    _seed_then_register(db, tmp_path)
    path = _write(tmp_path, {"oldmill": {"repos": ["oldmill"], "note": "x"},
                             "harrow": {"repos": ["harrow"]},
                             "fenwick": {"repos": ["fenwick"]}})
    rc = load_project_registry.main([str(path), "--actor", ACTOR, "--replace"])
    assert rc == 0
    assert {r["slug"] for r in _rows(db)} == {"oldmill", "harrow", "fenwick"}


def test_remove_without_replace_is_a_usage_error(db, tmp_path):
    path = _write(tmp_path, {"oldmill": {"repos": []}})
    with pytest.raises(SystemExit) as e:
        load_project_registry.main([str(path), "--actor", ACTOR,
                                    "--remove", "oldmill"])
    assert e.value.code == 2
    assert _rows(db) == []


# --- a replace removes only what --drop-field names (CQ·250) ----------------


def test_an_unnamed_field_removal_exits_4_and_writes_nothing(
        db, tmp_path, capsys):
    seed = _write(tmp_path, {"oldmill": {"repos": [], "agent_prefix": "om"}})
    load_project_registry.main([str(seed), "--actor", ACTOR])
    before = _rows(db)
    capsys.readouterr()
    edited = _write(tmp_path, {"oldmill": {"repos": []}})
    rc = load_project_registry.main(
        [str(edited), "--actor", ACTOR, "--replace"])
    assert rc == load_project_registry.EXIT_WOULD_REMOVE == 4
    err = capsys.readouterr().err
    assert "oldmill.agent_prefix" in err and "nothing was written" in err
    assert _rows(db) == before


def test_naming_the_field_with_drop_field_lets_the_replace_remove_it(
        db, tmp_path, capsys):
    seed = _write(tmp_path, {"oldmill": {"repos": [], "agent_prefix": "om"}})
    load_project_registry.main([str(seed), "--actor", ACTOR])
    capsys.readouterr()
    edited = _write(tmp_path, {"oldmill": {"repos": []}})
    rc = load_project_registry.main(
        [str(edited), "--actor", ACTOR, "--replace",
         "--drop-field", "oldmill.agent_prefix"])
    assert rc == 0
    rows = {r["slug"]: r for r in _rows(db)}
    assert "agent_prefix" not in json.loads(rows["oldmill"]["entry"])


def test_drop_field_naming_a_field_the_replace_would_not_remove_exits_4(
        db, tmp_path, capsys):
    seed = _write(tmp_path, {"oldmill": {"repos": [], "agent_prefix": "om"}})
    load_project_registry.main([str(seed), "--actor", ACTOR])
    before = _rows(db)
    capsys.readouterr()
    unchanged = _write(
        tmp_path, {"oldmill": {"repos": [], "agent_prefix": "om"}})
    rc = load_project_registry.main(
        [str(unchanged), "--actor", ACTOR, "--replace",
         "--drop-field", "oldmill.agent_prefix"])
    assert rc == 4
    assert "oldmill.agent_prefix" in capsys.readouterr().err
    assert _rows(db) == before


def test_a_changed_value_needs_no_drop_field(db, tmp_path):
    seed = _write(tmp_path, {"oldmill": {"repos": [], "agent_prefix": "om"}})
    load_project_registry.main([str(seed), "--actor", ACTOR])
    edited = _write(
        tmp_path, {"oldmill": {"repos": [], "agent_prefix": "mill"}})
    rc = load_project_registry.main(
        [str(edited), "--actor", ACTOR, "--replace"])
    assert rc == 0
    rows = {r["slug"]: r for r in _rows(db)}
    assert json.loads(rows["oldmill"]["entry"])["agent_prefix"] == "mill"


def test_a_map_valued_crew_field_losing_one_key_needs_no_drop_field(
        db, tmp_path):
    seed = _write(tmp_path, {"oldmill": {
        "repos": [], "repo_owners": {"a": "convivy", "b": "convivy"}}})
    load_project_registry.main([str(seed), "--actor", ACTOR])
    edited = _write(tmp_path, {"oldmill": {
        "repos": [], "repo_owners": {"a": "convivy"}}})
    rc = load_project_registry.main(
        [str(edited), "--actor", ACTOR, "--replace"])
    assert rc == 0


def test_a_nested_meta_field_removal_refuses_until_named(
        db, tmp_path, capsys):
    seed = _write(tmp_path, {
        "oldmill": {"repos": []},
        "_meta": {"fields": {"agent_prefix": "set by X"}}})
    load_project_registry.main([str(seed), "--actor", ACTOR])
    capsys.readouterr()
    edited = _write(tmp_path, {
        "oldmill": {"repos": []}, "_meta": {"fields": {}}})
    rc = load_project_registry.main(
        [str(edited), "--actor", ACTOR, "--replace"])
    assert rc == 4
    assert "_meta.fields.agent_prefix" in capsys.readouterr().err

    rc = load_project_registry.main(
        [str(edited), "--actor", ACTOR, "--replace",
         "--drop-field", "_meta.fields.agent_prefix"])
    assert rc == 0


def test_a_kb_legacy_list_shrinking_needs_no_drop_field(db, tmp_path):
    seed = _write(tmp_path, {
        "oldmill": {"repos": []}, "_kb_legacy": ["knowledge", "library"]})
    load_project_registry.main([str(seed), "--actor", ACTOR])
    edited = _write(tmp_path, {
        "oldmill": {"repos": []}, "_kb_legacy": ["knowledge"]})
    rc = load_project_registry.main(
        [str(edited), "--actor", ACTOR, "--replace"])
    assert rc == 0


def test_a_removed_slugs_fields_are_not_counted(db, tmp_path):
    seed = _write(tmp_path, {
        "oldmill": {"repos": []},
        "harrow": {"repos": [], "agent_prefix": "ha"}})
    load_project_registry.main([str(seed), "--actor", ACTOR])
    edited = _write(tmp_path, {"oldmill": {"repos": []}})
    rc = load_project_registry.main(
        [str(edited), "--actor", ACTOR, "--replace", "--remove", "harrow"])
    assert rc == 0


def test_the_field_refusal_message_carries_no_value(db, tmp_path, capsys):
    seed = _write(
        tmp_path,
        {"oldmill": {"repos": [], "agent_prefix": "do-not-print-me"}})
    load_project_registry.main([str(seed), "--actor", ACTOR])
    capsys.readouterr()
    edited = _write(tmp_path, {"oldmill": {"repos": []}})
    load_project_registry.main([str(edited), "--actor", ACTOR, "--replace"])
    err = capsys.readouterr().err
    assert "do-not-print-me" not in err
    assert "oldmill.agent_prefix" in err


def test_drop_field_without_replace_is_a_usage_error(db, tmp_path):
    path = _write(tmp_path, {"oldmill": {"repos": []}})
    with pytest.raises(SystemExit) as e:
        load_project_registry.main(
            [str(path), "--actor", ACTOR,
             "--drop-field", "oldmill.agent_prefix"])
    assert e.value.code == 2
    assert _rows(db) == []
