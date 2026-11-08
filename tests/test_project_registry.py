"""`project_registry.py`: `validate_document`, `replace`, `registry_document`
(CQ·241) and `register_crew`/`build_crew_row` (CQ·242).

Fixture slugs are synthetic (`oldmill`, `harrow`, `quill`) — no real crew
name.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

import identity
import project_registry
import store

ACTOR = "test, 2026-10-25"
ORCHESTRATOR = identity.Identity(actor=ACTOR, kind="orchestrator")
AGENT = identity.Identity(actor=ACTOR, kind="agent")


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


# --- validate_document -------------------------------------------------------


def test_validate_document_accepts_projects_and_metadata():
    project_registry.validate_document(
        {"oldmill": {"repos": []}, "_scopes": {"platform": {}}, "_meta": {}})


def test_validate_document_refuses_a_non_object_top_level():
    with pytest.raises(project_registry.InvalidRegistryDocument,
                       match="JSON object"):
        project_registry.validate_document(["oldmill"])


def test_validate_document_refuses_a_non_object_project_entry():
    with pytest.raises(project_registry.InvalidRegistryDocument,
                       match="oldmill"):
        project_registry.validate_document({"oldmill": "not an object"})


def test_validate_document_allows_a_non_object_metadata_entry():
    """`_kb_legacy` is a list, not an object — metadata keys are not held
    to the project-entry shape."""
    project_registry.validate_document({"_kb_legacy": ["knowledge"]})


def test_validate_document_refuses_a_slug_over_the_identifier_bound():
    with pytest.raises(project_registry.InvalidRegistryDocument,
                       match="characters"):
        project_registry.validate_document(
            {"x" * (store.MAX_IDENT + 1): {}})


# --- replace ------------------------------------------------------------------


def test_replace_inserts_every_entry_and_reports_it_as_added(db):
    added, removed = project_registry.replace(
        {"oldmill": {"repos": []}, "_meta": {}}, actor=ACTOR)
    assert added == ["_meta", "oldmill"]
    assert removed == []
    rows = {r["slug"]: r for r in _rows(db)}
    assert set(rows) == {"oldmill", "_meta"}
    assert rows["oldmill"]["updated_by"] == ACTOR


def test_replace_refuses_a_populated_table_and_writes_nothing(db):
    project_registry.replace({"oldmill": {"repos": []}}, actor=ACTOR)
    before = _rows(db)
    with pytest.raises(project_registry.RegistryPopulated):
        project_registry.replace({"harrow": {"repos": []}}, actor="second")
    assert _rows(db) == before


def test_replace_with_allow_populated_is_idempotent(db):
    document = {"oldmill": {"repos": []}}
    project_registry.replace(document, actor=ACTOR)
    added, removed = project_registry.replace(
        document, actor=ACTOR, allow_populated=True)
    assert added == []
    assert removed == []
    assert len(_rows(db)) == 1


def test_replace_reports_a_dropped_slug_as_removed(db):
    project_registry.replace(
        {"oldmill": {"repos": []}, "harrow": {"repos": []}}, actor=ACTOR)
    added, removed = project_registry.replace(
        {"oldmill": {"repos": []}}, actor=ACTOR, allow_populated=True,
        remove=["harrow"])
    assert added == []
    assert removed == ["harrow"]
    assert {r["slug"] for r in _rows(db)} == {"oldmill"}


def test_replace_round_trips_a_project_entry_through_registry_document(db):
    entry = {"repos": ["oldmill-app"], "private": True, "aliases": ["mill"]}
    project_registry.replace({"oldmill": entry}, actor=ACTOR)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        document = project_registry.registry_document(conn)
    finally:
        conn.close()
    assert document == {"oldmill": entry}


def test_replace_refuses_while_writes_are_disabled(db, monkeypatch):
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    with pytest.raises(store.WritesDisabled):
        project_registry.replace({"oldmill": {}}, actor=ACTOR)
    assert _rows(db) == []


# --- registry_document --------------------------------------------------------


def test_registry_document_is_none_when_the_table_is_empty(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        assert project_registry.registry_document(conn) is None
    finally:
        conn.close()


def test_registry_document_reproduces_the_loaded_document_exactly(db):
    document = {
        "oldmill": {"repos": ["oldmill-app"], "private": False},
        "quill": {"repos": [], "private": True, "aliases": ["scribe"]},
        "_scopes": {"platform": {}},
        "_meta": {"generated": "2026-10-25"},
    }
    project_registry.replace(document, actor=ACTOR)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        assert project_registry.registry_document(conn) == document
    finally:
        conn.close()


# --- build_crew_row -----------------------------------------------------------


def test_build_crew_row_fills_in_the_defaults():
    slug, row = project_registry.build_crew_row(
        {"slug": "harrow", "display": "Harrow", "repos": ["harrow-app"]})
    assert slug == "harrow"
    assert row == {
        "class": "project", "display": "Harrow", "owner": "convivy",
        "home_dir": "<root>/harrow", "dirs": [], "repos": ["harrow-app"],
        "aliases": [], "crew": "harrow", "lifecycle": "active",
        "products": [], "kb": "central", "kb_project": "harrow",
        "task_text_public": False, "crew_cockpit": True,
    }


def test_build_crew_row_refuses_a_bad_slug():
    with pytest.raises(project_registry.RegistrationRefused, match="slug"):
        project_registry.build_crew_row(
            {"slug": "Harrow", "display": "Harrow", "repos": ["harrow-app"]})


def test_build_crew_row_refuses_no_repos():
    with pytest.raises(project_registry.RegistrationRefused, match="repos"):
        project_registry.build_crew_row(
            {"slug": "harrow", "display": "Harrow", "repos": []})


def test_build_crew_row_refuses_a_private_crew_with_central_kb():
    with pytest.raises(project_registry.RegistrationRefused, match="private"):
        project_registry.build_crew_row(
            {"slug": "quill", "display": "Quill", "repos": ["quill-app"],
             "private": True})


def test_build_crew_row_refuses_a_private_crew_with_a_kb_project():
    with pytest.raises(project_registry.RegistrationRefused, match="kb_project"):
        project_registry.build_crew_row(
            {"slug": "quill", "display": "Quill", "repos": ["quill-app"],
             "private": True, "kb": "local", "kb_project": "quill"})


def test_build_crew_row_accepts_a_private_crew_with_local_kb_and_no_kb_project():
    slug, row = project_registry.build_crew_row(
        {"slug": "quill", "display": "Quill", "repos": ["quill-app"],
         "private": True, "kb": "local"})
    assert row["private"] is True
    assert row["kb"] == "local"
    assert "kb_project" not in row


# --- register_crew -------------------------------------------------------------


@pytest.fixture
def loaded_db(db):
    """`db`, with one unrelated crew already loaded — `register_crew`
    refuses an empty table, so every test below needs a non-empty one."""
    project_registry.replace(
        {"oldmill": {"repos": ["oldmill-app"], "aliases": ["mill"]}},
        actor=ACTOR)
    return db


def _document(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return project_registry.registry_document(conn)
    finally:
        conn.close()


def test_register_crew_adds_a_public_crew(loaded_db):
    row = project_registry.register_crew(
        ORCHESTRATOR,
        {"slug": "harrow", "display": "Harrow", "repos": ["harrow-app"]})
    assert row["slug"] == "harrow"
    assert row["updated_by"] == ACTOR
    assert _document(loaded_db)["harrow"]["display"] == "Harrow"


def test_register_crew_adds_a_private_crew(loaded_db):
    project_registry.register_crew(
        ORCHESTRATOR,
        {"slug": "quill", "display": "Quill", "repos": ["quill-app"],
         "private": True, "kb": "local"})
    assert _document(loaded_db)["quill"]["private"] is True


def test_register_crew_refuses_a_private_crew_with_central_kb(loaded_db):
    with pytest.raises(project_registry.RegistrationRefused):
        project_registry.register_crew(
            ORCHESTRATOR,
            {"slug": "quill", "display": "Quill", "repos": ["quill-app"],
             "private": True})
    assert "quill" not in _document(loaded_db)


def test_register_crew_refuses_a_private_crew_with_a_kb_project(loaded_db):
    with pytest.raises(project_registry.RegistrationRefused):
        project_registry.register_crew(
            ORCHESTRATOR,
            {"slug": "quill", "display": "Quill", "repos": ["quill-app"],
             "private": True, "kb": "local", "kb_project": "quill"})
    assert "quill" not in _document(loaded_db)


@pytest.mark.parametrize("field,value", [
    ("slug", "oldmill"), ("aliases", ["mill"]), ("repos", ["oldmill-app"])])
def test_register_crew_refuses_a_collision(loaded_db, field, value):
    payload = {"slug": "harrow", "display": "Harrow", "repos": ["harrow-app"]}
    if field != "slug":
        payload[field] = value
    else:
        payload["slug"] = value
    with pytest.raises(project_registry.RegistrationCollision) as exc:
        project_registry.register_crew(ORCHESTRATOR, payload)
    assert "harrow" not in (_document(loaded_db) or {})
    # The refusal names the caller's own colliding name and never the entry
    # that claims it: `registry.write` does not carry `registry.read`.
    colliding = value if field == "slug" else value[0]
    assert str(exc.value) == (
        f"{colliding!r} is already claimed; pick a different name")


def test_register_crew_names_no_other_entry_when_the_registry_has_a_bad_kb_project(db):
    project_registry.replace(
        {"oldmill": {"repos": ["oldmill-app"], "private": True, "kb": "local",
                     "kb_scope_exceptions": {"oldmill-app": {"kb_project": "Bad_Slug"}}}},
        actor=ACTOR)
    with pytest.raises(project_registry.RegistrationRefused) as exc:
        project_registry.register_crew(
            ORCHESTRATOR,
            {"slug": "harrow", "display": "Harrow", "repos": ["harrow-app"]})
    assert "1 KB-project value" in str(exc.value)
    for leaked in ("oldmill", "Bad_Slug"):
        assert leaked not in str(exc.value)


@pytest.mark.parametrize("field,value", [
    ("crew", "oldmill"), ("display", "Oldmill"), ("crew", "Mill"),
    ("dirs", ["/srv/crews/oldmill-app"]), ("home_dir", "/srv/crews/oldmill/")])
def test_register_crew_refuses_a_private_crew_answering_to_a_public_name(
        loaded_db, field, value):
    payload = {"slug": "harrow", "display": "Harrow", "repos": ["harrow-app"],
               "private": True, "kb": "local", field: value}
    with pytest.raises(project_registry.RegistrationCollision):
        project_registry.register_crew(ORCHESTRATOR, payload)
    assert "harrow" not in _document(loaded_db)


@pytest.mark.parametrize("private_field,private_value", [
    ("crew", "harrow"), ("display", "Harrow"),
    ("dirs", ["/srv/crews/harrow"]), ("home_dir", "/srv/crews/harrow/")])
def test_register_crew_refuses_a_public_crew_a_private_row_already_answers_to(
        loaded_db, private_field, private_value):
    project_registry.register_crew(
        ORCHESTRATOR,
        {"slug": "quill", "display": "Quill", "repos": ["quill-app"],
         "private": True, "kb": "local", private_field: private_value})
    with pytest.raises(project_registry.RegistrationCollision) as exc:
        project_registry.register_crew(
            ORCHESTRATOR,
            {"slug": "harrow", "display": "Harrow Works",
             "repos": ["harrow-app"]})
    # Word for word what a plain slug, alias or repo collision says, so the
    # wording cannot tell the caller that the claimant is private.
    assert str(exc.value) == "'harrow' is already claimed; pick a different name"
    assert "harrow" not in _document(loaded_db)


def test_register_crew_lets_two_private_crews_share_a_crew(loaded_db):
    for slug in ("quill", "wick"):
        project_registry.register_crew(
            ORCHESTRATOR,
            {"slug": slug, "display": slug.title(), "repos": [f"{slug}-app"],
             "private": True, "kb": "local", "crew": "ink"})
    assert _document(loaded_db)["wick"]["crew"] == "ink"


@pytest.mark.parametrize("entry", [
    {"repos": ["quill-app"], "aliases": ["Qu"], "display": "Quill Press",
     "crew": "ink", "home_dir": "/srv/crews/quill/", "dirs": ["/srv/a/b", ""]},
    {"repos": ["quill-app"]},
    {"repos": [], "display": "", "crew": None, "dirs": [None, "x/"]}])
def test_answered_names_agrees_with_projects_private_crew_names(entry):
    """`_answered_names` copies the collection block in
    `projects._Snapshot.private_crew_names`; this pins the two together."""
    import projects
    snapshot = projects._Snapshot({"quill": {**entry, "private": True}})
    assert project_registry._answered_names("quill", entry) == set(
        snapshot.private_crew_names)


def test_register_crew_lets_a_public_crew_share_an_existing_crew(loaded_db):
    row = project_registry.register_crew(
        ORCHESTRATOR,
        {"slug": "harrow", "display": "Harrow", "repos": ["harrow-app"],
         "crew": "oldmill"})
    assert row["crew"] == "oldmill"


def test_register_crew_stores_ci_workflows_for_its_own_repo(loaded_db):
    row = project_registry.register_crew(
        ORCHESTRATOR,
        {"slug": "harrow", "display": "Harrow", "repos": ["harrow-app"],
         "ci_workflows": {"harrow-app": ["ci.yml"]}})
    assert row["ci_workflows"] == {"harrow-app": ["ci.yml"]}


@pytest.mark.parametrize("ci_workflows", [
    {"other-app": ["ci.yml"]},
    {"harrow-app": [".github/workflows/ci.yml"]},
    {"harrow-app": "ci.yml"}])
def test_register_crew_refuses_bad_ci_workflows(loaded_db, ci_workflows):
    with pytest.raises(project_registry.RegistrationRefused) as exc:
        project_registry.register_crew(
            ORCHESTRATOR,
            {"slug": "harrow", "display": "Harrow", "repos": ["harrow-app"],
             "ci_workflows": ci_workflows})
    assert str(exc.value).startswith("ci_workflows:")
    assert "harrow" not in _document(loaded_db)


def test_register_crew_refuses_without_registry_write(loaded_db):
    with pytest.raises(project_registry.RegistrationForbidden):
        project_registry.register_crew(
            AGENT,
            {"slug": "harrow", "display": "Harrow", "repos": ["harrow-app"]})
    assert "harrow" not in _document(loaded_db)


def test_register_crew_refuses_on_an_empty_table(db):
    with pytest.raises(project_registry.RegistryNotLoaded):
        project_registry.register_crew(
            ORCHESTRATOR,
            {"slug": "harrow", "display": "Harrow", "repos": ["harrow-app"]})


def test_register_crew_never_replaces_an_existing_slug(loaded_db):
    with pytest.raises(project_registry.RegistrationCollision):
        project_registry.register_crew(
            ORCHESTRATOR,
            {"slug": "oldmill", "display": "Old Mill, Again",
             "repos": ["oldmill-app-2"]})
    assert _document(loaded_db)["oldmill"]["repos"] == ["oldmill-app"]


def test_register_crew_refuses_while_writes_are_disabled(loaded_db, monkeypatch):
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    with pytest.raises(store.WritesDisabled):
        project_registry.register_crew(
            ORCHESTRATOR,
            {"slug": "harrow", "display": "Harrow", "repos": ["harrow-app"]})
    assert "harrow" not in _document(loaded_db)


def test_a_populated_replace_raises_and_writes_nothing_for_an_unnamed_removal(db):
    """CQ·247: `remove` must equal the rows the replace would drop."""
    project_registry.replace({"oldmill": {"repos": []},
                              "harrow": {"repos": []}}, actor=ACTOR)
    before = _rows(db)
    with pytest.raises(project_registry.RegistryWouldRemove) as e:
        project_registry.replace({"oldmill": {"repos": []}}, actor="second",
                                 allow_populated=True)
    assert e.value.unnamed == ["harrow"]
    assert e.value.not_removed == []
    assert _rows(db) == before


def test_a_populated_replace_removes_exactly_the_named_rows(db):
    project_registry.replace({"oldmill": {"repos": []},
                              "harrow": {"repos": []}}, actor=ACTOR)
    added, removed = project_registry.replace(
        {"oldmill": {"repos": []}}, actor="second", allow_populated=True,
        remove=["harrow"])
    assert (added, removed) == ([], ["harrow"])
    assert {r["slug"] for r in _rows(db)} == {"oldmill"}


def test_remove_on_an_empty_table_refuses_rather_than_being_ignored(db):
    with pytest.raises(project_registry.RegistryWouldRemove) as e:
        project_registry.replace({"oldmill": {"repos": []}}, actor=ACTOR,
                                 allow_populated=True, remove=["harrow"])
    assert e.value.not_removed == ["harrow"]
    assert _rows(db) == []


# --- a replace removes only what --drop-field names (CQ·250) ----------------


def test_a_populated_replace_raises_and_writes_nothing_for_an_unnamed_field_removal(db):
    project_registry.replace(
        {"oldmill": {"repos": [], "agent_prefix": "om"}}, actor=ACTOR)
    before = _rows(db)
    with pytest.raises(project_registry.RegistryWouldRemove) as e:
        project_registry.replace(
            {"oldmill": {"repos": []}}, actor="second", allow_populated=True)
    assert e.value.unnamed_fields == ["oldmill.agent_prefix"]
    assert e.value.not_removed_fields == []
    assert _rows(db) == before


def test_naming_the_field_with_drop_field_lets_the_replace_remove_it(db):
    project_registry.replace(
        {"oldmill": {"repos": [], "agent_prefix": "om"}}, actor=ACTOR)
    added, removed = project_registry.replace(
        {"oldmill": {"repos": []}}, actor="second", allow_populated=True,
        drop_fields=["oldmill.agent_prefix"])
    assert (added, removed) == ([], [])
    rows = {r["slug"]: r for r in _rows(db)}
    assert "agent_prefix" not in json.loads(rows["oldmill"]["entry"])


def test_drop_field_naming_a_field_the_replace_would_not_remove_refuses(db):
    project_registry.replace(
        {"oldmill": {"repos": [], "agent_prefix": "om"}}, actor=ACTOR)
    before = _rows(db)
    with pytest.raises(project_registry.RegistryWouldRemove) as e:
        project_registry.replace(
            {"oldmill": {"repos": [], "agent_prefix": "om"}}, actor="second",
            allow_populated=True, drop_fields=["oldmill.agent_prefix"])
    assert e.value.not_removed_fields == ["oldmill.agent_prefix"]
    assert e.value.unnamed_fields == []
    assert _rows(db) == before


def test_a_changed_value_needs_no_drop_field(db):
    project_registry.replace(
        {"oldmill": {"repos": [], "agent_prefix": "om"}}, actor=ACTOR)
    added, removed = project_registry.replace(
        {"oldmill": {"repos": [], "agent_prefix": "mill"}}, actor="second",
        allow_populated=True)
    assert (added, removed) == ([], [])
    rows = {r["slug"]: r for r in _rows(db)}
    assert json.loads(rows["oldmill"]["entry"])["agent_prefix"] == "mill"


def test_a_map_valued_crew_field_losing_one_key_needs_no_drop_field(db):
    project_registry.replace(
        {"oldmill": {"repos": [],
                     "repo_owners": {"oldmill-app": "convivy",
                                      "oldmill-infra": "convivy"}}},
        actor=ACTOR)
    added, removed = project_registry.replace(
        {"oldmill": {"repos": [],
                     "repo_owners": {"oldmill-app": "convivy"}}},
        actor="second", allow_populated=True)
    assert (added, removed) == ([], [])


def test_a_nested_meta_field_removal_refuses_until_named(db):
    project_registry.replace(
        {"oldmill": {"repos": []},
         "_meta": {"fields": {"agent_prefix": "set by X"}}}, actor=ACTOR)
    before = _rows(db)
    with pytest.raises(project_registry.RegistryWouldRemove) as e:
        project_registry.replace(
            {"oldmill": {"repos": []}, "_meta": {"fields": {}}},
            actor="second", allow_populated=True)
    assert e.value.unnamed_fields == ["_meta.fields.agent_prefix"]
    assert _rows(db) == before

    added, removed = project_registry.replace(
        {"oldmill": {"repos": []}, "_meta": {"fields": {}}},
        actor="second", allow_populated=True,
        drop_fields=["_meta.fields.agent_prefix"])
    assert (added, removed) == ([], [])


def test_a_kb_legacy_list_shrinking_needs_no_drop_field(db):
    project_registry.replace(
        {"oldmill": {"repos": []}, "_kb_legacy": ["knowledge", "library"]},
        actor=ACTOR)
    added, removed = project_registry.replace(
        {"oldmill": {"repos": []}, "_kb_legacy": ["knowledge"]},
        actor="second", allow_populated=True)
    assert (added, removed) == ([], [])


def test_a_removed_slugs_fields_are_not_counted(db):
    project_registry.replace(
        {"oldmill": {"repos": []},
         "harrow": {"repos": [], "agent_prefix": "ha"}}, actor=ACTOR)
    added, removed = project_registry.replace(
        {"oldmill": {"repos": []}}, actor="second", allow_populated=True,
        remove=["harrow"])
    assert (added, removed) == ([], ["harrow"])


def test_the_field_refusal_message_carries_no_value(db):
    project_registry.replace(
        {"oldmill": {"repos": [], "agent_prefix": "do-not-print-me"}},
        actor=ACTOR)
    with pytest.raises(project_registry.RegistryWouldRemove) as e:
        project_registry.replace(
            {"oldmill": {"repos": []}}, actor="second", allow_populated=True)
    assert "do-not-print-me" not in str(e.value)
    assert "oldmill.agent_prefix" in str(e.value)


def test_one_drop_field_cannot_authorize_two_removals_whose_names_join_alike(db):
    # `foo.bar`'s `secret` and `foo`'s `bar.secret` both print as
    # foo.bar.secret; naming that once must not cover both.
    project_registry.replace(
        {"foo.bar": {"repos": [], "secret": "s1"},
         "foo": {"repos": [], "bar.secret": "s2"}}, actor=ACTOR)
    before = _rows(db)
    with pytest.raises(project_registry.RegistryWouldRemove) as e:
        project_registry.replace(
            {"foo.bar": {"repos": []}, "foo": {"repos": []}},
            actor="second", allow_populated=True,
            drop_fields=["foo.bar.secret"])
    assert e.value.unnamed_fields == ["foo.bar.secret", "foo.bar.secret"]
    assert _rows(db) == before


def test_a_nested_meta_key_flattened_to_a_dotted_key_is_a_removal(db):
    project_registry.replace(
        {"oldmill": {"repos": []}, "_meta": {"a": {"b": "x"}}}, actor=ACTOR)
    before = _rows(db)
    with pytest.raises(project_registry.RegistryWouldRemove) as e:
        project_registry.replace(
            {"oldmill": {"repos": []}, "_meta": {"a.b": "x"}},
            actor="second", allow_populated=True)
    assert e.value.unnamed_fields == ["_meta.a.b"]
    assert _rows(db) == before


@pytest.mark.parametrize("stored,file,removed", [
    # An empty map in a `_`-prefixed entry holds no field.
    ({"fields": {}}, {}, []),
    # A map replaced by a scalar loses every leaf under it.
    ({"fields": {"p": "x"}}, {"fields": "text"}, ["_meta.fields.p"]),
    # A scalar replaced by a map loses the scalar's own path.
    ({"note": "x"}, {"note": {"k": "v"}}, ["_meta.note"]),
    # A file that sets the whole entry to a non-map loses every leaf.
    ({"fields": {"p": "x", "q": "y"}}, "text",
     ["_meta.fields.p", "_meta.fields.q"]),
])
def test_a_meta_entry_changing_shape_reports_exactly_its_lost_leaves(
        db, stored, file, removed):
    project_registry.replace(
        {"oldmill": {"repos": []}, "_meta": stored}, actor=ACTOR)
    document = {"oldmill": {"repos": []}, "_meta": file}
    if not removed:
        assert project_registry.replace(
            document, actor="second", allow_populated=True) == ([], [])
        return
    with pytest.raises(project_registry.RegistryWouldRemove) as e:
        project_registry.replace(document, actor="second", allow_populated=True)
    assert e.value.unnamed_fields == removed


def test_a_crew_field_holding_an_empty_map_is_still_a_field(db):
    project_registry.replace(
        {"oldmill": {"repos": [], "repo_owners": {}}}, actor=ACTOR)
    with pytest.raises(project_registry.RegistryWouldRemove) as e:
        project_registry.replace(
            {"oldmill": {"repos": []}}, actor="second", allow_populated=True)
    assert e.value.unnamed_fields == ["oldmill.repo_owners"]
