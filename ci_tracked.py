"""ci_tracked — the (repo, workflow) pairs whose CI results the Library
expects in the Archives, and the repositories Deck draws that have none.

`GET /signals/ci-capture` reads this list to report a tracked pair with no
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
"""
from __future__ import annotations

from pathlib import Path

import projects

# One entry per `(repo, workflow)` pair, covering every repository
# `deck_tracked_repos()` says Deck's Pull Requests panel draws
# (issue_01M2PER2VWBMERJ6KSQTX3J0SW), each verified against GitHub when it
# was added, since a repo can exist with no CI, or with a workflow that
# never runs on `pull_request`.
#
# `convivy/vivy` has two entries. `ci.yml` runs its pull-request checks.
# `e2e-stage-smoke.yml` runs on `schedule` and `workflow_dispatch` against
# `main` only; its `test-result` rows (stream `e2e-stage-smoke`) are the
# only source for Orient's stage-smoke tile, and `GET /signals/pr-state`'s
# join on a PR's head sha does not reach them.
TRACKED = (
    {"repo": "convivy/constance", "workflow_file": "ci.yml", "product": "constance"},
    {"repo": "convivy/vivy", "workflow_file": "ci.yml", "product": "vivy"},
    {"repo": "convivy/vivy", "workflow_file": "e2e-stage-smoke.yml", "product": "vivy"},
    {"repo": "convivy/vivy-ownerrez-sandbox-tools", "workflow_file": "ci.yml", "product": "vivy"},
    {"repo": "convivy/sous-chef", "workflow_file": "ci.yml", "product": "sous-chef"},
    {"repo": "convivy/hollis", "workflow_file": "ci.yml", "product": "hollis"},
    {"repo": "convivy/selector", "workflow_file": "ci.yml", "product": "selector"},
    {"repo": "convivy/selector-backend", "workflow_file": "ci.yml", "product": "selector"},
    {"repo": "convivy/library", "workflow_file": "ci.yml", "product": "library"},
    {"repo": "convivy/knowledge", "workflow_file": "ci.yml", "product": "library"},
    {"repo": "convivy/agent-platform", "workflow_file": "ci.yml", "product": "bosun"},
    {"repo": "convivy/placard", "workflow_file": "ci.yml", "product": "placard"},
    {"repo": "convivy/shipwright", "workflow_file": "ci.yml", "product": "shipwright"},
    {"repo": "convivy/convivy-orient", "workflow_file": "ci.yml", "product": "convivy-orient"},
    # `build-deploy.yml` runs on `pull_request` to main.
    {"repo": "convivy/www", "workflow_file": "build-deploy.yml", "product": "convivy-orient"},
    {"repo": "convivy/convivy-app-kit", "workflow_file": "ci.yml", "product": "svc"},
    {"repo": "convivy/convivy-services", "workflow_file": "ci.yml", "product": "svc"},
    {"repo": "convivy/convivy-deck", "workflow_file": "ci.yml", "product": "convivy-deck"},
    {"repo": "convivy/substrate", "workflow_file": "checks.yml", "product": "steward"},
    # Its only workflow, `ci.yml`, runs on `pull_request` and on push to main.
    {"repo": "convivy/vivy-landing", "workflow_file": "ci.yml", "product": "vivy"},
)

# Repositories `deck_tracked_repos()` yields that have no pull-request CI to
# capture, each with the evidence for leaving it out, checked 2026-10-01 with
# `gh api repos/<repo>/actions/workflows`. `untracked` skips these so it
# names only a real gap (reviewer, PR #189 round 2: without this list the
# check printed eleven lines on every run). A repository that gains a PR
# workflow moves from here to `TRACKED`.
NOT_CAPTURED = {
    "convivy/signals": "no GitHub Actions workflows",
    "convivy/write-better": "no GitHub Actions workflows",
    "convivy/constance-landing": "no GitHub Actions workflows",
    "convivy/constance-commercial": "no GitHub Actions workflows",
    "convivy/constance-updates": "no GitHub Actions workflows",
    "convivy/vivy-commercial": "404 from the GitHub API",
    "convivy/convivy-commercial": "404 from the GitHub API",
    # Checked 2026-10-09, after library#208 admitted it.
    "convivy/archy": "404 from the GitHub API",
    # Checked 2026-10-16 under the owner `_github_owner` resolves.
    "convivybuilder/homebrew-tap": "no GitHub Actions workflows",
}

# Repositories with no pull-request CI whose GitHub owner is a person's own
# account, keyed by repository name alone. The owner stays in the projects
# registry and never in this shipped file; `not_captured()` attaches it from
# `deck_tracked_repos()`. Checked 2026-10-16 under that owner. Repository
# names are unique across the registry, so a name identifies one repository.
NOT_CAPTURED_BY_NAME = {
    "nyh-arch-tools": "no GitHub Actions workflows",
    "nyh-etc": "404 from the GitHub API",
    "nyh-park-villa": "404 from the GitHub API",
}


def workflow_slug(path: str) -> str:
    """.github/workflows/e2e-stage-smoke.yml -> e2e-stage-smoke, the
    `refs.workflow` value a captured run's signal carries."""
    return Path(path).stem


def _github_owner(project: dict, repo: str) -> str | None:
    """The GitHub owner of `repo`, by agent-platform's own rule
    (`projects.repo_github_slug`): the project's `repo_owners` entry for this
    repo, else its `github_owner`, else its `owner`. shipwright's
    `homebrew-tap` lives under `convivybuilder`, and the nyh repos under the
    `github_owner` their registry entry names, while each project's `owner`
    names the unit."""
    for candidate in ((project.get("repo_owners") or {}).get(repo),
                      project.get("github_owner"), project.get("owner")):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


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
    reach. This derives the closest available proxy instead:
    `agent-platform/config/projects.json`'s own `repos` field per project,
    which groups repos the same crew-shaped way.

    **A repo counts when its project is `lifecycle == "active"` and not
    `private`.** `private` is withheld because a private crew's repo is
    exactly the ADR-0043 disclosure this function must not make. This rule
    also yields repositories with no pull-request CI, which `NOT_CAPTURED`
    lists with their evidence.

    `registry` defaults to `projects.projects()`; a caller passes its own
    `{slug: project}` dict to test the derivation without touching any real
    file.
    """
    reg = registry if registry is not None else projects.projects()
    out: list[dict] = []
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
            owner = _github_owner(project, repo.strip())
            if owner is None:
                continue
            out.append({"repo": f"{owner}/{repo.strip()}", "product": slug})
    return out


def not_captured(deck_repos: list[dict]) -> dict[str, str]:
    """`{"<owner>/<repo>": reason}` for every repository known to have no
    pull-request CI: `NOT_CAPTURED` as written, plus each
    `NOT_CAPTURED_BY_NAME` entry under the full name `deck_repos` gives it.
    A by-name entry that `deck_repos` does not yield keeps its bare name."""
    out = dict(NOT_CAPTURED)
    full_by_name = {d["repo"].split("/", 1)[-1]: d["repo"] for d in deck_repos}
    for name, why in NOT_CAPTURED_BY_NAME.items():
        out[full_by_name.get(name, name)] = why
    return out
