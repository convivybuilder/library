"""The merge-admission evaluator carries a stale APPROVE onto a rebuilt
`kb/` head under platform/decisions/0078's three conditions (CQ-220,
issue_01M5FM32E3R3YWGB71XK9B0Y03).

**A separate file from `test_merge_admission_evaluator.py` on purpose.**
library#327 (CQ-219) extends that file without touching `merge_admission.py`;
this item touches only `merge_admission.py` and adds its own tests here so
the two pull requests never conflict on the same file.

**The git scenarios are ported from agent-platform's `tests/test_kb_merge.py`**
(`_LocalRepo`/`_scenario`, around its `lrepo` fixture): `M0 -> M1 (base)`,
`main` moving on with an unrelated file, and `M0 -> S` / `M1 -> H` as two
independent reviews of the same pull request, one before the move and one
after. `_scenario` here additionally seeds the agent policy file at `M0`
and points `refs/remotes/origin/main` at `base`, so the same scenario also
drives `merge_admission.evaluate()` end to end, not just the private carry
helpers.

**The control database is the real schema** (alembic, copied per test),
the same discipline `test_merge_admission_evaluator.py` uses, since the
review-records constraints are part of what `evaluate` relies on.
"""
from __future__ import annotations

import json
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
REPO = "convivy/knowledge"
PR = 53

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


# --- a throwaway git repo, scenario-built per test -------------------------------
# Ported from agent-platform's tests/test_kb_merge.py `_LocalRepo`/`_scenario`.


class _Repo:
    """A plain git repo, no remote — the carry reads only ever touch commits
    already in one checkout."""

    def __init__(self, path: Path):
        self.path = path
        self.path.mkdir()
        self._git("init", "-q", "-b", "main")
        for k, v in (("user.name", "t"), ("user.email", "t@example.com"),
                     ("commit.gpgsign", "false")):
            self._git("config", k, v)

    def _git(self, *a):
        r = subprocess.run(["git", *a], cwd=self.path, capture_output=True, text=True)
        assert r.returncode == 0, f"git {a}: {r.stderr}"
        return r.stdout.strip()

    def commit(self, files, message="x", parent=None):
        """`files`: path -> text | None (delete) | (mode, text). `parent`,
        when given, is checked out (detached) before committing, so a
        commit can be built on top of any earlier one without disturbing
        `main`."""
        if parent is not None:
            self._git("checkout", "-q", "--detach", parent)
        for rel, val in files.items():
            f = self.path / rel
            if val is None:
                f.unlink()
                continue
            f.parent.mkdir(parents=True, exist_ok=True)
            mode, text = val if isinstance(val, tuple) else ("100644", val)
            f.write_text(text)
            f.chmod(0o755 if mode == "100755" else 0o644)
        self._git("add", "-A")
        self._git("commit", "-q", "--allow-empty", "-m", message)
        return self._git("rev-parse", "HEAD")

    def set_origin_main(self, sha: str):
        self._git("update-ref", "refs/remotes/origin/main", sha)


@pytest.fixture
def repo(tmp_path):
    return _Repo(tmp_path / "knowledge")


# The trailer `agentctl kb propose` writes on every proposal commit.
TRAILER = "\n\nKB-Write: propose projects/x/new.md proposal library"


def _scenario(repo, *, before_main=None, before_pr=None, head_files=None, pr_files=None):
    """`(s, h, base)`. `before_main`/`before_pr` are extra files committed
    into `M0` before the two branches diverge (the pre-existing content
    each side's merge base carries). `head_files` defaults to the same
    content as `pr_files`, the "identical rebuild" case; pass it to build a
    refusal. `base` is also pointed to by `refs/remotes/origin/main`, so the
    same scenario drives `merge_admission.evaluate()` too."""
    m0 = repo.commit(dict({"projects/platform/policy/agent-policy.yaml": POLICY_TEXT},
                         **(before_main or {}), **(before_pr or {})), "m0")
    base = repo.commit({"projects/unrelated/y.md": "a different document\n"},
                       "main moves on", parent=m0)
    pr_files = pr_files or {"projects/x/new.md": "hello\n"}
    s = repo.commit(pr_files, "pr (first review)" + TRAILER, parent=m0)
    h = repo.commit(head_files if head_files is not None else pr_files,
                    "pr (rebuilt)" + TRAILER, parent=base)
    repo.set_origin_main(base)
    return s, h, base


# --- `_carry_blocked`, directly (ported from agent-platform's carry cases) ------


def test_carry_blocked_admits_identical_content_onto_a_moved_main(repo):
    s, h, base = _scenario(repo)
    assert merge_admission._carry_blocked(repo.path, s, h, base) is None


def test_carry_blocked_refuses_a_changed_byte_in_a_changed_file_at_h(repo):
    s, h, base = _scenario(repo, head_files={"projects/x/new.md": "hello, but different\n"})
    why = merge_admission._carry_blocked(repo.path, s, h, base)
    assert why is not None and "projects/x/new.md differs" in why


def test_carry_blocked_refuses_a_changed_main_side_original(repo):
    """`main` itself holds a different version of the very file S and H both
    end up with — S and H agree with each other, but not with what each
    one's own review judged it against."""
    s, h, base = _scenario(
        repo, before_main={"projects/x/shared.md": "the original on main\n"},
        pr_files={"projects/x/shared.md": "the same new text\n"})
    repo._git("checkout", "-q", "--detach", base)
    newbase = repo.commit({"projects/x/shared.md": "a different original, "
                                                    "edited on main after S\n"},
                         "main edits the shared file too", parent=base)
    why = merge_admission._carry_blocked(repo.path, s, h, newbase)
    assert why is None, why   # control: H was built from `base`, not `newbase`
    h2 = repo.commit({"projects/x/shared.md": "the same new text\n"},
                     "pr (rebuilt again)", parent=newbase)
    why = merge_admission._carry_blocked(repo.path, s, h2, newbase)
    assert why is not None and "main-side original" in why and "shared.md" in why


def test_carry_blocked_refuses_an_added_path(repo):
    s, h, base = _scenario(
        repo, head_files={"projects/x/new.md": "hello\n", "projects/x/extra.md": "also new\n"})
    why = merge_admission._carry_blocked(repo.path, s, h, base)
    assert why is not None and "only at" in why and "extra.md" in why


def test_carry_blocked_refuses_a_removed_path(repo):
    s, h, base = _scenario(
        repo, before_pr={"projects/x/old.md": "keep me\n"},
        pr_files={"projects/x/new.md": "hello\n", "projects/x/old.md": None},
        head_files={"projects/x/new.md": "hello\n"})   # H never deletes old.md
    why = merge_admission._carry_blocked(repo.path, s, h, base)
    assert why is not None and "only at" in why and "old.md" in why


def test_carry_blocked_refuses_a_mode_change(repo):
    s, h, base = _scenario(
        repo, pr_files={"projects/x/new.md": ("100644", "hello\n")},
        head_files={"projects/x/new.md": ("100755", "hello\n")})
    why = merge_admission._carry_blocked(repo.path, s, h, base)
    assert why is not None and "projects/x/new.md differs" in why


def test_carry_blocked_raises_on_a_stamp_commit_absent_from_the_object_store(repo):
    s, h, base = _scenario(repo)
    ghost = "d" * 40
    with pytest.raises(merge_admission._CarryUnreadable, match="object store"):
        merge_admission._carry_blocked(repo.path, ghost, h, base)


def test_carry_blocked_raises_when_no_merge_base_can_be_computed(repo):
    s, h, base = _scenario(repo)
    repo._git("checkout", "-q", "--orphan", "unrelated")
    unrelated = repo.commit({"nothing/to/do/with.md": "a disjoint history\n"}, "orphan")
    with pytest.raises(merge_admission._CarryUnreadable, match="merge-base"):
        merge_admission._carry_blocked(repo.path, s, unrelated, base)


# --- `_carry_reason`: `_CarryUnreadable` reported as a string, never raised ------


def test_carry_reason_reports_an_admitted_carry_as_none(repo):
    s, h, base = _scenario(repo)
    assert merge_admission._carry_reason(repo.path, s, h) is None


def test_carry_reason_reports_a_git_failure_as_a_string_not_a_raise(repo):
    s, h, base = _scenario(repo)
    ghost = "d" * 40
    reason = merge_admission._carry_reason(repo.path, ghost, h)
    assert reason is not None and "object store" in reason


# --- `_carry_shape_blocked`: only a Library proposal's shape carries -------------


def test_carry_reason_refuses_a_head_built_on_a_newer_main_than_the_checkout_holds(repo):
    """The stale-`origin/main` case. The true `main` (`m2`) edited the file
    the PR also changes, and the head, built on `m2`, overwrites that edit
    with the PR's blob. Judged against the checkout's older `origin/main`,
    `_carry_blocked` alone carries it; the shape check refuses, because the
    head's parent is not the ref the conditions are judged against."""
    s, h, base = _scenario(
        repo, before_main={"projects/x/shared.md": "the original on main\n"},
        pr_files={"projects/x/shared.md": "the same new text\n"})
    m2 = repo.commit({"projects/x/shared.md": "main's own later edit\n"},
                     "main edits the shared file", parent=base)
    h2 = repo.commit({"projects/x/shared.md": "the same new text\n"},
                     "pr (rebuilt onto m2)" + TRAILER, parent=m2)
    repo.set_origin_main(base)   # the checkout has not fetched m2 yet
    assert merge_admission._carry_blocked(repo.path, s, h2, base) is None   # the hazard
    reason = merge_admission._carry_reason(repo.path, s, h2)
    assert reason is not None and "is not one commit on" in reason
    repo.set_origin_main(m2)     # once the checkout holds m2, condition 3 sees the edit
    reason = merge_admission._carry_reason(repo.path, s, h2)
    assert reason is not None and "main-side original" in reason


def test_carry_reason_refuses_a_head_without_a_kb_write_trailer(repo):
    s, h, base = _scenario(repo)
    bare = repo.commit({"projects/x/new.md": "hello\n"}, "a hand-made rebase", parent=base)
    reason = merge_admission._carry_reason(repo.path, s, bare)
    assert reason is not None and "carries no KB-Write trailer" in reason


def test_carry_reason_refuses_a_stamped_commit_without_a_kb_write_trailer(repo):
    s, h, base = _scenario(repo)
    m0 = repo._git("rev-parse", f"{s}^")
    bare = repo.commit({"projects/x/new.md": "hello\n"}, "a hand-made first try", parent=m0)
    reason = merge_admission._carry_reason(repo.path, bare, h)
    assert reason is not None and "carries no KB-Write trailer" in reason


def test_carry_reason_refuses_a_head_of_two_commits_on_main(repo):
    s, h, base = _scenario(repo)
    first = repo.commit({"projects/x/new.md": "draft\n"}, "first" + TRAILER, parent=base)
    second = repo.commit({"projects/x/new.md": "hello\n"}, "second" + TRAILER, parent=first)
    assert merge_admission._carry_blocked(repo.path, s, second, base) is None   # same content
    reason = merge_admission._carry_reason(repo.path, s, second)
    assert reason is not None and "is not one commit on" in reason


def test_carry_reason_refuses_a_merge_commit_head(repo):
    s, h, base = _scenario(repo)
    side = repo.commit({"projects/x/new.md": "hello\n"}, "side" + TRAILER, parent=base)
    repo._git("checkout", "-q", "--detach", base)
    repo._git("merge", "-q", "--no-ff", "--no-edit", "-m", "merge" + TRAILER, side)
    merge = repo._git("rev-parse", "HEAD")
    reason = merge_admission._carry_reason(repo.path, s, merge)
    assert reason is not None and "is not one commit on" in reason


@pytest.mark.parametrize("bad", ["--all", "HEAD", "abc123", ""])
def test_carry_reason_refuses_a_head_that_is_not_a_full_sha_before_any_git_call(
        repo, bad, monkeypatch):
    s, h, base = _scenario(repo)
    calls = []
    real_git = merge_admission._git
    monkeypatch.setattr(merge_admission, "_git",
                        lambda root, *a: calls.append(a) or real_git(root, *a))
    reason = merge_admission._carry_reason(repo.path, s, bad)
    assert reason is not None and "is not a full commit sha" in reason
    assert calls, "control: the fixed origin/main read still ran"
    assert not any(bad in a for a in calls)


# --- `evaluate()`, end to end -----------------------------------------------------


def _insert_review(conn, *, role, verdict, reviewed_ref, run_id, updated_at,
                   pr=PR, verdict_bound=True, auth=store.REVIEW_AUTH_RUN_VERIFIED,
                   repo=REPO):
    record_id = f"rev_{role}_{run_id}_{reviewed_ref[:8]}"
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


@pytest.fixture
def conn(migrated_db, tmp_path, repo, monkeypatch):
    db = tmp_path / "library.db"
    shutil.copy(migrated_db, db)
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(reference, "KB_REPO", repo.path)
    monkeypatch.setenv("LIBRARY_REPOS_ROOT", str(tmp_path / "no-such-checkouts"))

    # No project names a "knowledge" repo, so `ci_tracked.tracked()` has no
    # entry for `convivy/knowledge` and `_check_missing` requires nothing —
    # these scenarios are about the review carry alone.
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({
        "library": {"class": "project", "lifecycle": "active", "owner": "convivy",
                   "repos": ["library"], "ci_workflows": {"library": ["ci.yml"]}},
    }))
    monkeypatch.setattr(projects, "REGISTRY_PATH", registry)
    projects.reload()

    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    yield c
    c.close()
    projects.reload()


def _by_kind(missing, kind):
    return [m for m in missing if m["kind"] == kind]


def test_evaluate_admits_an_approve_carried_over_a_pure_rebase(conn, repo):
    s, h, base = _scenario(repo)
    _insert_review(conn, role="reviewer", verdict="APPROVE", reviewed_ref=s,
                   run_id="run-r1", updated_at="2026-10-01 00:00:00.000000")
    _insert_review(conn, role="security-reviewer", verdict="APPROVE", reviewed_ref=h,
                   run_id="run-s1", updated_at="2026-10-01 00:00:00.000000")
    result = merge_admission.evaluate(REPO, PR, h, conn=conn)
    assert result == {"approved": True, "missing": []}


def test_evaluate_refuses_a_changed_byte_naming_the_condition(conn, repo):
    s, h, base = _scenario(repo, head_files={"projects/x/new.md": "hello, but different\n"})
    _insert_review(conn, role="reviewer", verdict="APPROVE", reviewed_ref=s,
                   run_id="run-r1", updated_at="2026-10-01 00:00:00.000000")
    _insert_review(conn, role="security-reviewer", verdict="APPROVE", reviewed_ref=h,
                   run_id="run-s1", updated_at="2026-10-01 00:00:00.000000")
    result = merge_admission.evaluate(REPO, PR, h, conn=conn)
    assert result["approved"] is False
    stale = _by_kind(result["missing"], "stale_review")
    assert len(stale) == 1 and stale[0]["role"] == "reviewer"
    assert stale[0]["retryable"] is True
    assert "does not carry" in stale[0]["detail"]
    assert "projects/x/new.md differs" in stale[0]["detail"]


def test_evaluate_refuses_a_changed_path_set_naming_the_condition(conn, repo):
    s, h, base = _scenario(
        repo, head_files={"projects/x/new.md": "hello\n", "projects/x/extra.md": "also new\n"})
    _insert_review(conn, role="reviewer", verdict="APPROVE", reviewed_ref=s,
                   run_id="run-r1", updated_at="2026-10-01 00:00:00.000000")
    _insert_review(conn, role="security-reviewer", verdict="APPROVE", reviewed_ref=h,
                   run_id="run-s1", updated_at="2026-10-01 00:00:00.000000")
    result = merge_admission.evaluate(REPO, PR, h, conn=conn)
    assert result["approved"] is False
    stale = _by_kind(result["missing"], "stale_review")
    assert len(stale) == 1
    assert "change different paths" in stale[0]["detail"]
    assert "extra.md" in stale[0]["detail"]


def test_evaluate_refuses_a_changed_main_side_original_naming_the_condition(conn, repo):
    s, h, base = _scenario(
        repo, before_main={"projects/x/shared.md": "the original on main\n"},
        pr_files={"projects/x/shared.md": "the same new text\n"})
    repo._git("checkout", "-q", "--detach", base)
    newbase = repo.commit({"projects/x/shared.md": "a different original, "
                                                    "edited on main after S\n"},
                         "main edits the shared file too", parent=base)
    h2 = repo.commit({"projects/x/shared.md": "the same new text\n"},
                     "pr (rebuilt again)" + TRAILER, parent=newbase)
    repo.set_origin_main(newbase)
    _insert_review(conn, role="reviewer", verdict="APPROVE", reviewed_ref=s,
                   run_id="run-r1", updated_at="2026-10-01 00:00:00.000000")
    _insert_review(conn, role="security-reviewer", verdict="APPROVE", reviewed_ref=h2,
                   run_id="run-s1", updated_at="2026-10-01 00:00:00.000000")
    result = merge_admission.evaluate(REPO, PR, h2, conn=conn)
    assert result["approved"] is False
    stale = _by_kind(result["missing"], "stale_review")
    assert len(stale) == 1
    assert "main-side original" in stale[0]["detail"]
    assert "shared.md" in stale[0]["detail"]


def test_evaluate_refuses_a_missing_stamped_commit_as_retryable(conn, repo):
    s, h, base = _scenario(repo)
    ghost = "d" * 40
    _insert_review(conn, role="reviewer", verdict="APPROVE", reviewed_ref=ghost,
                   run_id="run-r1", updated_at="2026-10-01 00:00:00.000000")
    _insert_review(conn, role="security-reviewer", verdict="APPROVE", reviewed_ref=h,
                   run_id="run-s1", updated_at="2026-10-01 00:00:00.000000")
    result = merge_admission.evaluate(REPO, PR, h, conn=conn)
    assert result["approved"] is False
    stale = _by_kind(result["missing"], "stale_review")
    assert len(stale) == 1 and stale[0]["role"] == "reviewer"
    assert stale[0]["retryable"] is True
    assert "object store" in stale[0]["detail"]


def test_evaluate_refuses_a_head_built_on_a_newer_main_as_retryable(conn, repo):
    s, h, base = _scenario(repo)
    m2 = repo.commit({"projects/unrelated/z.md": "main moves again\n"}, "m2", parent=base)
    h2 = repo.commit({"projects/x/new.md": "hello\n"}, "pr (rebuilt onto m2)" + TRAILER,
                     parent=m2)
    repo.set_origin_main(base)
    _insert_review(conn, role="reviewer", verdict="APPROVE", reviewed_ref=s,
                   run_id="run-r1", updated_at="2026-10-01 00:00:00.000000")
    _insert_review(conn, role="security-reviewer", verdict="APPROVE", reviewed_ref=h2,
                   run_id="run-s1", updated_at="2026-10-01 00:00:00.000000")
    result = merge_admission.evaluate(REPO, PR, h2, conn=conn)
    assert result["approved"] is False
    stale = _by_kind(result["missing"], "stale_review")
    assert len(stale) == 1 and stale[0]["retryable"] is True
    assert "is not one commit on" in stale[0]["detail"]


def test_evaluate_never_carries_a_latest_request_changes_over_an_earlier_approve(conn, repo):
    """The role's latest record is a REQUEST_CHANGES at a different commit
    than `S` — the latest record is what is considered, and it is not an
    APPROVE, so the carry is never even attempted, whatever an earlier
    APPROVE at S would have satisfied."""
    s, h, base = _scenario(repo)
    _insert_review(conn, role="reviewer", verdict="APPROVE", reviewed_ref=s,
                   run_id="run-r1", updated_at="2026-10-01 00:00:00.000000")
    _insert_review(conn, role="reviewer", verdict="REQUEST_CHANGES", reviewed_ref=base,
                   run_id="run-r2", updated_at="2026-10-02 00:00:00.000000")
    _insert_review(conn, role="security-reviewer", verdict="APPROVE", reviewed_ref=h,
                   run_id="run-s1", updated_at="2026-10-01 00:00:00.000000")
    result = merge_admission.evaluate(REPO, PR, h, conn=conn)
    assert result["approved"] is False
    stale = _by_kind(result["missing"], "stale_review")
    assert len(stale) == 1 and stale[0]["role"] == "reviewer"
    assert stale[0]["retryable"] is True
    assert "carry" not in stale[0]["detail"]
