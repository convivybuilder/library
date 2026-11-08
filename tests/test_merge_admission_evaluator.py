"""`merge_admission.evaluate` — the merge-admission evaluator as a pure
module (CQ·218, issue_01M5FM2K55J8RCPK218Y7JSHH2).

**The control database is the real schema**, built once by `alembic upgrade
head` and copied per test (the same discipline `test_bundles.py` uses),
because the review-records constraints (`verdict_bound`, the run-verified
check) are part of what `evaluate` relies on and a hand-written
`CREATE TABLE` would test a copy of them rather than the real thing.

**The knowledge checkout is a real git repository too.** `evaluate` reads
the agent policy file and the pull request's changed files with `git`
subprocess calls against `reference.KB_REPO`, exactly as it will in
production — a fixture that faked those reads would not exercise the git
plumbing this module ports from `agent-platform`.

Not wired through any route (that is CQ·219): every call here is `merge_
admission.evaluate(...)` directly.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import merge_admission
import projects
import reference
import store

REPO_ROOT = Path(__file__).resolve().parent.parent

REPO_IDENTITY_REPO = "convivy/library"
HEAD = "b" * 40

POLICY_TEXT = """policy: platform/agent-policy
schema_version: 1
agents_may_merge_knowledge: true
agents_may_merge_knowledge_instructions: true
agents_may_merge_documents: false
review_rounds_per_role: 7
review_rounds_code: 11
max_concurrent: 48
max_workers_per_team: 6
max_live_supervisors: 8
"""


# --- the control database (real alembic schema) ---------------------------------


@pytest.fixture(scope="module")
def migrated_db(tmp_path_factory):
    db = tmp_path_factory.mktemp("migrated") / "library.db"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=str(REPO_ROOT),
        capture_output=True, text=True, check=False,
        env={"PATH": "/usr/bin:/bin", "HOME": str(REPO_ROOT), "LIBRARY_DB": str(db)})
    assert result.returncode == 0, result.stderr
    return db


# --- the knowledge checkout (real git) -------------------------------------------


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                    capture_output=True, text=True)


def _commit(repo, *, message, write=()):
    for rel, content in write:
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _git(repo, "add", "-A")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", message],
                    check=True, capture_output=True, text=True, env=env)


def _rev(repo, ref):
    return subprocess.run(["git", "-C", str(repo), "rev-parse", ref],
                          check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture(scope="module")
def knowledge_repo(tmp_path_factory):
    """A real checkout with `origin/main` frozen at a base commit holding
    the policy file, then two branches off that same base: `docs-only`
    (an ordinary document, no front matter, outside every instruction
    directory — the review floor's own "no changed file is in the floor"
    case) and `instruction-file` (a file under `projects/platform/agents/`,
    a positive floor member by `_INSTRUCTION_DIRS` alone).
    """
    root = tmp_path_factory.mktemp("knowledge")
    repo = root / "knowledge"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _commit(repo, message="base", write=[
        ("projects/platform/policy/agent-policy.yaml", POLICY_TEXT),
        ("projects/library/notes/0001-a.md", "base content\n"),
    ])
    _git(repo, "update-ref", "refs/remotes/origin/main", "main")

    _git(repo, "checkout", "-q", "-b", "docs-only")
    _commit(repo, message="revise the note", write=[
        ("projects/library/notes/0001-a.md", "revised content\n")])
    docs_only_sha = _rev(repo, "docs-only")

    _git(repo, "checkout", "-q", "main")
    _git(repo, "checkout", "-q", "-b", "instruction-file")
    _commit(repo, message="add an agent definition", write=[
        ("projects/platform/agents/new-agent.md", "the new agent\n")])
    instruction_sha = _rev(repo, "instruction-file")

    _git(repo, "checkout", "-q", "main")
    return {"root": repo, "docs_only_sha": docs_only_sha,
           "instruction_sha": instruction_sha}


# --- per-test wiring --------------------------------------------------------------


@pytest.fixture
def conn(migrated_db, tmp_path, knowledge_repo, monkeypatch):
    db = tmp_path / "library.db"
    shutil.copy(migrated_db, db)
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(reference, "KB_REPO", knowledge_repo["root"])
    monkeypatch.setenv("LIBRARY_REPOS_ROOT", str(tmp_path / "no-such-checkouts"))

    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({
        "library": {"class": "project", "lifecycle": "active", "owner": "convivy",
                   "repos": ["library"],
                   "ci_workflows": {"library": ["ci.yml"]}},
    }))
    monkeypatch.setattr(projects, "REGISTRY_PATH", registry)
    projects.reload()

    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    yield c
    c.close()
    projects.reload()


def _insert_review(conn, *, role, verdict, reviewed_ref, run_id, pr=53,
                   updated_at, verdict_bound=True, auth=store.REVIEW_AUTH_RUN_VERIFIED,
                   record_id=None, repo=REPO_IDENTITY_REPO):
    record_id = record_id or f"rev_{role}_{run_id}_{reviewed_ref[:8]}"
    conn.execute(
        "INSERT INTO review_records (id, repo_identity, product, pr, "
        "reviewed_ref, role, run_id, identity_key, verdict, findings, prose, "
        "paths, doc_ids, item_ids, auth, verdict_bound, content_sha, version, "
        "created_at, updated_at) VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (record_id, f"github:{repo}", "library", pr, reviewed_ref,
         role, run_id, run_id, verdict, "[]", "", "[]", "[]", "[]", auth,
         1 if verdict_bound else 0, f"sha-{record_id}", 1, updated_at, updated_at))
    conn.commit()


def _insert_test_result(conn, *, commit, conclusion, ingested_by="ci-result-capture",
                        workflow="ci", product="library",
                        repo=REPO_IDENTITY_REPO, signal_id=None):
    signal_id = signal_id or f"sig_{commit}_{ingested_by}_{conclusion}"
    refs = json.dumps({"commit": commit, "conclusion": conclusion,
                       "workflow": workflow,
                       "run_url": f"https://github.com/{repo}/actions/runs/1"})
    conn.execute(
        "INSERT INTO signals (id, source, kind, actor, ingested_by, product, "
        "stream, body, refs, meta, privacy_class, dedup_key, produced_at, "
        "ingested_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (signal_id, "ci", "test-result", "github-actions", ingested_by, product,
         None, "ci run", refs, "{}", "internal", None,
         "2026-10-01 00:00:00.000000", "2026-10-01 00:00:00.000000"))
    conn.commit()


def _both_roles_approved(conn, *, reviewed_ref=HEAD, pr=53):
    _insert_review(conn, role="reviewer", verdict="APPROVE", reviewed_ref=reviewed_ref,
                   run_id="run-r1", pr=pr, updated_at="2026-10-01 00:00:00.000000")
    _insert_review(conn, role="security-reviewer", verdict="APPROVE",
                   reviewed_ref=reviewed_ref, run_id="run-s1", pr=pr,
                   updated_at="2026-10-01 00:00:00.000000")


def _green_check(conn, *, commit=HEAD):
    _insert_test_result(conn, commit=commit, conclusion="success")


def _by_kind(missing, kind):
    return [m for m in missing if m["kind"] == kind]


# --- admits ------------------------------------------------------------------------


def test_admits_with_both_roles_approved_and_checks_green(conn):
    _both_roles_approved(conn)
    _green_check(conn)
    result = merge_admission.evaluate(REPO_IDENTITY_REPO, 53, HEAD, conn=conn)
    assert result == {"approved": True, "missing": []}


# --- reviews -----------------------------------------------------------------------


@pytest.mark.parametrize("missing_role", ["reviewer", "security-reviewer"])
def test_refuses_naming_each_missing_role(conn, missing_role):
    present_role = "security-reviewer" if missing_role == "reviewer" else "reviewer"
    _insert_review(conn, role=present_role, verdict="APPROVE", reviewed_ref=HEAD,
                   run_id="run-1", updated_at="2026-10-01 00:00:00.000000")
    _green_check(conn)
    result = merge_admission.evaluate(REPO_IDENTITY_REPO, 53, HEAD, conn=conn)
    assert result["approved"] is False
    reviews = _by_kind(result["missing"], "review")
    assert any(m["role"] == missing_role for m in reviews)


def test_a_run_verified_unbound_only_record_names_verdict_binding(conn):
    """A role whose only record is run-verified but predates verdict
    binding (CQ·217) gets a detail naming verdict binding, rather than the
    false claim that it has never reviewed this pull request. The kind and
    retryable stay what a missing review always gets."""
    _insert_review(conn, role="reviewer", verdict="APPROVE", reviewed_ref=HEAD,
                   run_id="run-1", updated_at="2026-10-01 00:00:00.000000",
                   verdict_bound=False)
    _insert_review(conn, role="security-reviewer", verdict="APPROVE", reviewed_ref=HEAD,
                   run_id="run-s1", updated_at="2026-10-01 00:00:00.000000")
    _green_check(conn)
    result = merge_admission.evaluate(REPO_IDENTITY_REPO, 53, HEAD, conn=conn)
    assert result["approved"] is False
    reviewer = next(m for m in _by_kind(result["missing"], "review")
                    if m["role"] == "reviewer")
    assert reviewer["retryable"] is False
    assert "verdict binding" in reviewer["detail"]


def test_no_record_at_all_keeps_the_plain_missing_detail(conn):
    """A role that has never reviewed this pull request still gets the
    same kind and retryable as before, with a detail naming no
    run-verified, verdict-bound review rather than verdict binding."""
    _insert_review(conn, role="security-reviewer", verdict="APPROVE", reviewed_ref=HEAD,
                   run_id="run-s1", updated_at="2026-10-01 00:00:00.000000")
    _green_check(conn)
    result = merge_admission.evaluate(REPO_IDENTITY_REPO, 53, HEAD, conn=conn)
    assert result["approved"] is False
    reviewer = next(m for m in _by_kind(result["missing"], "review")
                    if m["role"] == "reviewer")
    assert reviewer["retryable"] is False
    assert "verdict binding" not in reviewer["detail"]
    assert "no run-verified, verdict-bound review from reviewer" in reviewer["detail"]


def test_an_unverified_only_record_gets_the_no_record_detail(conn):
    """An unverified record counts for nothing, including toward the new
    verdict-binding detail: a role with only one gets the same detail as a
    role with no record at all."""
    _insert_review(conn, role="reviewer", verdict="APPROVE", reviewed_ref=HEAD,
                   run_id="run-1", updated_at="2026-10-01 00:00:00.000000",
                   auth=store.REVIEW_AUTH_UNVERIFIED, verdict_bound=False)
    _insert_review(conn, role="security-reviewer", verdict="APPROVE", reviewed_ref=HEAD,
                   run_id="run-s1", updated_at="2026-10-01 00:00:00.000000")
    _green_check(conn)
    result = merge_admission.evaluate(REPO_IDENTITY_REPO, 53, HEAD, conn=conn)
    assert result["approved"] is False
    reviewer = next(m for m in _by_kind(result["missing"], "review")
                    if m["role"] == "reviewer")
    assert reviewer["retryable"] is False
    assert "verdict binding" not in reviewer["detail"]


def test_a_later_request_changes_at_the_same_head_refuses(conn):
    """Two different runs reviewed the same head as `reviewer`: an earlier
    one approved, a later one (a different `run_id`) requested changes.
    The later verdict outranks the earlier approval because the query
    picks the latest row by `updated_at`, not any row that happens to
    read APPROVE."""
    _insert_review(conn, role="reviewer", verdict="APPROVE", reviewed_ref=HEAD,
                   run_id="run-early", updated_at="2026-10-01 00:00:00.000000")
    _insert_review(conn, role="reviewer", verdict="REQUEST_CHANGES", reviewed_ref=HEAD,
                   run_id="run-late", updated_at="2026-10-02 00:00:00.000000")
    _insert_review(conn, role="security-reviewer", verdict="APPROVE", reviewed_ref=HEAD,
                   run_id="run-s1", updated_at="2026-10-01 00:00:00.000000")
    _green_check(conn)
    result = merge_admission.evaluate(REPO_IDENTITY_REPO, 53, HEAD, conn=conn)
    assert result["approved"] is False
    reviews = _by_kind(result["missing"], "review")
    assert any(m["role"] == "reviewer" and "REQUEST_CHANGES" in m["detail"]
              for m in reviews)


def test_an_unverified_approve_is_ignored(conn):
    """Without the `auth = run-verified` guard this exercises, an
    `unverified` APPROVE would read as a satisfied role."""
    _insert_review(conn, role="reviewer", verdict="APPROVE", reviewed_ref=HEAD,
                   run_id="run-1", updated_at="2026-10-01 00:00:00.000000",
                   auth=store.REVIEW_AUTH_UNVERIFIED, verdict_bound=False)
    _insert_review(conn, role="security-reviewer", verdict="APPROVE", reviewed_ref=HEAD,
                   run_id="run-s1", updated_at="2026-10-01 00:00:00.000000")
    _green_check(conn)
    result = merge_admission.evaluate(REPO_IDENTITY_REPO, 53, HEAD, conn=conn)
    assert result["approved"] is False
    assert any(m["kind"] == "review" and m["role"] == "reviewer"
              for m in result["missing"])


def test_a_stale_review_at_an_old_head_is_retryable(conn):
    _insert_review(conn, role="reviewer", verdict="APPROVE", reviewed_ref="a" * 40,
                   run_id="run-1", updated_at="2026-10-01 00:00:00.000000")
    _insert_review(conn, role="security-reviewer", verdict="APPROVE", reviewed_ref=HEAD,
                   run_id="run-s1", updated_at="2026-10-01 00:00:00.000000")
    _green_check(conn)
    result = merge_admission.evaluate(REPO_IDENTITY_REPO, 53, HEAD, conn=conn)
    assert result["approved"] is False
    stale = _by_kind(result["missing"], "stale_review")
    assert len(stale) == 1
    assert stale[0]["role"] == "reviewer"
    assert stale[0]["retryable"] is True


# --- checks ------------------------------------------------------------------------


def test_a_test_result_from_another_credential_is_ignored(conn):
    """Without the `ingested_by` guard this exercises, any `signal.create`
    holder's row would satisfy the check."""
    _both_roles_approved(conn)
    _insert_test_result(conn, commit=HEAD, conclusion="success",
                        ingested_by="some-other-credential")
    result = merge_admission.evaluate(REPO_IDENTITY_REPO, 53, HEAD, conn=conn)
    assert result["approved"] is False
    pending = _by_kind(result["missing"], "check_pending")
    assert any(m["workflow"] == "ci.yml" for m in pending)


def test_a_pending_check_is_marked_retryable(conn):
    _both_roles_approved(conn)
    result = merge_admission.evaluate(REPO_IDENTITY_REPO, 53, HEAD, conn=conn)
    assert result["approved"] is False
    pending = _by_kind(result["missing"], "check_pending")
    assert len(pending) == 1
    assert pending[0]["workflow"] == "ci.yml"
    assert pending[0]["retryable"] is True


def test_a_failed_check_is_not_retryable(conn):
    _both_roles_approved(conn)
    _insert_test_result(conn, commit=HEAD, conclusion="failure")
    result = merge_admission.evaluate(REPO_IDENTITY_REPO, 53, HEAD, conn=conn)
    assert result["approved"] is False
    checks = _by_kind(result["missing"], "check")
    assert len(checks) == 1
    assert checks[0]["workflow"] == "ci.yml"
    assert checks[0]["retryable"] is False


# --- the round cap -----------------------------------------------------------------


def test_eight_runs_against_a_cap_of_seven_refuses(conn):
    for n in range(8):
        _insert_review(conn, role="reviewer", verdict="APPROVE", reviewed_ref=HEAD,
                       run_id=f"run-{n}", updated_at=f"2026-10-01 00:00:0{n}.000000")
    _insert_review(conn, role="security-reviewer", verdict="APPROVE", reviewed_ref=HEAD,
                   run_id="run-s1", updated_at="2026-10-01 00:00:00.000000")
    _green_check(conn)
    result = merge_admission.evaluate(REPO_IDENTITY_REPO, 53, HEAD, conn=conn)
    assert result["approved"] is False
    cap = _by_kind(result["missing"], "round_cap")
    assert len(cap) == 1
    assert cap[0]["role"] == "reviewer"
    assert cap[0]["retryable"] is False


def test_an_unreadable_policy_file_refuses(conn, tmp_path, monkeypatch):
    empty = tmp_path / "not-a-checkout"
    empty.mkdir()
    monkeypatch.setattr(reference, "KB_REPO", empty)
    _both_roles_approved(conn)
    _green_check(conn)
    result = merge_admission.evaluate(REPO_IDENTITY_REPO, 53, HEAD, conn=conn)
    assert result["approved"] is False
    assert any(m["kind"] == "unreadable" for m in result["missing"])


def test_an_instruction_file_pr_at_eight_rounds_is_admitted(conn, knowledge_repo):
    """The review floor is positive (a file under `projects/platform/
    agents/`), so the pull request takes `review_rounds_code` (11), and
    eight rounds does not exceed it."""
    head = knowledge_repo["instruction_sha"]
    for n in range(8):
        _insert_review(conn, role="reviewer", verdict="APPROVE", reviewed_ref=head,
                       run_id=f"run-{n}", updated_at=f"2026-10-01 00:00:0{n}.000000",
                       repo="convivy/knowledge")
    _insert_review(conn, role="security-reviewer", verdict="APPROVE", reviewed_ref=head,
                   run_id="run-s1", updated_at="2026-10-01 00:00:00.000000",
                   repo="convivy/knowledge")
    _insert_test_result(conn, commit=head, conclusion="success")
    result = merge_admission.evaluate("convivy/knowledge", 53, head, conn=conn)
    assert _by_kind(result["missing"], "round_cap") == []


def test_a_shadow_reviewers_documents_only_head_lowers_the_cap(conn, knowledge_repo):
    """`kb_merge._round_cap` lowers the cap at every authenticated stamp's
    head, `shadow-reviewer`'s included. The current head is an instruction
    file (eleven rounds), but a `shadow-reviewer` record at a documents-only
    head lowers the cap to seven, so eight `reviewer` rounds are refused
    (CQ·218 round 1, reviewer)."""
    head = knowledge_repo["instruction_sha"]
    for n in range(8):
        _insert_review(conn, role="reviewer", verdict="APPROVE", reviewed_ref=head,
                       run_id=f"run-{n}", updated_at=f"2026-10-01 00:00:0{n}.000000",
                       repo="convivy/knowledge")
    _insert_review(conn, role="security-reviewer", verdict="APPROVE", reviewed_ref=head,
                   run_id="run-s1", updated_at="2026-10-01 00:00:00.000000",
                   repo="convivy/knowledge")
    _insert_review(conn, role="shadow-reviewer", verdict="APPROVE",
                   reviewed_ref=knowledge_repo["docs_only_sha"], run_id="run-shadow",
                   updated_at="2026-10-01 00:00:00.000000", repo="convivy/knowledge")
    _insert_test_result(conn, commit=head, conclusion="success")
    result = merge_admission.evaluate("convivy/knowledge", 53, head, conn=conn)
    cap = _by_kind(result["missing"], "round_cap")
    assert len(cap) == 1
    assert cap[0]["role"] == "reviewer"


def test_a_documents_only_pr_at_eight_rounds_is_refused(conn, knowledge_repo):
    """No changed file is in the review floor, so the pull request takes
    `review_rounds_per_role` (7), and eight rounds exceeds it."""
    head = knowledge_repo["docs_only_sha"]
    for n in range(8):
        _insert_review(conn, role="reviewer", verdict="APPROVE", reviewed_ref=head,
                       run_id=f"run-{n}", updated_at=f"2026-10-01 00:00:0{n}.000000",
                       repo="convivy/knowledge")
    _insert_review(conn, role="security-reviewer", verdict="APPROVE", reviewed_ref=head,
                   run_id="run-s1", updated_at="2026-10-01 00:00:00.000000",
                   repo="convivy/knowledge")
    _insert_test_result(conn, commit=head, conclusion="success")
    result = merge_admission.evaluate("convivy/knowledge", 53, head, conn=conn)
    cap = _by_kind(result["missing"], "round_cap")
    assert len(cap) == 1
    assert cap[0]["role"] == "reviewer"
