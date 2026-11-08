"""Whether a Reference Room document change may land (ADR-0022,
`issue_01M08YECXZ68NG1WWHG0NRD086`) — the frontmatter and checkout clauses,
both evaluated on every request.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import pathlib
import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

import app as libapp
import identity
import projects
import protocol
import reference
import reference_admission
import black_box_tables
import store

pytestmark = pytest.mark.usefixtures("kb_registry")

AGENT = "agent-token-fennick"
SYSTEM = "system-token"


@pytest.fixture
def client(tmp_path, monkeypatch):
    tokens = tmp_path / "api-tokens.json"
    tokens.write_text('{"%s": {"name": "fennick-agent", "products": ["fennick"]},'
                      ' "%s": {"name": "capture", "products": ["*"], '
                      '"kind": "system"}}' % (AGENT, SYSTEM))
    monkeypatch.setattr(identity, "TOKENS", tokens)
    return TestClient(libapp.app)


def _h(token):
    return {"Authorization": f"Bearer {token}",
            protocol.HEADER: str(protocol.CURRENT)}


EXISTING_PATH = "projects/library/specs/example.md"


def _fm(**over):
    out = {"id": "library/spec/example", "kind": "spec", "project": "library",
           "title": "An example", "status": "draft"}
    out.update(over)
    return out


# --- validate_frontmatter, the pure function ---------------------------------


def test_a_valid_frontmatter_has_no_errors():
    assert reference_admission.validate_frontmatter(_fm()) == []


def test_not_a_mapping():
    assert reference_admission.validate_frontmatter("not a dict") == [
        "frontmatter is not a JSON object"]


@pytest.mark.parametrize("field", ["id", "kind", "project", "title", "status"])
def test_missing_required_field(field):
    fm = _fm()
    del fm[field]
    errs = reference_admission.validate_frontmatter(fm)
    assert any(field in e for e in errs)


@pytest.mark.parametrize("field", ["id", "kind", "project", "title", "status"])
def test_empty_required_field(field):
    errs = reference_admission.validate_frontmatter(_fm(**{field: ""}))
    assert any(field in e for e in errs)


def test_invalid_kind():
    errs = reference_admission.validate_frontmatter(_fm(kind="not-a-kind"))
    assert any("invalid kind" in e for e in errs)


def test_a_build_plan_carrying_binds_topics_is_admitted():
    """issue_01M2TWRWHJMA6MF9NKZ9Z05KXN: the kind reaches this port with the
    validator, rather than after it the way `instruction-block` did."""
    fm = _fm(id="library/build-plans/example", kind="build-plan",
             binds_topics=["reference-room"])
    assert reference_admission.validate_frontmatter(fm) == []


def test_invalid_project():
    errs = reference_admission.validate_frontmatter(_fm(project="not-a-project"))
    assert any("invalid project" in e for e in errs)


def test_invalid_status():
    errs = reference_admission.validate_frontmatter(_fm(status="not-a-status"))
    assert any("invalid status" in e for e in errs)


def test_invalid_visibility():
    errs = reference_admission.validate_frontmatter(_fm(visibility="not-a-visibility"))
    assert any("invalid visibility" in e for e in errs)


@pytest.mark.parametrize("field", ["kind", "project", "status", "visibility"])
@pytest.mark.parametrize("value", [["a", "list"], {"a": "dict"}, 12345, True])
def test_non_string_scalar_field_refuses_cleanly_rather_than_500ing(field, value):
    """`in` against a frozenset hashes the probe — an unhashable value (a
    list or dict) must return a validation error, not raise `TypeError`
    (security-reviewer, PR #42)."""
    errs = reference_admission.validate_frontmatter(_fm(**{field: value}))
    assert any(f"invalid {field}" in e for e in errs)


def test_visibility_absent_is_fine():
    assert reference_admission.validate_frontmatter(_fm()) == []


def test_invalid_id_format():
    errs = reference_admission.validate_frontmatter(_fm(id="not-a-valid-id"))
    assert any("invalid id format" in e for e in errs)


@pytest.mark.parametrize("field", ["topics", "audience", "applies_to",
                                   "depends_on", "authors", "tools",
                                   "binds_crews", "binds_agents", "binds_topics",
                                   "binds_sessions", "binds_roles"])
def test_list_field_must_be_a_list(field):
    errs = reference_admission.validate_frontmatter(_fm(**{field: "not-a-list"}))
    assert any(f"{field} must be a list" in e for e in errs)


def test_list_field_as_a_real_list_is_fine():
    assert reference_admission.validate_frontmatter(
        _fm(topics=["a", "b"], authors=["operator"])) == []


def test_last_reviewed_valid_date():
    assert reference_admission.validate_frontmatter(
        _fm(last_reviewed="2026-08-19")) == []


def test_last_reviewed_invalid_format():
    errs = reference_admission.validate_frontmatter(_fm(last_reviewed="08/19/2026"))
    assert any("last_reviewed must be YYYY-MM-DD" in e for e in errs)


def test_last_reviewed_absent_is_fine():
    assert reference_admission.validate_frontmatter(_fm()) == []


def test_multiple_errors_all_reported():
    errs = reference_admission.validate_frontmatter(
        {"kind": "not-a-kind", "project": "not-a-project"})
    # id, title, status missing + invalid kind + invalid project = 5
    assert len(errs) == 5


# --- audience/binds_agents against a roster (issue_01M34SYVH2M48K2S0FQK3K6REY) --
#
# Admission is deliberately stricter here than `knowledge/scripts/validate.py`:
# that file only warns on `audience` drift and never checks `binds_agents`
# against the roster at all. Each pair below is identical except for the one
# value that is or is not on the roster passed in, so the case under test is
# the only thing that differs between the two.


def test_a_rostered_audience_admits():
    assert reference_admission.validate_frontmatter(
        _fm(audience=["architect"]), roster=frozenset({"architect"})) == []


def test_an_unrostered_audience_refuses_naming_the_value():
    errs = reference_admission.validate_frontmatter(
        _fm(audience=["ghost"]), roster=frozenset({"architect"}))
    assert errs == ["audience value not in roster: ghost"]


def test_a_rostered_binds_agents_admits():
    assert reference_admission.validate_frontmatter(
        _fm(binds_agents=["architect"]), roster=frozenset({"architect"})) == []


def test_an_unrostered_binds_agents_refuses_naming_the_value():
    errs = reference_admission.validate_frontmatter(
        _fm(binds_agents=["ghost"]), roster=frozenset({"architect"}))
    assert errs == ["binds_agents names an unknown agent: ghost"]


# --- binds_agents refuses a group or the operator marker even when
# rostered (NON_AGENT_GROUPS/operator_marker, ported from
# knowledge/scripts/validate.py's NON_AGENT_AUDIENCE) --
#
# `team` and the marker are both on the roster in every case below:
# `load_crew_registry.py` seeds `roster_agents` from the same superset
# `validate.py`'s `ALLOWED_AUDIENCE` is, so a plain membership test would
# pass either straight through.


def test_an_agent_in_binds_agents_admits_even_with_a_non_agent_on_the_roster():
    assert reference_admission.validate_frontmatter(
        _fm(binds_agents=["architect"]),
        roster=frozenset({"architect", "team"})) == []


def test_a_group_in_binds_agents_refuses_the_person_wording():
    errs = reference_admission.validate_frontmatter(
        _fm(binds_agents=["team"]), roster=frozenset({"architect", "team"}))
    assert errs == [
        "binds_agents names a person or a group, not an agent: team"]


def test_the_consultant_agent_in_binds_agents_admits():
    """`consultant` is a dispatchable agent (`agent-platform/agents/
    consultant`), so a block may bind it; it was once listed among the
    groups, which refused it with the person wording."""
    assert reference_admission.validate_frontmatter(
        _fm(binds_agents=["consultant"]),
        roster=frozenset({"architect", "consultant", "team"})) == []


def test_the_operator_marker_in_binds_agents_refuses_the_person_wording():
    errs = reference_admission.validate_frontmatter(
        _fm(binds_agents=["the-operator"]),
        roster=frozenset({"architect", "the-operator"}),
        operator_marker="the-operator")
    assert errs == [
        "binds_agents names a person or a group, not an agent: the-operator"]


def test_the_operator_marker_in_audience_is_unaffected():
    """`audience` may still name the operator — only `binds_agents` names
    what obeys a document."""
    assert reference_admission.validate_frontmatter(
        _fm(audience=["the-operator"]),
        roster=frozenset({"architect", "the-operator"}),
        operator_marker="the-operator") == []


def test_without_a_roster_neither_field_is_checked():
    """`roster=None` — not an empty set — means no live roster was offered,
    matching a caller that never read `frontmatter_needs_roster` as true. A
    value that would refuse against a real roster validates clean here."""
    assert reference_admission.validate_frontmatter(
        _fm(audience=["ghost"], binds_agents=["ghost"])) == []


def test_an_empty_roster_refuses_both_fields_naming_the_cause():
    """An empty roster is read the same way `agent-platform`'s indexer reads
    one: as unreadable rather than as a roster of zero legitimate names, so
    admission fails closed instead of refusing `ghost` by name."""
    errs = reference_admission.validate_frontmatter(
        _fm(audience=["ghost"], binds_agents=["ghost"]), roster=frozenset())
    assert errs == [
        "audience: the roster could not be read or is empty",
        "binds_agents: the roster could not be read or is empty"]


def test_an_absent_field_needs_no_roster():
    """Matching `frontmatter_needs_roster`: a frontmatter naming neither
    field is unaffected by whether a roster was given at all, including an
    empty one."""
    assert reference_admission.validate_frontmatter(
        _fm(), roster=frozenset()) == []


@pytest.mark.parametrize("field", ["audience", "binds_agents"])
def test_frontmatter_needs_roster_true_when_the_field_has_a_string(field):
    assert reference_admission.frontmatter_needs_roster(
        _fm(**{field: ["architect"]})) is True


@pytest.mark.parametrize("field", ["audience", "binds_agents"])
def test_frontmatter_needs_roster_false_when_the_field_is_absent_or_empty(field):
    assert reference_admission.frontmatter_needs_roster(_fm()) is False
    assert reference_admission.frontmatter_needs_roster(_fm(**{field: []})) is False


def test_frontmatter_needs_roster_false_on_a_non_dict():
    assert reference_admission.frontmatter_needs_roster("not a dict") is False


def test_binds_agents_needs_operator_marker_true_when_the_field_has_a_string():
    assert reference_admission.binds_agents_needs_operator_marker(
        _fm(binds_agents=["architect"])) is True


def test_binds_agents_needs_operator_marker_false_when_absent_or_empty():
    assert reference_admission.binds_agents_needs_operator_marker(_fm()) is False
    assert reference_admission.binds_agents_needs_operator_marker(
        _fm(binds_agents=[])) is False


def test_binds_agents_needs_operator_marker_false_for_audience_alone():
    """Only `binds_agents` is compared against the marker — an `audience`-
    only document should not cost a config read."""
    assert reference_admission.binds_agents_needs_operator_marker(
        _fm(audience=["architect"])) is False


def test_binds_agents_needs_operator_marker_false_on_a_non_dict():
    assert reference_admission.binds_agents_needs_operator_marker(
        "not a dict") is False


# --- the knowledge-tree-derived project catalogue (agent-platform#685,
# platform/decisions/0097 point 4) ---------------------------------------------
#
# `project_catalogue` widens the registry's admitted set (`projects.kb_slugs()`
# union `projects.scopes()` union `projects.kb_legacy()`) with whatever
# REGISTERED `<root>/projects/` directory the tree actually holds. A directory
# the registry does not name is refused even when it is on disk — an adopter's
# own project validates once it is in that install's registry, not on the
# strength of its name alone (platform/decisions/0097, Consequences).


@pytest.fixture
def registry(tmp_path, monkeypatch):
    """A synthetic `<root>/install/projects.json` `projects.kb_slugs()`,
    `projects.scopes()`, `projects.kb_legacy()` and `projects.private_repos()`
    all read, so admission can be tested without the real registry.

    **NEVER PASS `{}`.** Every one of those readers treats an empty dict the
    same as a missing file — `if not raw: return None` — so a genuinely empty
    registry reads as unreadable and every name gets refused for the wrong
    reason. Pass a harmless filler entry (`{"filler": {}}`) for "no registered
    projects today"; a real unreadable-registry case points `REGISTRY_PATH` at
    a file that does not exist instead of calling this."""
    def _set(entries):
        path = tmp_path / "projects.json"
        path.write_text(json.dumps(entries))
        monkeypatch.setattr(projects, "REGISTRY_PATH", path)
        projects.reload()
    yield _set
    # Clears the cache while `monkeypatch` still has `REGISTRY_PATH` pointed at
    # this fixture's tmp_path, so the next test's first read repopulates it from
    # the real registry `monkeypatch` restores immediately after (the same
    # ordering `test_item_writes.py`'s own registry fixture relies on).
    projects.reload()


def test_a_registered_project_directory_is_admitted(tmp_path, registry):
    """A name the registry's admitted set names is admitted once its
    directory exists on disk."""
    registry({"adopter-app": {"kb": "central"}})
    (tmp_path / "projects" / "adopter-app").mkdir(parents=True)

    cat = reference_admission.project_catalogue(tmp_path)
    assert "adopter-app" in cat.allowed
    assert cat.id_pattern.match("adopter-app/reference/x")
    fm = {"id": "adopter-app/reference/x", "kind": "reference",
          "project": "adopter-app", "title": "A doc", "status": "draft"}
    assert reference_admission.validate_frontmatter(fm, cat) == []
    # and without a catalogue, the baseline (computed from `kb_registry`'s
    # registry, not this fixture's) still refuses it
    assert reference_admission.validate_frontmatter(fm) != []


def test_a_registry_name_that_is_not_a_slug_is_dropped(registry):
    """`id_pattern_for` joins these names into a regex unescaped, so a name
    from the registry that fails `_PROJECT_SLUG` never reaches it: a
    non-string `kb_project` would fail `sorted()` at import, and a
    metacharacter would widen the pattern (both reviewers, PR #307 round 1)."""
    registry({"good": {"kb": "central"},
              "numbered": {"kb": "central", "kb_project": 123},
              "flagged": {"kb": "split", "kb_project": True},
              "dotted": {"kb": "central", "kb_project": "a.b"},
              "_scopes": {"plat|form": {}, "scope": {}},
              "_kb_legacy": [".*", "legacy"]})
    names = reference_admission._registry_projects()
    assert names == frozenset({"good", "scope", "legacy"})
    pattern = reference_admission.id_pattern_for(names)
    assert pattern.match("good/reference/x")
    assert not pattern.match("anything/reference/x")


def test_the_registry_set_is_read_off_one_snapshot(registry, monkeypatch):
    """The three pieces come from one registry generation: each public
    accessor re-checks the file and could see a newer one mid-call
    (security-reviewer, PR #307 round 1)."""
    registry({"good": {"kb": "central"}, "_scopes": {"scope": {}}})

    def stale(*_a, **_k):
        raise AssertionError("read through a public accessor, not one snapshot")

    for name in ("kb_slugs", "scopes", "kb_legacy"):
        monkeypatch.setattr(projects, name, stale)
    assert reference_admission._registry_projects() == frozenset({"good", "scope"})


def test_an_on_disk_directory_not_in_the_registry_is_refused(tmp_path, registry):
    """An adopter's own project, on disk but absent from the registry, no
    longer validates on the strength of its name alone
    (platform/decisions/0097 point 4) — it must be IN the registry."""
    registry({"filler": {}})
    (tmp_path / "projects" / "adopter-app").mkdir(parents=True)

    cat = reference_admission.project_catalogue(tmp_path)
    assert "adopter-app" not in cat.allowed
    assert "not a registered project" in cat.refused["adopter-app"]
    assert "0097" in cat.refused["adopter-app"]


def test_a_missing_project_directory_is_refused(tmp_path, registry):
    registry({"filler": {}})
    (tmp_path / "projects").mkdir(parents=True)
    cat = reference_admission.project_catalogue(tmp_path)
    assert "nowhere" not in cat.allowed
    assert reference_admission.project_dir_refusal(tmp_path, "nowhere") == (
        "'nowhere' is not a directory")
    fm = {"id": "nowhere/reference/x", "kind": "reference", "project": "nowhere",
          "title": "A doc", "status": "draft"}
    errs = reference_admission.validate_frontmatter(fm, cat)
    assert any("invalid project: 'nowhere'" in e for e in errs), errs


def test_root_unset_admits_only_the_registered_baseline(registry):
    """`KNOWLEDGE_ROOT` unset is today's behaviour — deploying this code ahead
    of the plist change that sets it must not widen anything, including
    through `pending`. The baseline is exactly the registry's admitted set,
    read live rather than from an import-time snapshot."""
    registry({"demoproj": {"kb": "central"}})
    cat = reference_admission.project_catalogue(None, pending="brand-new")
    assert cat.allowed == frozenset({"demoproj"})
    assert cat.id_pattern.pattern == reference_admission.id_pattern_for(
        frozenset({"demoproj"})).pattern
    assert "brand-new" not in cat.allowed


def test_an_unreadable_projects_directory_admits_only_the_registered_baseline(
        tmp_path, registry):
    registry({"demoproj": {"kb": "central"}})
    root = tmp_path / "tree"
    root.mkdir()
    # No `projects/` directory at all under a real root: `iterdir` raises OSError.
    cat = reference_admission.project_catalogue(root)
    assert cat.allowed == frozenset({"demoproj"})


@pytest.mark.parametrize("scope", ["shared", "convivy", "utility", "engineering"])
def test_a_retired_name_is_refused(tmp_path, registry, scope):
    registry({"filler": {}})
    (tmp_path / "projects" / scope).mkdir(parents=True)
    cat = reference_admission.project_catalogue(tmp_path)
    assert scope not in cat.allowed
    assert "retired scope" in cat.refused[scope]


def test_a_private_crew_name_is_refused(tmp_path, registry):
    registry({"secretproject": {"private": True, "repos": ["secret-repo"]}})
    (tmp_path / "projects" / "secretproject").mkdir(parents=True)
    cat = reference_admission.project_catalogue(tmp_path)
    assert "secretproject" not in cat.allowed
    assert "private crew" in cat.refused["secretproject"]
    assert "0043" in cat.refused["secretproject"]


# `_name_refusal` must refuse every name a private crew answers to, not just
# its slug and `repos` (`projects.private_repos()`'s set) — an alias, a
# `display` name, a `crew` name and a `home_dir`/`dirs` basename each reach
# `project_catalogue`'s `pending` path with no directory on disk to key a
# refusal off, so each is exercised through `pending` rather than a mkdir'd
# `projects/<name>` (reviewer finding, PR #160).


def test_a_private_crews_alias_is_refused(tmp_path, registry):
    registry({"secretproject": {"private": True, "aliases": ["secret-alias"]}})
    (tmp_path / "projects").mkdir(parents=True)
    cat = reference_admission.project_catalogue(tmp_path, pending="secret-alias")
    assert "secret-alias" not in cat.allowed
    assert "private crew" in cat.refused["secret-alias"]


def test_a_private_crews_display_name_is_refused(tmp_path, registry):
    registry({"secretproject": {"private": True, "display": "Secret-Display"}})
    (tmp_path / "projects").mkdir(parents=True)
    cat = reference_admission.project_catalogue(tmp_path, pending="secret-display")
    assert "secret-display" not in cat.allowed
    assert "private crew" in cat.refused["secret-display"]


def test_a_private_crews_crew_name_is_refused(tmp_path, registry):
    registry({"secretproject": {"private": True, "crew": "secret-crew"}})
    (tmp_path / "projects").mkdir(parents=True)
    cat = reference_admission.project_catalogue(tmp_path, pending="secret-crew")
    assert "secret-crew" not in cat.allowed
    assert "private crew" in cat.refused["secret-crew"]


def test_a_private_crews_home_dir_basename_is_refused(tmp_path, registry):
    registry({"secretproject": {"private": True,
                                 "home_dir": str(tmp_path / "Convivy" / "secret-home")}})
    (tmp_path / "projects").mkdir(parents=True)
    cat = reference_admission.project_catalogue(tmp_path, pending="secret-home")
    assert "secret-home" not in cat.allowed
    assert "private crew" in cat.refused["secret-home"]


def test_a_truthy_non_true_private_value_does_not_count(tmp_path, registry):
    """`private: "true"` (a string, not the boolean) does not mark a crew
    private, matching agent-platform#685, which drew the same line.

    `not-really-secret` is independently registered as its own unit, so the
    only thing isolated here is the truthy-vs-`True` distinction, not the
    registry-membership requirement this test is not about."""
    registry({"almostprivate": {"private": "true", "aliases": ["not-really-secret"]},
              "not-really-secret": {"kb": "central"}})
    (tmp_path / "projects").mkdir(parents=True)
    cat = reference_admission.project_catalogue(tmp_path, pending="not-really-secret")
    assert "not-really-secret" in cat.allowed


@pytest.mark.parametrize("pending", ["secretproject", "SecretProject", "shared"])
def test_a_pending_private_or_retired_name_is_refused(tmp_path, registry, pending):
    """Mirrors agent-platform#685's
    `test_a_private_crews_first_document_fails_the_write_precheck`: a caller
    cannot use the pending-write path to assert a private or retired name a
    directory listing would have refused."""
    registry({"secretproject": {"private": True, "repos": ["secret-repo"]}})
    (tmp_path / "projects").mkdir(parents=True)
    cat = reference_admission.project_catalogue(tmp_path, pending=pending)
    assert pending not in cat.allowed


@pytest.mark.parametrize("name", ["Upper", ".hidden", "under_score", "9lives"])
def test_a_bad_slug_is_refused(tmp_path, registry, name):
    registry({"filler": {}})
    (tmp_path / "projects" / name).mkdir(parents=True)
    cat = reference_admission.project_catalogue(tmp_path)
    assert name not in cat.allowed
    assert "not a plain lowercase directory name" in cat.refused[name]


def test_an_escaping_symlink_is_refused(tmp_path, registry):
    registry({"filler": {}})
    root = tmp_path / "tree"
    (root / "projects").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "projects" / "escapee").symlink_to(outside)
    cat = reference_admission.project_catalogue(root)
    assert "escapee" not in cat.allowed
    assert "outside the knowledge tree" in cat.refused["escapee"]


def test_a_registered_pending_project_is_admitted_by_name(tmp_path, registry):
    """The project a write is about to create is admitted before its
    directory exists — first-save-of-a-new-project has no directory yet —
    when the registry names it (platform/decisions/0097 point 4)."""
    registry({"brand-new": {"kb": "central"}})
    (tmp_path / "projects").mkdir(parents=True)
    assert not (tmp_path / "projects" / "brand-new").exists()
    cat = reference_admission.project_catalogue(tmp_path, pending="brand-new")
    assert "brand-new" in cat.allowed
    assert cat.id_pattern.match("brand-new/reference/x")


def test_an_unregistered_pending_project_is_refused(tmp_path, registry):
    """A pending name the registry does not know is refused rather than
    created on the strength of its name alone (platform/decisions/0097
    point 4) — this is the behaviour `test_a_registered_pending_project_
    is_admitted_by_name` above used to lack."""
    registry({"filler": {}})
    (tmp_path / "projects").mkdir(parents=True)
    cat = reference_admission.project_catalogue(tmp_path, pending="brand-new")
    assert "brand-new" not in cat.allowed
    assert "not a registered project" in cat.refused["brand-new"]


def test_an_unreadable_registry_admits_on_disk_directories_and_refuses_pending(
        tmp_path, monkeypatch, registry):
    """platform/decisions/0097 point 4's unreadable-registry fallback: trust
    what the knowledge tree already holds rather than refuse every document
    under it, but refuse every `pending` name, since there is no on-disk
    directory to trust instead for a project not yet created."""
    monkeypatch.setattr(projects, "REGISTRY_PATH", tmp_path / "no-such-file.json")
    projects.reload()
    assert projects.private_repos() is None

    (tmp_path / "projects" / "library").mkdir(parents=True)
    cat = reference_admission.project_catalogue(tmp_path, pending="also-new")
    assert "library" in cat.allowed
    assert "also-new" not in cat.allowed
    assert "could not be read" in cat.refused["also-new"]


# --- the route -----------------------------------------------------------------


@pytest.mark.parametrize("token", [AGENT, SYSTEM])
def test_every_identity_can_ask(client, held, token):
    held("library/spec/example", "operator")
    r = client.post("/reference/admission",
                    json={"frontmatter": _fm(), "owner": "operator"},
                    headers=_h(token))
    assert r.status_code == 200
    body = r.json()
    # Keys rather than whole-response equality: the protocol states that
    # within a version the Library only ADDS, and that clients must ignore
    # response fields they do not recognise. A test asserting an exact
    # response body forbids the one kind of change the contract allows.
    assert body["may_land"] is True and body["errors"] == []


def test_unauthenticated_is_refused(client):
    r = client.post("/reference/admission", json={"frontmatter": _fm()},
                    headers={protocol.HEADER: str(protocol.CURRENT)})
    assert r.status_code == 401


def test_invalid_frontmatter_reports_may_land_false(client):
    r = client.post("/reference/admission",
                    json={"frontmatter": _fm(kind="not-a-kind")},
                    headers=_h(AGENT))
    assert r.status_code == 200
    body = r.json()
    assert body["may_land"] is False
    assert any("invalid kind" in e for e in body["errors"])


def test_a_list_shaped_kind_does_not_500_through_the_route(client):
    """The end-to-end version of `test_non_string_scalar_field_refuses_
    cleanly_rather_than_500ing` — through the real HTTP path rather than the
    pure function directly (security-reviewer, PR #42)."""
    r = client.post("/reference/admission",
                    json={"frontmatter": _fm(kind=["spec"])},
                    headers=_h(AGENT))
    assert r.status_code == 200
    body = r.json()
    assert body["may_land"] is False
    assert any("invalid kind" in e for e in body["errors"])


def test_frontmatter_is_required(client):
    r = client.post("/reference/admission", json={}, headers=_h(AGENT))
    assert r.status_code == 400
    assert "frontmatter" in r.json()["error"]


def test_non_dict_body_is_refused(client):
    r = client.post("/reference/admission", json=["not", "a", "dict"],
                    headers=_h(AGENT))
    assert r.status_code == 400


def test_non_dict_frontmatter_is_a_may_land_false_not_a_400(client):
    """A malformed frontmatter value is itself a validation finding, not a
    request-shape error — the caller asked a well-formed question and got a
    well-formed (negative) answer. Names an owner so the assertion isolates to
    the frontmatter clause: a non-dict frontmatter has no `id` to look up, so
    the checkout clause is left unrun regardless."""
    r = client.post("/reference/admission",
                    json={"frontmatter": "not a dict", "owner": "operator"},
                    headers=_h(AGENT))
    assert r.status_code == 200
    body = r.json()
    assert body["may_land"] is False
    assert body["errors"] == ["frontmatter is not a JSON object"]
    assert body["clauses_checked"] == ["frontmatter"]


# --- drift against knowledge/scripts/validate.py --------------------------------


def test_the_validation_constants_have_not_drifted():
    """Two validators over one schema disagreeing would make "does this land"
    depend on which door you came through — the same shape
    `test_the_ranking_constants_have_not_drifted` already guards for the BM25
    constants.

    **Two halves, and both run in CI.** The literals below fail the moment
    THIS copy changes. The cross-repo comparison reads `scripts/validate.py`
    from the knowledge checkout `KNOWLEDGE_ROOT` names; CI checks
    `convivy/knowledge` out beside this repo and sets it. When the variable is
    set and `validate.py` is not there, the test FAILS, so a broken checkout
    step cannot turn back into a silent skip. It skips only when neither the
    variable nor `~/Convivy/knowledge` exists (a machine with no knowledge
    checkout).
    """
    assert reference_admission.ALLOWED_STATUSES == {
        "draft", "current", "deprecated", "archived"}
    assert reference_admission.ALLOWED_VISIBILITY == {"public", "team", "internal"}
    assert reference_admission.REQUIRED == (
        "id", "kind", "project", "title", "status")
    assert reference_admission.LIST_FIELDS == (
        "topics", "audience", "applies_to", "depends_on", "authors", "tools",
        "binds_crews", "binds_agents", "binds_topics", "binds_sessions",
        "binds_roles")
    assert reference_admission.ISO_DATE.pattern == r"^\d{4}-\d{2}-\d{2}$"
    assert reference_admission.NON_AGENT_GROUPS == {"team", "contractor"}
    assert "bead" in reference_admission.ALLOWED_KINDS
    assert "instruction-block" in reference_admission.ALLOWED_KINDS
    assert "build-plan" in reference_admission.ALLOWED_KINDS

    configured = os.environ.get("KNOWLEDGE_ROOT")
    if configured:
        source = pathlib.Path(configured) / "scripts/validate.py"
        assert source.is_file(), (
            f"KNOWLEDGE_ROOT is set to {configured!r} but {source} does not "
            "exist; the checkout step is broken, and skipping here would hide it")
    else:
        source = pathlib.Path.home() / "Convivy/knowledge/scripts/validate.py"
        if not source.is_file():
            pytest.skip("knowledge not checked out; the cross-repo half cannot run")
    text = source.read_text()

    # **EVERY CONSTANT IS COMPARED BEFORE ANYTHING FAILS, and the failure
    # names all of them.** This guard's job is to enumerate a multi-site
    # seam, and a chain of bare asserts stops at the first mismatch — so it
    # reports one drifted site while others are equally drifted, and it
    # under-counts exactly when someone is relying on it to tell them how
    # many sites to fix. That happened on 2026-09-06: `bannister` was missing
    # from both `ALLOWED_PROJECTS` and `ID_PATTERN`, the projects assertion
    # failed first, and the work item was written up as a one-line fix
    # because the pattern's drift was still hidden behind it.
    drifted = []

    def compare(name, found, ours):
        """Records a mismatch; for sets it records the DIFFERENCE rather than
        both memberships. `ALLOWED_PROJECTS` holds two dozen entries, so
        printing both sides in full buries the one entry that moved in fifty
        names of noise — the reader has to diff two unordered dumps by eye to
        learn what this already knows."""
        if found == ours:
            return
        if isinstance(found, set) and isinstance(ours, set):
            parts = []
            if found - ours:
                parts.append(f"missing here: {sorted(found - ours)}")
            if ours - found:
                parts.append(f"not in validate.py: {sorted(ours - found)}")
            drifted.append(f"  {name}: " + "; ".join(parts))
        else:
            drifted.append(f"  {name}: validate.py has {found!r}, "
                           f"reference_admission has {ours!r}")

    kinds = re.search(r"ALLOWED_KINDS = \{(.*?)\n\}", text, re.S)
    assert kinds, "could not find ALLOWED_KINDS in validate.py"
    compare("ALLOWED_KINDS",
            set(re.findall(r'"([a-z-]+)"', kinds.group(1))),
            set(reference_admission.ALLOWED_KINDS))

    # `ALLOWED_PROJECTS` is NOT compared here. `reference_admission.py` no
    # longer carries that literal (platform/decisions/0097 point 4): its
    # admitted project set is the registry's, not a copy of
    # `validate.py`'s own list, so there is no second literal left to drift
    # against it. `validate.py` keeps its own closed list until
    # platform/decisions/0097 point 5 drops it.

    statuses = re.search(r"ALLOWED_STATUSES = \{([^}]+)\}", text)
    assert statuses, "could not find ALLOWED_STATUSES in validate.py"
    compare("ALLOWED_STATUSES",
            set(re.findall(r'"(\w+)"', statuses.group(1))),
            set(reference_admission.ALLOWED_STATUSES))

    visibility = re.search(r"ALLOWED_VISIBILITY = \{([^}]+)\}", text)
    assert visibility, "could not find ALLOWED_VISIBILITY in validate.py"
    compare("ALLOWED_VISIBILITY",
            set(re.findall(r'"(\w+)"', visibility.group(1))),
            set(reference_admission.ALLOWED_VISIBILITY))

    required = re.search(r"REQUIRED = \(([^)]+)\)", text)
    assert required, "could not find REQUIRED in validate.py"
    compare("REQUIRED",
            tuple(re.findall(r'"(\w+)"', required.group(1))),
            reference_admission.REQUIRED)

    # The ID pattern's project alternatives are NOT compared here either, for
    # the same reason `ALLOWED_PROJECTS` is not: `reference_admission.
    # ID_PATTERN` is now derived from the registry's admitted set
    # (platform/decisions/0097 point 4), not from a copy of `validate.py`'s
    # `ALLOWED_PROJECTS`, so the two patterns have no shared literal left to
    # compare.

    list_fields = re.search(r"LIST_FIELDS = \(([^)]+)\)", text)
    assert list_fields, "could not find LIST_FIELDS in validate.py"
    compare("LIST_FIELDS",
            tuple(re.findall(r'"(\w+)"', list_fields.group(1))),
            reference_admission.LIST_FIELDS)

    iso_date = re.search(r'ISO_DATE = re\.compile\(r"([^"]+)"\)', text)
    assert iso_date, "could not find ISO_DATE in validate.py"
    compare("ISO_DATE",
            iso_date.group(1),
            reference_admission.ISO_DATE.pattern)

    # `NON_AGENT_GROUPS` has no cross-repo half. knowledge#968 (E7·19 step 4)
    # deletes `validate.py`'s `NON_AGENT_AUDIENCE` along with its roster copy,
    # because this module's admission check now owns the person/group refusal
    # on `audience` and `binds_agents`. The literal pin in the first half of
    # this test is what still fails when this copy changes.

    assert not drifted, (
        f"{len(drifted)} constant(s) have drifted from "
        f"knowledge/scripts/validate.py:\n" + "\n".join(drifted))


# ---------------------------------------------------------------- the checkout clause


def test_the_clause_passes_when_the_owner_holds_the_checkout():
    assert reference_admission.check_checkout(
        "d/x", held_by="operator@example.com", owner="operator@example.com") == []


def test_the_clause_refuses_when_nobody_holds_a_checkout():
    errs = reference_admission.check_checkout("d/x", held_by=None, owner="operator")
    assert errs and "no live checkout" in errs[0]
    assert "d/x" in errs[0]


def test_the_clause_refuses_when_someone_else_holds_it_and_names_them():
    errs = reference_admission.check_checkout("d/x", held_by="ann", owner="operator")
    assert errs and "ann" in errs[0] and "operator" in errs[0]


@pytest.fixture
def held(tmp_path, monkeypatch):
    """A real `reference_checkouts` table, so the clause's lookup is exercised
    rather than mocked. Returns a function that takes a checkout on a doc."""
    db = tmp_path / "control.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE reference_checkouts (
            id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, holder TEXT NOT NULL,
            run_id TEXT, taken_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL,
            released_at TEXT, release_reason TEXT, released_by TEXT);
        CREATE UNIQUE INDEX ix_reference_checkouts_live_doc
            ON reference_checkouts (doc_id) WHERE released_at IS NULL;
    """)
    conn.commit()
    conn.close()
    monkeypatch.setattr(store, "DB_PATH", db)
    # A knowledge index holding `_fm()`'s document, so the checkout clause
    # these tests exercise runs on an EXISTING document rather than being
    # waived as a create (see the create tests below).
    index = tmp_path / "index.db"
    iconn = sqlite3.connect(index)
    iconn.execute("CREATE TABLE docs (id TEXT PRIMARY KEY, project TEXT, "
                  "source_ref TEXT)")
    iconn.execute("INSERT INTO docs (id, project, source_ref) VALUES (?, ?, ?)",
                  ("library/spec/example", "library", EXISTING_PATH))
    iconn.commit()
    iconn.close()
    monkeypatch.setattr(reference, "INDEX_DB", index)

    def take(doc_id, holder, *, when=None):
        # The service's own clock and format, rather than a literal: a fixed
        # timestamp is already past the heartbeat grace by the time anyone runs
        # the suite, so a "live" checkout would read as dead and the admitting
        # half of the pair would fail for the wrong reason.
        if when is None:
            when = _dt.datetime.now(_dt.timezone.utc).replace(
                tzinfo=None).isoformat(" ")
        c = sqlite3.connect(db)
        c.execute("INSERT INTO reference_checkouts "
                  "(id, doc_id, holder, taken_at, heartbeat_at) "
                  "VALUES (?,?,?,?,?)", (f"co-{doc_id}", doc_id, holder, when, when))
        c.commit()
        c.close()
    return take


def test_omitting_owner_is_refused_naming_the_checkout_clause(client):
    """The checkout clause is unconditional now
    (issue_01M19QW42CFK7TDZCKWK18ECY5): a caller that does not name an owner
    is refused rather than answered on frontmatter alone, and the refusal
    names the checkout clause."""
    r = client.post("/reference/admission", headers=_h(AGENT),
                    json={"frontmatter": _fm()})
    assert r.status_code == 200
    body = r.json()
    assert body["may_land"] is False
    assert body["clauses_checked"] == ["frontmatter", "checkout"]
    assert body["clauses_refused"] == ["checkout"]
    assert any("owner" in e for e in body["errors"])


def test_naming_an_owner_who_holds_it_admits(client, held):
    """The admitting half of the pair. Identical to the refusing case below but
    for who holds the checkout, which is what proves the answer was read."""
    held("library/spec/example", "operator")
    r = client.post("/reference/admission", headers=_h(AGENT),
                    json={"frontmatter": _fm(), "owner": "operator"})
    body = r.json()
    assert body["may_land"] is True
    assert body["clauses_checked"] == ["frontmatter", "checkout"]
    assert body["clauses_refused"] == []


def test_naming_an_owner_who_does_not_hold_it_refuses(client, held):
    held("library/spec/example", "ann")
    r = client.post("/reference/admission", headers=_h(AGENT),
                    json={"frontmatter": _fm(), "owner": "operator"})
    body = r.json()
    assert body["may_land"] is False
    assert body["clauses_refused"] == ["checkout"]
    assert "ann" in body["errors"][0]


def test_an_unheld_document_refuses(client, held):
    r = client.post("/reference/admission", headers=_h(AGENT),
                    json={"frontmatter": _fm(), "owner": "operator"})
    body = r.json()
    assert body["may_land"] is False
    assert body["clauses_refused"] == ["checkout"]
    assert "no live checkout" in body["errors"][0]


def test_a_dead_checkout_reads_as_unheld_rather_than_as_a_conflict(client, held):
    """An expired hold is the row still occupying the document, and the next
    writer is entitled to it — so it refuses for the absence of a checkout
    rather than naming the stale holder as a conflict."""
    held("library/spec/example", "ann", when="2020-01-01 00:00:00")
    r = client.post("/reference/admission", headers=_h(AGENT),
                    json={"frontmatter": _fm(), "owner": "operator"})
    body = r.json()
    assert body["may_land"] is False
    assert "no live checkout" in body["errors"][0]
    assert "ann" not in body["errors"][0]


def test_a_document_failing_both_clauses_when_no_owner_is_named_is_refused_naming_both(client):
    """CLAUSE ISOLATION, and the reason it is not optional: a refusal case that
    fails BOTH clauses at once stays green against an implementation checking
    only one. Bad frontmatter and no owner fail both clauses at once — this
    used to be the omitted-owner clause-isolation case, before the checkout
    clause became unconditional and started failing here too."""
    r = client.post("/reference/admission", headers=_h(AGENT),
                    json={"frontmatter": _fm(kind="not-a-kind")})
    body = r.json()
    assert body["may_land"] is False
    assert body["clauses_checked"] == ["frontmatter", "checkout"]
    assert body["clauses_refused"] == ["frontmatter", "checkout"]


def test_a_document_failing_both_clauses_when_the_owner_does_not_hold_it_is_refused_naming_both(client, held):
    """The other flavor of failing both clauses: an owner is named, so the
    checkout clause actually runs its lookup, and it refuses because someone
    else holds the checkout rather than because no owner was given."""
    held("library/spec/example", "ann")
    r = client.post("/reference/admission", headers=_h(AGENT),
                    json={"frontmatter": _fm(kind="not-a-kind"), "owner": "operator"})
    body = r.json()
    assert body["may_land"] is False
    assert body["clauses_checked"] == ["frontmatter", "checkout"]
    assert body["clauses_refused"] == ["frontmatter", "checkout"]
    assert "ann" in body["errors"][-1]


def test_an_empty_id_with_an_owner_refuses_on_frontmatter(client):
    """Superseding an earlier version of this test that asserted a 400. The
    route no longer refuses the request shape for an unusable id: the
    frontmatter clause already refuses it, and answering 200 keeps the
    invariant that a content problem is never a request-shape error."""
    r = client.post("/reference/admission", headers=_h(AGENT),
                    json={"frontmatter": _fm(id=""), "owner": "operator"})
    assert r.status_code == 200
    body = r.json()
    assert body["may_land"] is False
    assert body["clauses_refused"] == ["frontmatter"]


def test_an_explicit_null_owner_is_refused_the_same_as_omission(client):
    """An earlier version of this route treated `"owner" in payload` as key
    PRESENCE, so an explicit null entered the owner-given branch and
    `_claim_owner` fell back to the token identity — the clause would have run
    against the SERVICE rather than the person, the refuse-every-save failure
    the gate exists to prevent, reached through null instead of omission. Now
    that omission itself is refused, null and omission both take the same
    path: `payload.get("owner")` reads `None` either way."""
    r = client.post("/reference/admission", headers=_h(AGENT),
                    json={"frontmatter": _fm(), "owner": None})
    body = r.json()
    assert body["may_land"] is False
    assert body["clauses_checked"] == ["frontmatter", "checkout"]
    assert body["clauses_refused"] == ["checkout"]


def test_a_non_string_id_is_a_validation_error(client):
    """Pre-existing gap found while adding the clause: the presence loop saw the
    key and the format check was guarded by `isinstance`, so `{"id": 123}`
    validated clean and was answered may_land true."""
    r = client.post("/reference/admission", headers=_h(AGENT),
                    json={"frontmatter": _fm(id=123)})
    body = r.json()
    assert body["may_land"] is False
    assert any("id must be a string" in e for e in body["errors"])


def test_an_unusable_id_with_an_owner_is_a_refusal_not_a_400(client, held):
    """The invariant `test_non_dict_frontmatter_is_a_may_land_false_not_a_400`
    holds even on the new path: a frontmatter content problem is answered 200
    with may_land false, never a request-shape error. The clause is left unrun
    and `clauses_checked` says so rather than claiming a check that could not
    happen — safe only because a non-string id now fails validation, so the
    answer is already a refusal."""
    r = client.post("/reference/admission", headers=_h(AGENT),
                    json={"frontmatter": _fm(id=123), "owner": "operator"})
    assert r.status_code == 200
    body = r.json()
    assert body["may_land"] is False
    assert body["clauses_checked"] == ["frontmatter"]
    assert body["clauses_refused"] == ["frontmatter"]


def test_the_frontmatter_clause_refuses_alone_while_the_checkout_clause_passes(client, held):
    """THE MIRROR ISOLATION CASE, which the first round was missing. The other
    tests show the checkout clause failing with frontmatter valid; this shows
    frontmatter failing while the checkout clause PASSES, so an active checkout
    clause cannot mask or taint the frontmatter clause's own refusal."""
    held("library/spec/example", "operator")
    r = client.post("/reference/admission", headers=_h(AGENT),
                    json={"frontmatter": _fm(kind="not-a-kind"), "owner": "operator"})
    body = r.json()
    assert body["may_land"] is False
    assert body["clauses_checked"] == ["frontmatter", "checkout"]
    assert body["clauses_refused"] == ["frontmatter"]


def test_naming_an_owner_without_reference_read_is_forbidden(client, held, monkeypatch):
    """The disclosure gate. Evaluating the clause tells a caller whether an
    arbitrary id is checked out, which the dedicated status route denies by
    requiring `reference.read`. Withholding only the holder's NAME was not
    enough: "no live checkout is held on X" versus "checked out to another
    holder" still distinguishes held from unheld. So the whole clause is gated.

    Monkeypatched because no role today holds `reference.admit` without
    `reference.read` — the gate is unreachable in the current role table and is
    here for the split the code comment treats as possible."""
    held("library/spec/example", "operator")
    real = identity.authorized

    def only_admit(ident, cap):
        return False if cap == "reference.read" else real(ident, cap)

    monkeypatch.setattr(identity, "authorized", only_admit)
    r = client.post("/reference/admission", headers=_h(AGENT),
                    json={"frontmatter": _fm(), "owner": "operator"})
    assert r.status_code == 403


def test_omitting_owner_needs_no_reference_read(client, monkeypatch):
    """The `reference.read` gate guards the checkout LOOKUP, not the refusal
    for an absent owner — refusing an omitted owner discloses nothing about
    any document, so it does not need the capability a real lookup does."""
    real = identity.authorized

    def only_admit(ident, cap):
        return False if cap == "reference.read" else real(ident, cap)

    monkeypatch.setattr(identity, "authorized", only_admit)
    r = client.post("/reference/admission", headers=_h(AGENT),
                    json={"frontmatter": _fm()})
    assert r.status_code == 200
    body = r.json()
    assert body["may_land"] is False
    assert body["clauses_checked"] == ["frontmatter", "checkout"]


# --- a create: an id the index does not hold ------------------------------------
#
# `POST /reference/documents/{id}/checkout` 404s an id the index does not hold,
# so before the waiver every new document was refused here once `kb apply`
# began asking this route (agent-platform #966; the steward's kb_create of
# platform/decisions/0098 on 2026-10-22).

NEW_ID = "library/spec/not-yet-written"
NEW_PATH = "projects/library/specs/not-yet-written.md"


def test_a_create_waives_the_checkout_clause(client, held):
    r = client.post("/reference/admission", headers=_h(AGENT),
                    json={"frontmatter": _fm(id=NEW_ID), "owner": "operator",
                          "path": NEW_PATH, "create": True})
    body = r.json()
    assert body["may_land"] is True, body
    assert body["clauses_checked"] == ["frontmatter"]
    assert body["clauses_refused"] == []
    assert body["clauses_waived"] == ["checkout"]


def test_an_indexed_document_is_not_waived(client, held):
    """The refusing half of the pair: identical but for the id being indexed."""
    r = client.post("/reference/admission", headers=_h(AGENT),
                    json={"frontmatter": _fm(), "owner": "operator"})
    body = r.json()
    assert body["may_land"] is False
    assert body["clauses_refused"] == ["checkout"]
    assert body["clauses_waived"] == []


def test_a_create_someone_else_holds_still_refuses_naming_them(client, held):
    held(NEW_ID, "ann")
    r = client.post("/reference/admission", headers=_h(AGENT),
                    json={"frontmatter": _fm(id=NEW_ID), "owner": "operator",
                          "path": NEW_PATH, "create": True})
    body = r.json()
    assert body["may_land"] is False
    assert body["clauses_refused"] == ["checkout"]
    assert "ann" in body["errors"][0]
    assert body["clauses_waived"] == []


def test_an_unreadable_index_waives_nothing(client, held, tmp_path, monkeypatch):
    monkeypatch.setattr(reference, "INDEX_DB", tmp_path / "no-such-index.db")
    r = client.post("/reference/admission", headers=_h(AGENT),
                    json={"frontmatter": _fm(id=NEW_ID), "owner": "operator",
                          "path": NEW_PATH, "create": True})
    body = r.json()
    assert body["may_land"] is False
    assert body["clauses_refused"] == ["checkout"]
    assert "could not be read to bind" in body["errors"][0]


def test_a_busy_index_waives_nothing(client, held, tmp_path, monkeypatch):
    """`reading()` re-raises SQLITE_BUSY unchanged, so `_index_rows_for` must
    catch it itself and refuse the named path: a busy index answers the
    binding refusal, neither a waiver nor the 503 `database_busy` gives."""
    path = tmp_path / "busy-index.db"
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("CREATE TABLE docs (id TEXT PRIMARY KEY, project TEXT, "
                   "source_ref TEXT)")
    holder.execute("BEGIN EXCLUSIVE")
    real_connect = reference.connect

    def busy_connect():
        conn = real_connect()
        conn.close()
        # A genuine SQLITE_BUSY, raised by SQLite rather than constructed.
        return sqlite3.connect(path, timeout=0.01)

    monkeypatch.setattr(reference, "connect", busy_connect)
    try:
        r = client.post("/reference/admission", headers=_h(AGENT),
                        json={"frontmatter": _fm(id=NEW_ID), "owner": "operator",
                              "path": NEW_PATH, "create": True})
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["may_land"] is False
    assert body["clauses_refused"] == ["checkout"]
    assert body["clauses_waived"] == []
    assert "could not be read to bind" in body["errors"][0]


def test_a_document_outside_a_bound_scope_is_not_waived(client, held, tmp_path, monkeypatch):
    """The index lookup is unscoped, so an existing document counts as existing
    whatever the caller may read; and a bound identity gets no waiver at all.
    An indexed id and an absent one get the same refusal text, so the answer
    cannot tell it a document exists outside its scope."""
    tokens = tmp_path / "bound-tokens.json"
    tokens.write_text(json.dumps({AGENT: {"name": "fennick-agent", "products": ["fennick"],
                                          "profile": "fennick/daytime"}}))
    monkeypatch.setattr(identity, "TOKENS", tokens)
    for doc_id in ("library/spec/example", NEW_ID):
        r = client.post("/reference/admission", headers=_h(AGENT),
                        json={"frontmatter": _fm(id=doc_id), "owner": "operator",
                              "path": NEW_PATH, "create": True})
        body = r.json()
        assert body["may_land"] is False, (doc_id, body)
        assert body["clauses_refused"] == ["checkout"], doc_id
        assert body["errors"][0] == f"{NEW_PATH} is not indexed as {doc_id}"


def _bind(tmp_path, monkeypatch):
    tokens = tmp_path / "bound-tokens.json"
    tokens.write_text(json.dumps({AGENT: {"name": "fennick-agent", "products": ["fennick"],
                                          "profile": "fennick/daytime"}}))
    monkeypatch.setattr(identity, "TOKENS", tokens)


def test_a_bound_identity_cannot_write_another_documents_path(client, held, tmp_path, monkeypatch):
    """Round 1's defect for a bound identity (security-reviewer, PR #306
    round 2): hold a checkout on one id and write another document's file."""
    _bind(tmp_path, monkeypatch)
    held(NEW_ID, "operator")
    r = client.post("/reference/admission", headers=_h(AGENT),
                    json={"frontmatter": _fm(id=NEW_ID), "owner": "operator",
                          "path": EXISTING_PATH})
    body = r.json()
    assert body["may_land"] is False
    assert body["clauses_refused"] == ["checkout"]
    assert body["errors"][0] == f"{EXISTING_PATH} is not indexed as {NEW_ID}"


def test_a_bound_identity_writing_its_own_held_document_is_admitted(client, held, tmp_path, monkeypatch):
    _bind(tmp_path, monkeypatch)
    held("library/spec/example", "operator")
    r = client.post("/reference/admission", headers=_h(AGENT),
                    json={"frontmatter": _fm(), "owner": "operator",
                          "path": EXISTING_PATH})
    body = r.json()
    assert body["may_land"] is True, body
    assert body["clauses_checked"] == ["frontmatter", "checkout"]


def test_an_unreadable_index_refuses_a_held_update_naming_a_path(client, held, tmp_path, monkeypatch):
    """Without the index the path cannot be bound to its document, so the
    write is refused rather than checked on the frontmatter id alone, which
    would let a held id carry a write to another document's file."""
    held(NEW_ID, "operator")
    monkeypatch.setattr(reference, "INDEX_DB", tmp_path / "no-such-index.db")
    r = client.post("/reference/admission", headers=_h(AGENT),
                    json={"frontmatter": _fm(id=NEW_ID), "owner": "operator",
                          "path": EXISTING_PATH})
    body = r.json()
    assert body["may_land"] is False
    assert body["clauses_refused"] == ["checkout"]
    assert "could not be read to bind" in body["errors"][0]


def test_a_create_naming_no_path_is_not_waived(client, held):
    """Without the path the write commits, nothing ties the frontmatter id to
    a file, so nothing is waived (security-reviewer, PR #306 round 1)."""
    r = client.post("/reference/admission", headers=_h(AGENT),
                    json={"frontmatter": _fm(id=NEW_ID), "owner": "operator"})
    body = r.json()
    assert body["may_land"] is False
    assert body["clauses_refused"] == ["checkout"]
    assert body["clauses_waived"] == []


def test_an_existing_path_labelled_with_a_fresh_id_is_refused(client, held):
    """The round-1 attack: overwrite an indexed document by labelling the new
    content with an id the index does not hold. The path binds the clause to
    the document indexed there, and the mismatch is refused."""
    r = client.post("/reference/admission", headers=_h(AGENT),
                    json={"frontmatter": _fm(id=NEW_ID), "owner": "operator",
                          "path": EXISTING_PATH, "create": True})
    body = r.json()
    assert body["may_land"] is False
    assert body["clauses_refused"] == ["checkout"]
    assert body["clauses_waived"] == []
    assert "library/spec/example" in body["errors"][0]


def test_an_existing_path_labelled_with_an_id_the_caller_holds_is_refused(client, held):
    """The same attack before the waiver existed: hold a checkout on one id
    and write another document's file under it."""
    held(NEW_ID, "operator")
    r = client.post("/reference/admission", headers=_h(AGENT),
                    json={"frontmatter": _fm(id=NEW_ID), "owner": "operator",
                          "path": EXISTING_PATH, "create": True})
    body = r.json()
    assert body["may_land"] is False
    assert body["clauses_refused"] == ["checkout"]


def test_an_existing_path_with_its_own_id_and_a_held_checkout_is_admitted(client, held):
    held("library/spec/example", "operator")
    r = client.post("/reference/admission", headers=_h(AGENT),
                    json={"frontmatter": _fm(), "owner": "operator",
                          "path": EXISTING_PATH})
    body = r.json()
    assert body["may_land"] is True, body
    assert body["clauses_checked"] == ["frontmatter", "checkout"]


def test_a_new_path_whose_id_is_indexed_elsewhere_is_not_waived(client, held):
    r = client.post("/reference/admission", headers=_h(AGENT),
                    json={"frontmatter": _fm(), "owner": "operator",
                          "path": NEW_PATH, "create": True})
    body = r.json()
    assert body["may_land"] is False
    assert body["clauses_waived"] == []


@pytest.mark.parametrize("path", [7, "", "   "])
def test_a_non_string_or_empty_path_is_a_400(client, held, path):
    r = client.post("/reference/admission", headers=_h(AGENT),
                    json={"frontmatter": _fm(), "owner": "operator", "path": path})
    assert r.status_code == 400


def test_an_over_long_path_is_a_400(client, held):
    r = client.post("/reference/admission", headers=_h(AGENT),
                    json={"frontmatter": _fm(), "owner": "operator",
                          "path": "projects/" + "x" * store.MAX_REF + ".md"})
    assert r.status_code == 400


@pytest.mark.parametrize("create", ["true", 1, None])
def test_a_non_boolean_create_is_a_400(client, held, create):
    r = client.post("/reference/admission", headers=_h(AGENT),
                    json={"frontmatter": _fm(id=NEW_ID), "owner": "operator",
                          "path": NEW_PATH, "create": create})
    assert r.status_code == 400


@pytest.mark.parametrize("extra", [{}, {"create": False}])
def test_an_update_the_index_has_not_caught_up_with_is_not_waived(client, held, extra):
    """The round-2 finding: a document that just merged but is not yet
    indexed reads as absent at both its path and its id. A second write to it
    is an update, so the committer does not send `create: true`, and the
    clause runs and refuses it for want of a checkout."""
    r = client.post("/reference/admission", headers=_h(AGENT),
                    json={"frontmatter": _fm(id=NEW_ID), "owner": "operator",
                          "path": NEW_PATH, **extra})
    body = r.json()
    assert body["may_land"] is False
    assert body["clauses_refused"] == ["checkout"]
    assert body["clauses_waived"] == []
    assert "no live checkout" in body["errors"][0]


def test_a_crew_scoped_agent_creating_in_its_own_project_is_admitted(client, held, tmp_path, monkeypatch):
    """The shape of a crew session's `kb_create`: an agent credential that
    names its crew and is limited to its own project, creating a document the
    index does not hold yet."""
    crew_token = "crew-token-library"
    tokens = tmp_path / "crew-tokens.json"
    tokens.write_text(json.dumps({crew_token: {"name": "co-library", "products": ["library"],
                                               "crew": "library"}}))
    monkeypatch.setattr(identity, "TOKENS", tokens)
    r = client.post("/reference/admission", headers=_h(crew_token),
                    json={"frontmatter": _fm(id=NEW_ID), "owner": "co-library",
                          "path": NEW_PATH, "create": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["may_land"] is True, body
    assert body["clauses_waived"] == ["checkout"]


# --- end to end: the bug this issue exists to fix -------------------------------


def test_an_orient_editor_save_for_an_adopters_own_project_is_admitted(
        client, held, monkeypatch, tmp_path, registry):
    """`issue_01M3NS89KVRXB368VA4TJ38MDG`: a project `kb_create` made on a
    fresh install, saved through Orient's in-window editor, is admitted once
    that install's registry names it (platform/decisions/0097 point 4) —
    not for want of a row in a static baseline, and not on its name alone."""
    registry({"adopter-app": {"kb": "central"}})
    (tmp_path / "projects" / "adopter-app").mkdir(parents=True)
    monkeypatch.setattr(reference_admission, "KNOWLEDGE_ROOT", str(tmp_path))
    held("adopter-app/reference/example", "operator")
    r = client.post("/reference/admission", headers=_h(AGENT),
                    json={"frontmatter": _fm(id="adopter-app/reference/example",
                                             project="adopter-app"),
                          "owner": "operator"})
    assert r.status_code == 200
    body = r.json()
    assert body["may_land"] is True, body


@pytest.mark.parametrize("legacy, admitted", [(["lantern-kit"], True), ([], False)])
def test_a_legacy_slug_with_documents_is_admitted_only_while_kb_legacy_names_it(
        client, held, monkeypatch, tmp_path, registry, legacy, admitted):
    """A legacy slug that holds documents in the knowledge tree and names no
    registry unit is admitted only through `_kb_legacy`
    (platform/decisions/0097 point 1). This is why CQ·227 must not deploy
    before the registry change that lists it: without the entry, the same save
    is refused."""
    registry({"filler": {}, "_kb_legacy": legacy})
    (tmp_path / "projects" / "lantern-kit").mkdir(parents=True)
    monkeypatch.setattr(reference_admission, "KNOWLEDGE_ROOT", str(tmp_path))
    doc_id = "lantern-kit/reference/example"
    held(doc_id, "operator")
    r = client.post("/reference/admission", headers=_h(AGENT),
                    json={"frontmatter": _fm(id=doc_id, project="lantern-kit"),
                          "owner": "operator"})
    assert r.status_code == 200
    body = r.json()
    assert body["may_land"] is admitted, body
    if not admitted:
        assert body["clauses_refused"] == ["frontmatter"]
        assert any("lantern-kit" in e for e in body["errors"]), body


# --- the live roster, read fresh through the route (issue_01M34SYVH2M48K2S0FQK3K6REY) --

ORCHESTRATOR = "orchestrator-token"


@pytest.fixture
def roster_db(tmp_path, monkeypatch):
    """A real `roster_agents` table on the same `control.db` `held` uses,
    seeded with one name so a roster read is never mistaken for an
    unreadable/empty one just because the table under test is new."""
    db = tmp_path / "control.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE roster_agents (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            source_doc TEXT,
            created_at TEXT NOT NULL,
            created_by TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            updated_by TEXT NOT NULL
        );
    """ + black_box_tables.DDL)
    conn.execute(
        "INSERT INTO roster_agents "
        "(name, created_at, created_by, updated_at, updated_by) "
        "VALUES ('architect', '2026-01-01', 'test', '2026-01-01', 'test')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)


def test_a_name_added_by_put_is_admitted_and_removed_by_delete_is_refused(
        client, roster_db, tmp_path, monkeypatch):
    """The definition of done's own shape: presented as real credentials over
    the real routes rather than asserted from the roster table, so a `PUT`
    that reached a different table than `GET /roster` reads from would still
    fail this the way it would fail a real caller."""
    tokens = tmp_path / "api-tokens-with-orchestrator.json"
    tokens.write_text(json.dumps({
        AGENT: {"name": "fennick-agent", "products": ["fennick"]},
        ORCHESTRATOR: {"name": "co-library", "products": ["*"],
                       "kind": "orchestrator"},
    }))
    monkeypatch.setattr(identity, "TOKENS", tokens)
    # `ORDINARY_REGISTRY` alone registers no central/split kb project, and
    # `_fm()`'s default `project: library` now needs the registry to name it
    # (platform/decisions/0097 point 4) rather than a hardcoded literal.
    black_box_tables.use_registry(tmp_path, monkeypatch, {
        **black_box_tables.ORDINARY_REGISTRY,
        "library": {"kb": "central"},
    })
    fm = _fm(audience=["newcomer"])

    before = client.post("/reference/admission", headers=_h(AGENT),
                          json={"frontmatter": fm}).json()
    assert any("audience value not in roster: newcomer" in e
               for e in before["errors"])

    put = client.put("/roster/newcomer", headers=_h(ORCHESTRATOR))
    assert put.status_code == 200

    after = client.post("/reference/admission", headers=_h(AGENT),
                        json={"frontmatter": fm}).json()
    assert "frontmatter" not in after["clauses_refused"]

    delete = client.delete("/roster/newcomer", headers=_h(ORCHESTRATOR))
    assert delete.status_code == 200

    again = client.post("/reference/admission", headers=_h(AGENT),
                        json={"frontmatter": fm}).json()
    assert any("audience value not in roster: newcomer" in e
               for e in again["errors"])


@pytest.fixture
def empty_roster_db(tmp_path, monkeypatch):
    """A real `roster_agents` table with zero rows — distinct from
    `roster_db`, which seeds one row specifically so a roster read is never
    mistaken for empty just because the table under test is new. This
    fixture is that case: a table that exists and reads clean, genuinely
    empty."""
    db = tmp_path / "control.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE roster_agents (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            source_doc TEXT,
            created_at TEXT NOT NULL,
            created_by TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            updated_by TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()
    monkeypatch.setattr(store, "DB_PATH", db)


def test_an_audience_against_a_genuinely_empty_roster_refuses_through_the_route(
        client, empty_roster_db):
    """The pure-function version of this case (`test_an_empty_roster_refuses_
    both_fields_naming_the_cause`) passes a literal `frozenset()`; this sends
    real credentials through the real route against a real, freshly-created
    `roster_agents` table that nobody has written a row to, the way
    `test_a_name_added_by_put_is_admitted_and_removed_by_delete_is_refused`
    exercises the populated case."""
    r = client.post("/reference/admission", headers=_h(AGENT),
                    json={"frontmatter": _fm(audience=["architect"])})
    assert r.status_code == 200
    body = r.json()
    assert body["may_land"] is False
    assert any("audience: the roster could not be read or is empty" in e
               for e in body["errors"])


# --- the operator marker, read fresh through the route -----------------------


OPERATOR_MARKER = "the-operator"


@pytest.fixture
def operator_config(tmp_path, monkeypatch):
    """A synthetic operator config, so no test reads the real
    `~/.config/convivy/operator.json` of the machine running it — the same
    fixture shape `test_load_crew_registry.py` uses."""
    path = tmp_path / "operator.json"
    path.write_text(json.dumps({"operator_marker": OPERATOR_MARKER}))
    monkeypatch.setenv("CONVIVY_OPERATOR_CONFIG", str(path))
    return path


def test_the_operator_marker_in_binds_agents_refuses_through_the_route(
        client, roster_db, operator_config):
    """The marker is rostered, the same as `team` is in the pure-function
    pair above — `load_crew_registry.py` seeds it alongside the real
    agents — so this proves the refusal is not just an accident of the
    marker being unrostered."""
    conn = sqlite3.connect(store.DB_PATH)
    conn.execute(
        "INSERT INTO roster_agents "
        "(name, created_at, created_by, updated_at, updated_by) "
        "VALUES (?, '2026-01-01', 'test', '2026-01-01', 'test')",
        (OPERATOR_MARKER,))
    conn.commit()
    conn.close()
    r = client.post("/reference/admission", headers=_h(AGENT),
                    json={"frontmatter": _fm(binds_agents=[OPERATOR_MARKER])})
    body = r.json()
    assert body["may_land"] is False
    assert any(
        f"binds_agents names a person or a group, not an agent: "
        f"{OPERATOR_MARKER}" in e for e in body["errors"])


def test_an_unreadable_operator_config_refuses_binds_agents_through_the_route(
        client, roster_db, monkeypatch, tmp_path):
    """`binds_agents` needs the marker to compare against; a config the
    service cannot read refuses it outright, naming the cause, the same
    fail-closed shape an empty roster gets — confirmed red without the
    check by `test_the_operator_marker_in_binds_agents_refuses_through_the_
    route` above, which depends on the config being readable."""
    monkeypatch.setenv("CONVIVY_OPERATOR_CONFIG",
                       str(tmp_path / "missing.json"))
    r = client.post("/reference/admission", headers=_h(AGENT),
                    json={"frontmatter": _fm(binds_agents=["architect"])})
    body = r.json()
    assert body["may_land"] is False
    assert "binds_agents: the operator marker could not be read" in body["errors"]


def test_an_unreadable_operator_config_leaves_audience_only_unaffected(
        client, roster_db, monkeypatch, tmp_path):
    """`audience` never needs the marker — a document naming only `audience`
    is unaffected by whether the operator config can be read at all."""
    monkeypatch.setenv("CONVIVY_OPERATOR_CONFIG",
                       str(tmp_path / "missing.json"))
    r = client.post("/reference/admission", headers=_h(AGENT),
                    json={"frontmatter": _fm(audience=["architect"])})
    body = r.json()
    assert "frontmatter" not in body["clauses_refused"], body
