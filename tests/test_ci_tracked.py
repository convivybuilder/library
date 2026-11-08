"""ci_tracked.py — the tracked (repo, workflow) pairs and the registry-derived
repo set `GET /signals/ci-capture` checks them against.

Moved from `tests/test_ci_result_fetch.py` when `bin/ci_result_fetch.py` was
retired (issue_01M2XGCSGYW6F0QEZAQ0VKN7CQ), then moved again off the
hand-maintained `TRACKED`/`NOT_CAPTURED`/`NOT_CAPTURED_BY_NAME` literals onto
the project registry, when `tracked()` and `not_captured()` started reading
it directly (issue_01M5GYC0EBWS1JBQF7DA6FQ1CG). Every test here reads a
fixture `{slug: project}` dict or a fixture registry file, never this
machine's real registry.
"""
from __future__ import annotations

import json

import ci_tracked
import projects
import pytest


@pytest.fixture(autouse=True)
def _no_real_projects_registry(tmp_path, monkeypatch):
    """`projects.REGISTRY_PATH` points at a path that does not exist, so no
    test here reads this machine's real `<root>/install/projects.json`.
    A test of the default registry overrides it again, to its own fixture."""
    monkeypatch.setattr(projects, "REGISTRY_PATH", tmp_path / "does-not-exist.json")
    projects.reload()
    yield
    projects.reload()


def test_workflow_slug_strips_the_directory_and_extension():
    assert ci_tracked.workflow_slug(".github/workflows/e2e-stage-smoke.yml") \
        == "e2e-stage-smoke"


# --- tracked() ----------------------------------------------------------------


def test_tracked_defaults_a_repo_with_no_ci_workflows_field_to_ci_yml():
    registry = {
        "bramwell": {"class": "product", "lifecycle": "active", "owner": "convivy",
                   "repos": ["bramwell"]},
    }
    assert ci_tracked.tracked(registry) == [
        {"repo": "convivy/bramwell", "workflow_file": "ci.yml", "product": "bramwell",
         "on_pull_request": True}]


def test_tracked_names_every_workflow_a_repos_ci_workflows_entry_lists():
    """`ci.yml` runs fennick's pull-request checks; `e2e-stage-smoke.yml` is the
    only source for Orient's stage-smoke tile, which went stale from
    2026-10-02 when an earlier edit replaced it rather than adding `ci.yml`
    beside it."""
    registry = {
        "fennick": {"class": "product", "lifecycle": "active", "owner": "convivy",
                 "repos": ["fennick"],
                 "ci_workflows": {"fennick": ["ci.yml", "e2e-stage-smoke.yml"]}},
    }
    workflows = sorted(e["workflow_file"] for e in ci_tracked.tracked(registry)
                       if e["repo"] == "convivy/fennick")
    assert workflows == ["ci.yml", "e2e-stage-smoke.yml"]


def test_tracked_names_a_repo_under_its_own_renamed_workflow():
    """`build-deploy.yml` runs `convivy/brochure`'s pull-request checks, named by
    its unit's own `ci_workflows` entry rather than the `ci.yml` default."""
    registry = {
        "convivy-orient": {"class": "product", "lifecycle": "active",
                           "owner": "convivy", "repos": ["brochure"],
                           "ci_workflows": {"brochure": ["build-deploy.yml"]}},
    }
    assert ci_tracked.tracked(registry) == [
        {"repo": "convivy/brochure", "workflow_file": "build-deploy.yml",
         "product": "convivy-orient", "on_pull_request": True}]


# --- on_pull_request and the additional-workflow override (CQ·218) -------------


def test_tracked_marks_a_stage_smoke_workflow_not_on_pull_request():
    """`e2e-stage-smoke.yml` runs only on `schedule` and `workflow_dispatch`
    against `main`, so it never reports at a pull request's head — the
    merge-admission evaluator must not wait on a result from it."""
    registry = {
        "example-app": {"class": "product", "lifecycle": "active", "owner": "convivy",
                        "repos": ["example-app"],
                        "ci_workflows": {"example-app": ["ci.yml", "e2e-stage-smoke.yml"]}},
    }
    by_workflow = {e["workflow_file"]: e["on_pull_request"]
                  for e in ci_tracked.tracked(registry) if e["repo"] == "convivy/example-app"}
    assert by_workflow == {"ci.yml": True, "e2e-stage-smoke.yml": False}


def test_a_repo_whose_only_workflow_is_stage_smoke_keeps_it_required():
    """The exemption applies only beside another tracked workflow, so a repo
    cannot leave itself with no required pull-request check by giving its
    only workflow this name (CQ·218 round 1, security-reviewer)."""
    registry = {
        "example-app": {"class": "product", "lifecycle": "active", "owner": "convivy",
                        "repos": ["example-app"],
                        "ci_workflows": {"example-app": ["e2e-stage-smoke.yml"]}},
    }
    by_workflow = {e["workflow_file"]: e["on_pull_request"]
                  for e in ci_tracked.tracked(registry) if e["repo"] == "convivy/example-app"}
    assert by_workflow == {"e2e-stage-smoke.yml": True}


def test_tracked_adds_knowledges_kb_write_trailer_workflow():
    """`library`'s registry unit names no `ci_workflows` entry for
    `knowledge` at all, so the plain registry walk would default it to
    `ci.yml` alone. `kb-write-trailer.yml` gates every pull request there
    and must be tracked regardless (CQ·218)."""
    registry = {
        "library": {"class": "project", "lifecycle": "active", "owner": "convivy",
                   "repos": ["library", "knowledge"]},
    }
    by_workflow = {e["workflow_file"]: e["on_pull_request"]
                  for e in ci_tracked.tracked(registry) if e["repo"] == "convivy/knowledge"}
    assert by_workflow == {"ci.yml": True, "kb-write-trailer.yml": True}


def test_tracked_does_not_add_the_knowledge_workflow_to_a_same_named_repo_elsewhere():
    """The addition is keyed on the exact `owner/repo`, never the bare name."""
    registry = {
        "someone-elses": {"class": "product", "lifecycle": "active",
                          "owner": "someone-else", "repos": ["knowledge"]},
    }
    by_workflow = {e["workflow_file"] for e in ci_tracked.tracked(registry)
                  if e["repo"] == "someone-else/knowledge"}
    assert by_workflow == {"ci.yml"}


def test_not_captured_omits_knowledge_even_with_an_explicit_empty_ci_workflows():
    """`_ADDITIONAL_WORKFLOWS` forces `kb-write-trailer.yml` onto
    `convivy/knowledge` even when a unit's own `ci_workflows` map opts it
    out of everything else — `tracked()` and `not_captured()` must still
    agree that the repo is tracked, not both at once."""
    registry = {
        "library": {"class": "project", "lifecycle": "active", "owner": "convivy",
                   "repos": ["knowledge"], "ci_workflows": {"knowledge": []}},
    }
    assert ci_tracked.not_captured(registry) == {}
    workflows = {e["workflow_file"] for e in ci_tracked.tracked(registry)}
    assert workflows == {"kb-write-trailer.yml"}


def test_tracked_excludes_a_repo_whose_ci_workflows_entry_is_empty():
    registry = {
        "signals": {"class": "project", "lifecycle": "active", "owner": "convivy",
                    "repos": ["signals"], "ci_workflows": {"signals": []}},
    }
    assert ci_tracked.tracked(registry) == []


def test_tracked_excludes_a_private_or_sunset_project_exactly_like_deck_tracked_repos():
    """`tracked()` walks the same `(slug, project, repo, owner)` set
    `deck_tracked_repos()` does, so a repo it would withhold from Deck (a
    sunset project, a private one) is withheld from CI tracking too."""
    registry = {
        "retired-product": {"class": "product", "lifecycle": "sunset",
                            "owner": "convivy", "repos": ["retired-product"]},
        "stealth-product": {"class": "product", "lifecycle": "active",
                            "owner": "convivy", "private": True,
                            "repos": ["stealth-product"]},
    }
    assert ci_tracked.tracked(registry) == []


def test_tracked_reads_the_registry_module_by_default(monkeypatch, tmp_path):
    """No `registry` argument means `projects.projects()`, proven against a
    fixture file via `projects.REGISTRY_PATH`."""
    registry_file = tmp_path / "projects.json"
    registry_file.write_text(json.dumps({
        "bramwell": {"class": "product", "lifecycle": "active", "owner": "convivy",
                   "repos": ["bramwell"]},
    }))
    monkeypatch.setattr(projects, "REGISTRY_PATH", registry_file)
    projects.reload()
    assert ci_tracked.tracked() == [
        {"repo": "convivy/bramwell", "workflow_file": "ci.yml", "product": "bramwell",
         "on_pull_request": True}]


# --- not_captured() ------------------------------------------------------------


def test_not_captured_names_a_repo_whose_ci_workflows_entry_is_empty():
    registry = {
        "signals": {"class": "project", "lifecycle": "active", "owner": "convivy",
                    "repos": ["signals"], "ci_workflows": {"signals": []}},
    }
    assert ci_tracked.not_captured(registry) == {
        "convivy/signals": ci_tracked.NOT_CAPTURED_REASON}


def test_not_captured_omits_a_repo_with_no_ci_workflows_entry():
    registry = {
        "bramwell": {"class": "product", "lifecycle": "active", "owner": "convivy",
                   "repos": ["bramwell"]},
    }
    assert ci_tracked.not_captured(registry) == {}


def test_not_captured_names_a_private_persons_repo_under_its_own_github_owner():
    """A repo with no pull-request CI whose GitHub owner is a person's own
    account (`driftwood-notes`, under `example-person`) is named by its full
    `owner/repo` here. The former `NOT_CAPTURED_BY_NAME` literal kept that
    owner out of this shipped file by keying on the bare name instead; there
    is no literal left to ship it in now, so that split is gone too."""
    registry = {
        "driftwood-notes": {"class": "project", "lifecycle": "active",
                            "owner": "driftwood",
                            "github_owner": "example-person",
                            "repos": ["driftwood-notes"],
                            "ci_workflows": {"driftwood-notes": []}},
    }
    assert ci_tracked.not_captured(registry) == {
        "example-person/driftwood-notes": ci_tracked.NOT_CAPTURED_REASON}


def test_no_repo_is_both_tracked_and_not_captured():
    registry = {
        "fennick": {"class": "product", "lifecycle": "active", "owner": "convivy",
                 "repos": ["fennick", "fennick-commercial"],
                 "ci_workflows": {"fennick-commercial": []}},
    }
    tracked_repos = {e["repo"] for e in ci_tracked.tracked(registry)}
    assert not tracked_repos & set(ci_tracked.not_captured(registry))


def test_not_captured_reads_the_registry_module_by_default(monkeypatch, tmp_path):
    registry_file = tmp_path / "projects.json"
    registry_file.write_text(json.dumps({
        "signals": {"class": "project", "lifecycle": "active", "owner": "convivy",
                    "repos": ["signals"], "ci_workflows": {"signals": []}},
    }))
    monkeypatch.setattr(projects, "REGISTRY_PATH", registry_file)
    projects.reload()
    assert ci_tracked.not_captured() == {
        "convivy/signals": ci_tracked.NOT_CAPTURED_REASON}


# --- registry_unusable_reason() -------------------------------------------------


def test_registry_unusable_reason_is_none_for_an_explicit_registry():
    """A caller passing its own `{slug: project}` dict already knows what an
    empty one means to it — `projects.available()` has nothing to say about
    a dict that never came from a read, empty or not."""
    assert ci_tracked.registry_unusable_reason({}) is None
    assert ci_tracked.registry_unusable_reason({"bramwell": {}}) is None


def test_registry_unusable_reason_is_none_when_the_default_registry_is_usable(
        monkeypatch, tmp_path):
    registry_file = tmp_path / "projects.json"
    registry_file.write_text(json.dumps({
        "bramwell": {"class": "product", "lifecycle": "active", "owner": "convivy",
                   "repos": ["bramwell"]},
    }))
    monkeypatch.setattr(projects, "REGISTRY_PATH", registry_file)
    projects.reload()
    assert ci_tracked.registry_unusable_reason() is None


def test_registry_unusable_reason_names_the_condition_for_an_unreadable_registry(
        monkeypatch, tmp_path):
    """`projects.REGISTRY_PATH` already points at a nonexistent file, via the
    autouse fixture above — the registry was never written at all."""
    assert ci_tracked.registry_unusable_reason() == ci_tracked.REGISTRY_UNUSABLE_REASON
    assert ci_tracked.tracked() == []
    assert ci_tracked.not_captured() == {}


def test_registry_unusable_reason_names_the_condition_for_a_malformed_registry(
        monkeypatch, tmp_path):
    registry_file = tmp_path / "projects.json"
    registry_file.write_text("{not valid json")
    monkeypatch.setattr(projects, "REGISTRY_PATH", registry_file)
    projects.reload()
    assert ci_tracked.registry_unusable_reason() == ci_tracked.REGISTRY_UNUSABLE_REASON
    assert ci_tracked.tracked() == []
    assert ci_tracked.not_captured() == {}


def test_registry_unusable_reason_names_the_condition_for_a_metadata_only_registry(
        monkeypatch, tmp_path):
    """Valid JSON, parses cleanly, and names no project past its metadata
    keys — the "dangerous middle" `projects._Snapshot.available` exists to
    catch: a raw-read check would call this file healthy."""
    registry_file = tmp_path / "projects.json"
    registry_file.write_text(json.dumps({
        "_meta": {"doc": "not a project"},
        "_scopes": {"platform": {}},
    }))
    monkeypatch.setattr(projects, "REGISTRY_PATH", registry_file)
    projects.reload()
    assert ci_tracked.registry_unusable_reason() == ci_tracked.REGISTRY_UNUSABLE_REASON
    assert ci_tracked.tracked() == []
    assert ci_tracked.not_captured() == {}


# --- deck_tracked_repos() -------------------------------------------------------


def test_deck_tracked_repos_includes_a_product_class_projects_repos():
    registry = {
        "marlow": {"class": "product", "lifecycle": "active", "owner": "convivy",
                      "repos": ["marlow", "marlow-landing"]},
    }
    assert ci_tracked.deck_tracked_repos(registry) == [
        {"repo": "convivy/marlow", "product": "marlow"},
        {"repo": "convivy/marlow-landing", "product": "marlow"},
    ]


def test_deck_tracked_repos_draws_no_distinction_between_product_and_project_class():
    """Deck's own rule (`CockpitResolution.prLadderRepos`, reading
    `Crew.repos`) never reads the registry's `class` field, so a
    `class == "project"` crew naming two repos (library's own shape) counts
    exactly as a product-class one does. An earlier version filtered to
    `class == "product"` and dropped the library entry whole."""
    registry = {
        "thistle": {"class": "product", "lifecycle": "active", "owner": "convivy",
                     "repos": ["thistle", "thistle-backend"]},
        "library": {"class": "project", "lifecycle": "active", "owner": "convivy",
                    "repos": ["library", "knowledge"]},
    }
    assert ci_tracked.deck_tracked_repos(registry) == [
        {"repo": "convivy/thistle", "product": "thistle"},
        {"repo": "convivy/thistle-backend", "product": "thistle"},
        {"repo": "convivy/library", "product": "library"},
        {"repo": "convivy/knowledge", "product": "library"},
    ]


def test_deck_tracked_repos_excludes_sunset_and_private_product_projects():
    registry = {
        "retired-product": {"class": "product", "lifecycle": "sunset",
                            "owner": "convivy", "repos": ["retired-product"]},
        "stealth-product": {"class": "product", "lifecycle": "active",
                            "owner": "convivy", "private": True,
                            "repos": ["stealth-product"]},
    }
    assert ci_tracked.deck_tracked_repos(registry) == []


def test_deck_tracked_repos_excludes_a_project_naming_no_owner():
    """A project the registry cannot resolve a GitHub owner for cannot form
    a verifiable `owner/repo` identity, so it is skipped rather than guessed."""
    registry = {
        "no-owner": {"class": "product", "lifecycle": "active", "repos": ["no-owner"]},
    }
    assert ci_tracked.deck_tracked_repos(registry) == []


def test_deck_tracked_repos_names_a_repo_under_its_repo_owners_override():
    """shipwright's `homebrew-tap` lives under `convivybuilder` while the
    unit's `owner` is `convivy`. Read from `owner` alone it was reported
    `untracked` as `convivy/homebrew-tap`, a repository that does not exist."""
    registry = {
        "shipwright": {"class": "project", "lifecycle": "active", "owner": "convivy",
                       "repos": ["shipwright", "homebrew-tap"],
                       "repo_owners": {"homebrew-tap": "convivybuilder"}},
    }
    assert ci_tracked.deck_tracked_repos(registry) == [
        {"repo": "convivy/shipwright", "product": "shipwright"},
        {"repo": "convivybuilder/homebrew-tap", "product": "shipwright"},
    ]


def test_deck_tracked_repos_prefers_github_owner_to_owner():
    registry = {
        "briarcliff": {"class": "project", "lifecycle": "active", "owner": "briarcliff",
                    "github_owner": "example-owner", "repos": ["briarcliff"]},
    }
    assert ci_tracked.deck_tracked_repos(registry) == [
        {"repo": "example-owner/briarcliff", "product": "briarcliff"},
    ]


def test_deck_tracked_repos_skips_the_metadata_keys():
    registry = {
        "_meta": {"doc": "not a project"},
        "_products": {"fennick": {"display": "Fennick"}},
        "fennick": {"class": "product", "lifecycle": "active", "owner": "convivy",
                "repos": ["fennick"]},
    }
    assert ci_tracked.deck_tracked_repos(registry) == [
        {"repo": "convivy/fennick", "product": "fennick"},
    ]


def test_deck_tracked_repos_reads_the_registry_module_by_default(monkeypatch, tmp_path):
    """No `registry` argument means `projects.projects()`, proven against a
    fixture file via `projects.REGISTRY_PATH`."""
    registry_file = tmp_path / "projects.json"
    registry_file.write_text(json.dumps({
        "bramwell": {"class": "product", "lifecycle": "active", "owner": "convivy",
                   "repos": ["bramwell"]},
    }))
    monkeypatch.setattr(projects, "REGISTRY_PATH", registry_file)
    projects.reload()
    assert ci_tracked.deck_tracked_repos() == [{"repo": "convivy/bramwell", "product": "bramwell"}]
