"""`GET /reference/history`: a project's recent knowledge-document commits,
read straight from `git log` against the served knowledge checkout
(`LIBRARY_KB_REPO`) rather than the index — the index's `docs` table keeps
only `updated_at`, with no row in it that remembers what changed, when, or
in which commit (issue_01M57NY6JE6371493282RGNS11, for Orient's per-project
pages).

**`limit`/`offset` BOUND COMMITS, NOT ROWS.** A commit touching several
documents under the project yields one row per document, so `limit=N`
matches `git log -N --name-status -- projects/<project>/` exactly rather
than slicing the row list after the fact — the done-when's own comparison.

**Three documents under `library/`, chosen so every test has both a
match and a non-match**: `0001-a` is admitted by the bound test profile
(audience `team`) and survives every commit; `0004-d` is excluded by that
profile (audience `other`) and survives every commit, so a request through
it exercises "out of scope" without also being "gone"; `0003-c` is deleted
in the newest commit and carries no index row at all.

**ROUND 1 (security-reviewer and reviewer): `0003-c`'s row is no longer
unconditionally visible.** A path with no index row left to scope-check
used to reach every caller regardless of its bound profile — the exact
leak both reviewers raised. Fixed to fail closed: a bound caller never
sees such a row, whether the project it asked about is in its scope
(`BOUND`, profile `library/narrow`) or entirely outside it (`OTHER_BOUND`,
profile `platform/only`, which names no rule for `library` at all). An
unbound caller — every credential minted before this route existed —
keeps seeing it exactly as before.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess

import pytest
from fastapi.testclient import TestClient

import app as libapp
import crew_registry
import identity
import protocol
import reference
import store

AGENT = "agent-token-fennick"
BOUND = "agent-token-bound"
# Bound to a profile that names no rule for `library` at all — the
# concrete attack path security-reviewer named in round 1: a caller whose
# binding never mentions a project should get the same answer an unknown
# project gives, not a 200 built from that project's own git history.
OTHER_BOUND = "agent-token-other-bound"
ORCHESTRATOR = identity.Identity(actor="co-library-test", kind="orchestrator")

# Committer timestamps as ISO-8601 with an explicit, non-UTC offset, so a
# test reading `committed_at` back proves the UTC conversion ran rather than
# merely echoing a value that happened to look right already.
WHEN_1 = "2026-09-01T10:00:00+05:00"   # 2026-09-01T05:00:00Z
WHEN_2 = "2026-09-02T10:00:00+05:00"   # 2026-09-02T05:00:00Z
WHEN_3 = "2026-09-03T10:00:00+05:00"   # 2026-09-03T05:00:00Z


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                    capture_output=True, text=True)


def _commit(repo, *, message, when, write=(), delete=()):
    for rel, content in write:
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    for rel in delete:
        (repo / rel).unlink()
    _git(repo, "add", "-A")
    env = {**os.environ, "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", message],
                    check=True, capture_output=True, text=True, env=env)


def _build_kb_repo(root):
    """A real git checkout with a `projects/` tree and `origin/main` set to
    the tip of `main`. This route reads `origin/main` explicitly rather
    than whatever happens to be checked out, matching a real deployment, so
    the fixture needs the remote-tracking ref too — not just commits on a
    local branch.
    """
    repo = root / "knowledge"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")

    _commit(repo, message="add library and platform docs", when=WHEN_1, write=[
        ("projects/library/decisions/0001-a.md", "a v1"),
        ("projects/library/decisions/0003-c.md", "c v1"),
        ("projects/library/decisions/0004-d.md", "d v1"),
        ("projects/platform/decisions/0099-z.md", "z v1"),
    ])
    _commit(repo, message="revise decision 0001", when=WHEN_2, write=[
        ("projects/library/decisions/0001-a.md", "a v2"),
    ])
    _commit(repo, message="modify decision 0004 and drop decision 0003",
             when=WHEN_3,
             write=[("projects/library/decisions/0004-d.md", "d v2")],
             delete=["projects/library/decisions/0003-c.md"])

    _git(repo, "update-ref", "refs/remotes/origin/main", "main")
    return repo


def _hash_bodies(iconn):
    for doc_id, body in iconn.execute("SELECT id, body FROM docs").fetchall():
        iconn.execute("UPDATE docs SET body_hash = ? WHERE id = ?",
                      (hashlib.sha256((body or "").encode()).hexdigest(), doc_id))


def _build_client(tmp_path, monkeypatch, *, include_library_docs=True):
    """Shared by `client` and `client_without_library` — the two differ
    only in whether `library`'s own documents are in the index at all, so
    the byte-for-byte comparison test below has a backing store where
    `project=library` is genuinely unknown rather than merely out of a
    bound caller's scope, with every other fixture detail held equal.
    """
    db = tmp_path / "control.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE roster_agents (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
            source_doc TEXT,
            created_at TEXT NOT NULL, created_by TEXT NOT NULL,
            updated_at TEXT NOT NULL, updated_by TEXT NOT NULL);
        CREATE TABLE profiles (
            id INTEGER PRIMARY KEY, project TEXT NOT NULL, variant TEXT NOT NULL,
            include TEXT NOT NULL, claude_md_extras TEXT NOT NULL,
            created_at TEXT NOT NULL, created_by TEXT NOT NULL,
            updated_at TEXT NOT NULL, updated_by TEXT NOT NULL,
            UNIQUE (project, variant));
    """)
    conn.commit()
    conn.close()

    index = tmp_path / "index.db"
    iconn = sqlite3.connect(index)
    iconn.executescript("""
        CREATE TABLE docs (
            id TEXT PRIMARY KEY, source_ref TEXT, kind TEXT, project TEXT,
            title TEXT, status TEXT, topics TEXT, audience TEXT,
            last_reviewed TEXT, updated_at TEXT, body TEXT, body_hash TEXT);
    """)
    # `library/decisions/0003-c` is deliberately absent even when the rest
    # of `library` is included: it is deleted in the newest commit, and a
    # real indexer drops a deleted document's row entirely rather than
    # leaving it stale.
    rows = [
            ("platform/decisions/0099-z", "projects/platform/decisions/0099-z.md",
             "platform", "decision 0099", ["team"]),
    ]
    if include_library_docs:
        rows += [
            ("library/decisions/0001-a", "projects/library/decisions/0001-a.md",
             "library", "decision 0001", ["team"]),
            ("library/decisions/0004-d", "projects/library/decisions/0004-d.md",
             "library", "decision 0004", ["other"]),
        ]
    for doc_id, source_ref, project, title, audience in rows:
        iconn.execute(
            "INSERT INTO docs (id, source_ref, kind, project, title, status, "
            "topics, audience, body, last_reviewed, updated_at) "
            "VALUES (?,?,?,?,?,'current','[]',?,?,'2026-08-01','2026-08-02')",
            (doc_id, source_ref, "decision", project, title,
             json.dumps(audience), f"body of {doc_id}"))
    _hash_bodies(iconn)
    iconn.commit()
    iconn.close()

    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        AGENT: {"name": "fennick-agent", "products": ["*"]},
        BOUND: {"name": "bound-crew", "products": ["*"],
               "profile": "library/narrow"},
        OTHER_BOUND: {"name": "other-bound-crew", "products": ["*"],
                      "profile": "platform/only"},
    }))

    kb_repo = _build_kb_repo(tmp_path)

    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(reference, "INDEX_DB", index)
    monkeypatch.setattr(reference, "KB_REPO", kb_repo)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)

    # Admits `library/decisions/0001-a` (audience `team`); excludes
    # `library/decisions/0004-d` (audience `other`).
    crew_registry.profile_put(
        ORCHESTRATOR, "library", "narrow",
        [{"kind": "decision", "project": ["library"],
          "audience_contains": ["team"]}], [])
    # Names no rule for `library` at all — every rule is scoped to
    # `platform` instead.
    crew_registry.profile_put(
        ORCHESTRATOR, "platform", "only",
        [{"kind": "decision", "project": ["platform"],
          "audience_contains": ["team"]}], [])

    return TestClient(libapp.app)


@pytest.fixture
def client(tmp_path, monkeypatch):
    return _build_client(tmp_path, monkeypatch, include_library_docs=True)


@pytest.fixture
def client_without_library(tmp_path, monkeypatch):
    """`library` has real commits in the git checkout but no row at all in
    the index — genuinely unknown to this route, the baseline the
    byte-for-byte comparison test needs, as distinct from merely outside
    a bound caller's scope.

    **ITS OWN SUBDIRECTORY**, not `tmp_path` itself: a test using both this
    fixture and `client` shares one `tmp_path`, and the two would otherwise
    collide writing `control.db`/`index.db` to the same paths.
    """
    root = tmp_path / "without_library"
    root.mkdir()
    return _build_client(root, monkeypatch, include_library_docs=False)


def _h(token=AGENT):
    return {"Authorization": f"Bearer {token}", protocol.HEADER: str(protocol.CURRENT)}


def _history(client, token=AGENT, **params):
    return client.get("/reference/history", params=params, headers=_h(token))


# --- registration order ---------------------------------------------------


def test_history_is_registered_before_the_document_catch_all(client):
    """Not load-bearing for `/reference/history` itself — its literal path
    never matches `/reference/documents/{doc_id:path}` — but the brief this
    route shipped under asked for the ordering explicitly, matching the
    house convention `test_signal_facets_is_registered_before_the_signal_id_
    route` already pins for a route where the ordering IS load-bearing.
    """
    paths = [r.path for r in libapp.app.routes if getattr(r, "methods", None)]
    assert paths.index("/reference/history") < \
        paths.index("/reference/documents/{doc_id:path}")


# --- the basic shape ------------------------------------------------------


def test_newest_first_order(client):
    r = _history(client, project="library")
    assert r.status_code == 200
    subjects = [row["subject"] for row in r.json()["history"]]
    assert subjects.index("modify decision 0004 and drop decision 0003") < \
        subjects.index("revise decision 0001") < \
        subjects.index("add library and platform docs")


def test_one_row_per_document_per_commit(client):
    r = _history(client, project="library")
    assert r.status_code == 200
    rows = r.json()["history"]
    first_commit_rows = [row for row in rows
                          if row["subject"] == "add library and platform docs"]
    # Three library documents changed in that commit; the platform one is
    # excluded by the pathspec entirely rather than appearing as a fourth row.
    assert len(first_commit_rows) == 3
    assert {row["status"] for row in first_commit_rows} == {"added"}


def test_committed_at_is_iso_utc(client):
    r = _history(client, project="library")
    assert r.status_code == 200
    by_subject = {}
    for row in r.json()["history"]:
        by_subject.setdefault(row["subject"], row["committed_at"])
    assert by_subject["modify decision 0004 and drop decision 0003"] == \
        "2026-09-03T05:00:00Z"
    assert by_subject["revise decision 0001"] == "2026-09-02T05:00:00Z"
    assert by_subject["add library and platform docs"] == "2026-09-01T05:00:00Z"


def test_a_modified_document_resolves_doc_id_and_title(client):
    r = _history(client, project="library")
    assert r.status_code == 200
    row = next(row for row in r.json()["history"]
              if row["subject"] == "revise decision 0001")
    assert row["status"] == "modified"
    assert row["doc_id"] == "library/decisions/0001-a"
    assert row["title"] == "decision 0001"


# `test_an_unbound_caller_still_sees_the_deleted_documents_row`, in the
# round-1 section below, covers the deleted-document row's shape for an
# unbound caller — grouped there with its bound-caller counterparts rather
# than here, since round 1 is what makes the three worth reading together.


def test_limit_bounds_commits_not_rows(client):
    # `limit=1` takes the single newest COMMIT, which touches two files, not
    # the single newest ROW out of a flattened list — the done-when's own
    # equivalence to `git log -1 --name-status`.
    r = _history(client, project="library", limit=1)
    assert r.status_code == 200
    rows = r.json()["history"]
    assert len(rows) == 2
    assert {row["subject"] for row in rows} == \
        {"modify decision 0004 and drop decision 0003"}

    r = _history(client, project="library", limit=3)
    assert r.status_code == 200
    rows = r.json()["history"]
    assert len(rows) == 6
    assert {row["subject"] for row in rows} == {
        "modify decision 0004 and drop decision 0003",
        "revise decision 0001", "add library and platform docs"}


def test_offset_skips_commits(client):
    r = _history(client, project="library", limit=1, offset=1)
    assert r.status_code == 200
    rows = r.json()["history"]
    assert len(rows) == 1
    assert rows[0]["subject"] == "revise decision 0001"


def test_pathspec_narrows_to_the_named_project(client):
    r = _history(client, project="platform")
    assert r.status_code == 200
    rows = r.json()["history"]
    assert len(rows) == 1
    assert rows[0]["doc_id"] == "platform/decisions/0099-z"


# --- project validation never reaches git ---------------------------------


@pytest.mark.parametrize("project", ["../x", ":(exclude)x", "a/../../etc",
                                     ":/", "nonexistent-project"])
def test_an_unsafe_or_unknown_project_is_refused_without_running_git(
        client, monkeypatch, project):
    calls = []

    def fake_commit_history(*a, **k):
        calls.append((a, k))
        raise AssertionError("git must not run for a refused project")

    monkeypatch.setattr(reference, "commit_history", fake_commit_history)
    r = _history(client, project=project)
    assert r.status_code == 400
    assert calls == []


def test_project_is_required(client):
    r = client.get("/reference/history", headers=_h())
    assert r.status_code == 400


def test_an_unknown_query_parameter_is_refused(client):
    r = _history(client, project="library", bogus="1")
    assert r.status_code == 400
    assert "bogus" in r.json()["error"]


# --- round 1: the project check is scoped too, so it is not an oracle ----
#
# reviewer: the "known project" check read the unscoped `docs.project`
# set, so a project real elsewhere but outside a bound caller's scope
# answered 200 (empty) while a made-up name answered 400 — a caller could
# tell "exists, not mine" from "does not exist". Fixed by checking
# membership against the projects the caller's own bound scope can see.


def test_bound_profile_sees_a_project_outside_its_scope_as_unrecognised(client):
    r = _history(client, token=OTHER_BOUND, project="library")
    assert r.status_code == 400
    assert r.json()["error"] == "unrecognised project"
    assert r.json()["got"] == "library"


def test_bound_profile_still_sees_its_own_in_scope_project(client):
    r = _history(client, token=OTHER_BOUND, project="platform")
    assert r.status_code == 200


def test_out_of_scope_project_answers_identically_to_an_unknown_one(
        client, client_without_library):
    """Byte for byte: `OTHER_BOUND` asking a `client` where `library` is
    real but outside its scope gets the same answer as any caller asking
    a `client_without_library` where `library` does not exist at all —
    same project name, same status, same body, so neither tells a bound
    caller which case it was.
    """
    out_of_scope = _history(client, token=OTHER_BOUND, project="library")
    truly_unknown = _history(client_without_library, project="library")
    assert out_of_scope.status_code == truly_unknown.status_code == 400
    assert out_of_scope.content == truly_unknown.content


# --- git failing never yields a partial list ------------------------------


def test_git_timing_out_refuses_503_and_returns_no_rows(client, monkeypatch):
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=1)

    monkeypatch.setattr(reference.subprocess, "run", fake_run)
    r = _history(client, project="library")
    assert r.status_code == 503
    assert "history" not in r.json()
    assert "timed out" in r.json()["error"]


def test_git_exiting_non_zero_refuses_503_and_returns_no_rows(client, monkeypatch):
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], returncode=128,
                                           stdout="", stderr="fatal: boom")

    monkeypatch.setattr(reference.subprocess, "run", fake_run)
    r = _history(client, project="library")
    assert r.status_code == 503
    assert "history" not in r.json()
    assert "fatal: boom" not in r.json()["error"]  # the detail stays server-side


# --- the bound profile filters rows rather than anonymising them ---------


def test_a_bound_profile_omits_an_existing_document_it_may_not_read(client):
    unbound = _history(client, project="library")
    assert unbound.status_code == 200
    assert len(unbound.json()["history"]) == 6

    r = _history(client, token=BOUND, project="library")
    assert r.status_code == 200
    rows = r.json()["history"]
    # `0004-d` (out of scope by audience, but its row still exists) gets a
    # row for the unbound caller and none for the bound one.
    assert all(row.get("doc_id") != "library/decisions/0004-d" for row in rows)
    assert any(row.get("doc_id") == "library/decisions/0004-d"
              for row in unbound.json()["history"])


# --- round 1: a deleted document's row still respects the bound profile --
#
# security-reviewer and reviewer both raised this: `raw is None` (no index
# row to check scope against) used to build a row unconditionally, before
# `_scoped_doc_row` ever ran. Fixed to fail closed — a document this route
# cannot scope-check is a document a bound caller never sees, matching
# `_scoped_doc_row`'s own rule for a document that still exists.


def test_an_unbound_caller_still_sees_the_deleted_documents_row(client):
    r = _history(client, project="library")
    assert r.status_code == 200
    rows = [row for row in r.json()["history"]
            if row["subject"] == "modify decision 0004 and drop decision 0003"]
    deleted = next(row for row in rows if row["status"] == "deleted")
    assert deleted["doc_id"] is None
    assert deleted["source_ref"] == "projects/library/decisions/0003-c.md"
    assert "title" not in deleted


def test_a_bound_profile_in_an_in_scope_project_gets_no_deleted_rows(client):
    """`BOUND`'s profile (`library/narrow`) does name a rule for `library`
    — the project check passes — but `0003-c` has no index row left for
    `_scoped_doc_row` to apply. Before round 1 this row reached `BOUND`
    anyway; now it is dropped the same way the existing-but-out-of-scope
    `0004-d` already was.
    """
    r = _history(client, token=BOUND, project="library")
    assert r.status_code == 200
    rows = r.json()["history"]
    assert all(row.get("source_ref") !=
               "projects/library/decisions/0003-c.md" for row in rows)
    assert all(row.get("doc_id") != "library/decisions/0003-c" for row in rows)
    # Still gets its own in-scope document's rows.
    assert any(row.get("doc_id") == "library/decisions/0001-a" for row in rows)


def test_a_bound_profile_excluded_from_the_project_gets_no_rows_at_all(client):
    """The concrete path security-reviewer named: `OTHER_BOUND`'s profile
    (`platform/only`) names no rule for `library` at all. Querying
    `project=library` directly used to still answer 200 with `0003-c`'s
    deletion, because the project-membership check and the per-row scope
    check were both unscoped. Both are now scoped to the caller's own
    binding, so this project answers the same 400 an unrecognised one
    does, and no row — deleted or otherwise — is ever built.
    """
    r = _history(client, token=OTHER_BOUND, project="library")
    assert r.status_code == 400
    assert "history" not in r.json()


# --- round 1: the blocking work runs off the event loop -------------------


def test_reference_history_runs_its_blocking_work_through_the_threadpool(
        client, monkeypatch):
    """reviewer: `reference.commit_history`'s `subprocess.run` ran straight
    inside the `async def` route, holding the event loop for up to
    `GIT_LOG_TIMEOUT_SECONDS` on every other request this process serves.
    Fixed by moving the index reads and the git subprocess into
    `_do_reference_history`, run through `run_in_threadpool` the way
    `checkout_reference_document` already does for its own blocking work.
    """
    calls = []
    real_run_in_threadpool = libapp.run_in_threadpool

    async def spy(func, *args, **kwargs):
        calls.append(func)
        return await real_run_in_threadpool(func, *args, **kwargs)

    monkeypatch.setattr(libapp, "run_in_threadpool", spy)
    r = _history(client, project="library")
    assert r.status_code == 200
    assert calls == [libapp._do_reference_history]
