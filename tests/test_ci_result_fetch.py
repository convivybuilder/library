"""bin/ci_result_fetch.py — the CI-result pull fetcher
(issue_01M0B97RC21TXM71CVKSZMFW99).

No network and no `gh` binary anywhere in this file. `gh_runs` and
`post_signal` are the two I/O seams (matching `convivy-orient/github_capture.py`'s
own split), and every test here either calls the pure transforms directly or
passes a fake through `capture_workflow`'s `fetcher`/`poster` parameters —
never a real subprocess or a real socket. `pytest-socket` would fail a real
one anyway (pytest-discipline skill), which is a second reason this is the
only shape a test here could take.

The four scenarios the coder brief asks for by name each get their own test,
named so a failure says which one broke:
  - test_the_cursor_advances_and_is_resumable
  - test_a_rerun_does_not_duplicate_rows
  - test_a_backfill_over_an_already_captured_window_is_idempotent
  - test_a_malformed_response_does_not_advance_the_cursor
"""
from __future__ import annotations

import datetime as real_datetime
import io
import json
import plistlib
import subprocess
import urllib.parse
from pathlib import Path

import ci_result_fetch as fetch
import projects
import pytest
import vectors


@pytest.fixture(autouse=True)
def _no_real_projects_registry(tmp_path, monkeypatch):
    """Every test in this file runs with `projects.REGISTRY_PATH` pointed at
    a path that does not exist, so `main()`'s untracked-repo check (which
    calls `deck_tracked_repos()`) never reads this machine's real
    `agent-platform/config/projects.json` — proven by running this whole
    suite with `AGENT_PLATFORM_PROJECTS=/nonexistent/projects.json
    HOME=$(mktemp -d)` (see the PR body). A test of `deck_tracked_repos`
    itself overrides `REGISTRY_PATH` again, to its own fixture file."""
    monkeypatch.setattr(projects, "REGISTRY_PATH", tmp_path / "does-not-exist.json")
    projects.reload()
    yield
    projects.reload()


# --- pure transforms ---------------------------------------------------------

def test_workflow_slug_strips_the_directory_and_extension():
    assert fetch.workflow_slug(".github/workflows/e2e-stage-smoke.yml") \
        == "e2e-stage-smoke"


def _run(run_id=32121060043, conclusion="success"):
    """One completed run, shaped exactly like `gh api
    repos/convivy/vivy/actions/runs/<id>` returns it — the real response this
    module was built against."""
    return {
        "id": run_id,
        "name": "E2E stage smoke",
        "path": ".github/workflows/e2e-stage-smoke.yml",
        "workflow_id": 310288665,
        "head_sha": "ecaa28f084f2ee9776dc97ba7e72f0dc5c696876",
        "conclusion": conclusion,
        "status": "completed",
        "run_number": 45,
        "event": "schedule",
        "created_at": "2026-08-18T09:20:15Z",
        "updated_at": "2026-08-18T09:22:22Z",
        "html_url": f"https://github.com/convivy/vivy/actions/runs/{run_id}",
    }


def test_signal_from_run_s_dedup_key_matches_the_retired_push_s_scheme():
    """**Load-bearing for the backfill.** The row this exact run (32121060043)
    already produced through the old push carries dedup_key
    `ci:vivy:e2e-stage-smoke:32121060043` (verified against the live Library
    database before this fetcher was written). A backfill starting at or
    before this run must reproduce the SAME key, or `POST /signals`'s dedup
    guard cannot recognise it as the row already captured and the backfill
    duplicates history rather than resuming it."""
    payload = fetch.signal_from_run(_run(), "vivy")
    assert payload["dedup_key"] == "ci:vivy:e2e-stage-smoke:32121060043"


def test_signal_from_run_carries_conclusion_workflow_commit_and_url_in_refs():
    payload = fetch.signal_from_run(_run(), "vivy")
    assert payload["source"] == "ci"
    assert payload["kind"] == "test-result"
    assert payload["product"] == "vivy"
    assert payload["refs"] == {
        "conclusion": "success",
        "workflow": "e2e-stage-smoke",
        "commit": "ecaa28f084f2ee9776dc97ba7e72f0dc5c696876",
        "run_url": "https://github.com/convivy/vivy/actions/runs/32121060043",
    }


# --- meta.status / meta.run_url: what Orient's /engineering/e2e actually reads ---

def test_conclusion_to_meta_status_maps_success_and_failure():
    assert fetch.conclusion_to_meta_status("success") == "pass"
    assert fetch.conclusion_to_meta_status("failure") == "fail"


def test_conclusion_to_meta_status_omits_unrecognised_values():
    """Fail closed: `cancelled` is the one non-success/failure value seen in
    the live 14 rows this fix backfills, and every other value GitHub's API
    can send (`skipped`, `timed_out`, `neutral`, `action_required`) plus a
    still-in-flight run's `None` must map the same way — to nothing, so the
    caller omits `meta.status` rather than guess."""
    for conclusion in ("cancelled", "skipped", "timed_out", "neutral",
                       "action_required", None, "some-future-value"):
        assert fetch.conclusion_to_meta_status(conclusion) is None, conclusion


def test_signal_from_run_populates_meta_status_and_run_url_for_a_success():
    payload = fetch.signal_from_run(_run(), "vivy")
    assert payload["meta"] == {
        "run_number": 45,
        "event": "schedule",
        "run_url": "https://github.com/convivy/vivy/actions/runs/32121060043",
        "status": "pass",
    }


def test_signal_from_run_omits_meta_status_for_an_unrecognised_conclusion_but_keeps_run_url():
    payload = fetch.signal_from_run(_run(conclusion="cancelled"), "vivy")
    assert "status" not in payload["meta"]
    assert payload["meta"]["run_url"] == \
        "https://github.com/convivy/vivy/actions/runs/32121060043"


def test_refs_conclusion_is_unchanged_by_the_meta_status_mapping():
    """`refs.conclusion` must still carry the raw GitHub value verbatim, even
    when `meta.status` is a mapped `pass`/`fail` or omitted entirely — it is
    the only place the un-mapped value survives."""
    payload = fetch.signal_from_run(_run(conclusion="cancelled"), "vivy")
    assert payload["refs"]["conclusion"] == "cancelled"


def test_signal_from_run_refuses_a_run_missing_a_dedup_dependent_field():
    for missing in ("id", "path", "created_at"):
        broken = _run()
        del broken[missing]
        try:
            fetch.signal_from_run(broken, "vivy")
            assert False, f"missing {missing} should have raised CaptureError"
        except fetch.CaptureError:
            pass


# --- TRACKED / deck_tracked_repos: the registry-derived repo set ------------
#
# Every test here reads a fixture `{slug: project}` dict passed directly to
# `deck_tracked_repos`, never this machine's real registry — the module-level
# `_no_real_projects_registry` fixture above covers `fetch.projects` itself
# for the `main()`-level tests further down.

def test_vivys_tracked_entry_names_its_pull_request_check_workflow():
    """**Fails on the pre-fix code.** `vivy`'s old TRACKED entry named
    `e2e-stage-smoke.yml` — a stage smoke workflow on a timer, not the
    workflow that runs pull-request checks (`ci.yml`, verified against
    GitHub in the PR body). This is the one entry the coder brief names by
    id, so it gets its own assertion rather than only showing up inside the
    full-TRACKED test below.

    `e2e-stage-smoke.yml` is tracked again beside it, as a second entry,
    because Orient's stage-smoke tile reads only its rows: replacing it
    rather than adding `ci.yml` alongside left that tile stale from
    2026-10-02."""
    vivy_workflows = sorted(
        e["workflow_file"] for e in fetch.TRACKED if e["repo"] == "convivy/vivy")
    assert vivy_workflows == ["ci.yml", "e2e-stage-smoke.yml"], (
        "convivy/vivy must track its pull-request check workflow (ci.yml) "
        "and the stage smoke workflow Orient's stage-smoke tile reads")


def test_deck_tracked_repos_includes_a_product_class_projects_repos():
    registry = {
        "constance": {"class": "product", "lifecycle": "active", "owner": "convivy",
                      "repos": ["constance", "constance-landing"]},
    }
    assert fetch.deck_tracked_repos(registry) == [
        {"repo": "convivy/constance", "product": "constance"},
        {"repo": "convivy/constance-landing", "product": "constance"},
    ]


def test_deck_tracked_repos_includes_a_non_product_class_project():
    """**Round 2, reviewer + security-reviewer finding.** Deck's own rule
    (`CockpitResolution.prLadderRepos`, reading `Crew.repos`) draws no
    distinction on this registry's `class` field — `class == "product"` is
    this registry's own grouping, not something Deck's panel reads at all.
    A `class == "project"` entry naming two repos (library's own shape:
    `library`, `knowledge`) must be included exactly like a product-class
    one would be.

    **Fails on the round-1 code**, which filtered to `class == "product"`
    and dropped this registry entry whole — confirmed red against the
    pre-fix `deck_tracked_repos` (see the PR body's Round 2 section)."""
    registry = {
        "library": {"class": "project", "lifecycle": "active", "owner": "convivy",
                    "repos": ["library", "knowledge"]},
    }
    assert fetch.deck_tracked_repos(registry) == [
        {"repo": "convivy/library", "product": "library"},
        {"repo": "convivy/knowledge", "product": "library"},
    ]


def test_deck_tracked_repos_draws_no_distinction_between_product_and_project_class():
    """**The strongest fixture-level stand-in for live drift CI can run**
    (round 2 item 4): a registry shaped like the real one, with one
    `class == "product"` crew and one `class == "project"` crew that each
    name two repos — the exact shape of entry the round-1 filter would have
    missed. `deck_tracked_repos` must draw the same two-repo set from both,
    since Deck's own rule never reads `class`.

    Live drift against the operator's real registry is NOT what this test
    (or any CI test) catches — see this module's own docstring and the PR
    body's Round 2 section: that is `main()`'s startup `untracked:` stderr
    warning's job, at run time, against a file no CI test may read."""
    registry = {
        "selector": {"class": "product", "lifecycle": "active", "owner": "convivy",
                     "repos": ["selector", "selector-backend"]},
        "library": {"class": "project", "lifecycle": "active", "owner": "convivy",
                    "repos": ["library", "knowledge"]},
    }
    assert fetch.deck_tracked_repos(registry) == [
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
    assert fetch.deck_tracked_repos(registry) == []


def test_deck_tracked_repos_excludes_a_product_class_project_naming_no_owner():
    """A project the registry cannot resolve a GitHub owner for cannot form
    a verifiable `owner/repo` identity — skipped rather than guessed."""
    registry = {
        "no-owner": {"class": "product", "lifecycle": "active", "repos": ["no-owner"]},
    }
    assert fetch.deck_tracked_repos(registry) == []


def test_deck_tracked_repos_skips_the_metadata_keys():
    registry = {
        "_meta": {"doc": "not a project"},
        "_products": {"vivy": {"display": "Vivy"}},
        "vivy": {"class": "product", "lifecycle": "active", "owner": "convivy",
                "repos": ["vivy"]},
    }
    assert fetch.deck_tracked_repos(registry) == [
        {"repo": "convivy/vivy", "product": "vivy"},
    ]


def test_deck_tracked_repos_reads_the_real_registry_module_by_default(monkeypatch, tmp_path):
    """No `registry` argument -> `projects.projects()`, proven against a
    fixture file via `projects.REGISTRY_PATH` rather than this machine's
    real one."""
    registry_file = tmp_path / "projects.json"
    registry_file.write_text(json.dumps({
        "hollis": {"class": "product", "lifecycle": "active", "owner": "convivy",
                   "repos": ["hollis"]},
    }))
    monkeypatch.setattr(projects, "REGISTRY_PATH", registry_file)
    projects.reload()
    assert fetch.deck_tracked_repos() == [{"repo": "convivy/hollis", "product": "hollis"}]


def test_main_warns_on_stderr_for_a_repo_deck_draws_with_no_tracked_entry(
        tmp_path, monkeypatch, capsys):
    """**The startup check the coder brief asks for by name.** A registry
    naming a product-class project TRACKED has no entry for must print an
    `untracked:` line to stderr, naming the repo and its product — loud
    rather than a silent gap the way `vivy`'s old entry was."""
    monkeypatch.setattr(fetch, "TRACKED", (
        {"repo": "convivy/vivy", "workflow_file": "ci.yml", "product": "vivy"},
    ))
    monkeypatch.setattr(fetch, "CURSOR_FILE", tmp_path / "cursor.json")
    monkeypatch.setattr(fetch, "TOKEN_FILE", tmp_path / "token")
    (tmp_path / "token").write_text("tok")
    monkeypatch.setattr(fetch, "gh_runs", fetcher_returning([]))
    monkeypatch.setattr(fetch, "post_signal", FakeServer().poster)
    monkeypatch.setattr("sys.argv", ["ci_result_fetch.py"])

    registry_file = tmp_path / "projects.json"
    registry_file.write_text(json.dumps({
        "vivy": {"class": "product", "lifecycle": "active", "owner": "convivy",
                 "repos": ["vivy"]},
        "hollis": {"class": "product", "lifecycle": "active", "owner": "convivy",
                   "repos": ["hollis"]},
    }))
    monkeypatch.setattr(projects, "REGISTRY_PATH", registry_file)
    projects.reload()

    monkeypatch.setattr(fetch, "STATUS_FILE", tmp_path / "status.json")
    monkeypatch.setattr(fetch, "_print_never_captured", lambda token: None)
    fetch.main()

    err = capsys.readouterr().err
    assert "untracked: convivy/hollis" in err
    assert "'hollis'" in err
    assert "untracked: convivy/vivy" not in err, (
        "a repo TRACKED already names must not be reported untracked")


def test_main_continues_when_the_startup_drift_check_itself_raises(
        tmp_path, monkeypatch, capsys):
    """**Round 2, security-reviewer finding.** A registry that parses as
    valid JSON but is shaped wrong (here: a top-level list rather than an
    object) makes `projects.projects()` raise `AttributeError` out of its
    own `.items()` call — uncaught inside `deck_tracked_repos()`, reached
    before `token`/`cursor` are loaded. That must cost this run one named
    warning line, not the whole capture: `main()` still loads the token,
    fetches, posts, and advances the cursor exactly as if the registry had
    been readable."""
    monkeypatch.setattr(fetch, "TRACKED", (
        {"repo": "convivy/vivy", "workflow_file": "ci.yml", "product": "vivy"},
    ))
    monkeypatch.setattr(fetch, "CURSOR_FILE", tmp_path / "cursor.json")
    monkeypatch.setattr(fetch, "TOKEN_FILE", tmp_path / "token")
    (tmp_path / "token").write_text("tok")
    monkeypatch.setattr(fetch, "gh_runs", fetcher_returning([_run(1)]))
    server = FakeServer()
    monkeypatch.setattr(fetch, "post_signal", server.poster)
    monkeypatch.setattr("sys.argv", ["ci_result_fetch.py"])

    registry_file = tmp_path / "projects.json"
    registry_file.write_text(json.dumps(["not", "an", "object"]))
    monkeypatch.setattr(projects, "REGISTRY_PATH", registry_file)
    projects.reload()

    monkeypatch.setattr(fetch, "STATUS_FILE", tmp_path / "status.json")
    monkeypatch.setattr(fetch, "_print_never_captured", lambda token: None)
    fetch.main()  # must not raise

    err = capsys.readouterr().err
    assert "deck_tracked_repos:" in err
    assert "AttributeError" in err
    assert len(server.posted) == 1, (
        "the capture itself must still run normally after the startup "
        "check's own failure")
    assert "convivy/vivy#ci.yml" in fetch.load_cursor(), (
        "the cursor must still advance — the malformed registry costs "
        "only the untracked-repo warning, not the run")


# --- capture_workflow: fetcher/poster fakes, no network ---------------------

class FakeServer:
    """A minimal stand-in for `POST /signals`'s idempotency behaviour: a
    dedup_key seen before answers 'duplicate', an unseen one 'created' and is
    remembered. `errors` names dedup_keys this fake refuses outright (a
    transient POST failure), so a test can prove `capture_workflow` counts a
    real post failure as an error without the cursor being told it succeeded.
    `conflicts` names dedup_keys that answer the permanent, no-leak-rule 409
    (`post_signal`'s `'conflict'`) rather than a transient failure.
    """
    def __init__(self, seed_dedup_keys=(), error_dedup_keys=(), conflict_dedup_keys=()):
        self.seen = set(seed_dedup_keys)
        self.errors = set(error_dedup_keys)
        self.conflicts = set(conflict_dedup_keys)
        self.posted = []

    def poster(self, payload, token):
        key = payload["dedup_key"]
        self.posted.append(key)
        if key in self.errors:
            return "error:500"
        if key in self.conflicts:
            return "conflict"
        if key in self.seen:
            return "duplicate"
        self.seen.add(key)
        return "created"


def fetcher_returning(runs):
    def _fetch(repo, workflow_file, since):
        return runs
    return _fetch


def fetcher_raising(message="simulated gh api failure"):
    def _fetch(repo, workflow_file, since):
        raise fetch.CaptureError(message)
    return _fetch


# --- the in-flight-run reproduction: a fake GitHub, not a fetcher stub ------
#
# Every fetcher fake above returns whatever a test tells it to, regardless of
# what was actually asked for — fine for capture_workflow's own posting/tally
# logic, but useless for proving the production bug, which lives in what
# gh_runs ASKS GitHub for. FakeGitHubActions instead parses the real query
# string gh_runs builds and answers it the way GitHub's API actually does
# (verified against the live API before writing this: no status filter means
# every status comes back; `created` supports `>=` as well as `>`). Only
# `subprocess.run` (the `gh` binary) is faked; gh_runs's own query
# construction and capture_workflow's own classification run for real.

class FakeGitHubActions:
    """`cap`, when given, reproduces GitHub's own `created`-filter cap
    (`GITHUB_CREATED_FILTER_CAP` in `ci_result_fetch.py`): any query whose
    filter matches more than `cap` runs is truncated to the newest `cap` of
    them, the same silent truncation the real API performs — `self.runs`
    must therefore be supplied newest-first, exactly as GitHub returns
    them, for that truncation to keep the right ones."""
    def __init__(self, runs, cap=None):
        self.runs = runs  # mutated externally between calls, like real runs completing
        self.cap = cap

    def run(self, cmd, capture_output, text, timeout):
        path = cmd[3]
        params = urllib.parse.parse_qs(urllib.parse.urlparse(f"http://x/{path}").query)
        status = params.get("status", [None])[0]
        created = params.get("created", [None])[0]
        matched = []
        for run in self.runs:
            if status is not None and run["status"] != status:
                continue
            if created is not None:
                run_dt = real_datetime.datetime.fromisoformat(
                    run["created_at"].replace("Z", "+00:00"))
                if ".." in created:
                    lo_s, hi_s = created.split("..", 1)
                    lo = real_datetime.datetime.fromisoformat(lo_s.replace("Z", "+00:00"))
                    hi = real_datetime.datetime.fromisoformat(hi_s.replace("Z", "+00:00"))
                    if not (lo <= run_dt <= hi):
                        continue
                elif created.startswith(">="):
                    since_dt = real_datetime.datetime.fromisoformat(
                        created[2:].replace("Z", "+00:00"))
                    if not (run_dt >= since_dt):
                        continue
                elif created.startswith(">"):
                    since_dt = real_datetime.datetime.fromisoformat(
                        created[1:].replace("Z", "+00:00"))
                    if not (run_dt > since_dt):
                        continue
                else:
                    raise AssertionError(f"unexpected created filter: {created!r}")
            matched.append(run)
        if self.cap is not None and len(matched) > self.cap:
            matched = matched[:self.cap]
        stdout = "\n".join(json.dumps(r) for r in matched)
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")


class _FrozenClock(real_datetime.datetime):
    """Subclasses the real `datetime.datetime` so everything else fetch.py
    does through `dt.datetime.*` (fromisoformat, strftime, arithmetic
    against a real timedelta) behaves exactly as in production; only
    `now()` is pinned, letting a test simulate a specific instant in each
    of several poll cycles without touching the real clock anywhere else in
    the process."""
    _frozen = None

    @classmethod
    def now(cls, tz=None):
        return cls._frozen


class _FrozenDt:
    """Stands in for `fetch.dt` (the whole `datetime` module as fetch.py
    sees it) — only `.datetime` is swapped for `_FrozenClock`; `.timedelta`
    and `.timezone` stay the real ones (e.g. the module-level `OVERLAP`
    constant, built at import time from the real module, keeps working
    unmodified against the frozen clock)."""
    datetime = _FrozenClock
    timedelta = real_datetime.timedelta
    timezone = real_datetime.timezone


def test_gh_runs_fetches_every_status_not_just_completed(monkeypatch):
    """**The root cause of the production data-loss bug.** `gh_runs` used
    to bake `status=completed` into the query, so a run still in flight
    when a poll fired could never be seen at all — not even to notice it
    existed and hold the cursor back for it. Fetching every status
    client-side is what lets `capture_workflow` tell "nothing happened
    yet" apart from "something is still running"."""
    captured = {}
    def fake_run(cmd, capture_output, text, timeout):
        captured["path"] = cmd[3]
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    monkeypatch.setattr(fetch.subprocess, "run", fake_run)
    fetch.gh_runs("convivy/vivy", "e2e-stage-smoke.yml", "2026-08-18T22:00:00Z")
    assert "status=" not in captured["path"], (
        "a status filter server-side hides in-flight runs from "
        "capture_workflow entirely, reintroducing the data-loss bug")
    assert "created=%3E%3D" in captured["path"], (
        "must use >= (not the strict > this endpoint used before): the "
        "cursor can be held to the EXACT created_at of a still-running "
        "run, and a strict > would then exclude that exact run on the "
        "very next poll — an off-by-one that would silently reintroduce "
        "the same class of loss")


def test_a_capped_query_is_split_so_every_run_in_the_window_is_still_stored(
        monkeypatch):
    """**The created-filter cap, reviewer round 1.** A no-cursor (or any)
    fetch whose window holds more runs than GitHub's own `created`-filter
    cap used to come back truncated to the newest `cap` of them, with
    `gh_runs` returning that truncated list as if it were the whole
    window — `next_since` would then advance past the OLDER runs the cap
    hid, permanently skipping them (`convivy/knowledge#ci.yml` in
    production: ~2,500 runs in 90 days against a 1,000-result cap). Here
    the fake caps every query at 10 results while 25 runs exist in the
    window: on the current (pre-fix) code this fetches only the newest 10
    and still reports the cursor safe to advance. The fix must split the
    window and recurse until every one of the 25 is actually returned and
    posted, and the resulting cursor must be the ordinary OVERLAP-based
    value — not an earlier point that still leaves some of the window
    uncovered."""
    base = real_datetime.datetime(2026, 9, 1, 0, 0, tzinfo=real_datetime.timezone.utc)
    # Newest-first, matching GitHub's own ordering and FakeGitHubActions's
    # truncate-to-the-newest-`cap` contract.
    runs = [
        {
            "id": 100 + i, "name": "CI", "path": ".github/workflows/ci.yml",
            "workflow_id": 1, "head_sha": "deadbeef", "conclusion": "success",
            "status": "completed", "run_number": i, "event": "push",
            "created_at": (base + real_datetime.timedelta(hours=24 - i)
                           ).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "updated_at": (base + real_datetime.timedelta(hours=24 - i)
                           ).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "html_url": f"https://github.com/convivy/knowledge/actions/runs/{100 + i}",
        }
        for i in range(25)
    ]
    gh = FakeGitHubActions(runs, cap=10)
    monkeypatch.setattr(fetch.subprocess, "run", gh.run)
    # The fake caps each query at 10, so the code must detect that same
    # threshold as GitHub's own cap to know a query was truncated — a
    # small N keeps the test fast without needing 1,001 fake runs to
    # exercise the real GITHUB_CREATED_FILTER_CAP.
    monkeypatch.setattr(fetch, "GITHUB_CREATED_FILTER_CAP", 10)
    server = FakeServer()
    run_started = base + real_datetime.timedelta(hours=25)

    tally = fetch.capture_workflow(
        "convivy/knowledge", "ci.yml", "library",
        "2026-08-01T00:00:00Z", "tok",
        poster=server.poster, run_started=run_started)

    assert len(server.seen) == 25, (
        "every run in the window must be stored, not just the newest "
        "10 the capped query returned")
    assert tally == {
        "created": 25, "duplicate": 0, "conflict": 0, "errors": 0,
        "stale_pending": 0, "runs_returned": 25, "reason": None,
        "signals_written": 25,
        "next_since": (run_started - fetch.OVERLAP).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
    }, (
        "with nothing in flight, the cursor must land on the ordinary "
        "OVERLAP-based watermark -- not an earlier point that would "
        "leave part of the window unfetched next poll")


def test_a_window_still_capped_under_one_second_fails_closed_without_storing_or_advancing(
        monkeypatch, capsys):
    """**The terminal fail-closed branch, reviewer round 2.** A query whose
    range has already been split down to under one second wide and STILL
    comes back at the cap cannot be split any further and cannot prove it
    covered its range — `_gh_runs_window` raises `CaptureError` rather than
    returning a window it cannot prove it got in full. Here the fake `gh`
    always reports exactly the cap, no matter how narrow the queried range
    is, forcing every split to keep splitting until the one-second floor is
    hit.

    Asserts on the specific wording of that raise's message, not just that
    some error was tallied: deleting the `raise` (restored after writing
    this test) does not make this pass by falling through cleanly -- it
    lets the recursion keep halving a timedelta that eventually stops
    changing, which re-calls `_gh_runs_window` with identical bounds forever
    and blows the stack. `_gh_runs_page`'s own generic `except Exception`
    catches that `RecursionError` and re-raises it as a DIFFERENT
    `CaptureError` (mentioning `RecursionError`, not the one-second-wide
    wording below), so a tally-shape-only assertion would pass either way
    and this test would never notice the branch was gone."""
    monkeypatch.setattr(fetch, "GITHUB_CREATED_FILTER_CAP", 3)
    monkeypatch.setattr(fetch, "dt", _FrozenDt)
    _FrozenClock._frozen = real_datetime.datetime(
        2026, 9, 1, 0, 0, 8, tzinfo=real_datetime.timezone.utc)

    def fake_run(cmd, capture_output, text, timeout):
        # Always reports exactly the cap regardless of the queried range --
        # the pathological response this branch exists to refuse rather
        # than silently trust.
        runs = [{"id": i} for i in range(3)]
        stdout = "\n".join(json.dumps(r) for r in runs)
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(fetch.subprocess, "run", fake_run)
    server = FakeServer()

    tally = fetch.capture_workflow(
        "convivy/knowledge", "ci.yml", "library",
        "2026-09-01T00:00:00Z", "tok", poster=server.poster,
        run_started=_FrozenClock._frozen)

    tally.pop("reason")  # the dedicated message below is checked via capsys
    assert tally == {
        "created": 0, "duplicate": 0, "conflict": 0, "errors": 1,
        "stale_pending": 0, "next_since": None, "runs_returned": None,
        "signals_written": 0,
    }, (
        "a window that still reports the cap once split below one second "
        "must fail this pair closed, the same as any other CaptureError")
    assert len(server.posted) == 0, (
        "nothing past the unfetched range may be stored -- a window this "
        "call refused to trust must not post anything from it")
    err = capsys.readouterr().err
    assert "under one second wide" in err and "cannot split it any further" in err, (
        "must fail via the dedicated terminal branch's own message, not "
        "via some other error (e.g. a RecursionError from an unbounded "
        "recursion) that happens to tally the same error count")


def test_an_in_flight_run_that_outlasts_the_overlap_is_captured_not_dropped(tmp_path, monkeypatch):
    """**Reproduces the exact production scenario from 2026-08-31.** A run
    created at 22:00 is still running when the 22:31 poll fires;
    `status=completed` correctly excludes it from that page. The OLD
    cursor logic then advanced the cursor to `fetch_start - OVERLAP`
    (22:26) from wall-clock time alone, with no regard for what was still
    in flight. The run finishes at 22:45. The next poll (22:46) asks for
    `created since 22:26` — the run, created at 22:00, is now permanently
    behind the cursor: silently lost, with a clean `errors: 0` tally.

    This drives the REAL `gh_runs` (its actual query string, via
    `FakeGitHubActions`) and the REAL `main()` cursor persistence across
    two simulated poll cycles — only `subprocess.run` and the wall clock
    are faked — so it fails on the current code for the actual reason
    production lost data, not because of an interface change.
    """
    monkeypatch.setattr(fetch, "TRACKED", (
        {"repo": "convivy/vivy", "workflow_file": "e2e-stage-smoke.yml",
         "product": "vivy"},
    ))
    monkeypatch.setattr(fetch, "CURSOR_FILE", tmp_path / "cursor.json")
    monkeypatch.setattr(fetch, "TOKEN_FILE", tmp_path / "token")
    (tmp_path / "token").write_text("tok")
    monkeypatch.setattr("sys.argv", ["ci_result_fetch.py"])

    run = {
        "id": 999999, "name": "E2E stage smoke",
        "path": ".github/workflows/e2e-stage-smoke.yml",
        "workflow_id": 310288665, "head_sha": "deadbeefdeadbeefdeadbeef",
        "conclusion": None, "status": "in_progress",
        "run_number": 99, "event": "schedule",
        "created_at": "2026-08-18T22:00:00Z",
        "updated_at": "2026-08-18T22:00:00Z",
        "html_url": "https://github.com/convivy/vivy/actions/runs/999999",
    }
    gh = FakeGitHubActions([run])
    monkeypatch.setattr(fetch.subprocess, "run", gh.run)

    server = FakeServer()
    monkeypatch.setattr(fetch, "post_signal", server.poster)
    monkeypatch.setattr(fetch, "dt", _FrozenDt)

    _FrozenClock._frozen = real_datetime.datetime(
        2026, 8, 18, 22, 31, tzinfo=real_datetime.timezone.utc)
    monkeypatch.setattr(fetch, "STATUS_FILE", tmp_path / "status.json")
    monkeypatch.setattr(fetch, "_print_never_captured", lambda token: None)
    fetch.main()  # the run is still in_progress: nothing to capture yet

    run["status"] = "completed"
    run["conclusion"] = "success"
    run["updated_at"] = "2026-08-18T22:45:30Z"

    _FrozenClock._frozen = real_datetime.datetime(
        2026, 8, 18, 22, 46, tzinfo=real_datetime.timezone.utc)
    fetch.main()  # the run finished 15 minutes ago; must be captured now

    dedup_key = fetch.signal_from_run(run, "vivy")["dedup_key"]
    assert dedup_key in server.seen, (
        "the run completed and should have been captured on the next "
        "poll after it finished — it must not be permanently behind "
        "the cursor")


def test_a_stale_pending_run_does_not_freeze_the_cursor_forever():
    """The opposite failure from the one this fetcher exists to fix, and
    just as bad: a run stuck `in_progress` well past anything this
    workflow has ever taken must not hold the cursor back forever — that
    would re-fetch a growing window every poll, permanently. Tallied
    `stale_pending` (loud, not silent) and excluded from the watermark, so
    the cursor still advances."""
    stuck = _run(1, conclusion=None)
    stuck["status"] = "in_progress"
    stuck["created_at"] = "2026-08-01T00:00:00Z"  # far older than the ceiling
    run_started = real_datetime.datetime(
        2026, 8, 18, 22, 31, tzinfo=real_datetime.timezone.utc)
    server = FakeServer()
    tally = fetch.capture_workflow(
        "convivy/vivy", "e2e-stage-smoke.yml", "vivy", None, "tok",
        fetcher=fetcher_returning([stuck]), poster=server.poster,
        run_started=run_started)
    assert tally["stale_pending"] == 1
    assert tally["created"] == 0
    assert tally["next_since"] == (
        run_started - fetch.OVERLAP).strftime("%Y-%m-%dT%H:%M:%SZ"), (
        "nothing legitimately in flight — the cursor must fall back to "
        "the ordinary overlap margin, not stay pinned to the stuck run")


def test_an_in_flight_run_under_the_staleness_ceiling_holds_the_cursor_back():
    """The direct unit-level counterpart to the full reproduction above:
    given one incomplete, non-stale run, `capture_workflow` must hold
    `next_since` to that run's own `created_at` rather than the ordinary
    `run_started - OVERLAP` fallback, whenever the run's creation time is
    the earlier of the two."""
    in_flight = _run(1, conclusion=None)
    in_flight["status"] = "in_progress"
    in_flight["created_at"] = "2026-08-18T22:00:00Z"
    run_started = real_datetime.datetime(
        2026, 8, 18, 22, 31, tzinfo=real_datetime.timezone.utc)
    server = FakeServer()
    tally = fetch.capture_workflow(
        "convivy/vivy", "e2e-stage-smoke.yml", "vivy", None, "tok",
        fetcher=fetcher_returning([in_flight]), poster=server.poster,
        run_started=run_started)
    assert tally["stale_pending"] == 0
    assert tally["created"] == 0
    assert tally["next_since"] == "2026-08-18T22:00:00Z", (
        "the cursor must be held back to the in-flight run's own "
        "created_at, not advanced to run_started - OVERLAP")


def test_a_self_hosted_style_entry_does_not_inherit_the_default_staleness_ceiling():
    """**Reviewer finding 1.** `STALE_PENDING_AFTER`'s 24h default is
    grounded in GitHub-hosted runners' 6-hour job cap — a cap that does not
    bind a self-hosted runner, where a job can sit legitimately queued far
    longer behind a serial runner backed up with other work. A run 30 hours
    old (past the 24h default, but well inside a wider self-hosted-style
    ceiling passed explicitly) must NOT be tallied stale_pending when this
    pair supplies its own `stale_pending_after` — proving the ceiling is
    scoped per entry rather than a single global that would silently apply
    the hosted-runner reasoning to it too."""
    in_flight = _run(1, conclusion=None)
    in_flight["status"] = "in_progress"
    in_flight["created_at"] = "2026-08-17T16:00:00Z"  # 30h before run_started
    run_started = real_datetime.datetime(
        2026, 8, 18, 22, 0, tzinfo=real_datetime.timezone.utc)
    server = FakeServer()
    tally = fetch.capture_workflow(
        "convivy/some-self-hosted-repo", "self-hosted-workflow.yml", "vivy",
        None, "tok", fetcher=fetcher_returning([in_flight]), poster=server.poster,
        run_started=run_started,
        stale_pending_after=real_datetime.timedelta(hours=48))
    assert tally["stale_pending"] == 0, (
        "a pair with its own wider stale_pending_after must not apply the "
        "24h GitHub-hosted-runner default to a run that is legitimately "
        "still queued under its own ceiling")
    assert tally["next_since"] == "2026-08-17T16:00:00Z", (
        "the cursor must still hold back to the in-flight run's own "
        "created_at rather than advance past it")

    # the same run, with no override, DOES trip the module default —
    # confirming the override is what changed the outcome above, not a
    # different bug making STALE_PENDING never fire at all.
    default_tally = fetch.capture_workflow(
        "convivy/some-self-hosted-repo", "self-hosted-workflow.yml", "vivy",
        None, "tok", fetcher=fetcher_returning([in_flight]), poster=server.poster,
        run_started=run_started)
    assert default_tally["stale_pending"] == 1


def test_a_present_but_unparseable_created_at_raises_capture_error_not_the_outer_handler():
    """**Reviewer finding 2.** `capture_workflow`'s docstring promises a run
    still incomplete with no PARSEABLE `created_at` is handled the same safe
    way as one with no `created_at` at all: the whole window aborts as a
    `CaptureError`, tallied as this pair's own error with a contextual log
    line naming the run. Before the fix, a present-but-malformed value
    reached `_parse_timestamp` unguarded and raised a bare `ValueError`,
    which is not a `CaptureError` and so escaped `capture_workflow` entirely
    rather than being tallied and logged the way the docstring says."""
    broken = _run(1, conclusion=None)
    broken["status"] = "in_progress"
    broken["created_at"] = "not-a-timestamp"
    server = FakeServer()
    tally = fetch.capture_workflow(
        "convivy/vivy", "e2e-stage-smoke.yml", "vivy", None, "tok",
        fetcher=fetcher_returning([broken]), poster=server.poster)
    assert tally["errors"] == 1, (
        "an unparseable created_at must be tallied as this pair's own "
        "error via CaptureError, not escape to main()'s outer handler")
    assert tally["next_since"] is None, (
        "a window that failed to parse must not advance the cursor"
    )
    assert server.posted == [], "nothing should have been posted from an aborted window"


def test_capture_workflow_posts_one_signal_per_run_and_tallies_created():
    runs = [_run(1), _run(2), _run(3)]
    server = FakeServer()
    tally = fetch.capture_workflow(
        "convivy/vivy", "e2e-stage-smoke.yml", "vivy", None, "tok",
        fetcher=fetcher_returning(runs), poster=server.poster)
    tally.pop("next_since")  # wall-clock dependent; the cursor tests cover it
    assert tally == {"created": 3, "duplicate": 0, "conflict": 0, "errors": 0,
                      "stale_pending": 0, "runs_returned": 3, "reason": None,
                      "signals_written": 3}
    assert len(server.seen) == 3


def test_a_rerun_does_not_duplicate_rows():
    """The Library's own dedup_key uniqueness is what this fetcher leans on
    rather than a second mechanism — simulated here by FakeServer answering
    'duplicate' the second time the same key arrives. A rerun over the exact
    same runs must therefore report zero NEW creates the second time."""
    runs = [_run(10), _run(11)]
    server = FakeServer()
    first = fetch.capture_workflow(
        "convivy/vivy", "e2e-stage-smoke.yml", "vivy", None, "tok",
        fetcher=fetcher_returning(runs), poster=server.poster)
    second = fetch.capture_workflow(
        "convivy/vivy", "e2e-stage-smoke.yml", "vivy", None, "tok",
        fetcher=fetcher_returning(runs), poster=server.poster)
    first.pop("next_since")
    second.pop("next_since")
    assert first == {"created": 2, "duplicate": 0, "conflict": 0, "errors": 0,
                      "stale_pending": 0, "runs_returned": 2, "reason": None,
                      "signals_written": 2}
    assert second == {"created": 0, "duplicate": 2, "conflict": 0, "errors": 0,
                       "stale_pending": 0, "runs_returned": 2, "reason": None,
                       "signals_written": 2}
    assert len(server.seen) == 2, "the rerun created rows the first pass already made"


def test_a_backfill_over_an_already_captured_window_is_idempotent():
    """Runs already captured (server already holds their dedup_key) mixed
    with genuinely new ones in the same backfill window — the shape a real
    --since backfill produces when it overlaps the last successful push.
    Running the SAME backfill window twice must settle at the same state
    both times, adding nothing on the second pass."""
    already_captured = fetch.signal_from_run(_run(32121060043), "vivy")["dedup_key"]
    runs = [_run(32121060043), _run(32208), _run(32209)]  # first already known
    server = FakeServer(seed_dedup_keys={already_captured})

    first = fetch.capture_workflow(
        "convivy/vivy", "e2e-stage-smoke.yml", "vivy",
        "2026-08-18T00:00:00Z", "tok",
        fetcher=fetcher_returning(runs), poster=server.poster)
    first.pop("next_since")
    assert first == {"created": 2, "duplicate": 1, "conflict": 0, "errors": 0,
                      "stale_pending": 0, "runs_returned": 3, "reason": None,
                      "signals_written": 3}

    second = fetch.capture_workflow(
        "convivy/vivy", "e2e-stage-smoke.yml", "vivy",
        "2026-08-18T00:00:00Z", "tok",
        fetcher=fetcher_returning(runs), poster=server.poster)
    second.pop("next_since")
    assert second == {"created": 0, "duplicate": 3, "conflict": 0, "errors": 0,
                       "stale_pending": 0, "runs_returned": 3, "reason": None,
                       "signals_written": 3}, (
        "re-running the same backfill window must not create anything new")


def test_a_gh_api_failure_produces_no_posts_and_an_error_tally():
    server = FakeServer()
    tally = fetch.capture_workflow(
        "convivy/vivy", "e2e-stage-smoke.yml", "vivy", None, "tok",
        fetcher=fetcher_raising(), poster=server.poster)
    assert tally == {"created": 0, "duplicate": 0, "conflict": 0, "errors": 1,
                      "stale_pending": 0, "next_since": None,
                      "runs_returned": None,
                      "reason": "simulated gh api failure",
                      "signals_written": 0}, (
        "a fetch failure returns before next_since is ever computed — the "
        "old cursor must be left exactly alone")
    assert server.posted == [], "nothing should have been posted from a failed fetch"


def test_a_malformed_run_aborts_the_whole_batch_before_any_post():
    """**The scenario that matters most.** Two good runs and one missing
    `id`, in one response. If the good two were posted before the bad one
    was noticed, a caller advancing the cursor on `errors == 0` would never
    see the failure — but here errors IS >0, and nothing must have been
    posted at all, good runs included, so a retry re-covers the whole
    window rather than silently keeping two of three."""
    good_a, good_b = _run(1), _run(2)
    broken = _run(3)
    del broken["id"]
    server = FakeServer()
    tally = fetch.capture_workflow(
        "convivy/vivy", "e2e-stage-smoke.yml", "vivy", None, "tok",
        fetcher=fetcher_returning([good_a, broken, good_b]), poster=server.poster)
    assert tally["errors"] == 1
    assert tally["created"] == 0
    assert server.posted == [], (
        "a malformed run must abort the batch before any of it is posted, "
        "not just skip the one bad entry")


def test_a_post_failure_is_counted_as_an_error_and_does_not_get_silently_marked_duplicate():
    run = _run(1)
    key = fetch.signal_from_run(run, "vivy")["dedup_key"]
    server = FakeServer(error_dedup_keys={key})
    tally = fetch.capture_workflow(
        "convivy/vivy", "e2e-stage-smoke.yml", "vivy", None, "tok",
        fetcher=fetcher_returning([run]), poster=server.poster)
    tally.pop("next_since")
    tally.pop("reason")  # pins the dedup_key, not worth asserting here
    assert tally == {"created": 0, "duplicate": 0, "conflict": 0, "errors": 1,
                      "stale_pending": 0, "runs_returned": 1,
                      "signals_written": 0}


def test_a_permanent_conflict_is_tallied_separately_from_errors_and_does_not_block_the_pair():
    """A 409 (`post_signal`'s `'conflict'`) is the Library's no-leak-rule
    answer to a `dedup_key` collision this token cannot read — a token whose
    scope narrowed, or a pre-existing customer-class row — and will never
    clear on retry. `capture_workflow` must tally it apart from `errors`
    (reviewer finding 1): folded into `errors`, `main()` would leave this
    pair's cursor stuck re-fetching and re-hitting the same immovable 409
    forever, indistinguishable in the tally from an ordinary transient
    failure."""
    good = _run(1)
    stuck = _run(2)
    stuck_key = fetch.signal_from_run(stuck, "vivy")["dedup_key"]
    server = FakeServer(conflict_dedup_keys={stuck_key})
    tally = fetch.capture_workflow(
        "convivy/vivy", "e2e-stage-smoke.yml", "vivy", None, "tok",
        fetcher=fetcher_returning([good, stuck]), poster=server.poster)
    tally.pop("next_since")
    assert tally == {"created": 1, "duplicate": 0, "conflict": 1, "errors": 0,
                      "stale_pending": 0, "runs_returned": 2, "reason": None,
                      "signals_written": 1}, (
        "a conflict must not be counted as an error — errors == 0 is what "
        "main() reads to decide whether this pair's cursor may advance")


# --- post_signal: real urllib/HTTPError semantics, no FakeServer ------------
#
# Every other test in this file replaces post_signal with FakeServer, which
# returns pre-baked strings rather than exercising the real status-code
# branching post_signal itself does. This function decides whether a run's
# data was actually written, so it gets tested against the real urllib
# surface directly (reviewer finding 2) — no network, `urllib.request.urlopen`
# is monkeypatched, but the real HTTPError/status-code paths run.

class _FakeResponse:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def test_post_signal_maps_201_to_created(monkeypatch):
    monkeypatch.setattr(fetch.urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeResponse(201))
    assert fetch.post_signal({"dedup_key": "k"}, "tok") == "created"


def test_post_signal_maps_200_to_duplicate(monkeypatch):
    monkeypatch.setattr(fetch.urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeResponse(200))
    assert fetch.post_signal({"dedup_key": "k"}, "tok") == "duplicate"


def test_post_signal_maps_a_409_httperror_to_conflict_not_duplicate_or_error(monkeypatch):
    """The no-leak rule's 409 arrives as `urllib.error.HTTPError` (urlopen
    raises for any non-2xx/3xx status), never as a plain response object —
    the real behaviour a FakeServer-based test cannot exercise."""
    def raise_409(req, timeout=None):
        raise fetch.urllib.error.HTTPError(
            "http://x/signals", 409, "Conflict", {}, None)
    monkeypatch.setattr(fetch.urllib.request, "urlopen", raise_409)
    assert fetch.post_signal({"dedup_key": "k"}, "tok") == "conflict"


def test_post_signal_maps_a_500_httperror_to_error_with_the_code(monkeypatch):
    def raise_500(req, timeout=None):
        raise fetch.urllib.error.HTTPError(
            "http://x/signals", 500, "Internal Server Error", {}, None)
    monkeypatch.setattr(fetch.urllib.request, "urlopen", raise_500)
    assert fetch.post_signal({"dedup_key": "k"}, "tok") == "error:500"


def test_post_signal_maps_a_timeout_to_an_error_tag(monkeypatch):
    def raise_timeout(req, timeout=None):
        raise TimeoutError("timed out")
    monkeypatch.setattr(fetch.urllib.request, "urlopen", raise_timeout)
    assert fetch.post_signal({"dedup_key": "k"}, "tok") == "error:TimeoutError"


def test_post_signal_raising_httperror_is_never_mapped_to_duplicate(monkeypatch):
    """A regression that mapped a raised HTTPError to 'duplicate' instead of
    'error:*'/'conflict' would tell main() a run was captured when it never
    reached the Library at all — pinned directly rather than only implied by
    the 409/500 cases above."""
    def raise_503(req, timeout=None):
        raise fetch.urllib.error.HTTPError(
            "http://x/signals", 503, "Service Unavailable", {}, None)
    monkeypatch.setattr(fetch.urllib.request, "urlopen", raise_503)
    result = fetch.post_signal({"dedup_key": "k"}, "tok")
    assert result not in ("created", "duplicate")
    assert result == "error:503"


# --- post_signal: the 429 retry (the livelock fix, 2026-10-01 18:15-18:30Z) -
#
# Every test below drives `post_signal`'s real retry loop against a faked
# `urllib.request.urlopen` and a faked `time.sleep` — never a real socket,
# never a real sleep. `_http_429` builds the exact shape `app.py`'s rate
# gate sends: a JSON body with `retry_after` and/or an identically-valued
# `Retry-After` header, matching how `refusals.refuse` and the route set
# both (app.py lines ~8649-8659).

def _http_429(retry_after=None, header_retry_after=None):
    body = {"error": "rate limit exceeded"}
    if retry_after is not None:
        body["retry_after"] = retry_after
    headers = {}
    if header_retry_after is not None:
        headers["Retry-After"] = str(header_retry_after)
    return fetch.urllib.error.HTTPError(
        "http://x/signals", 429, "Too Many Requests", headers,
        io.BytesIO(json.dumps(body).encode("utf-8")))


# --- _retry_after_seconds: the "never raises" contract, security-reviewer --
# round 1. `int(float("inf"))` raises `OverflowError`, which the function's
# own `except` clauses used to not catch — a hostile or broken `Retry-After`
# escaped it uncaught, abandoning the rest of the pair's batch rather than
# falling back to RATE_LIMIT_FALLBACK_RETRY_SECONDS the way every other
# malformed 429 is documented to behave.

@pytest.mark.parametrize("bad_value", ["inf", "nan", "-5", "abc"])
def test_retry_after_seconds_falls_back_for_a_non_finite_negative_or_unparseable_header(
        bad_value):
    e = _http_429(header_retry_after=bad_value)
    assert fetch._retry_after_seconds(e) == fetch.RATE_LIMIT_FALLBACK_RETRY_SECONDS


def test_retry_after_seconds_falls_back_when_the_bodys_value_is_non_finite():
    """The body-field path hits the same `int()`-on-a-non-finite-float
    overflow for a 429 shaped like `{"retry_after": Infinity}` (a value
    `json.loads` happily round-trips) — covered by the same fix, not just
    the header path."""
    e = _http_429(retry_after=float("inf"))
    assert fetch._retry_after_seconds(e) == fetch.RATE_LIMIT_FALLBACK_RETRY_SECONDS


def test_post_signal_retries_a_429_sleeping_what_the_body_said_and_then_succeeds(
        monkeypatch):
    """**The livelock fix, at post_signal's own level.** A 429 naming
    `retry_after: 7` in its body must be slept for exactly that long — via
    the injected fake, never a real sleep — and the same payload re-posted;
    succeeding on the second attempt counts as 'created', not folded into
    an error the way every 429 used to be."""
    calls = {"n": 0}
    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_429(retry_after=7)
        return _FakeResponse(201)
    monkeypatch.setattr(fetch.urllib.request, "urlopen", fake_urlopen)
    slept = []
    monkeypatch.setattr(fetch.time, "sleep", lambda s: slept.append(s))

    assert fetch.post_signal({"dedup_key": "k"}, "tok") == "created"
    assert slept == [7], "must sleep exactly what the 429's body said"
    assert calls["n"] == 2, "must re-post the same payload after the wait"


def test_post_signal_falls_back_to_the_retry_after_header_when_the_body_has_none(
        monkeypatch):
    """`retry_after` missing from the body (a 429 from something other than
    this exact rate gate) must still be retried, using the `Retry-After`
    header instead — 'use what is actually there' rather than giving up for
    want of one specific field."""
    calls = {"n": 0}
    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_429(header_retry_after=5)
        return _FakeResponse(201)
    monkeypatch.setattr(fetch.urllib.request, "urlopen", fake_urlopen)
    slept = []
    monkeypatch.setattr(fetch.time, "sleep", lambda s: slept.append(s))

    assert fetch.post_signal({"dedup_key": "k"}, "tok") == "created"
    assert slept == [5]


def test_post_signal_clamps_a_single_wait_to_the_max_even_if_the_body_claims_longer(
        monkeypatch):
    """A broken server naming an absurd `retry_after` must not make this
    fetcher sleep that long — `RATE_LIMIT_MAX_WAIT_SECONDS` clamps one
    wait regardless of what the response claims."""
    calls = {"n": 0}
    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_429(retry_after=10_000)
        return _FakeResponse(201)
    monkeypatch.setattr(fetch.urllib.request, "urlopen", fake_urlopen)
    slept = []
    monkeypatch.setattr(fetch.time, "sleep", lambda s: slept.append(s))

    assert fetch.post_signal({"dedup_key": "k"}, "tok") == "created"
    assert slept == [fetch.RATE_LIMIT_MAX_WAIT_SECONDS]


def test_post_signal_gives_up_after_the_retry_bound_and_reports_error_429(
        monkeypatch):
    """**The bound against a broken server that never clears.** A 429 on
    every attempt must not hang this process forever: after
    `RATE_LIMIT_MAX_RETRIES` retries, `post_signal` gives up and reports
    `'error:429'` — tallied by `capture_workflow` exactly like any other
    post failure."""
    calls = {"n": 0}
    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        raise _http_429(retry_after=1)
    monkeypatch.setattr(fetch.urllib.request, "urlopen", fake_urlopen)
    slept = []
    monkeypatch.setattr(fetch.time, "sleep", lambda s: slept.append(s))

    assert fetch.post_signal({"dedup_key": "k"}, "tok") == "error:429"
    assert calls["n"] == fetch.RATE_LIMIT_MAX_RETRIES + 1, (
        "one initial attempt plus RATE_LIMIT_MAX_RETRIES retries, then give up")
    assert len(slept) == fetch.RATE_LIMIT_MAX_RETRIES


def test_a_persistent_429_is_tallied_as_an_error_and_does_not_block_a_post_at_the_capture_workflow_level():
    """The direct `capture_workflow`-level counterpart: a signal that never
    clears its rate limit is tallied exactly like any other post
    failure — nothing marked created or duplicate. `next_since` is still
    computed the same way it is after an ordinary post failure (see
    `test_a_post_failure_is_counted_as_an_error_and_does_not_get_silently_marked_duplicate`
    just above); it is `main()`'s own `errors == 0` check, exercised by the
    end-to-end test below, that is what actually withholds the cursor."""
    run = _run(1)
    key = fetch.signal_from_run(run, "vivy")["dedup_key"]
    server = FakeServer(error_dedup_keys={key})  # stands in for an exhausted 429 retry
    tally = fetch.capture_workflow(
        "convivy/vivy", "e2e-stage-smoke.yml", "vivy", None, "tok",
        fetcher=fetcher_returning([run]), poster=server.poster)
    tally.pop("next_since")
    tally.pop("reason")  # pins the dedup_key, not worth asserting here
    assert tally == {"created": 0, "duplicate": 0, "conflict": 0, "errors": 1,
                      "stale_pending": 0, "runs_returned": 1,
                      "signals_written": 0}


def test_a_persistent_429_through_main_leaves_the_pair_s_cursor_exactly_where_it_was(
        tmp_path, monkeypatch):
    """**The end-to-end shape of the livelock fix.** A run whose every post
    attempt 429s and never clears must not advance this pair's cursor:
    `main()` only writes a new cursor when `errors == 0` (see
    `test_a_malformed_response_does_not_advance_the_cursor`'s own pattern),
    and a signal still 429'd past `RATE_LIMIT_MAX_RETRIES` is exactly that —
    an error, same as any other post failure."""
    monkeypatch.setattr(fetch, "TRACKED", (
        {"repo": "convivy/vivy", "workflow_file": "e2e-stage-smoke.yml",
         "product": "vivy"},
    ))
    cursor_file = tmp_path / "cursor.json"
    key = "convivy/vivy#e2e-stage-smoke.yml"
    prior_value = "2026-08-20T00:00:00Z"
    cursor_file.write_text(json.dumps({key: prior_value}))
    monkeypatch.setattr(fetch, "CURSOR_FILE", cursor_file)
    monkeypatch.setattr(fetch, "TOKEN_FILE", tmp_path / "token")
    (tmp_path / "token").write_text("tok")
    monkeypatch.setattr(fetch, "gh_runs", fetcher_returning([_run(1)]))
    monkeypatch.setattr("sys.argv", ["ci_result_fetch.py"])

    def fake_urlopen(req, timeout=None):
        raise _http_429(retry_after=0)
    monkeypatch.setattr(fetch.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(fetch.time, "sleep", lambda s: None)

    try:
        monkeypatch.setattr(fetch, "STATUS_FILE", tmp_path / "status.json")
        monkeypatch.setattr(fetch, "_print_never_captured", lambda token: None)
        fetch.main()
        assert False, "a run with errors should exit non-zero"
    except SystemExit as e:
        assert e.code == 1

    assert fetch.load_cursor()[key] == prior_value, (
        "a pair whose every signal 429'd past the retry bound must not "
        "have its cursor advanced")


def test_a_sustained_429_stops_the_pair_s_batch_instead_of_retrying_each_remaining_signal(
        monkeypatch):
    """**The run-level bound, reviewer/security-reviewer round 1.**
    `post_signal`'s retry bound is per POST, not per pair — the first
    test above proves ONE signal gives up after `RATE_LIMIT_MAX_RETRIES`.
    A pair whose batch holds several runs behind a sustained 429 (a stuck
    rate limiter, not an ordinary volume spike) must not spend that same
    ~RATE_LIMIT_MAX_RETRIES-deep bound on EACH remaining signal too —
    this drives three runs through the real `post_signal` (its own
    retry loop, with `time.sleep` faked) and proves `urlopen` is called
    only enough times to exhaust the FIRST payload's bound, never
    multiplied by the batch size."""
    runs = [_run(1), _run(2), _run(3)]
    calls = {"n": 0}
    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        raise _http_429(retry_after=0)
    monkeypatch.setattr(fetch.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(fetch.time, "sleep", lambda s: None)

    tally = fetch.capture_workflow(
        "convivy/vivy", "e2e-stage-smoke.yml", "vivy", None, "tok",
        fetcher=fetcher_returning(runs))

    assert calls["n"] == fetch.RATE_LIMIT_MAX_RETRIES + 1, (
        "one initial attempt plus RATE_LIMIT_MAX_RETRIES retries for the "
        "FIRST payload only — the other two runs must not each spend "
        "their own full bound behind a sustained 429")
    assert tally["created"] == 0
    assert tally["errors"] == 1, (
        "only the one exhausted signal is tallied — the two left unposted "
        "this run are not separately counted as errors; the pair's "
        "cursor is already withheld by the one"
    )


# --- cursor / token file I/O --------------------------------------------------

def test_cursor_round_trips_through_the_state_file(tmp_path, monkeypatch):
    cursor_file = tmp_path / "ci-fetch-cursor.json"
    monkeypatch.setattr(fetch, "CURSOR_FILE", cursor_file)
    assert fetch.load_cursor() == {}, "a missing cursor file reads as empty, not an error"
    fetch.save_cursor({"convivy/vivy#e2e-stage-smoke.yml": "2026-08-31T00:00:00Z"})
    assert fetch.load_cursor() == {"convivy/vivy#e2e-stage-smoke.yml": "2026-08-31T00:00:00Z"}


def test_save_cursor_writes_via_a_temp_file_and_replace_not_a_direct_truncate(tmp_path, monkeypatch):
    """`save_cursor` must never leave `CURSOR_FILE` visibly truncated: a
    temp-file-plus-`os.replace` means an observer only ever sees the OLD
    complete content or the NEW complete content, never a partial write
    (reviewer suggestion). Proven by spying on `os.replace` itself — the
    argument it's called with must be a temp path distinct from and already
    fully written before `CURSOR_FILE` is touched, and no temp file may
    survive a successful save."""
    cursor_file = tmp_path / "ci-fetch-cursor.json"
    monkeypatch.setattr(fetch, "CURSOR_FILE", cursor_file)
    fetch.save_cursor({"a": "1"})
    assert cursor_file.exists()
    assert list(tmp_path.glob("*.tmp")) == [], (
        "a successful save must not leave its temp file behind")
    assert json.loads(cursor_file.read_text()) == {"a": "1"}

    real_replace = fetch.os.replace
    replaced = []
    def spying_replace(src, dst):
        # the temp file must be fully written and distinct from the final
        # path at the moment of replace — proof this isn't write_text direct
        # to CURSOR_FILE
        assert Path(src).exists()
        assert Path(src) != Path(dst)
        replaced.append((src, dst))
        return real_replace(src, dst)
    monkeypatch.setattr(fetch.os, "replace", spying_replace)
    fetch.save_cursor({"a": "2"})
    assert len(replaced) == 1
    assert json.loads(cursor_file.read_text()) == {"a": "2"}


def test_save_cursor_cleans_up_its_temp_file_when_os_replace_raises(tmp_path, monkeypatch):
    """A `PermissionError` (or any `OSError`) out of `os.replace` must not
    leave the temp file behind under `~/.library`, and must not touch the
    existing `CURSOR_FILE` (reviewer round-2 finding 1)."""
    cursor_file = tmp_path / "ci-fetch-cursor.json"
    monkeypatch.setattr(fetch, "CURSOR_FILE", cursor_file)
    fetch.save_cursor({"a": "1"})
    assert json.loads(cursor_file.read_text()) == {"a": "1"}

    def raising_replace(src, dst):
        raise PermissionError("denied")
    monkeypatch.setattr(fetch.os, "replace", raising_replace)

    with pytest.raises(PermissionError):
        fetch.save_cursor({"a": "2"})

    assert list(tmp_path.glob("*.tmp")) == [], (
        "a failed replace must not leave its temp file behind")
    assert json.loads(cursor_file.read_text()) == {"a": "1"}, (
        "a failed replace must leave the existing cursor file untouched")


# --- status file I/O --------------------------------------------------------

def test_status_round_trips_through_the_state_file(tmp_path, monkeypatch):
    status_file = tmp_path / "ci-fetch-status.json"
    monkeypatch.setattr(fetch, "STATUS_FILE", status_file)
    assert fetch.load_status() is None, (
        "a missing status file reads as no run to report on, not an error")
    fetch.save_status({"run_started": "2026-10-01T00:00:00Z",
                       "run_finished": "2026-10-01T00:00:05Z", "pairs": {}})
    assert fetch.load_status() == {
        "run_started": "2026-10-01T00:00:00Z",
        "run_finished": "2026-10-01T00:00:05Z", "pairs": {}}


def test_load_status_reads_an_unparseable_file_the_same_as_a_missing_one(
        tmp_path, monkeypatch):
    status_file = tmp_path / "ci-fetch-status.json"
    status_file.write_text("{not json")
    monkeypatch.setattr(fetch, "STATUS_FILE", status_file)
    assert fetch.load_status() is None


def test_save_status_writes_via_a_temp_file_and_replace_not_a_direct_truncate(
        tmp_path, monkeypatch):
    """Mirrors `test_save_cursor_writes_via_a_temp_file_and_replace_not_a_direct_truncate`
    for the status file: an observer must only ever see the OLD complete
    content or the NEW complete content, never a partial write."""
    status_file = tmp_path / "ci-fetch-status.json"
    monkeypatch.setattr(fetch, "STATUS_FILE", status_file)
    fetch.save_status({"a": 1})
    assert status_file.exists()
    assert list(tmp_path.glob("*.tmp")) == [], (
        "a successful save must not leave its temp file behind")
    assert json.loads(status_file.read_text()) == {"a": 1}

    real_replace = fetch.os.replace
    replaced = []
    def spying_replace(src, dst):
        assert Path(src).exists()
        assert Path(src) != Path(dst)
        replaced.append((src, dst))
        return real_replace(src, dst)
    monkeypatch.setattr(fetch.os, "replace", spying_replace)
    fetch.save_status({"a": 2})
    assert len(replaced) == 1
    assert json.loads(status_file.read_text()) == {"a": 2}


def test_save_status_cleans_up_its_temp_file_when_os_replace_raises(
        tmp_path, monkeypatch):
    """An interrupted write (`os.replace` raising) must leave the previous
    status file intact and must not leave its temp file behind — mirrors
    `test_save_cursor_cleans_up_its_temp_file_when_os_replace_raises`."""
    status_file = tmp_path / "ci-fetch-status.json"
    monkeypatch.setattr(fetch, "STATUS_FILE", status_file)
    fetch.save_status({"a": 1})
    assert json.loads(status_file.read_text()) == {"a": 1}

    def raising_replace(src, dst):
        raise PermissionError("denied")
    monkeypatch.setattr(fetch.os, "replace", raising_replace)

    with pytest.raises(PermissionError):
        fetch.save_status({"a": 2})

    assert list(tmp_path.glob("*.tmp")) == [], (
        "a failed replace must not leave its temp file behind")
    assert json.loads(status_file.read_text()) == {"a": 1}, (
        "a failed replace must leave the existing status file untouched")


def test_main_writes_a_status_entry_per_pair_and_reports_their_outcome(
        tmp_path, monkeypatch):
    """One good pair and one that raises `CaptureError` — both must be
    recorded in the status file `main()` writes, the good one `ok` and the
    broken one `error` with its reason, proving one pair's failure does not
    cost the other its entry."""
    monkeypatch.setattr(fetch, "TRACKED", (
        {"repo": "convivy/vivy", "workflow_file": "e2e-stage-smoke.yml", "product": "vivy"},
        {"repo": "convivy/vivy", "workflow_file": "ci.yml", "product": "vivy"},
    ))
    monkeypatch.setattr(fetch, "CURSOR_FILE", tmp_path / "cursor.json")
    monkeypatch.setattr(fetch, "TOKEN_FILE", tmp_path / "token")
    (tmp_path / "token").write_text("tok")
    monkeypatch.setattr(fetch, "OVERLAP", __import__("datetime").timedelta(0))

    def fake_gh_runs(repo, workflow_file, since):
        if workflow_file == "ci.yml":
            raise fetch.CaptureError("simulated failure for ci.yml only")
        return [_run(1)]
    monkeypatch.setattr(fetch, "gh_runs", fake_gh_runs)
    monkeypatch.setattr(fetch, "post_signal", FakeServer().poster)
    monkeypatch.setattr("sys.argv", ["ci_result_fetch.py"])
    monkeypatch.setattr(fetch, "STATUS_FILE", tmp_path / "status.json")
    monkeypatch.setattr(fetch, "_print_never_captured", lambda token: None)

    try:
        fetch.main()
    except SystemExit as e:
        assert e.code == 1

    status = fetch.load_status()
    assert status is not None
    assert "run_started" in status and "run_finished" in status
    good = status["pairs"]["convivy/vivy#e2e-stage-smoke.yml"]
    assert good["outcome"] == "ok"
    assert good["reason"] is None
    assert good["runs_returned"] == 1
    assert good["signals_written"] == 1
    broken = status["pairs"]["convivy/vivy#ci.yml"]
    assert broken["outcome"] == "error"
    assert broken["reason"] == "simulated failure for ci.yml only"
    assert broken["runs_returned"] is None


def test_print_never_captured_names_every_repo_the_route_reports(monkeypatch):
    class _FakeStatusResponse:
        def __init__(self, body):
            self._body = json.dumps(body).encode("utf-8")
        def __enter__(self):
            return self
        def __exit__(self, *exc_info):
            return False
        def read(self):
            return self._body
    monkeypatch.setattr(
        fetch.urllib.request, "urlopen",
        lambda req, timeout=None: _FakeStatusResponse(
            {"never_captured": [{"repo": "convivy/hollis"},
                                {"repo": "convivy/placard"}]}))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    fetch._print_never_captured("tok")
    assert out.getvalue().strip() == "never_captured: convivy/hollis, convivy/placard"


def test_print_never_captured_never_raises_on_a_transport_failure(monkeypatch, capsys):
    def raise_timeout(req, timeout=None):
        raise TimeoutError("timed out")
    monkeypatch.setattr(fetch.urllib.request, "urlopen", raise_timeout)
    fetch._print_never_captured("tok")  # must not raise
    assert "never_captured: could not be read" in capsys.readouterr().err


def test_load_token_prefers_the_env_var_over_the_file(tmp_path, monkeypatch):
    token_file = tmp_path / "ci-fetch-token"
    token_file.write_text("file-token\n")
    monkeypatch.setattr(fetch, "TOKEN_FILE", token_file)
    monkeypatch.setenv("LIBRARY_CI_FETCH_TOKEN", "env-token")
    assert fetch.load_token() == "env-token"


def test_load_token_falls_back_to_the_state_file(tmp_path, monkeypatch):
    token_file = tmp_path / "ci-fetch-token"
    token_file.write_text("file-token\n")
    monkeypatch.setattr(fetch, "TOKEN_FILE", token_file)
    monkeypatch.delenv("LIBRARY_CI_FETCH_TOKEN", raising=False)
    assert fetch.load_token() == "file-token"


def test_load_token_exits_when_neither_is_set(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch, "TOKEN_FILE", tmp_path / "does-not-exist")
    monkeypatch.delenv("LIBRARY_CI_FETCH_TOKEN", raising=False)
    try:
        fetch.load_token()
        assert False, "should have exited"
    except SystemExit:
        pass


# --- main(): cursor orchestration end to end, one tracked pair at a time ----

def test_the_cursor_advances_and_is_resumable(tmp_path, monkeypatch):
    """`main()` end to end against fakes: a successful run writes a cursor
    forward, and the NEXT run reads that exact value back as `since` — the
    resumability the coder brief names explicitly."""
    monkeypatch.setattr(fetch, "TRACKED",
                        ({"repo": "convivy/vivy",
                          "workflow_file": "e2e-stage-smoke.yml",
                          "product": "vivy"},))
    monkeypatch.setattr(fetch, "CURSOR_FILE", tmp_path / "cursor.json")
    monkeypatch.setattr(fetch, "TOKEN_FILE", tmp_path / "token")
    (tmp_path / "token").write_text("tok")
    monkeypatch.setattr(fetch, "OVERLAP", __import__("datetime").timedelta(0))

    seen_since = []

    def fake_gh_runs(repo, workflow_file, since):
        seen_since.append(since)
        return [_run(1)]

    server = FakeServer()
    monkeypatch.setattr(fetch, "gh_runs", fake_gh_runs)
    monkeypatch.setattr(fetch, "post_signal", server.poster)
    monkeypatch.setattr("sys.argv", ["ci_result_fetch.py"])

    monkeypatch.setattr(fetch, "STATUS_FILE", tmp_path / "status.json")
    monkeypatch.setattr(fetch, "_print_never_captured", lambda token: None)
    fetch.main()
    cursor_after_first = fetch.load_cursor()
    key = "convivy/vivy#e2e-stage-smoke.yml"
    assert key in cursor_after_first
    assert seen_since[0] is not None, (
        "the first run has no cursor to resume from, so it must fall back "
        "to the no-cursor window rather than ask for full history")

    fetch.main()
    assert seen_since[1] == cursor_after_first[key], (
        "the second run must resume from exactly what the first run wrote")


def test_no_cursor_window_days_matches_the_vectors_hot_window_for_test_result():
    """`NO_CURSOR_WINDOW_DAYS` is copied from
    `vectors.HOT_WINDOW_DAYS["test-result"]` rather than imported (see the
    constant's own comment: `vectors.py` resolves `LIBRARY_VECTORS_DB` at
    import, which this job's plist does not set) — pinned equal here so the
    two cannot drift silently."""
    assert fetch.NO_CURSOR_WINDOW_DAYS == vectors.HOT_WINDOW_DAYS["test-result"]


def test_a_pair_with_no_cursor_requests_a_bounded_window_not_full_history(
        tmp_path, monkeypatch):
    """**The production bug this fixes, 2026-10-01.** A pair with no stored
    cursor used to call `gh_runs` with `since=None`, walking a workflow's
    entire run history under `subprocess.run`'s 120s budget — the first
    scheduled run after a tracked-repo widening added
    `convivy/vivy#ci.yml` and `convivy/knowledge#ci.yml` with no cursor
    entry timed out on both, every poll, forever (a failed pair saves no
    cursor). `since` must now be bounded to `NO_CURSOR_WINDOW_DAYS` before
    this run's own "now" rather than left `None`."""
    monkeypatch.setattr(fetch, "TRACKED",
                        ({"repo": "convivy/vivy", "workflow_file": "ci.yml",
                          "product": "vivy"},))
    monkeypatch.setattr(fetch, "CURSOR_FILE", tmp_path / "cursor.json")
    monkeypatch.setattr(fetch, "TOKEN_FILE", tmp_path / "token")
    (tmp_path / "token").write_text("tok")
    monkeypatch.setattr(fetch, "dt", _FrozenDt)
    _FrozenClock._frozen = real_datetime.datetime(
        2026, 10, 1, 18, 0, tzinfo=real_datetime.timezone.utc)

    seen_since = []

    def fake_gh_runs(repo, workflow_file, since):
        seen_since.append(since)
        return []

    monkeypatch.setattr(fetch, "gh_runs", fake_gh_runs)
    monkeypatch.setattr(fetch, "post_signal", FakeServer().poster)
    monkeypatch.setattr("sys.argv", ["ci_result_fetch.py"])

    monkeypatch.setattr(fetch, "STATUS_FILE", tmp_path / "status.json")
    monkeypatch.setattr(fetch, "_print_never_captured", lambda token: None)
    fetch.main()

    expected = (_FrozenClock._frozen
               - real_datetime.timedelta(days=fetch.NO_CURSOR_WINDOW_DAYS)
               ).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert seen_since == [expected]


def test_a_pair_with_a_stored_cursor_ignores_the_no_cursor_window(
        tmp_path, monkeypatch):
    monkeypatch.setattr(fetch, "TRACKED",
                        ({"repo": "convivy/vivy", "workflow_file": "ci.yml",
                          "product": "vivy"},))
    cursor_file = tmp_path / "cursor.json"
    key = "convivy/vivy#ci.yml"
    stored = "2026-09-29T00:00:00Z"
    cursor_file.write_text(json.dumps({key: stored}))
    monkeypatch.setattr(fetch, "CURSOR_FILE", cursor_file)
    monkeypatch.setattr(fetch, "TOKEN_FILE", tmp_path / "token")
    (tmp_path / "token").write_text("tok")

    seen_since = []

    def fake_gh_runs(repo, workflow_file, since):
        seen_since.append(since)
        return []

    monkeypatch.setattr(fetch, "gh_runs", fake_gh_runs)
    monkeypatch.setattr(fetch, "post_signal", FakeServer().poster)
    monkeypatch.setattr("sys.argv", ["ci_result_fetch.py"])

    monkeypatch.setattr(fetch, "STATUS_FILE", tmp_path / "status.json")
    monkeypatch.setattr(fetch, "_print_never_captured", lambda token: None)
    fetch.main()

    assert seen_since == [stored], (
        "a pair that already has a stored cursor must resume from it "
        "exactly, not fall back to the no-cursor window")


def test_since_flag_overrides_both_a_stored_cursor_and_the_no_cursor_window(
        tmp_path, monkeypatch):
    monkeypatch.setattr(fetch, "TRACKED",
                        ({"repo": "convivy/vivy", "workflow_file": "ci.yml",
                          "product": "vivy"},))
    cursor_file = tmp_path / "cursor.json"
    key = "convivy/vivy#ci.yml"
    cursor_file.write_text(json.dumps({key: "2026-09-29T00:00:00Z"}))
    monkeypatch.setattr(fetch, "CURSOR_FILE", cursor_file)
    monkeypatch.setattr(fetch, "TOKEN_FILE", tmp_path / "token")
    (tmp_path / "token").write_text("tok")

    seen_since = []

    def fake_gh_runs(repo, workflow_file, since):
        seen_since.append(since)
        return []

    monkeypatch.setattr(fetch, "gh_runs", fake_gh_runs)
    monkeypatch.setattr(fetch, "post_signal", FakeServer().poster)
    monkeypatch.setattr(
        "sys.argv", ["ci_result_fetch.py", "--since", "2020-01-01T00:00:00Z"])

    monkeypatch.setattr(fetch, "STATUS_FILE", tmp_path / "status.json")
    monkeypatch.setattr(fetch, "_print_never_captured", lambda token: None)
    fetch.main()

    assert seen_since == ["2020-01-01T00:00:00Z"], (
        "--since must override both a stored cursor and the no-cursor "
        "window default")


def test_a_malformed_response_does_not_advance_the_cursor(tmp_path, monkeypatch):
    """**The scenario the brief calls out as mattering most, at the `main()`
    level.** One tracked pair whose fetch fails must leave its cursor
    UNCHANGED — proven here by running it once with a prior cursor value
    seeded, then confirming that exact value survives a failed run, rather
    than being overwritten or deleted."""
    monkeypatch.setattr(fetch, "TRACKED",
                        ({"repo": "convivy/vivy",
                          "workflow_file": "e2e-stage-smoke.yml",
                          "product": "vivy"},))
    cursor_file = tmp_path / "cursor.json"
    key = "convivy/vivy#e2e-stage-smoke.yml"
    prior_value = "2026-08-20T00:00:00Z"
    cursor_file.write_text(json.dumps({key: prior_value}))
    monkeypatch.setattr(fetch, "CURSOR_FILE", cursor_file)
    monkeypatch.setattr(fetch, "TOKEN_FILE", tmp_path / "token")
    (tmp_path / "token").write_text("tok")

    monkeypatch.setattr(fetch, "gh_runs", fetcher_raising())
    monkeypatch.setattr(fetch, "post_signal", FakeServer().poster)
    monkeypatch.setattr("sys.argv", ["ci_result_fetch.py"])

    try:
        monkeypatch.setattr(fetch, "STATUS_FILE", tmp_path / "status.json")
        monkeypatch.setattr(fetch, "_print_never_captured", lambda token: None)
        fetch.main()
        assert False, "a run with errors should exit non-zero"
    except SystemExit as e:
        assert e.code == 1

    assert fetch.load_cursor()[key] == prior_value, (
        "a failed fetch must not move the cursor past data it never wrote")


def test_one_pair_s_failure_does_not_block_another_pair_s_cursor(tmp_path, monkeypatch):
    """Two tracked pairs, one good and one broken — the good one's cursor
    must still advance. Proves the per-pair isolation `capture_workflow`'s
    per-repo tally is meant to give, at the level `main()` actually uses it."""
    monkeypatch.setattr(fetch, "TRACKED", (
        {"repo": "convivy/vivy", "workflow_file": "e2e-stage-smoke.yml", "product": "vivy"},
        {"repo": "convivy/vivy", "workflow_file": "ci.yml", "product": "vivy"},
    ))
    monkeypatch.setattr(fetch, "CURSOR_FILE", tmp_path / "cursor.json")
    monkeypatch.setattr(fetch, "TOKEN_FILE", tmp_path / "token")
    (tmp_path / "token").write_text("tok")
    monkeypatch.setattr(fetch, "OVERLAP", __import__("datetime").timedelta(0))

    def fake_gh_runs(repo, workflow_file, since):
        if workflow_file == "ci.yml":
            raise fetch.CaptureError("simulated failure for ci.yml only")
        return [_run(1)]

    monkeypatch.setattr(fetch, "gh_runs", fake_gh_runs)
    monkeypatch.setattr(fetch, "post_signal", FakeServer().poster)
    monkeypatch.setattr("sys.argv", ["ci_result_fetch.py"])

    try:
        monkeypatch.setattr(fetch, "STATUS_FILE", tmp_path / "status.json")
        monkeypatch.setattr(fetch, "_print_never_captured", lambda token: None)
        fetch.main()
    except SystemExit as e:
        assert e.code == 1

    cursor = fetch.load_cursor()
    assert "convivy/vivy#e2e-stage-smoke.yml" in cursor, (
        "the pair that succeeded must still get a cursor")
    assert "convivy/vivy#ci.yml" not in cursor, (
        "the pair that failed must not get one")


def test_one_pair_s_unexpected_exception_does_not_block_another_pair_s_cursor(tmp_path, monkeypatch):
    """The isolation the test above proves for a `CaptureError`-shaped
    failure must also hold for an exception `capture_workflow` never
    anticipated — an `AttributeError` from a non-dict `gh api` entry
    reaching `run.get(...)`, say (reviewer finding 3). Three tracked pairs,
    the MIDDLE one raising something other than `CaptureError`: both the
    pair processed before it and the pair processed after it must still get
    their cursors saved, proving `save_cursor` runs every iteration rather
    than once at the end where the middle pair's exception would have
    discarded even the FIRST pair's already-earned advance."""
    monkeypatch.setattr(fetch, "TRACKED", (
        {"repo": "convivy/vivy", "workflow_file": "e2e-stage-smoke.yml", "product": "vivy"},
        {"repo": "convivy/vivy", "workflow_file": "ci.yml", "product": "vivy"},
        {"repo": "convivy/vivy", "workflow_file": "deploy.yml", "product": "vivy"},
    ))
    monkeypatch.setattr(fetch, "CURSOR_FILE", tmp_path / "cursor.json")
    monkeypatch.setattr(fetch, "TOKEN_FILE", tmp_path / "token")
    (tmp_path / "token").write_text("tok")
    monkeypatch.setattr(fetch, "OVERLAP", __import__("datetime").timedelta(0))

    def fake_gh_runs(repo, workflow_file, since):
        if workflow_file == "ci.yml":
            # not a CaptureError — the shape capture_workflow does not catch
            raise AttributeError("simulated: a non-dict run reached .get()")
        return [_run(1)]

    monkeypatch.setattr(fetch, "gh_runs", fake_gh_runs)
    monkeypatch.setattr(fetch, "post_signal", FakeServer().poster)
    monkeypatch.setattr("sys.argv", ["ci_result_fetch.py"])

    try:
        monkeypatch.setattr(fetch, "STATUS_FILE", tmp_path / "status.json")
        monkeypatch.setattr(fetch, "_print_never_captured", lambda token: None)
        fetch.main()
    except SystemExit as e:
        assert e.code == 1

    cursor = fetch.load_cursor()
    assert "convivy/vivy#e2e-stage-smoke.yml" in cursor, (
        "the pair processed BEFORE the unexpected exception must keep its cursor")
    assert "convivy/vivy#deploy.yml" in cursor, (
        "the pair processed AFTER the unexpected exception must still run and get one")
    assert "convivy/vivy#ci.yml" not in cursor, (
        "the pair that raised unexpectedly must not get one")


# --- the plist template -------------------------------------------------------

PLIST = (Path(__file__).resolve().parent.parent / "launchd"
         / "com.convivy.library.ci-result-fetch.plist")


def test_the_plist_points_at_the_deploy_area_not_the_checkout():
    plist = plistlib.loads(PLIST.read_bytes())
    for arg in plist["ProgramArguments"]:
        assert arg.startswith("@@LIBRARY_HOME@@/.local/deploy/library/"), (
            f"{arg!r} points at a working checkout rather than the deploy area "
            f"— a timer must never invoke the checkout (the operator, 2026-08-30)")
    assert plist["WorkingDirectory"].startswith(
        "@@LIBRARY_HOME@@/.local/deploy/library")


def test_the_plist_is_a_poll_not_a_kept_alive_server():
    plist = plistlib.loads(PLIST.read_bytes())
    assert "StartInterval" in plist
    assert "KeepAlive" not in plist, (
        "this job is expected to exit every run; KeepAlive would fight that")


def test_the_plist_s_state_paths_are_outside_the_deploy_area():
    plist = plistlib.loads(PLIST.read_bytes())
    for key in ("StandardOutPath", "StandardErrorPath"):
        assert plist[key].startswith("@@LIBRARY_HOME@@/.library/"), (
            f"{key} must live in the Library's state area, not the deploy "
            f"area an install swap replaces wholesale")


def test_main_does_not_warn_for_a_repo_listed_as_not_captured(
        tmp_path, monkeypatch, capsys):
    """A repository Deck draws that has no pull-request CI is listed in
    `NOT_CAPTURED` with its evidence and draws no `untracked:` line, so the
    warning names only a real gap (reviewer, PR #189 round 2)."""
    monkeypatch.setattr(fetch, "TRACKED", (
        {"repo": "convivy/vivy", "workflow_file": "ci.yml", "product": "vivy"},
    ))
    monkeypatch.setattr(fetch, "NOT_CAPTURED", {"convivy/signals": "no workflows"})
    monkeypatch.setattr(fetch, "CURSOR_FILE", tmp_path / "cursor.json")
    monkeypatch.setattr(fetch, "TOKEN_FILE", tmp_path / "token")
    (tmp_path / "token").write_text("tok")
    monkeypatch.setattr(fetch, "gh_runs", fetcher_returning([]))
    monkeypatch.setattr(fetch, "post_signal", FakeServer().poster)
    monkeypatch.setattr("sys.argv", ["ci_result_fetch.py"])

    registry_file = tmp_path / "projects.json"
    registry_file.write_text(json.dumps({
        "vivy": {"lifecycle": "active", "owner": "convivy", "repos": ["vivy"]},
        "signals": {"lifecycle": "active", "owner": "convivy", "repos": ["signals"]},
        "hollis": {"lifecycle": "active", "owner": "convivy", "repos": ["hollis"]},
    }))
    monkeypatch.setattr(projects, "REGISTRY_PATH", registry_file)
    projects.reload()

    monkeypatch.setattr(fetch, "STATUS_FILE", tmp_path / "status.json")
    monkeypatch.setattr(fetch, "_print_never_captured", lambda token: None)
    fetch.main()

    err = capsys.readouterr().err
    assert "untracked: convivy/signals" not in err
    assert "untracked: convivy/hollis" in err


def test_no_repo_is_both_tracked_and_not_captured():
    tracked = {entry["repo"] for entry in fetch.TRACKED}
    assert not tracked & set(fetch.NOT_CAPTURED)


def test_www_is_tracked_by_its_pull_request_workflow():
    entries = [e for e in fetch.TRACKED if e["repo"] == "convivy/www"]
    assert entries == [{"repo": "convivy/www", "workflow_file": "build-deploy.yml",
                        "product": "convivy-orient"}]
