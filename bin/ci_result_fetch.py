#!/usr/bin/env python3.11
"""ci_result_fetch — pulls CI conclusions from GitHub and writes them to the
Library as signals (issue_01M0B97RC21TXM71CVKSZMFW99).

**The direction is a pull, permanently.** The Library binds loopback and
holds no credential a GitHub runner could present, so nothing calls in from
CI. This job runs on this machine, reads completed workflow runs through
`gh api` — riding the operator's own `gh` login, no token in a repo secret —
and posts one signal per run through `POST /signals`.

Shape follows `convivy-orient/github_capture.py`: pure transforms are
unit-tested without network; fetching and posting are thin I/O wrappers
around `gh api` and `urllib`. This file does not import that one and is not
imported by it — the Library owns this end to end, and that file is a shape
reference only.

Every signal's `dedup_key` is `ci:<product>:<workflow-slug>:<run-id>`, the
same scheme the retired push (`vivy/.github/workflows/e2e-stage-smoke.yml`,
posting to `POST /signals` via a now-dropped Orient route since 2026-08-19)
already used — so backfilling the gap it left cannot create a second row for
the one run that got through before the push started failing.

**Cursor**: `~/.library/ci-fetch-cursor.json`, one entry per tracked (repo,
workflow) pair, holding the `created` lower bound for the next incremental
fetch. Advanced only after a pair's whole window is fetched AND posted with
zero errors (see `capture_workflow`) — a fetch failure or one malformed run
inside an otherwise-good page leaves that pair's cursor exactly where it was,
so the next run re-covers the whole window rather than skipping over data
that was never actually written. `dedup_key` absorbs the re-covered rows
that DID make it through.

**A pair with no stored cursor starts from `NO_CURSOR_WINDOW_DAYS` ago, not
from the workflow's entire history.** `since=None` used to make `gh_runs`
walk every run a workflow has ever had under `subprocess.run`'s 120-second
budget — production, 2026-10-01: `convivy/vivy#ci.yml` and
`convivy/knowledge#ci.yml` both timed out on the first scheduled run after a
tracked-repo widening added them with no cursor entry, and because a failed
pair saves no cursor (`main()` only writes `next_since` on `errors == 0`),
each timed out again on every poll after, forever. `main()` computes this
bound once per run and falls back to it only when neither `--since` nor a
stored cursor names a `since`; a pair that already has a cursor, or a run
given `--since` explicitly, is unaffected.

**THE INVARIANT: no completed run may ever fall behind the cursor without
having been captured.** `gh_runs` therefore fetches every run since the
cursor REGARDLESS of status — completed or not — and `capture_workflow`
classifies them itself: a completed run is turned into a signal and posted;
an incomplete one (still `queued`/`in_progress`/`waiting`/`requested`) holds
the cursor back to its own `created_at`, so it is asked for again on every
later poll until it finishes. The old design queried GitHub with
`status=completed` baked into the request and advanced the cursor to
`fetch-start - OVERLAP` on wall-clock time alone — a run still in flight was
correctly excluded from that page, but the cursor moved past its creation
time anyway, and a run that took longer than `OVERLAP` to finish was then
permanently invisible to every future `created >` query: excluded from the
first page for being incomplete, excluded from every later page for being
too old. No error, no tally, nothing in the log — this is the exact bug
found in production 2026-08-31.

The held-back cursor uses GitHub's `created >=` (not `>`) filter
deliberately: holding the cursor to the EXACT `created_at` of the run still
in flight, then re-querying with a strict `>`, would exclude that exact run
on the very next poll — the same class of off-by-one loss this fix exists
to close, just one step removed. `dedup_key` absorbs whatever `>=` causes
to be re-fetched a second time.

A run can also be incomplete and never legitimately finish — GitHub itself
bounds every run to 35 days before force-resolving it, but that is far too
long to hold a fetch cursor back on the strength of one stuck run (see
`STALE_PENDING_AFTER`). A run still incomplete past that ceiling is treated
as abandoned: excluded from the watermark (the cursor advances past it) and
logged loudly as `stale_pending`, not silently dropped — a real risk of
losing that one specific run if it improbably completes after all, traded
deliberately against every OTHER pair's cursor being stuck re-fetching a
forever-growing window on its account.

**`post_signal` retries a 429, it does not fail on one.** `POST /signals`
enforces `store.SIGNAL_RATE_LIMIT` per `store.SIGNAL_RATE_WINDOW_SECONDS` per
actor (app.py's rate gate), and an idempotent `dedup_key` resubmission still
spends a slot there — production, 2026-10-01 18:15-18:30Z: the first run
after this job's credential was widened to every tracked product created
120 rows across 7 repositories and got 13,307 429s. The old code counted
each 429 as an ordinary post failure, which left the pair's cursor exactly
where it was (the same rule `CaptureError`-shaped failures rely on); the
next run re-asked for the same newest runs first, spent its whole slot
budget on the identical duplicates, created nothing, and never advanced —
a livelock, not a slow drain, for any pair with more than
`SIGNAL_RATE_LIMIT` runs in its fetch window. `post_signal` now reads the
429's own `retry_after` (from the JSON body or the `Retry-After` header —
see `_retry_after_seconds`) and retries the same signal, so a run paces
itself to the limit instead. `RATE_LIMIT_MAX_RETRIES` and
`RATE_LIMIT_MAX_WAIT_SECONDS` bound that retrying: a signal still 429'd past
the bound reports `"error:429"`, which `capture_workflow` tallies exactly
like any other post failure — the pair's cursor still holds in place, so a
server that never clears still leaves the next poll free to re-cover the
window rather than this process hanging on it.

**That bound is per POST, not per pair, and `capture_workflow` stops the
batch rather than letting it compound.** `RATE_LIMIT_MAX_RETRIES` ×
`RATE_LIMIT_MAX_WAIT_SECONDS` is one signal's own ceiling; posting every
remaining signal in a pair's batch through its own independent copy of that
ceiling would let a sustained 429 (a stuck rate limiter, not an ordinary
volume spike) cost one pair's run a multiple of that bound rather than the
bound itself (reviewer/security-reviewer, round 1). The first signal to
report `"error:429"` therefore stops `capture_workflow`'s posting loop for
the rest of that pair's batch — the exhausted signal is tallied as an
error, the rest are left unposted and unretried this run, and the pair's
cursor (already held by the one error) picks every one of them up again on
the next poll.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# `projects.py` sits one level up, at the repo root — `pytest.ini`'s own
# `pythonpath = . bin` solves the same problem for the test run; a script
# invoked directly (as launchd does, `bin/measure_course_position.py`'s own
# pattern) has no pytest.ini to read it from, so it adds the root itself.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import projects  # noqa: E402

# The workflows this fetcher captures: one entry per `(repo, workflow)` pair,
# covering every repository `deck_tracked_repos()` says Deck's Pull Requests
# panel draws (issue_01M2PER2VWBMERJ6KSQTX3J0SW) — verified against GitHub
# individually (see the PR body) rather than assumed from the registry, since
# a repo can exist with no CI, or with a workflow that never runs on
# `pull_request`.
#
# `vivy`'s entry originally named `e2e-stage-smoke.yml`, a stage smoke
# workflow scheduled on a timer, not the workflow that runs pull-request
# checks — replaced here with `ci.yml`, `vivy`'s actual PR-check workflow.
# `e2e-stage-smoke.yml` is tracked again as a SECOND `convivy/vivy` entry,
# beside `ci.yml` rather than instead of it: its `test-result` rows (stream
# `e2e-stage-smoke`) are the only source for Orient's stage-smoke tile, which
# went stale when the replacement dropped them. It runs on `schedule` and
# `workflow_dispatch` against `main` only, so its head sha is a commit on
# `main`, never a pull request's head, and `GET /signals/pr-state`'s join on
# the PR's head sha does not reach its rows.
#
# **`stale_pending_after` is optional per entry** (a `dt.timedelta`) and
# overrides the module-level `STALE_PENDING_AFTER` default for that pair
# only. Leave it unset for a GitHub-hosted workflow — the default's 6-hour
# reasoning holds. Set it explicitly for a self-hosted-runner workflow,
# where GitHub's 6-hour hosted-runner cap this default is grounded in does
# not apply and a job can legitimately queue far longer.
TRACKED = (
    {"repo": "convivy/constance", "workflow_file": "ci.yml", "product": "constance"},
    {"repo": "convivy/vivy", "workflow_file": "ci.yml", "product": "vivy"},
    {"repo": "convivy/vivy", "workflow_file": "e2e-stage-smoke.yml", "product": "vivy"},
    {"repo": "convivy/vivy-ownerrez-sandbox-tools", "workflow_file": "ci.yml", "product": "vivy"},
    {"repo": "convivy/sous-chef", "workflow_file": "ci.yml", "product": "sous-chef"},
    {"repo": "convivy/hollis", "workflow_file": "ci.yml", "product": "hollis"},
    {"repo": "convivy/selector", "workflow_file": "ci.yml", "product": "selector"},
    {"repo": "convivy/selector-backend", "workflow_file": "ci.yml", "product": "selector"},
    # Round 2: `deck_tracked_repos()` no longer filters by the registry's
    # `class` field (see its own docstring) — these are every substrate/
    # platform crew's repo the widened derivation now yields, each verified
    # against GitHub directly (see the PR body's Round 2 section).
    {"repo": "convivy/library", "workflow_file": "ci.yml", "product": "library"},
    {"repo": "convivy/knowledge", "workflow_file": "ci.yml", "product": "library"},
    {"repo": "convivy/agent-platform", "workflow_file": "ci.yml", "product": "bosun"},
    {"repo": "convivy/placard", "workflow_file": "ci.yml", "product": "placard"},
    {"repo": "convivy/shipwright", "workflow_file": "ci.yml", "product": "shipwright"},
    {"repo": "convivy/convivy-orient", "workflow_file": "ci.yml", "product": "convivy-orient"},
    # Round 3: `build-deploy.yml` runs on `pull_request` to main.
    {"repo": "convivy/www", "workflow_file": "build-deploy.yml", "product": "convivy-orient"},
    {"repo": "convivy/convivy-app-kit", "workflow_file": "ci.yml", "product": "svc"},
    {"repo": "convivy/convivy-services", "workflow_file": "ci.yml", "product": "svc"},
    {"repo": "convivy/convivy-deck", "workflow_file": "ci.yml", "product": "convivy-deck"},
    {"repo": "convivy/substrate", "workflow_file": "checks.yml", "product": "steward"},
    # `vivy-landing` joined `deck_tracked_repos()` after Round 3 (library#209);
    # its only workflow, `ci.yml`, runs on `pull_request` and on push to main.
    {"repo": "convivy/vivy-landing", "workflow_file": "ci.yml", "product": "vivy"},
)

# Repositories `deck_tracked_repos()` yields that have no pull-request CI to
# capture, each with the evidence for leaving it out, checked 2026-10-01 with
# `gh api repos/<repo>/actions/workflows`. The startup check skips these so its
# `untracked:` lines name only a real gap (reviewer, PR #189 round 2: without
# this list it printed eleven lines on every run). A repository that gains a
# PR workflow moves from here to `TRACKED`.
NOT_CAPTURED = {
    "convivy/signals": "no GitHub Actions workflows",
    "convivy/write-better": "no GitHub Actions workflows",
    "convivy/constance-landing": "no GitHub Actions workflows",
    "convivy/constance-commercial": "no GitHub Actions workflows",
    "convivy/constance-updates": "no GitHub Actions workflows",
    "convivy/vivy-commercial": "404 from the GitHub API",
    "convivy/convivy-commercial": "404 from the GitHub API",
    "nyh/nyh-arch-tools": "404 from the GitHub API",
    "nyh/nyh-etc": "404 from the GitHub API",
    "nyh/nyh-park-villa": "404 from the GitHub API",
    # Checked 2026-10-09, after library#208 admitted it.
    "convivy/archy": "404 from the GitHub API",
}


def deck_tracked_repos(registry: dict | None = None) -> list[dict]:
    """Every `{"repo": "<owner>/<name>", "product": <slug>}` Deck's Pull
    Requests panel follows, mirroring Deck's own rule
    (issue_01M2PER2VWBMERJ6KSQTX3J0SW): `TRACKED`'s own untracked-repo check
    below calls this at startup, and `tests/test_ci_result_fetch.py` calls it
    directly against a fixture registry rather than this machine's real one.

    **Deck's rule, and where it lives.** The panel's repo list is
    `CockpitResolution.prLadderRepos(for: crew)`
    (`convivy-deck/Sources/Deck/Cockpit/CockpitResolution.swift`), which reads
    `crew.repos` (`convivy-deck/Sources/Deck/Rail/Crew.swift`) — a flat
    `owner/repo` list set PER CREW, with no reference to this registry's
    `class` field anywhere in that read. Round 1 of this function filtered to
    `class == "product"`, which silently dropped every crew whose registry
    entry is `class == "project"` — `library` (whose own repos are `library`,
    `knowledge`) among them, cited by `Crew.repos`'s own doc as a crew that
    follows more than one repo. `class` is this registry's distinction, not
    Deck's; this function no longer filters on it at all.

    **`crew.repos` itself is not a file this fetcher can read.** Deck fills
    it from one of two places, both outside this repo's reach: the
    operator's own per-host `theme.json` (hand-typed in Settings › Sessions),
    or a served crew spec Deck decodes over ssh
    (`ServedCrew.resolvedRepos`, `convivy-deck/Sources/Deck/Rail/ServedCrew.swift`)
    from `~/.config/convivy/deck/crews.d/<id>.json` — written by the
    steward's `new-crew`/`new-subcrew` skill and served by `session-agent
    crews` (`agent-platform/lib/agent-platform/session_crews.py`'s `_crew()`).
    As of this writing `_crew()` builds its served row with no `repo`/`repos`
    key at all, so that second path currently contributes nothing to a real
    `crew.repos` either way — today's only populated source is the
    per-host file. Neither is readable from here, so this derives the
    closest available proxy instead: `agent-platform/config/projects.json`'s
    own `repos` field per project, which already groups repos the same
    crew-shaped way Deck's config does (`library`: `["library",
    "knowledge"]`; `svc`: `["convivy-app-kit", "convivy-services"]`) and,
    unlike `crew.repos`, is a file this process can open at startup.

    **A repo counts when its project is `lifecycle == "active"` and not
    `private`.** No `class` check, per the above. `private` is withheld
    because a private crew's repo is exactly the ADR-0043 disclosure this
    function must not make (`cfo`/`ea`/`exampleco` in the real registry).
    This rule also yields repositories with no pull-request CI, such as the
    `nyh/*` repos, which return 404 from the GitHub API; `NOT_CAPTURED`
    lists them with their evidence, and the startup check skips them.

    `registry` defaults to `projects.projects()` (the real registry via
    `AGENT_PLATFORM_PROJECTS`/the agent-platform tree); a caller passes its
    own `{slug: project}` dict to test the derivation without touching any
    real file.
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
        owner = project.get("owner")
        if not isinstance(owner, str) or not owner.strip():
            continue
        for repo in project.get("repos") or []:
            if isinstance(repo, str) and repo.strip():
                out.append({"repo": f"{owner.strip()}/{repo.strip()}", "product": slug})
    return out


# STATE, NOT CONFIGURATION SHIPPED WITH THE CODE (platform/decisions/0059
# rule 3) — the cursor and the credential both outlive any one deploy, and an
# atomic install swap replaces ~/.local/deploy/library wholesale, which would
# destroy either if it lived there.
STATE_DIR = Path(os.environ.get("LIBRARY_STATE_DIR", str(Path.home() / ".library")))
CURSOR_FILE = STATE_DIR / "ci-fetch-cursor.json"
TOKEN_FILE = STATE_DIR / "ci-fetch-token"

# **A run's own record of itself** (issue_01M2PERXQA76N5TAYZRFS4AVRN):
# "tracked, asked, got nothing, ever" (no `test-result` signal exists for a
# pair, in any run) has to read differently from "tracked, asked, got
# nothing this run" — and neither the cursor nor `dedup_key` answers that.
# The cursor advances even when a pair's window held zero runs, and holds in
# place on an error the same way whether that error is this pair's first
# ever or its hundredth. Written once per run, after every `TRACKED` pair
# has had its turn (see `main`) — a run's finish time is only known once the
# loop is done, so unlike the cursor this is never written per pair.
STATUS_FILE = STATE_DIR / "ci-fetch-status.json"

LIBRARY_URL = os.environ.get("LIBRARY_URL", "http://127.0.0.1:8100")
PROTOCOL_VERSION = "1"

# GitHub's own eventual-consistency margin, the same role OVERLAP plays in
# github_capture.py: a run that concluded moments before this process read
# "now" might not yet be visible to the API, so the next cursor sits a little
# behind the run's own start rather than exactly at it. dedup_key absorbs the
# re-covered runs this creates.
#
# **This is a fallback, not the mechanism that keeps an in-flight run
# safe.** A run still incomplete when this fetch started holds the cursor
# back to its OWN created_at (see capture_workflow) regardless of how long
# it takes to finish; OVERLAP only covers the gap this margin was always for
# — a run that already completed moments before "now" but isn't visible to
# the API yet. Treating OVERLAP itself as the safety margin for in-flight
# runs is the bug this file exists to fix.
OVERLAP = dt.timedelta(minutes=5)

# The DEFAULT ceiling on how long an incomplete run may hold a pair's cursor
# back before it is treated as abandoned rather than in flight. GitHub's own
# hard ceiling is 35 days (every workflow run is force-resolved to
# `completed` by then), far too long to let one stuck run freeze a pair's
# cursor and grow its refetch window every 15-minute poll forever — the
# opposite failure, and just as bad. 24 hours is grounded in GitHub's own
# default job-execution cap for GitHub-HOSTED runners (6 hours) plus a wide
# margin, not in any one workflow's OBSERVED runtime — the original bug came
# from sizing a margin to what "normal" looked like, and this ceiling is
# deliberately not that.
#
# **That 6-hour cap does not bind self-hosted runners** — a job there can sit
# legitimately queued far longer behind a serial runner working through a
# backlog, so this default must not silently apply to one. A `TRACKED` entry
# whose runner isn't GitHub-hosted MUST override it with its own
# `stale_pending_after` (see `TRACKED` above and `capture_workflow`) rather
# than inherit reasoning that doesn't hold for it — reviewer finding,
# 2026-08-31.
STALE_PENDING_AFTER = dt.timedelta(hours=24)

# **THE BOUND A PAIR WITH NO STORED CURSOR STARTS FROM.** Every signal this
# fetcher posts is `kind == "test-result"` (see `signal_from_run`), and
# `cold_tier.py` moves anything older than `vectors.HOT_WINDOW_DAYS["test-result"]`
# out of the hot index on its own 6-hour timer regardless of how it got
# there — so a first fetch reaching further back than that only captures rows
# that get moved to cold right after, for no benefit over starting at the
# bound itself.
#
# Copied here rather than imported: `vectors.py` resolves `LIBRARY_VECTORS_DB`
# at import time (`VECTORS_DB_PATH = resolve_vectors_db_path()`, raising
# `MisconfiguredVectorsDB` when unset), and this job's own plist
# (`launchd/com.convivy.library.ci-result-fetch.plist`) sets no such
# variable — `import vectors` from here would turn a missing env var into an
# import-time crash of the whole fetcher, not just the vector index.
# `tests/test_ci_result_fetch.py` pins this constant equal to
# `vectors.HOT_WINDOW_DAYS["test-result"]` so the two cannot drift silently.
NO_CURSOR_WINDOW_DAYS = 90

# **THE RATE-LIMIT RETRY BOUND, PER SIGNAL** (see the module docstring for
# the production incident this answers). `check_signal_rate_limit`'s own
# math caps a 429's `retry_after` at `SIGNAL_RATE_WINDOW_SECONDS` (60s) in
# the ordinary case — the oldest timestamp in the actor's window exits it
# after at most one window — but `post_signal` does not trust that forever:
# a single wait is clamped to `RATE_LIMIT_MAX_WAIT_SECONDS` regardless of
# what a response claims, and a signal still 429'd after
# `RATE_LIMIT_MAX_RETRIES` attempts is reported as an ordinary post failure
# rather than retried again. Ten retries at up to five minutes each is far
# above anything one window reset has ever required here, while still
# bounding ONE SIGNAL'S pathological case (a stuck rate limiter, or a 429
# from something other than this gate) to under an hour.
#
# This is not a per-run or per-pair bound — `capture_workflow` is what keeps
# a sustained 429 from compounding across a whole batch: the first signal to
# exhaust this bound stops that pair's posting loop for the rest of this
# run rather than letting every remaining signal retry it independently.
RATE_LIMIT_MAX_RETRIES = 10
RATE_LIMIT_MAX_WAIT_SECONDS = 300

# The wait used only when a 429 carries neither a parseable `retry_after`
# body field nor a `Retry-After` header — a shape this fetcher has never
# seen the real rate gate send (app.py sets both, identically), but a
# defensive fallback rather than treating an unparseable 429 as a fetch it
# should give up on at once.
RATE_LIMIT_FALLBACK_RETRY_SECONDS = 60


class CaptureError(RuntimeError):
    """A `gh api` fetch failed, or a run in its response could not be turned
    into a signal (missing a field `signal_from_run` depends on). Raised
    rather than swallowed, and raised BEFORE anything in the same batch is
    posted — so a partial or malformed page never leaves the cursor
    advanced past data this run never actually wrote."""


# --- pure transforms (unit-tested, no network) ------------------------------

def workflow_slug(path: str) -> str:
    """.github/workflows/e2e-stage-smoke.yml -> e2e-stage-smoke."""
    return Path(path).stem


def _parse_timestamp(value: str) -> dt.datetime:
    """GitHub's run timestamps: always `YYYY-MM-DDTHH:MM:SSZ` (UTC, zero
    padded, no fractional seconds) in every response this fetcher has seen —
    swapping the trailing `Z` for an explicit offset is enough for
    `fromisoformat`; no timezone library needed."""
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


# Orient's `engineering_e2e_build.py` reads `meta.status` and `meta.run_url`
# (`_status_of` / `_run_link`) — not `refs.conclusion` / `refs.run_url`, which
# is where this fetcher used to leave them, unread by the one consumer that
# exists (issue_01M1F431KC6DPYY2D185GD036J). `refs` keeps carrying the raw
# values regardless: it is the only place the conclusion GitHub actually sent
# survives once the mapping below has thrown information away.
#
# **Fail closed, not a two-way switch.** `_status_of` treats anything outside
# the literal strings `pass`/`fail` as `unknown`, so writing GitHub's own
# `success`/`failure` verbatim would satisfy "meta.status is populated" while
# leaving the page exactly as broken. GitHub's `conclusion` also produces
# `skipped`, `cancelled`, `timed_out`, `neutral` and `action_required`, and is
# `None` for a run still in flight — every one of those omits `meta.status`
# entirely here, so the page renders `unknown` honestly rather than being
# forced into a guessed `pass` or `fail`.
_CONCLUSION_TO_META_STATUS = {"success": "pass", "failure": "fail"}


def conclusion_to_meta_status(conclusion: str | None) -> str | None:
    """GitHub's `conclusion` -> Orient's `meta.status` vocabulary, or `None`
    for anything `_CONCLUSION_TO_META_STATUS` doesn't recognise — the caller
    omits `meta.status` rather than write a guessed value."""
    return _CONCLUSION_TO_META_STATUS.get(conclusion)


def signal_from_run(run: dict, product: str) -> dict:
    """One completed GitHub Actions run -> one Library `POST /signals` body.

    Raises `CaptureError` when the run is missing a field the dedup key or
    the row depends on (`id`, `path`, `created_at`) — treated the same as a
    fetch failure, since a signal this fetcher cannot dedup or attribute
    reliably is worse than one it retries for.
    """
    run_id = run.get("id")
    path = run.get("path")
    created_at = run.get("created_at")
    if not isinstance(run_id, int) or not path or not created_at:
        raise CaptureError(f"malformed workflow run in GitHub's response: {run!r}")
    workflow = workflow_slug(path)
    conclusion = run.get("conclusion")
    meta = {"run_number": run.get("run_number"), "event": run.get("event"),
            "run_url": run.get("html_url")}
    status = conclusion_to_meta_status(conclusion)
    if status is not None:
        meta["status"] = status
    return {
        "source": "ci",
        "kind": "test-result",
        "product": product,
        "stream": workflow,
        "actor": "github-actions",
        "body": f"{workflow}: {conclusion or 'unknown'}",
        "refs": {
            "conclusion": conclusion,
            "workflow": workflow,
            "commit": run.get("head_sha"),
            "run_url": run.get("html_url"),
        },
        "meta": meta,
        "produced_at": created_at,
        "dedup_key": f"ci:{product}:{workflow}:{run_id}",
    }


# --- I/O: gh api fetch, signal POST, cursor and token files -----------------

# **GitHub caps a `created`-filtered run list at this many results, and
# `total_count` keeps reporting the true, higher figure regardless** — the
# page past the cap returns empty rather than erroring, so a caller that
# doesn't account for it silently gets a truncated result with no signal
# that anything was left out. Measured 2026-10-01 against
# `convivy/knowledge#ci.yml`'s 90-day window (see the PR body): `total_count`
# 2500, page 10 returned 100 runs, page 11 returned 0 filtered vs 100
# unfiltered for the same page. `gh_runs` answers this by splitting a capped
# query's date range and re-querying the halves (`_gh_runs_window`) — the
# cap is a per-query result limit, not a limit on how much history actually
# exists, and a range query over a narrower window is subject to the exact
# same cap but matches far fewer runs.
GITHUB_CREATED_FILTER_CAP = 1000


def _gh_runs_page(repo: str, workflow_file: str,
                  created_filter: str | None) -> list[dict]:
    """One `gh api --paginate` call — every page GitHub returns for this
    exact `created` filter value, newest first. `created_filter` is the
    complete, already `urllib.parse.quote`d query-string value to send as
    `created=<created_filter>`, or `None` to send no `created` filter at
    all. The shared low-level fetch both `gh_runs` and `_gh_runs_window`
    build on; see `gh_runs` for the raised-not-swallowed `CaptureError`
    contract."""
    filter_q = f"&created={created_filter}" if created_filter else ""
    path = (f"repos/{repo}/actions/workflows/{workflow_file}/runs"
            f"?per_page=100{filter_q}")
    try:
        out = subprocess.run(
            ["gh", "api", "--paginate", path, "--jq", ".workflow_runs[]"],
            capture_output=True, text=True, timeout=120)
    except Exception as ex:
        raise CaptureError(f"gh api {path}: {type(ex).__name__}") from ex
    if out.returncode != 0:
        raise CaptureError(f"gh api {path}: {out.stderr.strip()[:300]}")
    runs = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            runs.append(json.loads(line))
        except json.JSONDecodeError as ex:
            raise CaptureError(f"gh api {path}: unparseable line: {ex}") from ex
    return runs


def _gh_runs_window(repo: str, workflow_file: str, since_dt: dt.datetime,
                    until_dt: dt.datetime) -> list[dict]:
    """Every run with `created` inside `[since_dt, until_dt]`, BOTH ends
    inclusive (GitHub's `A..B` range syntax) — recursing by splitting the
    range in half whenever one query comes back at exactly
    `GITHUB_CREATED_FILTER_CAP` results, which means GitHub truncated it
    rather than that being the true count for the range. Deduplicates by
    `id` across the two halves, since the shared inclusive boundary can
    return the same run from both.

    Raises `CaptureError` — never silently returns a window it cannot prove
    it covered in full — if a capped query's range is already under one
    second wide and so cannot be split any further. That needs over
    `GITHUB_CREATED_FILTER_CAP` runs created inside the same second and has
    never been observed; failing the fetch (which leaves the pair's cursor
    exactly where it was, same as any other `CaptureError`) is the correct
    answer to a case this code cannot prove it got right, not a guess at
    one.
    """
    since_s = since_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    until_s = until_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    created_filter = urllib.parse.quote(f"{since_s}..{until_s}", safe="")
    runs = _gh_runs_page(repo, workflow_file, created_filter)
    if len(runs) < GITHUB_CREATED_FILTER_CAP:
        return runs
    if until_dt - since_dt <= dt.timedelta(seconds=1):
        raise CaptureError(
            f"repos/{repo}/actions/workflows/{workflow_file}/runs: "
            f"created-filter cap ({GITHUB_CREATED_FILTER_CAP}) reached for "
            f"a window ({since_s}..{until_s}) under one second wide — "
            f"cannot split it any further, refusing to advance the cursor "
            f"past a range this call cannot prove it fetched in full")
    mid_dt = since_dt + (until_dt - since_dt) / 2
    newer = _gh_runs_window(repo, workflow_file, mid_dt, until_dt)
    older = _gh_runs_window(repo, workflow_file, since_dt, mid_dt)
    seen_ids = set()
    merged = []
    for run in newer + older:
        run_id = run.get("id")
        if run_id not in seen_ids:
            seen_ids.add(run_id)
            merged.append(run)
    return merged


def gh_runs(repo: str, workflow_file: str, since: str | None) -> list[dict]:
    """Every run of one workflow — ANY status, completed or not — newest-
    first as GitHub returns them, `created` on/after `since` (GitHub's own
    range filter, inclusive: `>=`) when given.

    **Deliberately not filtered to `status=completed` server-side.** That
    filter is what caused this fetcher to silently lose runs still in
    flight when a poll fired: excluded from that page for being incomplete,
    then excluded from every later page once the cursor moved past its
    creation time on wall-clock alone. `capture_workflow` does the
    completed/incomplete split itself, because only it can hold the cursor
    back for whatever this call returns as still running.

    **A `since` query that comes back at exactly `GITHUB_CREATED_FILTER_CAP`
    results is retried as a split, recursive range fetch instead of
    returned as-is** (`_gh_runs_window`, over `[since, now]`) — see that
    constant's own comment for the cap this answers. The ordinary,
    nowhere-near-the-cap case (every tracked pair's incremental poll, and
    most pairs' very first fetch) costs exactly the one `>=since` query this
    function always used to make; only a pair whose window holds more than
    the cap pays for the extra queries the split needs.

    Raises `CaptureError` on any transport, exit-code, or parse failure —
    never returns a partial list silently. `gh api --paginate` merges every
    page's `workflow_runs` into one JSON-lines stream via `--jq`, so a
    mid-pagination failure is a non-zero exit here, not a partial success.
    """
    if since is None:
        return _gh_runs_page(repo, workflow_file, None)
    runs = _gh_runs_page(
        repo, workflow_file, f"%3E%3D{urllib.parse.quote(since, safe='')}")
    if len(runs) < GITHUB_CREATED_FILTER_CAP:
        return runs
    since_dt = _parse_timestamp(since)
    until_dt = dt.datetime.now(dt.timezone.utc)
    return _gh_runs_window(repo, workflow_file, since_dt, until_dt)


def _retry_after_seconds(e: urllib.error.HTTPError) -> int:
    """How long a 429's own response says to wait before retrying it — read
    from whichever of the body's `retry_after` field (`refusals.refuse`'s
    `**fields`, app.py's rate gate) or the identically-valued `Retry-After`
    header this response actually carries, body first since it is the
    documented contract. Falls back to `RATE_LIMIT_FALLBACK_RETRY_SECONDS`
    for a 429 shaped unlike the Library's own rate gate, OR for a value
    that is non-finite (`inf`, `nan`), negative, or unparseable in either
    place — never raises. `int(float("inf"))` raises `OverflowError`, which
    neither place's own `except` used to catch (fixed here, reviewer/
    security-reviewer round 1: a hostile or broken `Retry-After: inf`
    escaped this function uncaught, abandoning the rest of the pair's
    batch instead of falling back to the documented default)."""
    try:
        raw = e.read()
    except Exception:
        raw = b""
    try:
        value = json.loads(raw.decode("utf-8")).get("retry_after")
        if (isinstance(value, (int, float)) and math.isfinite(value)
                and value >= 0):
            return int(value)
    except Exception:
        pass
    try:
        header = e.headers.get("Retry-After") if e.headers else None
        if header is not None:
            value = float(header)
            if math.isfinite(value) and value >= 0:
                return int(value)
    except (TypeError, ValueError, OverflowError):
        pass
    return RATE_LIMIT_FALLBACK_RETRY_SECONDS


def post_signal(payload: dict, token: str) -> str:
    """POST one signal. Returns 'created' | 'duplicate' | 'conflict' |
    'error:<code-or-exception-name>'.

    **'conflict' (`POST /signals`'s 409) is deliberately not an 'error'.**
    The Library's no-leak rule (app.py's `create_signal`, `store.insert_signal`)
    answers a `dedup_key` collision the caller cannot read — a token whose
    scope no longer covers the row, or a pre-existing customer-class row —
    with a bare 409, and that 409 will never clear on retry: the same
    `dedup_key` will collide against the same unreadable row every time this
    exact run is re-posted. Folding it into 'error' would leave
    `capture_workflow`'s tally unable to tell a permanent mismatch from a
    transient 5xx/timeout, and `main()` would re-fetch and re-hit the same
    409 forever (see `capture_workflow`'s docstring for what that pair does
    with it instead).

    **A 429 is retried in place, not reported as a failure, up to
    `RATE_LIMIT_MAX_RETRIES` times.** Each retry waits `_retry_after_seconds`
    of this exact 429 (clamped to `RATE_LIMIT_MAX_WAIT_SECONDS`) and re-POSTs
    the same payload — see the module docstring for the livelock this closes.
    A signal still 429'd past the bound returns `'error:429'`, tallied by
    `capture_workflow` exactly like any other post failure: the pair's
    cursor does not advance past it."""
    req = urllib.request.Request(
        f"{LIBRARY_URL}/signals",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Library-Protocol": PROTOCOL_VERSION,
        },
        method="POST")
    retries = 0
    while True:
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status == 201:
                    return "created"
                if resp.status == 200:
                    return "duplicate"
                if resp.status == 409:
                    return "conflict"
                return f"error:{resp.status}"
        except urllib.error.HTTPError as e:
            if e.code == 409:
                return "conflict"
            if e.code == 429:
                if retries >= RATE_LIMIT_MAX_RETRIES:
                    return "error:429"
                time.sleep(min(_retry_after_seconds(e), RATE_LIMIT_MAX_WAIT_SECONDS))
                retries += 1
                continue
            return f"error:{e.code}"
        except Exception as ex:
            return f"error:{type(ex).__name__}"


def load_cursor() -> dict:
    try:
        return json.loads(CURSOR_FILE.read_text())
    except Exception:
        return {}


def save_cursor(cursor: dict) -> None:
    """Write the cursor file atomically: a temp file in the same directory,
    then `os.replace`, never a direct `write_text` (truncate-then-write).
    A process killed mid-write must never leave `CURSOR_FILE` holding a
    truncated JSON fragment — `load_cursor`'s broad `except` would read that
    as "no cursor at all" and silently trigger a full refetch of every
    tracked pair, which `dedup_key` makes safe but slow.

    **No lock file guards concurrent invocations, and that is deliberate
    rather than an oversight.** launchd does not start a second instance of
    the same `Label` while a prior one is still running — this job carries
    no `KeepAlive` and no queueing directive that would change that — so the
    only way to get two writers is running this script by hand while the
    scheduled instance is also mid-run, an operator action rather than
    something `StartInterval=900` (`launchd/com.convivy.library.ci-result-fetch.plist`)
    can trigger on its own. A same-directory temp name plus `os.replace`
    already makes even that case land on ONE writer's full cursor rather than
    a byte-interleaved corrupt file; a lock would only prevent the loser's
    update from being silently overwritten by the winner's, which `dedup_key`
    already makes a slow refetch rather than a data-loss risk.

    If `os.replace` itself raises (e.g. a permission error), the temp file
    is unlinked before the exception propagates — `CURSOR_FILE` is never
    touched on this path, so this is temp-file hygiene under `~/.library`,
    not a correctness fix.
    """
    CURSOR_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CURSOR_FILE.with_name(f"{CURSOR_FILE.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(cursor, indent=1, sort_keys=True))
    try:
        os.replace(tmp, CURSOR_FILE)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def load_status() -> dict | None:
    """The last run's status, or `None` when `STATUS_FILE` is missing or is
    not parseable JSON — both read the same way by every caller (this
    module's own run, and `GET /signals/ci-capture`): there is no run to
    report on yet, never a 500 and never a silently empty answer with no
    explanation."""
    try:
        return json.loads(STATUS_FILE.read_text())
    except Exception:
        return None


def save_status(status: dict) -> None:
    """Write `STATUS_FILE` atomically — the same temp-file-then-`os.replace`
    pattern `save_cursor` uses, and for the same reason: a process killed
    mid-write must never leave this file holding a truncated JSON fragment,
    which `load_status`'s broad `except` would otherwise read as "no run has
    ever finished" rather than "the last one was interrupted"."""
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATUS_FILE.with_name(f"{STATUS_FILE.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(status, indent=1, sort_keys=True))
    try:
        os.replace(tmp, STATUS_FILE)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def load_token() -> str:
    tok = os.environ.get("LIBRARY_CI_FETCH_TOKEN", "").strip()
    if tok:
        return tok
    try:
        return TOKEN_FILE.read_text().strip()
    except OSError:
        sys.exit(f"no CI-fetch token: set LIBRARY_CI_FETCH_TOKEN or create "
                  f"{TOKEN_FILE} (0600)")


# --- the capture run ---------------------------------------------------------

def capture_workflow(repo: str, workflow_file: str, product: str,
                     since: str | None, token: str, *,
                     fetcher=None, poster=None, run_started=None,
                     stale_pending_after=None) -> dict:
    """One tracked (repo, workflow) pair. Returns a tally dict with keys
    `created`/`duplicate`/`conflict`/`errors`/`stale_pending`, plus
    `next_since` — the cursor value `main()` should persist for this pair
    when `errors == 0` (see below; `None` whenever it isn't safe to advance
    at all).

    Also carries `runs_returned` (how many runs `fetcher` returned this
    call, `None` when the fetch itself never completed), `reason` (the
    first `CaptureError` or post failure text this call hit, `None` on a
    clean run) and `signals_written` (`created` + `duplicate` — every
    payload this call's own POST calls left in a written state, as opposed
    to failed or conflicted) — `main()` folds these into the status file it
    writes once per run (see `save_status`), not into the cursor.

    `run_started` is this fetch's own "now" — the instant every incomplete
    run's age is measured against, and the basis for the `OVERLAP` fallback.
    Resolved at call time the same way `fetcher`/`poster` are, so a direct
    caller not testing time-dependent behaviour can omit it.

    `stale_pending_after` is this pair's own ceiling on how long an
    incomplete run may hold the cursor back before being treated as
    abandoned — defaults to the module-level `STALE_PENDING_AFTER`
    (GitHub-hosted-runner reasoning) when omitted, but `main()` passes a
    `TRACKED` entry's own `stale_pending_after` when that entry sets one, so
    a self-hosted-runner workflow is never silently held to a ceiling
    grounded in a cap that doesn't bind it.

    `fetcher`/`poster` default to the module-level `gh_runs`/`post_signal`,
    resolved at call time (not bound as a definition-time default) so a
    caller going through `main()` still gets whatever the module currently
    names — the seam a test replaces either by passing its own fake here
    directly, or by monkeypatching the module attribute before calling
    `main()`.

    Every run this call posts is idempotent via `dedup_key`, so calling this
    twice over the same window — a re-run, or a re-triggered backfill —
    creates nothing twice; the second call only adds `duplicate` counts.

    A fetch failure, OR one malformed run inside an otherwise-good page,
    aborts the WHOLE window before anything is posted: every run is turned
    into a payload first, and only once that has fully succeeded does
    posting begin — so a batch that fails partway through transformation
    never ends up with some of its runs captured and others silently
    skipped. A run still incomplete with no parseable `created_at` counts as
    malformed the same way — the watermark cannot be computed safely without
    it, so the whole window aborts rather than guessing.

    **`next_since` is the oldest timestamp `fetcher` returned this call
    could be resumed from without skipping a run that has not yet been
    captured.** `runs` is split by `status`: a `completed` run is turned
    into a signal and posted as before; anything else is still in flight
    (or, past `STALE_PENDING_AFTER`, presumed abandoned — see below) and
    MUST be asked for again next time, so it holds `next_since` back to its
    own `created_at` rather than letting the cursor advance past it on
    wall-clock time alone. `next_since` is the minimum of `run_started -
    OVERLAP` (the ordinary eventual-consistency fallback) and every
    still-in-flight run's `created_at` — never later than either, so it
    never skips a run genuinely worth re-checking.

    A run stuck incomplete past `STALE_PENDING_AFTER` is excluded from that
    minimum and tallied `stale_pending` instead: printed loudly (never
    silently), but not held onto, because an item that never finishes would
    freeze this pair's cursor and its refetch window forever — the failure
    this exists to fix, inverted.

    **A signal reporting `"error:429"` (its own `RATE_LIMIT_MAX_RETRIES`
    exhausted — see `post_signal`) stops the posting loop for the REST of
    this pair's batch, rather than letting each remaining signal retry the
    same bound independently.** `post_signal`'s retry bound is per POST; a
    sustained 429 (a stuck rate limiter, not an ordinary volume spike)
    would otherwise cost this one pair's run a multiple of that bound —
    `RATE_LIMIT_MAX_RETRIES` × `RATE_LIMIT_MAX_WAIT_SECONDS` for EVERY
    payload still queued (reviewer/security-reviewer, round 1). The
    exhausted signal is tallied as an ordinary error, every payload after
    it is left unposted this run, and the pair's cursor — already withheld
    by that one error — picks all of them back up on the next poll.

    **`conflict` is tallied separately from `errors`, and that separation is
    the chosen answer to the stuck-pair problem `post_signal` documents: a
    permanent 409 does not hold this pair's cursor back.** `main()` only
    withholds a pair's cursor advance on `errors`, so a run that hits a
    409 is loudly reported (its `dedup_key`, tagged `CONFLICT` so it reads
    differently from a `post failed` line) but does not force every later
    run to re-fetch and re-hit that same immovable row on every future
    invocation. This is deliberate, not an oversight: `dedup_key` is this
    fetcher's own deterministic `ci:<product>:<workflow-slug>:<run-id>`
    scheme, so a 409 on it means a row under that exact key already exists
    — the event was already captured through *some* path, just not one this
    call can read back — rather than the row never having been written at
    all. Blocking the cursor forever would buy nothing (the row is not
    coming back writable on a later retry) and would cost every other run
    in the window a repeated re-fetch on every future invocation, forever.

    **That the key belongs to this fetcher is convention, not something
    `POST /signals` enforces.** `dedup_key` is an ordinary caller-supplied
    string on a shared write surface; no reservation ties it to this
    fetcher or to any particular caller. Another credential holding
    `signal.create` (and `signal.create_customer` for a customer-class row)
    could write a row under the exact string this fetcher will later
    compute for a real run, and that run's own post would then 409 against
    it and be skipped as if already captured. Today nothing else is known
    to generate this scheme's keys, so the assumption holds in practice —
    but it is an accepted, pre-existing property of `/signals` that this
    fetcher relies on, not one it enforces.
    """
    fetcher = fetcher if fetcher is not None else gh_runs
    poster = poster if poster is not None else post_signal
    run_started = (run_started if run_started is not None
                   else dt.datetime.now(dt.timezone.utc))
    stale_pending_after = (stale_pending_after if stale_pending_after is not None
                           else STALE_PENDING_AFTER)
    tally = {"created": 0, "duplicate": 0, "conflict": 0, "errors": 0,
             "stale_pending": 0, "next_since": None, "runs_returned": None,
             "reason": None, "signals_written": 0}
    try:
        runs = fetcher(repo, workflow_file, since)
    except CaptureError as ex:
        print(f"  {repo}/{workflow_file}: {ex}", file=sys.stderr)
        tally["errors"] += 1
        tally["reason"] = str(ex)
        return tally
    tally["runs_returned"] = len(runs)
    completed_runs = [run for run in runs if run.get("status") == "completed"]
    incomplete_runs = [run for run in runs if run.get("status") != "completed"]
    try:
        payloads = [signal_from_run(run, product) for run in completed_runs]
        in_flight_created_ats = []
        for run in incomplete_runs:
            created_at = run.get("created_at")
            if not created_at:
                raise CaptureError(
                    f"incomplete workflow run with no created_at, cannot "
                    f"safely place it relative to the cursor: {run!r}")
            try:
                created_dt = _parse_timestamp(created_at)
            except (ValueError, AttributeError, TypeError) as ex:
                raise CaptureError(
                    f"incomplete workflow run with an unparseable "
                    f"created_at, cannot safely place it relative to the "
                    f"cursor: {run!r} ({type(ex).__name__}: {ex})") from ex
            age = run_started - created_dt
            if age > stale_pending_after:
                print(f"  {repo}/{workflow_file}: STALE_PENDING (still "
                      f"{run.get('status')!r} after {age}, past the "
                      f"{stale_pending_after} ceiling — treated as "
                      f"abandoned, cursor advances past it): run "
                      f"{run.get('id')}, created {created_at}",
                      file=sys.stderr)
                tally["stale_pending"] += 1
            else:
                in_flight_created_ats.append(created_dt)
    except CaptureError as ex:
        print(f"  {repo}/{workflow_file}: {ex}", file=sys.stderr)
        tally["errors"] += 1
        tally["reason"] = str(ex)
        return tally
    for i, payload in enumerate(payloads):
        result = poster(payload, token)
        if result == "created":
            tally["created"] += 1
        elif result == "duplicate":
            tally["duplicate"] += 1
        elif result == "conflict":
            print(f"  {repo}/{workflow_file}: CONFLICT (permanent, not "
                  f"retried — dedup_key already matches a row this token "
                  f"cannot read): {payload['dedup_key']}", file=sys.stderr)
            tally["conflict"] += 1
        elif result == "error:429":
            remaining = len(payloads) - i - 1
            print(f"  {repo}/{workflow_file}: sustained 429 — "
                  f"RATE_LIMIT_MAX_RETRIES exhausted on "
                  f"{payload['dedup_key']}; not spending that same bound "
                  f"again on each of the {remaining} remaining signal(s) "
                  f"this run — stopping this pair's batch here, cursor "
                  f"held, all of it re-asked for next poll",
                  file=sys.stderr)
            tally["errors"] += 1
            if tally["reason"] is None:
                tally["reason"] = (
                    f"sustained 429 on {payload['dedup_key']}")
            break
        else:
            print(f"  {repo}/{workflow_file}: post failed: {result} "
                  f"({payload['dedup_key']})", file=sys.stderr)
            tally["errors"] += 1
            if tally["reason"] is None:
                tally["reason"] = (
                    f"post failed: {result} ({payload['dedup_key']})")
    tally["signals_written"] = tally["created"] + tally["duplicate"]
    watermark_candidates = [run_started - OVERLAP, *in_flight_created_ats]
    tally["next_since"] = min(watermark_candidates).strftime("%Y-%m-%dT%H:%M:%SZ")
    return tally


def _print_never_captured(token: str) -> None:
    """Names, on this run's own printed tally, any `TRACKED` pair `GET
    /signals/ci-capture` reports as `never_captured` — read back through
    that exact route (never recomputed here) so this print and that route
    can never disagree about what "never captured" means. This run already
    knows what IT fetched; it does not know what every prior run ever
    wrote, which is a fact only the Library's own store can answer.

    **Never raises, and never stops the run.** A token lacking
    `signal.read`, or any transport failure, costs this one diagnostic
    line — the same posture `main`'s own untracked-repo check already
    takes toward a check that has no caller above it to fail safely back
    to."""
    req = urllib.request.Request(
        f"{LIBRARY_URL}/signals/ci-capture",
        headers={"Authorization": f"Bearer {token}",
                 "Library-Protocol": PROTOCOL_VERSION},
        method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        repos = [e["repo"] for e in body.get("never_captured", [])]
    except Exception as ex:
        print(f"never_captured: could not be read from GET "
              f"/signals/ci-capture: {type(ex).__name__}: {ex}",
              file=sys.stderr)
        return
    print(f"never_captured: {', '.join(repos) if repos else 'none'}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Fetch CI conclusions from GitHub and post them to the "
                     "Library as signals")
    ap.add_argument(
        "--since", help="override every tracked pair's cursor with this "
                         "ISO8601 timestamp for this run — the backfill "
                         "flag. The cursor still advances normally "
                         "afterward, so a backfill is safe to re-run.")
    args = ap.parse_args()

    # Loud, not silent: a repo Deck's panel draws with no TRACKED entry is
    # exactly the gap `vivy`'s stage-smoke entry used to hide (see TRACKED's
    # own docstring note) — named here so the next one is a startup line, not
    # a rediscovery.
    #
    # **Must never abort the run.** `deck_tracked_repos()` -> `projects.projects()`
    # -> `projects._raw()` is fail-open for an unreadable or non-JSON registry
    # (see `projects.py`'s own module docstring) and returns `{}`, which this
    # loop handles fine — but a registry that parses yet is shaped wrong (not
    # a top-level object, say) raises uncaught inside `projects()` itself,
    # with nothing here to catch it; reached before `token`/`cursor` are even
    # loaded, that would turn one missed warning line into a total outage of
    # this run's capture (security-reviewer finding, round 1). Every other
    # `projects.py` consumer in this codebase (`app.py`, `backfill_common.py`,
    # `review_record_backfill.py`) checks `projects.available()` before
    # trusting the registry rather than letting a read of it raise into their
    # own caller; this is the same posture applied to a check that has no
    # caller above it to fail safely back to.
    try:
        tracked_repos = {entry["repo"] for entry in TRACKED}
        for deck_entry in deck_tracked_repos():
            if (deck_entry["repo"] not in tracked_repos
                    and deck_entry["repo"] not in NOT_CAPTURED):
                print(f"untracked: {deck_entry['repo']} (Deck draws it for product "
                      f"{deck_entry['product']!r}, but TRACKED names no entry for it)",
                      file=sys.stderr)
    except Exception as ex:
        print(f"deck_tracked_repos: registry unreadable or malformed, skipping "
              f"the untracked-repo check this run: {type(ex).__name__}: {ex}",
              file=sys.stderr)

    token = load_token()
    cursor = load_cursor()
    run_started = dt.datetime.now(dt.timezone.utc)
    # The fallback for a pair with neither an explicit --since nor a stored
    # cursor — see NO_CURSOR_WINDOW_DAYS. Computed once, from this run's own
    # "now", rather than per pair.
    no_cursor_since = (run_started - dt.timedelta(days=NO_CURSOR_WINDOW_DAYS)
                       ).strftime("%Y-%m-%dT%H:%M:%SZ")

    grand = {"created": 0, "duplicate": 0, "conflict": 0, "errors": 0,
             "stale_pending": 0}
    # **BUILT UP ACROSS THE WHOLE LOOP, WRITTEN ONCE AT THE END.** Unlike the
    # cursor, this has no "furthest safe to resume from" the way `next_since`
    # does, so there is nothing to lose by deferring the write — and the run's
    # own finish time (below) isn't known until the loop is done anyway. One
    # pair's failure still can't cost another pair its entry: every branch
    # below, including the outer safety net, appends to `status_pairs` the
    # same way `save_cursor` already lets one pair's failure stand alone.
    status_pairs = {}
    for entry in TRACKED:
        key = f"{entry['repo']}#{entry['workflow_file']}"
        since = args.since or cursor.get(key) or no_cursor_since
        try:
            t = capture_workflow(entry["repo"], entry["workflow_file"],
                                 entry["product"], since, token,
                                 run_started=run_started,
                                 stale_pending_after=entry.get("stale_pending_after"))
        except Exception as ex:
            # **THE OUTER SAFETY NET.** `capture_workflow` only catches
            # `CaptureError` — anything else (a non-dict entry from `gh api`
            # reaching `run.get(...)` as an `AttributeError`, say) propagates
            # here uncaught. Caught at THIS level, tallied as an error for
            # this pair only, so the pair keeps its old cursor while every
            # OTHER pair in `TRACKED` still gets its turn and its own
            # `save_cursor` call below, rather than one pair's unexpected
            # exception aborting the whole run and losing every earlier
            # pair's already-earned cursor advance with it.
            print(f"{key}: unexpected failure: {type(ex).__name__}: {ex}",
                  file=sys.stderr)
            t = {"created": 0, "duplicate": 0, "conflict": 0, "errors": 1,
                 "stale_pending": 0, "runs_returned": None,
                 "reason": f"{type(ex).__name__}: {ex}", "signals_written": 0}
        print(f"{key}: {t}")
        for k in grand:
            grand[k] += t[k]
        if t["errors"] == 0:
            # capture_workflow computed the furthest-back timestamp still
            # safe to resume from — the ordinary OVERLAP margin, or an
            # in-flight run's own created_at, whichever is earlier (see its
            # docstring). A pair with errors keeps its old cursor so
            # nothing is skipped past.
            cursor[key] = t["next_since"]
        # **SAVED EVERY ITERATION, NOT ONCE AFTER THE LOOP.** Persisting each
        # pair's cursor as soon as it's computed means an exception this
        # `try` doesn't anticipate (a bug in `print`, a `KeyboardInterrupt`,
        # anything) loses at most the pair being processed when it happens,
        # never the advances every earlier pair in `TRACKED` already earned.
        save_cursor(cursor)
        status_pairs[key] = {
            "repo": entry["repo"],
            "workflow_file": entry["workflow_file"],
            "outcome": "error" if t["errors"] else "ok",
            "reason": t.get("reason"),
            "runs_returned": t.get("runs_returned"),
            "signals_written": t.get("signals_written", 0),
        }
    print(f"total: {grand}")
    run_finished = dt.datetime.now(dt.timezone.utc)
    save_status({
        "run_started": run_started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_finished": run_finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pairs": status_pairs,
    })
    _print_never_captured(token)
    if grand["errors"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
