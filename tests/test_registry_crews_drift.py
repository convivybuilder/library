"""`project_registry`'s ported schema checks (CQ·242) against agent-platform's
`lib/agent-platform/crew_registration.py` (`build_row`'s regexes and
literals) and the `projects.py` constants and patterns it calls
(`CLASSES`, `KB_VALUES`, `LIFECYCLES`, `MODEL_TIERS`, `KB_LEGACY_KEY`,
`PERSON_ID_RE`, `_CI_WORKFLOW_FILE_RE`, `privacy_invariant_errors`).
`project_registry.py`'s own module docstring states why this is a port
rather than an import.

**Two halves, the same shape `test_read_surface.py::
test_the_ranking_constants_have_not_drifted` takes for `reference.py`'s BM25
weights and stopword list against `mcp-kb-server`, and only the first runs
in CI.** The literals below are asserted directly against this module's own
copies, so a change to THIS copy fails here immediately, in CI, with no
other checkout needed. The second half re-reads agent-platform's two files
and fails the moment THAT copy drifts, but only runs when an agent-platform
checkout is present next to this one — not in CI, which does not have it.

**No fixture holds a copy of agent-platform's source.** Builder's content
scan refuses a push naming a real crew, and agent-platform's own files name
several in surrounding prose (its `crew_registration.py` module docstring
alone names two) that have nothing to do with the regexes and enumerations
this file pins — committing even a trimmed excerpt risks carrying one into
this repository's history. Reading the live checkout's text at test time,
the way the cross-repo half below already does, commits nothing.
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

import project_registry

AGENT_PLATFORM_LIB = (
    pathlib.Path.home() / "Convivy" / "agent-platform" / "lib" / "agent-platform")


# --- the pinned copies, asserted directly: this half always runs -----------


def test_the_slug_and_related_patterns_have_not_drifted():
    assert project_registry._SLUG_OK.pattern == r"^[a-z][a-z0-9-]*$"
    assert project_registry._KB_PROJECT_SLUG_OK.pattern == r"^[a-z][a-z-]*$"
    assert (project_registry._CI_WORKFLOW_FILE_RE.pattern
            == r"^[A-Za-z0-9][A-Za-z0-9._-]*\.ya?ml$")
    assert (project_registry._PERSON_ID_RE.pattern
            == r"^person_[0-9A-HJKMNP-TV-Z]{26}$")
    assert project_registry._ROOT_PREFIX == "<root>/"


def test_the_enumerations_have_not_drifted():
    assert project_registry.CLASSES == ("product", "project")
    assert project_registry.KB_VALUES == ("central", "local", "split")
    assert project_registry.LIFECYCLES == ("active", "sunset", "archived")
    assert project_registry.MODEL_TIERS == ("opus", "sonnet")


def test_the_privacy_invariant_matches_adr_0043():
    """A private crew that is not local-kb or that publishes task text is
    refused, and nothing else about `private` is checked — the exact shape
    `projects.privacy_invariant_errors` enforces."""
    assert project_registry._privacy_invariant_errors({"private": False}) == []
    assert project_registry._privacy_invariant_errors(
        {"private": True, "kb": "local"}) == []
    assert project_registry._privacy_invariant_errors(
        {"private": True, "kb": "central"}) == ["kb='central', not 'local'"]
    assert project_registry._privacy_invariant_errors(
        {"private": True, "kb": "local", "task_text_public": True}) == [
        "task_text_public is true"]


# --- the cross-repo half: skipped unless agent-platform is checked out -----


def _source(name: str) -> str:
    path = AGENT_PLATFORM_LIB / name
    if not path.is_file():
        pytest.skip(
            "agent-platform not checked out at "
            f"{AGENT_PLATFORM_LIB}; the cross-repo half cannot run")
    return path.read_text()


def _literal_tuple(text: str, name: str) -> tuple:
    m = re.search(rf"^{name} = \(([^)]*)\)", text, re.M)
    assert m, f"{name} not found in the live checkout"
    return tuple(ast.literal_eval(f"({m.group(1)})"))


def _literal_str(text: str, name: str) -> str:
    m = re.search(rf'^{name} = "([^"]*)"', text, re.M)
    assert m, f"{name} not found in the live checkout"
    return m.group(1)


def _compiled_pattern(text: str, name: str) -> str:
    m = re.search(rf'{re.escape(name)} = re\.compile\(r"([^"]+)"\)', text)
    assert m, f"{name} not found in the live checkout"
    return m.group(1)


def test_crew_registration_literals_match_the_live_checkout():
    text = _source("crew_registration.py")
    assert _compiled_pattern(text, "_SLUG_OK") == project_registry._SLUG_OK.pattern
    assert (_compiled_pattern(text, "_KB_PROJECT_SLUG_OK")
            == project_registry._KB_PROJECT_SLUG_OK.pattern)
    assert _literal_str(text, "_ROOT_PREFIX") == project_registry._ROOT_PREFIX


def test_projects_constants_match_the_live_checkout():
    text = _source("projects.py")
    assert _literal_tuple(text, "CLASSES") == project_registry.CLASSES
    assert _literal_tuple(text, "KB_VALUES") == project_registry.KB_VALUES
    assert _literal_tuple(text, "LIFECYCLES") == project_registry.LIFECYCLES
    assert _literal_tuple(text, "MODEL_TIERS") == project_registry.MODEL_TIERS
    assert _literal_str(text, "KB_LEGACY_KEY") == "_kb_legacy"
    assert (_compiled_pattern(text, "PERSON_ID_RE")
            == project_registry._PERSON_ID_RE.pattern)
    assert (_compiled_pattern(text, "_CI_WORKFLOW_FILE_RE")
            == project_registry._CI_WORKFLOW_FILE_RE.pattern)


def test_privacy_invariant_errors_conditions_match_the_live_checkout():
    """The three literal conditions `privacy_invariant_errors` branches on,
    checked as text rather than by import — so a reorder or a rename shows
    up here without ever executing agent-platform's code."""
    text = _source("projects.py")
    assert 'if unit.get("private") is not True:' in text
    assert 'if unit.get("kb") != "local":' in text
    assert 'if unit.get("task_text_public") is True:' in text
