"""ci_tracked.py — the tracked (repo, workflow) pairs and the registry-derived
repo set `GET /signals/ci-capture` checks them against.

Moved from `tests/test_ci_result_fetch.py` when `bin/ci_result_fetch.py` was
retired (issue_01M2XGCSGYW6F0QEZAQ0VKN7CQ). Every `deck_tracked_repos` test
reads a fixture `{slug: project}` dict or a fixture registry file, never
this machine's real registry.
"""
from __future__ import annotations

import json

import ci_tracked
import projects
import pytest


@pytest.fixture(autouse=True)
def _no_real_projects_registry(tmp_path, monkeypatch):
    """`projects.REGISTRY_PATH` points at a path that does not exist, so no
    test here reads this machine's real `agent-platform/config/projects.json`.
    A test of the default registry overrides it again, to its own fixture."""
    monkeypatch.setattr(projects, "REGISTRY_PATH", tmp_path / "does-not-exist.json")
    projects.reload()
    yield
    projects.reload()


def test_workflow_slug_strips_the_directory_and_extension():
    assert ci_tracked.workflow_slug(".github/workflows/e2e-stage-smoke.yml") \
        == "e2e-stage-smoke"


def test_vivys_tracked_entries_name_its_pull_request_check_and_stage_smoke():
    """`ci.yml` runs vivy's pull-request checks; `e2e-stage-smoke.yml` is the
    only source for Orient's stage-smoke tile, which went stale from
    2026-10-02 when an earlier edit replaced it rather than adding `ci.yml`
    beside it."""
    vivy_workflows = sorted(
        e["workflow_file"] for e in ci_tracked.TRACKED if e["repo"] == "convivy/vivy")
    assert vivy_workflows == ["ci.yml", "e2e-stage-smoke.yml"]


def test_www_is_tracked_by_its_pull_request_workflow():
    entries = [e for e in ci_tracked.TRACKED if e["repo"] == "convivy/www"]
    assert entries == [{"repo": "convivy/www", "workflow_file": "build-deploy.yml",
                        "product": "convivy-orient"}]


def test_no_repo_is_both_tracked_and_not_captured():
    tracked = {entry["repo"] for entry in ci_tracked.TRACKED}
    assert not tracked & set(ci_tracked.NOT_CAPTURED)
    assert not {r.split("/", 1)[1] for r in tracked} & set(ci_tracked.NOT_CAPTURED_BY_NAME)


def test_not_captured_names_a_by_name_entry_under_its_registry_owner(monkeypatch):
    """A `NOT_CAPTURED_BY_NAME` entry is keyed by repository name so a
    person's own GitHub account never appears in this shipped file; the
    owner comes from the registry through `deck_tracked_repos()`."""
    monkeypatch.setattr(ci_tracked, "NOT_CAPTURED", {"convivy/signals": "no workflows"})
    monkeypatch.setattr(ci_tracked, "NOT_CAPTURED_BY_NAME", {"side-project": "404"})
    deck = [{"repo": "convivy/signals", "product": "signals"},
            {"repo": "example-owner/side-project", "product": "side-project"}]
    assert ci_tracked.not_captured(deck) == {
        "convivy/signals": "no workflows",
        "example-owner/side-project": "404",
    }


def test_not_captured_keeps_a_by_name_entry_the_registry_does_not_yield_bare(monkeypatch):
    monkeypatch.setattr(ci_tracked, "NOT_CAPTURED", {})
    monkeypatch.setattr(ci_tracked, "NOT_CAPTURED_BY_NAME", {"side-project": "404"})
    assert ci_tracked.not_captured([]) == {"side-project": "404"}


def test_deck_tracked_repos_includes_a_product_class_projects_repos():
    registry = {
        "constance": {"class": "product", "lifecycle": "active", "owner": "convivy",
                      "repos": ["constance", "constance-landing"]},
    }
    assert ci_tracked.deck_tracked_repos(registry) == [
        {"repo": "convivy/constance", "product": "constance"},
        {"repo": "convivy/constance-landing", "product": "constance"},
    ]


def test_deck_tracked_repos_draws_no_distinction_between_product_and_project_class():
    """Deck's own rule (`CockpitResolution.prLadderRepos`, reading
    `Crew.repos`) never reads the registry's `class` field, so a
    `class == "project"` crew naming two repos (library's own shape) counts
    exactly as a product-class one does. An earlier version filtered to
    `class == "product"` and dropped the library entry whole."""
    registry = {
        "selector": {"class": "product", "lifecycle": "active", "owner": "convivy",
                     "repos": ["selector", "selector-backend"]},
        "library": {"class": "project", "lifecycle": "active", "owner": "convivy",
                    "repos": ["library", "knowledge"]},
    }
    assert ci_tracked.deck_tracked_repos(registry) == [
        {"repo": "convivy/selector", "product": "selector"},
        {"repo": "convivy/selector-backend", "product": "selector"},
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
        "nyh-etc": {"class": "project", "lifecycle": "active", "owner": "nyh",
                    "github_owner": "example-owner", "repos": ["nyh-etc"]},
    }
    assert ci_tracked.deck_tracked_repos(registry) == [
        {"repo": "example-owner/nyh-etc", "product": "nyh-etc"},
    ]


def test_deck_tracked_repos_skips_the_metadata_keys():
    registry = {
        "_meta": {"doc": "not a project"},
        "_products": {"vivy": {"display": "Vivy"}},
        "vivy": {"class": "product", "lifecycle": "active", "owner": "convivy",
                "repos": ["vivy"]},
    }
    assert ci_tracked.deck_tracked_repos(registry) == [
        {"repo": "convivy/vivy", "product": "vivy"},
    ]


def test_deck_tracked_repos_reads_the_registry_module_by_default(monkeypatch, tmp_path):
    """No `registry` argument means `projects.projects()`, proven against a
    fixture file via `projects.REGISTRY_PATH`."""
    registry_file = tmp_path / "projects.json"
    registry_file.write_text(json.dumps({
        "hollis": {"class": "product", "lifecycle": "active", "owner": "convivy",
                   "repos": ["hollis"]},
    }))
    monkeypatch.setattr(projects, "REGISTRY_PATH", registry_file)
    projects.reload()
    assert ci_tracked.deck_tracked_repos() == [{"repo": "convivy/hollis", "product": "hollis"}]
