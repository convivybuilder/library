"""ci_tracked — the (repo, workflow) pairs whose CI results the Library
expects in the Archives, and the repositories Deck draws that have none.

`GET /signals/ci-capture` reads `tracked()` to report a tracked pair with no
`test-result` row (`never_captured`) and a repository Deck draws that
nothing tracks (`untracked`).

**The Library no longer fetches CI results itself.** `bin/ci_result_fetch.py`
was retired in issue_01M2XGCSGYW6F0QEZAQ0VKN7CQ after the Bosun's
`ci-result-capture` (agent-platform `bin/ci-result-capture`, posting as the
`convivy-bosun` App) ran alone for 24 hours and every completed run in that
window reached the Archives. That job keeps its own copy of this list,
seeded from the fetcher's on 2026-10-13. Nothing syncs the two: a pair added
here and not there shows in `never_captured`, which is the check this
module exists for.

**Every pair here is derived from the project registry, never a fixed
literal** (issue_01M5GYC0EBWS1JBQF7DA6FQ1CG). `tracked()` and
`not_captured()` both walk the same registry `projects.projects()` reads,
through the per-unit `repos` list and the per-unit, per-repo `ci_workflows`
map agent-platform's own registry loader defines (`DEFAULT_CI_WORKFLOWS`,
`ci_workflows_errors`, `ci_workflows` in its `lib/agent-platform/projects.py`,
agent-platform#981). An unreadable, unparsable, or metadata-only registry
tracks nothing — rather than falling back to a literal — AND SAYS SO:
`registry_unusable_reason()` reports that condition separately from an empty
`tracked()`/`not_captured()`, because an empty list alone cannot be told
apart from a registry that legitimately tracks no active, public project.
`GET /signals/ci-capture` carries that reason in its response rather than
only logging it, which `projects.py`'s own read failure already does.
"""
from __future__ import annotations

from pathlib import Path

import projects

# The workflow file(s) a repo runs on its pull requests when its unit's
# `ci_workflows` map (agent-platform's field, read directly here — the
# Library does not import agent-platform's code, only the same registry
# file) names nothing for it: every repo's default before any per-repo
# override is entered.
DEFAULT_CI_WORKFLOWS = ("ci.yml",)

# The reason reported for a repo whose unit maps it to `ci_workflows: []` —
# no pull-request CI to capture. One generic reason replaces the per-repo,
# hand-verified evidence strings ("no GitHub Actions workflows", "404 from
# the GitHub API") the former `NOT_CAPTURED`/`NOT_CAPTURED_BY_NAME` literals
# carried: that evidence is now the registry entry's own justification for
# its `[]`, not this module's to restate.
NOT_CAPTURED_REASON = "the registry's ci_workflows names no pull-request CI for this repo"

# The reason `GET /signals/ci-capture` reports when the default registry read
# cannot be trusted — see `registry_unusable_reason`. One generic reason
# because `projects.available()` already collapses "missing", "unparsable"
# and "holds nothing past its metadata keys" into one boolean, and this
# module has no way to tell the three apart once the read has happened.
REGISTRY_UNUSABLE_REASON = (
    "the project registry is unreadable, unparsable, or has no project "
    "entries past its metadata keys, so tracked, never_captured, untracked "
    "and not_captured read as nothing known rather than a verified empty "
    "registry"
)


def workflow_slug(path: str) -> str:
    """.github/workflows/e2e-stage-smoke.yml -> e2e-stage-smoke, the
    `refs.workflow` value a captured run's signal carries."""
    return Path(path).stem


def _ci_workflow_files(project: dict, repo: str) -> list[str]:
    """The workflow file name(s) `repo`'s pull requests run, by `project`'s
    own `ci_workflows` map: that map's own entry for `repo` when it has one
    (including an explicit `[]`, meaning no pull-request CI at all), else
    `DEFAULT_CI_WORKFLOWS`. Mirrors agent-platform's `ci_workflows()`
    resolution for one already-identified unit and repo; `ci_workflows_errors`
    there is what keeps a malformed entry out of the committed registry in
    the first place, so this reads the map as already well-formed and falls
    back to the default on anything that is not."""
    mapping = project.get("ci_workflows")
    if isinstance(mapping, dict) and repo in mapping:
        files = mapping.get(repo)
        if isinstance(files, list) and all(isinstance(f, str) for f in files):
            return list(files)
    return list(DEFAULT_CI_WORKFLOWS)


# A pull-request workflow the merge-admission evaluator (CQ·218) needs
# tracked that no unit's `ci_workflows` map names at all, keyed on the exact
# `owner/repo` rather than the bare name, so a same-named repo under another
# owner is never swept in by accident. `convivy/knowledge`'s
# `kb-write-trailer.yml` gates every pull request there (it runs on
# `pull_request_target`, which still reports against the pull request) but
# the `library` project's registry unit names no `ci_workflows` entry for
# `knowledge` at all, so the registry walk alone would never produce it.
# Appended rather than replacing the registry's own list, so a future
# registry entry for this repo adds to this pair rather than silently
# dropping it.
_ADDITIONAL_WORKFLOWS: dict[str, tuple[str, ...]] = {
    "convivy/knowledge": ("kb-write-trailer.yml",),
}

# A workflow file `ci_workflows` already names for pull requests but that
# does NOT run on them — the merge-admission evaluator must never require a
# result from one of these at a pull request's head, since none will ever
# arrive. The one in the live registry, `e2e-stage-smoke.yml`, runs only on
# `schedule` and `workflow_dispatch` against `main` (see `tracked()`'s own
# docstring); every other tracked pair defaults to `True`. Keyed on the
# workflow file rather than the repo because this module never names a
# registry product: Builder's publish scan refuses a release that does, and
# the registry records no trigger per workflow to derive this from. The
# repo's own registry entry scopes it instead: see `on_pull_request`.
_NOT_ON_PULL_REQUEST: frozenset[str] = frozenset({"e2e-stage-smoke.yml"})


def on_pull_request(workflow_file: str, repo_files: list[str]) -> bool:
    """Whether `workflow_file`, one of `repo_files` (every workflow the repo's
    registry entry tracks), runs on, and so can report a result at, a pull
    request's head. `False` only for a `_NOT_ON_PULL_REQUEST` file in a repo
    that tracks at least one other workflow, so no repo can leave itself
    with no required pull-request check by naming its only workflow this way
    (CQ·218 round 1, security-reviewer). A repo whose only tracked workflow
    is one of these keeps it required, and fails closed."""
    if workflow_file not in _NOT_ON_PULL_REQUEST:
        return True
    return not any(f not in _NOT_ON_PULL_REQUEST for f in repo_files)


def _effective_workflow_files(project: dict, repo: str, owner: str) -> list[str]:
    """`_ci_workflow_files`'s answer, plus any `_ADDITIONAL_WORKFLOWS` entry
    for this exact `owner/repo` that is not already in it. The one place
    `tracked()` and `not_captured()` both read, so a repo `_ADDITIONAL_
    WORKFLOWS` forces a workflow onto is never reported by both — tracked
    for the workflow it adds and not_captured for a registry entry that, on
    its own, names none."""
    files = _ci_workflow_files(project, repo)
    for extra in _ADDITIONAL_WORKFLOWS.get(f"{owner}/{repo}", ()):
        if extra not in files:
            files.append(extra)
    return files


def _active_project_repos(reg: dict) -> list[tuple[str, dict, str, str]]:
    """`(slug, project, repo, owner)` for every repo an active, non-private
    project names, skipping a repo whose owner `projects.repo_owner` cannot
    resolve. The shared walk behind `deck_tracked_repos`, `tracked` and
    `not_captured`, so the three cannot drift on which repos and owners they
    agree on."""
    out: list[tuple[str, dict, str, str]] = []
    for slug, project in reg.items():
        if not isinstance(project, dict):
            continue
        if project.get("lifecycle") != "active":
            continue
        if project.get("private"):
            continue
        for repo in project.get("repos") or []:
            if not isinstance(repo, str) or not repo.strip():
                continue
            repo = repo.strip()
            owner = projects.repo_owner(project, repo)
            if owner is None:
                continue
            out.append((slug, project, repo, owner))
    return out


def registry_unusable_reason(registry: dict | None = None) -> str | None:
    """Why `tracked()` and `not_captured()` cannot be trusted to report a
    VERIFIED empty registry right now, or `None` when they can
    (issue_01M5GYC0EBWS1JBQF7DA6FQ1CG).

    `None` whenever `registry` is given explicitly: a caller passing its own
    `{slug: project}` dict already knows what an empty one means to it, and
    `projects.available()` has nothing to say about a dict that never came
    from a read.

    On the default path, this is `projects.available()`'s own distinction:
    False for a registry that is missing, unparsable, or holds nothing past
    its `_meta`/`_scopes` keys. All three already happened a read and came
    back with nothing to validate against — `tracked()` and `not_captured()`
    cannot tell that apart from a registry that legitimately tracks no
    active, public project, so `GET /signals/ci-capture` reports this reason
    rather than guessing which of the three it is.
    """
    if registry is not None:
        return None
    if projects.available():
        return None
    return REGISTRY_UNUSABLE_REASON


def deck_tracked_repos(registry: dict | None = None) -> list[dict]:
    """Every `{"repo": "<owner>/<name>", "product": <slug>}` Deck's Pull
    Requests panel follows, mirroring Deck's own rule
    (issue_01M2PER2VWBMERJ6KSQTX3J0SW).

    **Deck's rule, and where it lives.** The panel's repo list is
    `CockpitResolution.prLadderRepos(for: crew)`
    (`convivy-deck/Sources/Deck/Cockpit/CockpitResolution.swift`), which reads
    `crew.repos` (`convivy-deck/Sources/Deck/Rail/Crew.swift`), a flat
    `owner/repo` list set per crew with no reference to this registry's
    `class` field. So this function does not filter on `class`: a
    `class == "project"` crew such as `library` (`library`, `knowledge`)
    counts exactly as a product-class one does.

    **`crew.repos` itself is not a file the Library can read.** Deck fills
    it from the operator's per-host `theme.json` or from a served crew spec
    (`~/.config/convivy/deck/crews.d/<id>.json`), both outside this repo's
    reach. This derives the closest available proxy instead: the project
    registry's own `repos` field per project, which groups repos the same
    crew-shaped way.

    **A repo counts when its project is `lifecycle == "active"` and not
    `private`.** `private` is withheld because a private crew's repo is
    exactly the ADR-0043 disclosure this function must not make. This rule
    also yields repositories with no pull-request CI, which `not_captured()`
    lists with its reason.

    `registry` defaults to `projects.projects()`; a caller passes its own
    `{slug: project}` dict to test the derivation without touching any real
    file.
    """
    reg = registry if registry is not None else projects.projects()
    return [{"repo": f"{owner}/{repo}", "product": slug}
            for slug, _project, repo, owner in _active_project_repos(reg)]


def tracked(registry: dict | None = None) -> list[dict]:
    """Every `{repo, workflow_file, product, on_pull_request}` pair whose CI
    result `GET /signals/ci-capture` expects in the Archives — the
    registry-derived replacement for the former hand-maintained `TRACKED`
    literal (issue_01M5GYC0EBWS1JBQF7DA6FQ1CG).

    Walks the same `(slug, project, repo, owner)` set `deck_tracked_repos`
    does, and for each repo emits one entry per workflow file
    `_effective_workflow_files` names for it — ordinarily
    `DEFAULT_CI_WORKFLOWS`, or more than one where a unit's `ci_workflows`
    map says so, or where `_ADDITIONAL_WORKFLOWS` names one the map does
    not: one repo in the live registry names both its pull-request workflow
    and a second workflow that runs on `schedule` and `workflow_dispatch`
    against `main` only, whose runs are the only source for a dashboard
    tile with no PR of its own to key off. A repo whose map entry is `[]`
    and names no `_ADDITIONAL_WORKFLOWS` entry has no pull-request CI and
    contributes nothing here — `not_captured()` is where it shows instead.

    **`on_pull_request`** (CQ·218) is `False` for a pair this repo's own
    pull requests never run, by `on_pull_request()`'s own hand-maintained
    exceptions — the merge-admission evaluator reads this to know which
    tracked pairs it may require a result from at a pull request's head,
    and which (a schedule-only `e2e-stage-smoke.yml`) it never will.

    `registry` defaults to `projects.projects()`, exactly as
    `deck_tracked_repos` does, and for the same reason: a caller passes its
    own `{slug: project}` dict to test this without touching any real file.
    Checked against `registry_unusable_reason()` first on that default
    path, the one place "is the default registry unusable" is decided: an
    unusable registry (missing, unparsable, or metadata-only) yields no
    projects at all, so this returns an empty list rather than falling back
    to any fixed list of pairs. That function is where the condition is
    reported, since this return value alone cannot distinguish it from a
    registry that legitimately tracks nothing.
    """
    if registry_unusable_reason(registry) is not None:
        return []
    reg = registry if registry is not None else projects.projects()
    return [{"repo": f"{owner}/{repo}", "workflow_file": workflow_file,
             "product": slug,
             "on_pull_request": on_pull_request(workflow_file, files)}
            for slug, project, repo, owner in _active_project_repos(reg)
            for files in [_effective_workflow_files(project, repo, owner)]
            for workflow_file in files]


def not_captured(registry: dict | None = None) -> dict[str, str]:
    """`{"<owner>/<repo>": reason}` for every repository known to have no
    pull-request CI: every repo from the same registry walk `tracked()` and
    `deck_tracked_repos()` use whose unit maps it to `ci_workflows: []`,
    under `NOT_CAPTURED_REASON`.

    Replaces the former hand-maintained `NOT_CAPTURED`/`NOT_CAPTURED_BY_NAME`
    literals (issue_01M5GYC0EBWS1JBQF7DA6FQ1CG): both named a repo with no
    pull-request CI, each verified against GitHub when it was added, and the
    `_BY_NAME` one existed only to keep a person's own GitHub account out of
    this shipped file. Deriving the set from the registry at read time rather
    than shipping either list closes both: the full `owner/repo` name is
    never written into this file, hand-verified or not.

    `registry` defaults to `projects.projects()`, exactly as `tracked` and
    `deck_tracked_repos` do. Checked against `registry_unusable_reason()`
    first on that default path, for the same reason `tracked()` is: an
    unusable registry yields no entries here either, reported separately by
    that function.
    """
    if registry_unusable_reason(registry) is not None:
        return {}
    reg = registry if registry is not None else projects.projects()
    return {f"{owner}/{repo}": NOT_CAPTURED_REASON
            for slug, project, repo, owner in _active_project_repos(reg)
            if _effective_workflow_files(project, repo, owner) == []}
