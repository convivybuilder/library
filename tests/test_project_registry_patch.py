"""`project_registry.patch_crew` (CQ·249): the merge, the reused checks and
the `project_registry_edits` audit row behind `PATCH /registry/crews/{slug}`
(§6.8 of knowledge#1062).

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

ACTOR = "test, 2026-10-27"
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
    conn.execute("""
        CREATE TABLE project_registry_edits (
            id INTEGER PRIMARY KEY,
            slug TEXT NOT NULL,
            actor TEXT NOT NULL,
            at TEXT NOT NULL,
            changes TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()
    monkeypatch.setattr(store, "DB_PATH", path)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    return path


def _document(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return project_registry.registry_document(conn)
    finally:
        conn.close()


def _rows(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in
                conn.execute("SELECT * FROM project_registry ORDER BY slug")]
    finally:
        conn.close()


def _edits(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in
                conn.execute("SELECT * FROM project_registry_edits ORDER BY id")]
    finally:
        conn.close()


def _built(payload):
    """A fully-built row, the shape `register_crew` would have stored —
    `patch_crew` rebuilds its target through `build_crew_row`, so a fixture
    row must already be one of its outputs, not a hand-written fragment."""
    return project_registry.build_crew_row(payload)


@pytest.fixture
def loaded_db(db):
    """One public crew (`oldmill`) and one private crew (`quill`), each
    fully built — the same shape `register_crew` stores — with an alias and
    a repo of its own, so collision and private-line checks below have
    something real to collide with."""
    oldmill_slug, oldmill_row = _built(
        {"slug": "oldmill", "display": "Old Mill", "repos": ["oldmill-app"],
         "aliases": ["mill"]})
    quill_slug, quill_row = _built(
        {"slug": "quill", "display": "Quill", "repos": ["quill-app"],
         "aliases": ["scribe"], "private": True, "kb": "local"})
    project_registry.replace(
        {oldmill_slug: oldmill_row, quill_slug: quill_row}, actor=ACTOR)
    return db


def test_patch_refuses_without_registry_write(loaded_db):
    with pytest.raises(project_registry.RegistrationForbidden):
        project_registry.patch_crew(AGENT, "oldmill", {"display": "New"})
    assert _document(loaded_db)["oldmill"]["display"] == "Old Mill"


@pytest.mark.parametrize("payload", [None, [], "nope", {}])
def test_patch_refuses_a_body_that_is_not_a_populated_object(loaded_db, payload):
    with pytest.raises(project_registry.RegistrationRefused, match="JSON object"):
        project_registry.patch_crew(ORCHESTRATOR, "oldmill", payload)


def test_patch_refuses_a_body_naming_slug(loaded_db):
    with pytest.raises(project_registry.RegistrationRefused, match="slug"):
        project_registry.patch_crew(ORCHESTRATOR, "oldmill", {"slug": "newname"})


def test_patch_refuses_a_field_outside_the_declared_set(loaded_db):
    with pytest.raises(project_registry.RegistrationRefused, match="made_up_field"):
        project_registry.patch_crew(ORCHESTRATOR, "oldmill", {"made_up_field": 1})


@pytest.mark.parametrize("field", sorted(project_registry.ALWAYS_CARRIED_FIELDS))
def test_patch_refuses_null_on_an_always_carried_field(loaded_db, field):
    with pytest.raises(project_registry.RegistrationRefused, match=field):
        project_registry.patch_crew(ORCHESTRATOR, "oldmill", {field: None})
    assert _document(loaded_db)["oldmill"].get(field) is not None


def test_patch_refuses_a_value_failing_its_shape(loaded_db):
    with pytest.raises(project_registry.RegistrationRefused, match="aliases"):
        project_registry.patch_crew(ORCHESTRATOR, "oldmill", {"aliases": "not-a-list"})


def test_patch_refuses_an_extra_field_failing_its_shape(loaded_db):
    with pytest.raises(project_registry.RegistrationRefused, match="velocity_order"):
        project_registry.patch_crew(
            ORCHESTRATOR, "oldmill", {"velocity_order": "five"})


def test_patch_refuses_the_404_naming_the_slug(loaded_db):
    with pytest.raises(project_registry.CrewNotFound, match="harrow"):
        project_registry.patch_crew(ORCHESTRATOR, "harrow", {"display": "Harrow"})


def test_patch_refuses_the_404_for_a_metadata_key(db):
    project_registry.replace(
        {"oldmill": {"repos": ["oldmill-app"]}, "_scopes": {"platform": {}}},
        actor=ACTOR)
    with pytest.raises(project_registry.CrewNotFound):
        project_registry.patch_crew(ORCHESTRATOR, "_scopes", {"display": "x"})


def test_patch_refuses_on_an_empty_table(db):
    with pytest.raises(project_registry.RegistryNotLoaded):
        project_registry.patch_crew(ORCHESTRATOR, "oldmill", {"display": "x"})


def test_patch_refuses_while_writes_are_disabled(loaded_db, monkeypatch):
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    with pytest.raises(store.WritesDisabled):
        project_registry.patch_crew(ORCHESTRATOR, "oldmill", {"display": "x"})


def test_a_stored_row_breaking_the_privacy_invariant_refuses_every_patch(db):
    # A patch names none of the fields the invariant reads, so the
    # invariant can refuse only a row that already broke it on disk.
    slug, row = _built(
        {"slug": "quill", "display": "Quill", "repos": ["quill-app"],
         "private": True, "kb": "local"})
    row["task_text_public"] = True
    project_registry.replace({slug: row}, actor=ACTOR)
    with pytest.raises(project_registry.RegistrationRefused, match="private"):
        project_registry.patch_crew(ORCHESTRATOR, "quill", {"display": "Q"})
    assert _document(db)["quill"]["display"] == "Quill"


@pytest.mark.parametrize("value", [["scribe"], ["quill-app"]])
def test_patch_refuses_an_alias_colliding_with_another_entrys_alias_or_repo(
        loaded_db, value):
    with pytest.raises(project_registry.RegistrationCollision) as exc:
        project_registry.patch_crew(ORCHESTRATOR, "oldmill", {"aliases": value})
    assert str(exc.value) == f"{value[0]!r} is already claimed; pick a different name"
    assert _document(loaded_db)["oldmill"]["aliases"] == ["mill"]


def test_patch_allows_keeping_an_unchanged_alias(loaded_db):
    row = project_registry.patch_crew(
        ORCHESTRATOR, "oldmill",
        {"aliases": ["mill"], "display": "Old Mill Works"})
    assert _document(loaded_db)["oldmill"]["aliases"] == ["mill"]
    assert row["changed"] == ["display"]


@pytest.mark.parametrize("field,value", [
    ("repos", ["oldmill-app", "oldmill-site"]),
    ("ci_workflows", {"oldmill-app": []}),
    ("lifecycle", "sunset"),
    ("private", True),
    ("private", None),
    ("kb", "central"),
    ("kb_project", "quill"),
    ("task_text_public", True),
    ("owner", "someone-else"),
    ("github_owner", "someone-else"),
    ("github_owner_private", True),
    ("repo_owners", {"oldmill-app": "someone-else"}),
    ("kb_project_by_repo",
     {"oldmill-app": {"kb_project": "quill", "reason": "x"}}),
    ("kb_scope_exceptions", {"oldmill-app": {"kb_project": "quill"}}),
])
def test_patch_refuses_naming_an_operator_field(loaded_db, field, value):
    before = _rows(loaded_db)
    with pytest.raises(project_registry.RegistrationRefused) as exc:
        project_registry.patch_crew(
            ORCHESTRATOR, "oldmill", {field: value, "display": "Old Mill Works"})
    assert str(exc.value) == f"{field!r} is an operator change"
    assert _rows(loaded_db) == before
    assert _edits(loaded_db) == []


@pytest.mark.parametrize("field,values", [
    ("private", (True, False)),
    ("kb", ("local", "central")),
    ("task_text_public", (True, False)),
    ("kb_project", ("quill", None)),
])
def test_an_operator_refusal_says_nothing_about_the_stored_value(
        loaded_db, field, values):
    # `quill` is private and `oldmill` is not; every field a private crew's
    # rules read must answer both crews identically whatever is sent, or
    # the answer tells a caller without `registry.read` which is private.
    answers = []
    for slug in ("quill", "oldmill"):
        for value in values:
            with pytest.raises(project_registry.RegistrationRefused) as exc:
                project_registry.patch_crew(ORCHESTRATOR, slug, {field: value})
            answers.append(str(exc.value))
    assert set(answers) == {f"{field!r} is an operator change"}


def test_an_operator_refusal_reads_no_row(db):
    # Refused before the registry is read: an empty registry, which would
    # otherwise answer 503, and an unknown slug both get the same refusal.
    with pytest.raises(project_registry.RegistrationRefused,
                       match="'private' is an operator change"):
        project_registry.patch_crew(ORCHESTRATOR, "nobody", {"private": True})


def test_patch_keeps_an_unnamed_operator_field_as_stored(db):
    slug, row = _built(
        {"slug": "oldmill", "display": "Old Mill", "repos": ["oldmill-app"]})
    row["kb_project_by_repo"] = {
        "oldmill-app": {"kb_project": "oldmill", "reason": "satellite repo"}}
    project_registry.replace({slug: row}, actor=ACTOR)
    patched = project_registry.patch_crew(
        ORCHESTRATOR, "oldmill", {"display": "Old Mill Works"})
    assert patched["changed"] == ["display"]
    assert _document(db)["oldmill"]["kb_project_by_repo"] == row["kb_project_by_repo"]


def test_patch_answers_with_no_stored_field(loaded_db):
    row = project_registry.patch_crew(
        ORCHESTRATOR, "quill", {"display": "Quill Works"})
    assert set(row) == {"slug", "changed", "updated_at", "updated_by"}
    noop = project_registry.patch_crew(
        ORCHESTRATOR, "quill", {"display": "Quill Works"})
    assert set(noop) == {"slug", "changed", "updated_at", "updated_by"}
    assert noop["updated_at"] == row["updated_at"]


def test_patch_keeps_an_unnamed_field_as_stored(db):
    # A row loaded with `--replace` may lack a field the row builder would
    # default, or carry one this route does not declare; a patch naming
    # neither leaves both exactly as they were.
    slug, row = _built(
        {"slug": "oldmill", "display": "Old Mill", "repos": ["oldmill-app"]})
    row.pop("crew_cockpit", None)
    row["not_declared_yet"] = {"oldmill-app": "x"}
    project_registry.replace({slug: row}, actor=ACTOR)
    patched = project_registry.patch_crew(
        ORCHESTRATOR, "oldmill", {"display": "Old Mill Works"})
    assert patched["changed"] == ["display"]
    stored = _document(db)["oldmill"]
    assert "crew_cockpit" not in stored
    assert stored["not_declared_yet"] == {"oldmill-app": "x"}


def test_patch_null_removes_an_optional_field_and_restores_its_default(db):
    slug, row = _built(
        {"slug": "oldmill", "display": "Old Mill", "repos": ["oldmill-app"],
         "kb": "central", "crew_cockpit": False})
    project_registry.replace({slug: row}, actor=ACTOR)
    assert "crew_cockpit" not in _document(db)["oldmill"]
    patched = project_registry.patch_crew(
        ORCHESTRATOR, "oldmill", {"crew_cockpit": None})
    assert patched["changed"] == ["crew_cockpit"]
    assert _document(db)["oldmill"]["crew_cockpit"] is True


def test_patch_is_a_noop_when_nothing_changes(loaded_db):
    before = _rows(loaded_db)
    row = project_registry.patch_crew(
        ORCHESTRATOR, "oldmill", {"aliases": ["mill"], "display": "Old Mill"})
    assert row["changed"] == []
    assert _rows(loaded_db) == before
    assert _edits(loaded_db) == []


def test_patch_writes_one_audit_row_naming_the_before_and_after(loaded_db):
    row = project_registry.patch_crew(
        ORCHESTRATOR, "oldmill", {"display": "Old Mill Works"})
    assert row["changed"] == ["display"]
    edits = _edits(loaded_db)
    assert len(edits) == 1
    edit = edits[0]
    assert edit["slug"] == "oldmill"
    assert edit["actor"] == ACTOR
    assert edit["at"] == row["updated_at"]
    changes = json.loads(edit["changes"])
    assert changes == {"display": {"before": "Old Mill", "after": "Old Mill Works"}}


def test_patch_edits_and_keeps_an_extra_field(loaded_db):
    project_registry.patch_crew(
        ORCHESTRATOR, "oldmill", {"velocity_order": 5})
    assert _document(loaded_db)["oldmill"]["velocity_order"] == 5
    row2 = project_registry.patch_crew(
        ORCHESTRATOR, "oldmill", {"velocity_order": 9})
    assert row2["changed"] == ["velocity_order"]
    assert _document(loaded_db)["oldmill"]["velocity_order"] == 9


def test_patch_names_no_other_entry_when_the_registry_has_a_bad_kb_project(db):
    harrow_slug, harrow_row = _built(
        {"slug": "harrow", "display": "Harrow", "repos": ["harrow-app"]})
    project_registry.replace({
        "oldmill": {"repos": ["oldmill-app"], "private": True, "kb": "local",
                    "kb_scope_exceptions": {"oldmill-app": {"kb_project": "Bad_Slug"}}},
        harrow_slug: harrow_row,
    }, actor=ACTOR)
    with pytest.raises(project_registry.RegistrationRefused) as exc:
        project_registry.patch_crew(ORCHESTRATOR, "harrow", {"display": "Harrow Works"})
    assert "1 KB-project value" in str(exc.value)
    for leaked in ("oldmill", "Bad_Slug"):
        assert leaked not in str(exc.value)


def test_a_stored_value_failing_a_check_is_refused_without_its_value(db):
    # The stored row breaks a registration check on a field the caller
    # never named; the refusal names the field and never quotes the value,
    # since a `registry.write` holder need not hold `registry.read`.
    slug, row = _built(
        {"slug": "oldmill", "display": "Old Mill", "repos": ["oldmill-app"]})
    row["created_by"] = "secret-stored-value " * 3
    project_registry.replace({slug: row}, actor=ACTOR)
    with pytest.raises(project_registry.RegistrationRefused) as exc:
        project_registry.patch_crew(
            ORCHESTRATOR, "oldmill", {"display": "Old Mill Works"})
    assert str(exc.value).startswith("created_by: this crew's stored value")
    assert "secret-stored-value" not in str(exc.value)
    assert _document(db)["oldmill"]["display"] == "Old Mill"
