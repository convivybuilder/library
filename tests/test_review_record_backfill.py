"""The one-time backfill of unverified review records from stamped GitHub
review comments (issue_01M2HS4DY0M5S5D4ND4V76VXJ5). Built on a database from
the real migration, `test_review_records_store.py`'s reason: the behaviour
under test includes the uniqueness key and the supersede mechanics
`store.write_review_record` already owns, and a hand-written fixture schema
is a second copy of that contract that can drift from it.

Every assertion here runs against a temporary database, never
`~/.library/library.db` — the brief for this backfill is explicit that a
rehearsal against a copy of the live database is a separate, later step
this test suite does not perform.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import projects
import review_record_backfill as backfill
import store

REPO = Path(__file__).resolve().parent.parent


def make_db(tmp_path):
    db = tmp_path / "library.db"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
             "LIBRARY_DB": str(db)})
    assert result.returncode == 0, result.stderr
    return db


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = make_db(tmp_path)
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({
        "_meta": {}, "library": {"repos": ["library"]},
        "vivy": {"repos": ["vivy"]}}))
    monkeypatch.setattr(store, "DB_PATH", path)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    monkeypatch.setattr(projects, "REGISTRY_PATH", registry)
    projects.reload()
    yield path
    projects.reload()


def stamp_header(*, pr=42, repo="library", reviewed_commit="a" * 40,
                 verdict="APPROVE", agent="reviewer", run_id=""):
    """The exact shape `agent-platform`'s `review_protocol.render_header`
    writes — `run-id:` is always present, empty for a runless review."""
    return (
        "<!-- agentctl-review\n"
        "v: 1\n"
        f"pr: {pr}\n"
        f"repo: {repo}\n"
        f"reviewed-commit: {reviewed_commit}\n"
        f"verdict: {verdict}\n"
        f"agent: {agent}\n"
        f"run-id: {run_id}\n"
        "-->"
    )


def _insert(conn, id_, *, repo="library", pr=42, reviewed_commit="a" * 40,
           verdict="APPROVE", agent="reviewer", run_id="", prose="looks fine",
           comment_id="1000", produced_at="2026-06-20 12:00:00", body=None):
    if body is None:
        body = stamp_header(pr=pr, repo=repo, reviewed_commit=reviewed_commit,
                            verdict=verdict, agent=agent,
                            run_id=run_id) + f"\n\n{prose}"
    refs = {"url": f"https://github.com/convivy/{repo}/pull/{pr}"
                   f"#issuecomment-{comment_id}"}
    conn.execute(
        "INSERT INTO signals (id, source, kind, actor, ingested_by, product, "
        "refs, body, meta, privacy_class, produced_at, ingested_at) "
        "VALUES (?, 'github_review', 'pr_comment', ?, 'orient-github-capture', "
        "?, ?, ?, '{}', 'internal', ?, ?)",
        (id_, agent, repo, json.dumps(refs), body, produced_at, produced_at))


def _conn(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _records(path):
    conn = _conn(path)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM review_records ORDER BY created_at").fetchall()]
    finally:
        conn.close()


def _versions(path, record_id):
    conn = _conn(path)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM review_record_versions WHERE record_id = ? "
            "ORDER BY version", (record_id,)).fetchall()]
    finally:
        conn.close()


def _signal_count(path, **where):
    conn = _conn(path)
    try:
        clauses = " AND ".join(f"{k} = ?" for k in where)
        sql = "SELECT COUNT(*) FROM signals"
        if clauses:
            sql += f" WHERE {clauses}"
        return conn.execute(sql, tuple(where.values())).fetchone()[0]
    finally:
        conn.close()


def test_a_stamped_comment_becomes_one_unverified_record(db):
    conn = _conn(db)
    _insert(conn, "sig_a", comment_id="5001", produced_at="2026-06-20 10:00:00")
    conn.commit()
    conn.close()

    assert backfill.main(["--write"]) == 0

    records = _records(db)
    assert len(records) == 1
    rec = records[0]
    assert rec["auth"] == "unverified"
    assert rec["auth_reason"] == "backfilled comment"
    assert rec["source_comment_id"] == "5001"
    assert rec["signal_id"] == "sig_a"
    assert rec["repo_identity"] == "github:convivy/library"
    assert rec["product"] == "library"
    assert json.loads(rec["findings"]) == []
    assert "looks fine" in rec["prose"]
    assert "agentctl-review" not in rec["prose"]

    # No new signal — the backfilled record links the comment already
    # captured, and nothing else was written to the Archives.
    assert _signal_count(db) == 1
    assert _signal_count(db, kind="review_record") == 0


def test_two_comments_sharing_a_key_posted_in_order_leave_the_later_current(db):
    # The LATER comment (by produced_at) is inserted FIRST, under an id that
    # sorts lexicographically SMALLER than the earlier comment's — so both
    # physical insertion order and an `id ASC` tiebreak put it first too.
    # Only `ORDER BY produced_at ASC` recovers the correct oldest-first
    # order; dropping or reversing it would leave this test failing instead
    # of passing by construction.
    conn = _conn(db)
    _insert(conn, "sig_0_second", run_id="run-shared", verdict="REQUEST_CHANGES",
            prose="actually no", comment_id="6002",
            produced_at="2026-06-20 10:02:00")
    _insert(conn, "sig_9_first", run_id="run-shared", verdict="APPROVE",
            prose="clean", comment_id="6001", produced_at="2026-06-20 10:00:00")
    conn.commit()
    conn.close()

    assert backfill.main(["--write"]) == 0

    records = _records(db)
    assert len(records) == 1
    rec = records[0]
    assert rec["verdict"] == "REQUEST_CHANGES"
    assert rec["version"] == 2
    assert rec["signal_id"] == "sig_0_second"

    versions = _versions(db, rec["id"])
    assert len(versions) == 1
    assert versions[0]["verdict"] == "APPROVE"
    assert versions[0]["version"] == 1
    assert versions[0]["signal_id"] == "sig_9_first"


def test_an_identical_repeat_is_a_no_op(db):
    conn = _conn(db)
    _insert(conn, "sig_x", run_id="run-dup", verdict="APPROVE", prose="same text",
            comment_id="7001", produced_at="2026-06-20 10:00:00")
    _insert(conn, "sig_y", run_id="run-dup", verdict="APPROVE", prose="same text",
            comment_id="7002", produced_at="2026-06-20 10:05:00")
    conn.commit()
    conn.close()

    assert backfill.main(["--write"]) == 0

    records = _records(db)
    assert len(records) == 1
    assert records[0]["version"] == 1
    assert records[0]["signal_id"] == "sig_x"
    assert _versions(db, records[0]["id"]) == []


def test_a_runless_stamp_gets_a_content_identity_key(db):
    conn = _conn(db)
    _insert(conn, "sig_r1", run_id="", verdict="APPROVE", prose="fine as is",
            comment_id="8001", produced_at="2026-06-20 10:00:00")
    conn.commit()
    conn.close()

    assert backfill.main(["--write"]) == 0

    records = _records(db)
    assert len(records) == 1
    assert records[0]["run_id"] is None
    assert records[0]["identity_key"].startswith("content:")


def test_two_runless_stamps_differing_in_verdict_produce_two_records(db):
    conn = _conn(db)
    _insert(conn, "sig_r2", run_id="", verdict="APPROVE", prose="ok",
            comment_id="8101", produced_at="2026-06-20 10:00:00")
    _insert(conn, "sig_r3", run_id="", verdict="REQUEST_CHANGES", prose="ok",
            comment_id="8102", produced_at="2026-06-20 10:01:00")
    conn.commit()
    conn.close()

    assert backfill.main(["--write"]) == 0

    records = _records(db)
    assert len(records) == 2
    assert {r["verdict"] for r in records} == {"APPROVE", "REQUEST_CHANGES"}
    assert all(r["version"] == 1 for r in records)
    assert len({r["identity_key"] for r in records}) == 2


def test_a_repo_that_does_not_resolve_is_skipped_and_counted(db, capsys):
    conn = _conn(db)
    _insert(conn, "sig_unknown", repo="not-a-real-repo", comment_id="9001",
            produced_at="2026-06-20 10:00:00")
    conn.commit()
    conn.close()

    assert backfill.main(["--write"]) == 0
    err = capsys.readouterr().err
    assert "sig_unknown" in err
    assert "repo does not resolve" in err
    assert _records(db) == []


def test_an_owner_qualified_repo_resolves_the_same_as_the_bare_name(db):
    conn = _conn(db)
    _insert(conn, "sig_qualified", repo="convivy/library", comment_id="9301",
            produced_at="2026-06-20 10:00:00")
    conn.commit()
    conn.close()

    assert backfill.main(["--write"]) == 0

    records = _records(db)
    assert len(records) == 1
    assert records[0]["repo_identity"] == "github:convivy/library"
    assert records[0]["product"] == "library"


def test_an_unparseable_stamp_is_skipped_and_counted(db, capsys):
    conn = _conn(db)
    body = "<!-- agentctl-review\nv: 1\npr: 1\nrepo: library\n-->\nno verdict here"
    _insert(conn, "sig_bad", comment_id="9101",
            produced_at="2026-06-20 10:00:00", body=body)
    conn.commit()
    conn.close()

    assert backfill.main(["--write"]) == 0
    err = capsys.readouterr().err
    assert "sig_bad" in err
    assert "unparseable stamp" in err
    assert _records(db) == []


def test_an_invalid_reviewed_ref_is_skipped_and_counted(db, capsys):
    conn = _conn(db)
    _insert(conn, "sig_badref", reviewed_commit="not-a-hex-sha", comment_id="9201",
            produced_at="2026-06-20 10:00:00")
    conn.commit()
    conn.close()

    assert backfill.main(["--write"]) == 0
    err = capsys.readouterr().err
    assert "sig_badref" in err
    assert "invalid reviewed_ref" in err
    assert _records(db) == []


def test_a_second_write_pass_writes_nothing_new(db):
    conn = _conn(db)
    _insert(conn, "sig_once", comment_id="1101", produced_at="2026-06-20 10:00:00")
    conn.commit()
    conn.close()

    assert backfill.main(["--write"]) == 0
    first = _records(db)
    assert len(first) == 1

    assert backfill.main(["--write"]) == 0
    second = _records(db)
    assert second == first


def test_a_resumed_pass_finishes_a_half_linked_record(db, capsys):
    """Simulates the crash window between `write_review_record`'s commit and
    `set_review_record_signal`'s own, independent one — the record is stored
    with `signal_id` NULL, and content hasn't changed, so a resumed pass must
    still finish the link rather than reading `unchanged` as "nothing to do".
    Fails against the outcome-gated check this replaces: that check skips the
    link whenever `write_review_record` answers `unchanged`."""
    conn = _conn(db)
    _insert(conn, "sig_half", comment_id="1301", produced_at="2026-06-20 10:00:00")
    conn.commit()
    conn.close()

    assert backfill.main(["--write"]) == 0
    first = _records(db)
    assert len(first) == 1
    assert first[0]["signal_id"] == "sig_half"
    assert first[0]["version"] == 1

    conn = _conn(db)
    conn.execute("UPDATE review_records SET signal_id = NULL WHERE id = ?",
                (first[0]["id"],))
    conn.commit()
    conn.close()

    assert backfill.main(["--write"]) == 0
    second = _records(db)
    assert len(second) == 1
    assert second[0]["id"] == first[0]["id"]
    assert second[0]["signal_id"] == "sig_half"
    assert second[0]["version"] == 1
    assert "relinked 1" in capsys.readouterr().err


def _write_verified_record(*, repo_identity="github:convivy/library",
                           product="library", pr=42, reviewed_ref="a" * 40,
                           role="reviewer", run_id="run-already-verified",
                           verdict="APPROVE", prose="a real, run-verified review"):
    """Insert a record the same way `POST /review-records` would for a
    genuine dispatched review, sharing the exact uniqueness key
    (`repo_identity`, `identity_key`, `role`, `reviewed_ref`) a stamped
    comment for the same review would land on."""
    row = {
        "repo_identity": repo_identity, "product": product, "pr": pr,
        "reviewed_ref": reviewed_ref, "role": role, "run_id": run_id,
        "verdict": verdict, "findings": [], "prose": prose, "paths": [],
        "doc_ids": [], "item_ids": [], "auth": store.REVIEW_AUTH_RUN_VERIFIED,
        "auth_reason": "run dispatch-8be20af0 in the Bosun's register",
        "source_comment_id": None,
    }
    _status, _outcome, out = store.write_review_record(row)
    return out


def test_a_run_verified_record_is_left_alone_by_a_differing_comment(db, capsys):
    verified = _write_verified_record()
    conn = _conn(db)
    _insert(conn, "sig_comment_copy", run_id="run-already-verified",
            verdict="REQUEST_CHANGES", prose="a differently-worded copy of "
            "the same review", comment_id="4001",
            produced_at="2026-06-20 11:00:00")
    conn.commit()
    conn.close()

    assert backfill.main(["--write"]) == 0

    records = _records(db)
    assert len(records) == 1
    rec = records[0]
    assert rec["id"] == verified["id"]
    assert rec["auth"] == "run-verified"
    assert rec["version"] == 1
    assert rec["verdict"] == "APPROVE"
    assert _versions(db, rec["id"]) == []

    err = capsys.readouterr().err
    assert "left 1 verified" in err
    assert "sig_comment_copy" in err


def test_dry_run_also_leaves_a_run_verified_record_alone(db, capsys):
    verified = _write_verified_record()
    conn = _conn(db)
    _insert(conn, "sig_comment_copy2", run_id="run-already-verified",
            verdict="REQUEST_CHANGES", prose="another differently-worded "
            "copy", comment_id="4002", produced_at="2026-06-20 11:05:00")
    conn.commit()
    conn.close()

    assert backfill.main([]) == 0

    records = _records(db)
    assert len(records) == 1
    assert records[0]["id"] == verified["id"]
    assert records[0]["auth"] == "run-verified"
    assert records[0]["version"] == 1

    out = capsys.readouterr()
    assert "would write" not in out.out
    assert "would supersede" not in out.out
    assert "left 1 verified" in out.err


def test_an_unverified_record_is_still_superseded_by_a_differing_comment(db):
    conn = _conn(db)
    _insert(conn, "sig_first", run_id="run-unverified-chain", verdict="APPROVE",
            prose="first pass", comment_id="4101",
            produced_at="2026-06-20 10:00:00")
    conn.commit()
    conn.close()

    assert backfill.main(["--write"]) == 0
    first = _records(db)
    assert len(first) == 1
    assert first[0]["auth"] == "unverified"
    assert first[0]["version"] == 1

    conn = _conn(db)
    _insert(conn, "sig_second", run_id="run-unverified-chain",
            verdict="REQUEST_CHANGES", prose="second pass, differs",
            comment_id="4102", produced_at="2026-06-20 10:05:00")
    conn.commit()
    conn.close()

    assert backfill.main(["--write"]) == 0
    second = _records(db)
    assert len(second) == 1
    assert second[0]["id"] == first[0]["id"]
    assert second[0]["verdict"] == "REQUEST_CHANGES"
    assert second[0]["version"] == 2
    assert len(_versions(db, second[0]["id"])) == 1


def test_a_second_pass_over_a_shared_key_group_adds_no_further_churn(db, capsys):
    """Round 5's defect: two comments sharing a key with differing verdicts,
    replayed on a second `--write` pass, must not read the OLDER comment as
    differing from what the record now holds and supersede it again — the
    version and the version-history row count must be identical after both
    passes, and the second pass must report `already current`, not
    `superseded`."""
    conn = _conn(db)
    _insert(conn, "sig_older", run_id="run-shared-2x", verdict="APPROVE",
            prose="clean", comment_id="6101", produced_at="2026-06-20 10:00:00")
    _insert(conn, "sig_newer", run_id="run-shared-2x", verdict="REQUEST_CHANGES",
            prose="actually no", comment_id="6102",
            produced_at="2026-06-20 10:02:00")
    conn.commit()
    conn.close()

    assert backfill.main(["--write"]) == 0
    capsys.readouterr()
    first = _records(db)
    assert len(first) == 1
    assert first[0]["version"] == 2
    assert first[0]["verdict"] == "REQUEST_CHANGES"
    assert len(_versions(db, first[0]["id"])) == 1

    assert backfill.main(["--write"]) == 0
    err = capsys.readouterr().err
    second = _records(db)
    assert len(second) == 1
    assert second[0]["id"] == first[0]["id"]
    assert second[0]["version"] == 2
    assert second[0]["verdict"] == "REQUEST_CHANGES"
    assert len(_versions(db, second[0]["id"])) == 1
    # sig_older (strictly older than the stored `updated_at`) lands in
    # `already_current` from `plan()`'s own pre-filter. sig_newer's own
    # `produced_at` ties the stored `updated_at`, so `plan()` still hands it
    # to the write loop — but it is a re-run of the exact comment that
    # already produced this content, so `store.write_review_record`'s own
    # `only_if_after` guard (round 6) now answers `already_current` for it
    # too, at write time, rather than falling through to a content compare.
    # Either way, neither comment is `superseded`.
    assert "already current 2" in err
    assert "superseded 0" in err


def test_a_second_pass_over_a_three_comment_group_adds_no_further_churn(db, capsys):
    conn = _conn(db)
    _insert(conn, "sig_g1", run_id="run-shared-3x", verdict="APPROVE",
            prose="first", comment_id="6201", produced_at="2026-06-20 10:00:00")
    _insert(conn, "sig_g2", run_id="run-shared-3x", verdict="REQUEST_CHANGES",
            prose="second", comment_id="6202", produced_at="2026-06-20 10:02:00")
    _insert(conn, "sig_g3", run_id="run-shared-3x", verdict="APPROVE",
            prose="third", comment_id="6203", produced_at="2026-06-20 10:04:00")
    conn.commit()
    conn.close()

    assert backfill.main(["--write"]) == 0
    capsys.readouterr()
    first = _records(db)
    assert len(first) == 1
    version_after_pass_1 = first[0]["version"]
    versions_after_pass_1 = len(_versions(db, first[0]["id"]))

    assert backfill.main(["--write"]) == 0
    err = capsys.readouterr().err
    second = _records(db)
    assert len(second) == 1
    assert second[0]["id"] == first[0]["id"]
    assert second[0]["version"] == version_after_pass_1
    assert len(_versions(db, second[0]["id"])) == versions_after_pass_1
    # sig_g1 and sig_g2 (strictly older than the stored `updated_at`) land
    # in `already_current` from `plan()`'s own pre-filter; sig_g3
    # (produced_at equal to it — a re-run of the comment the record already
    # reflects) is handed to the write loop, where `only_if_after`'s
    # position check (round 6) also answers `already_current` for it,
    # rather than falling through to a content compare.
    assert "already current 3" in err
    assert "superseded 0" in err


def test_a_second_pass_over_an_identical_timestamp_tie_adds_no_churn(db, capsys):
    """Round 6's tie defect: GitHub timestamps are second-precision, so two
    DIFFERENT comments in a differing-content group can share one
    `produced_at`. `already_current`'s old strict `<` comparison against a
    single timestamp never catches this — both comments fall through to a
    content compare on every pass, and (since each differs from the other)
    each re-supersedes the other forever. Fails against that comparison: a
    second `--write` pass would add two more version rows for this pair,
    alternating which one is current."""
    conn = _conn(db)
    _insert(conn, "sig_tie_a", run_id="run-tie", verdict="APPROVE",
            prose="first take", comment_id="6301",
            produced_at="2026-06-20 10:00:00")
    _insert(conn, "sig_tie_b", run_id="run-tie", verdict="REQUEST_CHANGES",
            prose="second take", comment_id="6302",
            produced_at="2026-06-20 10:00:00")
    conn.commit()
    conn.close()

    assert backfill.main(["--write"]) == 0
    capsys.readouterr()
    first = _records(db)
    assert len(first) == 1
    # sig_tie_b sorts after sig_tie_a under the load query's own
    # `id ASC` tie-break ("sig_tie_a" < "sig_tie_b"), so it is the later
    # comment in the order the record is meant to reflect.
    assert first[0]["signal_id"] == "sig_tie_b"
    assert first[0]["verdict"] == "REQUEST_CHANGES"
    version_after_pass_1 = first[0]["version"]
    versions_after_pass_1 = len(_versions(db, first[0]["id"]))
    assert version_after_pass_1 == 2
    assert versions_after_pass_1 == 1

    assert backfill.main(["--write"]) == 0
    err = capsys.readouterr().err
    second = _records(db)
    assert len(second) == 1
    assert second[0]["id"] == first[0]["id"]
    assert second[0]["signal_id"] == "sig_tie_b"
    assert second[0]["version"] == version_after_pass_1
    assert len(_versions(db, second[0]["id"])) == versions_after_pass_1
    assert "already current 2" in err
    assert "superseded 0" in err


def test_a_second_pass_over_a_three_comment_group_with_a_tied_pair_adds_no_churn(
        db, capsys):
    """The same tie defect inside a larger group: two of three comments
    share a `produced_at`, the third does not."""
    conn = _conn(db)
    _insert(conn, "sig_tri_a", run_id="run-tri", verdict="APPROVE",
            prose="first", comment_id="6401", produced_at="2026-06-20 10:00:00")
    _insert(conn, "sig_tri_b", run_id="run-tri", verdict="REQUEST_CHANGES",
            prose="second, same timestamp as the first", comment_id="6402",
            produced_at="2026-06-20 10:00:00")
    _insert(conn, "sig_tri_c", run_id="run-tri", verdict="APPROVE",
            prose="third, minutes later", comment_id="6403",
            produced_at="2026-06-20 10:05:00")
    conn.commit()
    conn.close()

    assert backfill.main(["--write"]) == 0
    capsys.readouterr()
    first = _records(db)
    assert len(first) == 1
    assert first[0]["signal_id"] == "sig_tri_c"
    version_after_pass_1 = first[0]["version"]
    versions_after_pass_1 = len(_versions(db, first[0]["id"]))
    assert version_after_pass_1 == 3
    assert versions_after_pass_1 == 2

    assert backfill.main(["--write"]) == 0
    err = capsys.readouterr().err
    second = _records(db)
    assert len(second) == 1
    assert second[0]["id"] == first[0]["id"]
    assert second[0]["version"] == version_after_pass_1
    assert len(_versions(db, second[0]["id"])) == versions_after_pass_1
    assert "already current 3" in err
    assert "superseded 0" in err


def test_a_live_verification_landing_during_the_run_survives_the_backfill(
        db, monkeypatch, capsys):
    """Round 6's race: `plan()` reads a key's `auth` once, up front, and the
    write loop can still be working through that key's row minutes later.
    A genuine `POST /review-records` write marking the same key
    `run-verified` in that window must not be superseded by the backfill's
    own later write for it. Fails against a plan()-time-only check: this
    comment's key carries no record at all when `plan()` runs, so the
    round-4/5 pre-filter never sees anything to protect — only a check made
    fresh at write time, inside `store.write_review_record`'s own
    transaction, can catch a record that starts existing after `plan()` has
    already run."""
    conn = _conn(db)
    _insert(conn, "sig_race", run_id="run-race", verdict="REQUEST_CHANGES",
            prose="a differently-worded copy of the same review",
            comment_id="5101", produced_at="2026-06-20 10:00:00")
    conn.commit()
    conn.close()

    real_write = store.write_review_record
    calls = []

    def racing_write(row, **kwargs):
        if not calls:
            # A genuine, independently-authenticated review lands for this
            # exact uniqueness key between `plan()`'s read (which saw no
            # record at all for it) and this item's turn in the write loop.
            real_write({
                "repo_identity": row["repo_identity"], "product": "library",
                "pr": 42, "reviewed_ref": row["reviewed_ref"],
                "role": row["role"], "run_id": row["run_id"],
                "verdict": "APPROVE", "findings": [],
                "prose": "the real, run-verified review", "paths": [],
                "doc_ids": [], "item_ids": [],
                "auth": store.REVIEW_AUTH_RUN_VERIFIED,
                "auth_reason": "run dispatch-race in the Bosun's register",
                "source_comment_id": None,
            })
        calls.append(row)
        return real_write(row, **kwargs)

    monkeypatch.setattr(store, "write_review_record", racing_write)

    assert backfill.main(["--write"]) == 0

    records = _records(db)
    assert len(records) == 1
    rec = records[0]
    assert rec["auth"] == "run-verified"
    assert rec["verdict"] == "APPROVE"
    assert rec["version"] == 1
    assert _versions(db, rec["id"]) == []
    assert "left 1 verified" in capsys.readouterr().err


def test_write_without_writes_enabled_refuses_cleanly(db, monkeypatch, capsys):
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    conn = _conn(db)
    _insert(conn, "sig_refused", comment_id="4201",
            produced_at="2026-06-20 10:00:00")
    conn.commit()
    conn.close()

    assert backfill.main(["--write"]) == 2

    err = capsys.readouterr().err
    assert "LIBRARY_WRITES_ENABLED" in err
    assert _records(db) == []


def test_dry_run_writes_nothing_at_all(db, capsys):
    conn = _conn(db)
    _insert(conn, "sig_dry", comment_id="1201", produced_at="2026-06-20 10:00:00")
    conn.commit()
    conn.close()

    assert backfill.main([]) == 0
    assert "would write" in capsys.readouterr().out
    assert _records(db) == []
    assert _signal_count(db) == 1
