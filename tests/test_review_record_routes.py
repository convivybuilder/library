"""The review-record write route (issue_01M2HS3Q36RE5F82QXRN4Z0D0K), the
Library's half of "All reviews are memory" after the schema
(issue_01M2HS394KTFMRATFK1N3G4ZNB).

Built on a database from the real migration, `test_ledger_routes.py`'s
reason: the behaviour under test includes the uniqueness key and the
`auth = 'run-verified'` CHECK, and a hand-written fixture schema is a second
copy that drifts.

`runs.run_facts` is faked for the route tests below — the auth decision only
needs to see what it returns, not how it gets there. `run_facts` itself is
tested separately, against a real UNIX socket, in its own section: the
failures worth catching there live in the transport, exactly as
`test_runs.py`'s own module docstring says for `/runs`.
"""
from __future__ import annotations

import json
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as libapp
import identity
import projects
import runs
import store

REPO = Path(__file__).resolve().parent.parent
SYSTEM = "system-token"
AGENT = "agent-token"
NARROW = "narrow-token"
H = {"Library-Protocol": "1"}

RUN = "run-aaaa1111"


def _h(token=SYSTEM):
    return {**H, "Authorization": f"Bearer {token}"}


@pytest.fixture
def client(tmp_path, monkeypatch, vector_index):
    db = tmp_path / "reviews.db"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LIBRARY_DB": str(db)})
    assert result.returncode == 0, result.stderr
    vector_index(db)

    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        SYSTEM: {"name": "system", "products": ["*"], "kind": "system"},
        AGENT: {"name": "an-agent", "products": ["*"]},
        NARROW: {"name": "vivy-only", "products": ["vivy"], "kind": "system"}}))
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({
        "_meta": {}, "library": {"repos": ["library"]},
        "vivy": {"repos": ["vivy"]}}))

    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    monkeypatch.setattr(projects, "REGISTRY_PATH", registry)
    projects.reload()
    yield TestClient(libapp.app)
    projects.reload()


@pytest.fixture
def facts(monkeypatch):
    """`runs.run_facts`, faked. Each test seeds `facts[run_id] = {...}`; an
    unseeded run_id answers `{"available": True, "found": False}`, matching
    what the real function answers for a run the Bosun does not recognise.
    """
    seeded: dict[str, dict] = {}
    monkeypatch.setattr(
        runs, "run_facts",
        lambda run_id: seeded.get(run_id, {"available": True, "found": False}))
    return seeded


def _run_verified_facts(**over):
    facts = {"available": True, "found": True, "agent": "coder",
            "repo": "library", "review_sha": "a" * 40, "pr": None,
            "review_role": "reviewer"}
    facts.update(over)
    return facts


def _body(**over):
    body = {"repo_identity": "github:convivy/library", "reviewed_ref": "a" * 40,
           "role": "reviewer", "verdict": "APPROVE"}
    body.update(over)
    return body


def post(client, **over):
    token = over.pop("token", SYSTEM)
    return client.post("/review-records", headers=_h(token), json=_body(**over))


def signal_count(db_path, kind="review_record"):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM signals WHERE kind = ?", (kind,)).fetchone()[0]
    finally:
        conn.close()


def record_count(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM review_records").fetchone()[0]
    finally:
        conn.close()


def version_rows(db_path, record_id):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM review_record_versions WHERE record_id = ?",
            (record_id,)).fetchall()]
    finally:
        conn.close()


@pytest.fixture
def client_with_cold(tmp_path, monkeypatch, vector_index):
    """`client`, plus a real cold tier — for the two tests below that move a
    review-record signal cold and repost at the same version, the case
    `_record_review_signal`'s own best-effort contract is about
    (archives-cold-tier.md, round 2)."""
    db = tmp_path / "reviews.db"
    cold = tmp_path / "cold.db"
    env_vars = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
               "LIBRARY_DB": str(db)}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO, capture_output=True, text=True, env=env_vars)
    assert result.returncode == 0, result.stderr
    result = subprocess.run(
        [sys.executable, "init_cold.py"], cwd=REPO, capture_output=True,
        text=True, env={**env_vars, "LIBRARY_COLD_DB": str(cold)})
    assert result.returncode == 0, result.stderr
    vector_index(db)

    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        SYSTEM: {"name": "system", "products": ["*"], "kind": "system"},
        AGENT: {"name": "an-agent", "products": ["*"]},
        NARROW: {"name": "vivy-only", "products": ["vivy"], "kind": "system"}}))
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({
        "_meta": {}, "library": {"repos": ["library"]},
        "vivy": {"repos": ["vivy"]}}))

    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    monkeypatch.setattr(projects, "REGISTRY_PATH", registry)
    monkeypatch.setenv("LIBRARY_COLD_DB", str(cold))
    projects.reload()
    yield TestClient(libapp.app), cold
    projects.reload()


def _move_cold(db_path, cold_path, sig_id, dedup_key):
    """The same two-phase move `cold_tier.py` and
    `test_archives_cold_tier_reads.py`'s own `_move` use, by hand, so this
    test is about the read/write side rather than the mover's own policy."""
    cols = ",".join(store.TWO_TIER_SIGNAL_COLUMNS)
    conn = sqlite3.connect(db_path)
    conn.execute("ATTACH DATABASE ? AS cold", (str(cold_path),))
    conn.execute(
        f"INSERT INTO cold.signals ({cols}) "
        f"SELECT {cols} FROM main.signals WHERE id = ?", (sig_id,))
    conn.execute(
        "INSERT INTO cold_dedup_keys (dedup_key, signal_id) VALUES (?, ?)",
        (dedup_key, sig_id))
    conn.execute("DELETE FROM main.signals WHERE id = ?", (sig_id,))
    conn.commit()
    conn.close()


def test_a_repost_at_an_unchanged_version_after_the_signal_moves_cold_mints_no_second_signal(
        client_with_cold, facts):
    """Plan §5: "a review-record re-post at an unchanged version after a
    move mints no second signal." The PR body claimed this was covered by
    `insert_signal`'s own dedup test; round 2 found `_record_review_signal`
    has its own wrapper around that call, so it needs its own test."""
    client, cold = client_with_cold
    facts[RUN] = _run_verified_facts()
    first = post(client, run_id=RUN)
    assert first.status_code == 201, first.text
    record_id, version = first.json()["id"], first.json()["version"]
    dedup_key = f"review_record:{record_id}:v{version}"

    conn = sqlite3.connect(store.DB_PATH)
    sig_id = conn.execute(
        "SELECT id FROM signals WHERE dedup_key = ?", (dedup_key,)).fetchone()[0]
    conn.close()
    _move_cold(store.DB_PATH, cold, sig_id, dedup_key)

    second = post(client, run_id=RUN)
    assert second.status_code == 200, second.text
    assert second.json()["outcome"] == "unchanged"
    assert signal_count(store.DB_PATH) == 0, "the hot signal moved cold"
    conn = sqlite3.connect(cold)
    try:
        count = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    finally:
        conn.close()
    assert count == 1, "a repost at the same version must mint no second signal"


def test_a_repost_whose_signal_moved_cold_keeps_the_routes_answer_when_cold_is_gone(
        client_with_cold, facts):
    """Finding 1b (reviewer run-4f4ac20c): `_record_review_signal`'s own
    best-effort contract says a signal-recording failure must not undo
    `POST /review-records`' already-committed answer. Taking the cold file
    away after the move must still answer 200 for the unchanged repost,
    with the record's already-committed row untouched."""
    client, cold = client_with_cold
    facts[RUN] = _run_verified_facts()
    first = post(client, run_id=RUN)
    assert first.status_code == 201, first.text
    record_id, version = first.json()["id"], first.json()["version"]
    dedup_key = f"review_record:{record_id}:v{version}"

    conn = sqlite3.connect(store.DB_PATH)
    sig_id = conn.execute(
        "SELECT id FROM signals WHERE dedup_key = ?", (dedup_key,)).fetchone()[0]
    conn.close()
    _move_cold(store.DB_PATH, cold, sig_id, dedup_key)
    cold.unlink()

    second = post(client, run_id=RUN)
    assert second.status_code == 200, second.text
    assert second.json()["outcome"] == "unchanged"
    assert second.json()["id"] == record_id
    assert record_count(store.DB_PATH) == 1


# --- capability -------------------------------------------------------------

def test_an_agent_lacks_the_capability(client):
    r = post(client, token=AGENT)
    assert r.status_code == 403
    assert "review_record.create" in r.json()["error"]


# --- idempotency and supersede ----------------------------------------------

def test_a_double_post_of_one_review_yields_one_record_one_signal(client, facts):
    facts[RUN] = _run_verified_facts()
    first = post(client, run_id=RUN)
    assert first.status_code == 201, first.text
    assert first.json()["outcome"] == "created"
    assert first.json()["version"] == 1
    assert first.json()["auth"] == "run-verified"

    second = post(client, run_id=RUN)
    assert second.status_code == 200, second.text
    assert second.json()["outcome"] == "unchanged"
    assert second.json()["version"] == 1
    assert second.json()["id"] == first.json()["id"]

    assert record_count(store.DB_PATH) == 1
    assert signal_count(store.DB_PATH) == 1


def test_the_review_signal_carries_a_produced_at_since_can_bound_on(
        client, facts):
    """CQ·207: `produced_at` used to be stored `None` on this kind, which
    `GET /signals?since=` never matches (`_since_clause`'s own docstring). A
    bound just before the post finds the row; a bound just after it does
    not — the shape that breaks again if the write site reverts to `None`."""
    facts[RUN] = _run_verified_facts()
    before = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    created = post(client, run_id=RUN)
    assert created.status_code == 201, created.text
    after = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    since_before = client.get(
        "/signals?kind=review_record&since=" + before, headers=_h()).json()
    assert len(since_before["signals"]) == 1
    assert since_before["signals"][0]["produced_at"] is not None

    since_after = client.get(
        "/signals?kind=review_record&since=" + after, headers=_h()).json()
    assert since_after["signals"] == []


def test_the_linked_signal_is_findable_through_search(client, facts):
    """`review_record` is embedded (library/decisions/0001, revised by
    knowledge#692) because `GET /search`'s `reviews` vault has no vector
    index of its own — it rides the Archives KNN through its linked signal
    (`app._search_review_page`), so a `review_record` signal left
    unembedded is unfindable through search no matter what
    `review_record_backfill.py` linked. Goes through the real routes:
    `POST /review-records` to create the record, then `GET /search` with
    `vault=reviews` for a phrase only its prose carries.

    Confirmed this fails with `review_record` removed from
    `vectors.EMBEDDED_SIGNAL_KINDS`: the linked signal is stamped holding
    zero vectors, `_search_review_page`'s Archives KNN never returns it as
    a candidate, and this assertion fails."""
    facts[RUN] = _run_verified_facts()
    created = post(client, run_id=RUN,
                   prose="the zylofoam gasket review looks solid")
    assert created.status_code == 201, created.text
    assert signal_count(store.DB_PATH) == 1

    r = client.get("/search", headers=_h(),
                   params={"q": "zylofoam gasket", "vault": "reviews"})
    assert r.status_code == 200, r.text
    ids = {row["id"] for row in r.json()["results"]}
    assert created.json()["id"] in ids


def test_a_changed_verdict_on_the_same_run_role_ref_supersedes(client, facts):
    facts[RUN] = _run_verified_facts()
    first = post(client, run_id=RUN, verdict="APPROVE")
    assert first.status_code == 201, first.text
    record_id = first.json()["id"]

    second = post(client, run_id=RUN, verdict="REQUEST_CHANGES")
    assert second.status_code == 200, second.text
    assert second.json()["outcome"] == "superseded"
    assert second.json()["id"] == record_id
    assert second.json()["version"] == 2
    assert second.json()["verdict"] == "REQUEST_CHANGES"

    versions = version_rows(store.DB_PATH, record_id)
    assert len(versions) == 1
    assert versions[0]["version"] == 1
    assert versions[0]["verdict"] == "APPROVE"

    assert record_count(store.DB_PATH) == 1
    assert signal_count(store.DB_PATH) == 2


def test_a_runless_identical_repost_collapses_to_one_record(client):
    first = post(client)
    second = post(client)
    assert first.status_code == 201, first.text
    assert second.status_code == 200, second.text
    assert second.json()["outcome"] == "unchanged"
    assert second.json()["id"] == first.json()["id"]
    assert record_count(store.DB_PATH) == 1


def test_a_runless_changed_verdict_post_creates_a_second_record(client):
    first = post(client, verdict="APPROVE")
    second = post(client, verdict="REQUEST_CHANGES")
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["id"] != second.json()["id"]
    assert record_count(store.DB_PATH) == 2


# --- auth downgrades ---------------------------------------------------------

def test_no_run_is_unverified(client):
    r = post(client)
    assert r.status_code == 201, r.text
    assert r.json()["auth"] == "unverified"
    assert r.json()["auth_reason"] == "no run"


def test_a_caller_claiming_run_verified_with_no_run_is_still_unverified(client):
    r = post(client, auth="run-verified")
    assert r.status_code == 201, r.text
    assert r.json()["auth"] == "unverified"
    assert r.json()["auth_reason"] == "no run"


def test_run_not_found_is_unverified(client, facts):
    facts[RUN] = {"available": True, "found": False}
    r = post(client, run_id=RUN)
    assert r.status_code == 201, r.text
    assert r.json()["auth"] == "unverified"
    assert r.json()["auth_reason"] == "run not found"


def test_a_private_run_is_unverified(client, facts):
    facts[RUN] = {"available": True, "found": True, "private": True}
    r = post(client, run_id=RUN)
    assert r.status_code == 201, r.text
    assert r.json()["auth"] == "unverified"
    assert r.json()["auth_reason"] == "private run"


def test_a_role_mismatch_is_unverified(client, facts):
    facts[RUN] = _run_verified_facts(review_role="security-reviewer")
    r = post(client, run_id=RUN, role="reviewer")
    assert r.status_code == 201, r.text
    assert r.json()["auth"] == "unverified"
    assert r.json()["auth_reason"] == "role mismatch"


def test_a_repo_mismatch_is_unverified(client, facts):
    facts[RUN] = _run_verified_facts(repo="some-other-repo")
    r = post(client, run_id=RUN)
    assert r.status_code == 201, r.text
    assert r.json()["auth"] == "unverified"
    assert r.json()["auth_reason"] == "repo mismatch"


def test_a_review_sha_mismatch_is_unverified(client, facts):
    facts[RUN] = _run_verified_facts(review_sha="b" * 40)
    r = post(client, run_id=RUN, reviewed_ref="a" * 40)
    assert r.status_code == 201, r.text
    assert r.json()["auth"] == "unverified"
    assert r.json()["auth_reason"] == "review_sha mismatch"


def test_run_verified_when_every_fact_matches(client, facts):
    facts[RUN] = _run_verified_facts()
    r = post(client, run_id=RUN)
    assert r.status_code == 201, r.text
    assert r.json()["auth"] == "run-verified"
    assert r.json()["auth_reason"] is None


def test_an_unpinned_review_sha_is_unverified(client, facts):
    facts[RUN] = _run_verified_facts(review_sha="")
    r = post(client, run_id=RUN)
    assert r.status_code == 201, r.text
    assert r.json()["auth"] == "unverified"
    assert r.json()["auth_reason"] == "run not pinned"


def test_an_unknown_run_repo_is_unverified(client, facts):
    facts[RUN] = _run_verified_facts(repo="")
    r = post(client, run_id=RUN)
    assert r.status_code == 201, r.text
    assert r.json()["auth"] == "unverified"
    assert r.json()["auth_reason"] == "run repo unknown"


def test_run_verified_with_a_string_pr_fact_and_a_matching_int_pr_field(
        client, facts):
    facts[RUN] = _run_verified_facts(pr="42")
    r = post(client, run_id=RUN, pr=42)
    assert r.status_code == 201, r.text
    assert r.json()["auth"] == "run-verified"
    assert r.json()["auth_reason"] is None


def test_a_pr_mismatch_is_unverified(client, facts):
    facts[RUN] = _run_verified_facts(pr="42")
    r = post(client, run_id=RUN, pr=43)
    assert r.status_code == 201, r.text
    assert r.json()["auth"] == "unverified"
    assert r.json()["auth_reason"] == "pr mismatch"


def test_a_pr_present_on_the_run_but_omitted_from_the_record_is_unverified(
        client, facts):
    facts[RUN] = _run_verified_facts(pr="42")
    r = post(client, run_id=RUN)
    assert r.status_code == 201, r.text
    assert r.json()["auth"] == "unverified"
    assert r.json()["auth_reason"] == "pr mismatch"


def test_a_pr_absent_from_the_run_but_present_on_the_record_is_unverified(
        client, facts):
    facts[RUN] = _run_verified_facts(pr=None)
    r = post(client, run_id=RUN, pr=42)
    assert r.status_code == 201, r.text
    assert r.json()["auth"] == "unverified"
    assert r.json()["auth_reason"] == "pr mismatch"


def test_run_verified_when_neither_the_run_nor_the_record_names_a_pr(
        client, facts):
    facts[RUN] = _run_verified_facts(pr=None)
    r = post(client, run_id=RUN)
    assert r.status_code == 201, r.text
    assert r.json()["auth"] == "run-verified"
    assert r.json()["auth_reason"] is None


def test_an_unparseable_facts_pr_is_unverified(client, facts):
    facts[RUN] = _run_verified_facts(pr="abc")
    r = post(client, run_id=RUN, pr=42)
    assert r.status_code == 201, r.text
    assert r.json()["auth"] == "unverified"
    assert r.json()["auth_reason"] == "pr mismatch"


def test_a_repost_with_a_mismatched_pr_cannot_downgrade_a_run_verified_record(
        client, facts):
    """The forgery both round-3 reviews found: a genuine `run-verified`
    review, re-posted under the same run/role/commit but a different `pr`,
    used to supersede the row into `unverified` on a PR the run never
    reviewed. Round 7 (issue_01M3WBNHB615DTK9VARCRN9S2Y) found that
    "unverified" landing was itself the hole — it dropped a run-verified
    `REQUEST_CHANGES` from Deck's PR ladder, which counts it only when
    run-verified — so this write is now refused outright, 409, and the
    run-verified record is left exactly as it was: same verdict, same
    auth, same version.
    """
    facts[RUN] = _run_verified_facts(pr="42")
    first = post(client, run_id=RUN, pr=42, verdict="REQUEST_CHANGES")
    assert first.status_code == 201, first.text
    assert first.json()["auth"] == "run-verified"
    assert first.json()["verdict"] == "REQUEST_CHANGES"
    record_id = first.json()["id"]

    second = post(client, run_id=RUN, pr=99, verdict="APPROVE")
    assert second.status_code == 409, second.text
    assert second.json()["existing"]["id"] == record_id
    assert second.json()["existing"]["auth"] == "run-verified"
    assert second.json()["existing"]["verdict"] == "REQUEST_CHANGES"

    conn = sqlite3.connect(store.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            f"SELECT {', '.join(store.REVIEW_RECORD_COLUMNS)} "
            f"FROM review_records WHERE id = ?", (record_id,)).fetchone()
    finally:
        conn.close()
    out = store.review_record_dict(row)
    assert out["pr"] == 42
    assert out["verdict"] == "REQUEST_CHANGES"
    assert out["auth"] == "run-verified"
    assert out["version"] == 1
    assert record_count(store.DB_PATH) == 1


def test_unverified_over_unverified_on_the_same_key_still_supersedes(
        client, facts):
    """The other three auth pairings keep today's outcome
    (issue_01M3WBNHB615DTK9VARCRN9S2Y) — this is `unverified` over
    `unverified`. Both posts name the same run, role and commit, so they
    collide on the same uniqueness key; both mismatch the run's own `pr`
    fact, so both land `unverified`."""
    facts[RUN] = _run_verified_facts(pr="42")
    first = post(client, run_id=RUN, pr=99, verdict="APPROVE")
    assert first.status_code == 201, first.text
    assert first.json()["auth"] == "unverified"
    record_id = first.json()["id"]

    second = post(client, run_id=RUN, pr=100, verdict="REQUEST_CHANGES")
    assert second.status_code == 200, second.text
    assert second.json()["outcome"] == "superseded"
    assert second.json()["id"] == record_id
    assert second.json()["auth"] == "unverified"
    assert second.json()["verdict"] == "REQUEST_CHANGES"
    assert second.json()["version"] == 2


def test_run_verified_over_unverified_on_the_same_key_still_supersedes(
        client, facts):
    """The other three auth pairings keep today's outcome
    (issue_01M3WBNHB615DTK9VARCRN9S2Y) — this is `run-verified` over
    `unverified`, the upgrade case: a first post whose `pr` mismatches
    lands `unverified`, and a second post on the same key whose `pr`
    matches must still be free to supersede it into `run-verified`."""
    facts[RUN] = _run_verified_facts(pr="42")
    first = post(client, run_id=RUN, pr=99, verdict="APPROVE")
    assert first.status_code == 201, first.text
    assert first.json()["auth"] == "unverified"
    record_id = first.json()["id"]

    second = post(client, run_id=RUN, pr=42, verdict="REQUEST_CHANGES")
    assert second.status_code == 200, second.text
    assert second.json()["outcome"] == "superseded"
    assert second.json()["id"] == record_id
    assert second.json()["auth"] == "run-verified"
    assert second.json()["verdict"] == "REQUEST_CHANGES"
    assert second.json()["version"] == 2


# `run-verified` over `run-verified` still superseding, the fourth pairing,
# is already pinned above by
# `test_a_changed_verdict_on_the_same_run_role_ref_supersedes`.


def test_a_git_identity_is_never_run_verified_even_when_every_fact_matches(
        client, facts):
    facts[RUN] = _run_verified_facts(repo="library", review_sha="c" * 40)
    r = post(client, run_id=RUN, repo_identity="git:" + "c" * 40,
             reviewed_ref="c" * 40, product="library")
    assert r.status_code == 201, r.text
    assert r.json()["auth"] == "unverified"
    assert r.json()["auth_reason"] == "git identity cannot be verified against a run"


def test_a_git_identity_with_a_private_run_is_unverified_not_404(client, facts):
    facts[RUN] = {"available": True, "found": True, "private": True}
    r = post(client, run_id=RUN, repo_identity="git:" + "d" * 40,
             product="library")
    assert r.status_code == 201, r.text
    assert r.json()["auth"] == "unverified"
    assert r.json()["auth_reason"] == "private run"


def test_a_git_identity_with_a_run_not_found_is_unverified_not_404(client, facts):
    facts[RUN] = {"available": True, "found": False}
    r = post(client, run_id=RUN, repo_identity="git:" + "e" * 40,
             product="library")
    assert r.status_code == 201, r.text
    assert r.json()["auth"] == "unverified"
    assert r.json()["auth_reason"] == "run not found"


def test_a_git_identity_with_no_product_is_refused(client):
    r = post(client, repo_identity="git:" + "f" * 40)
    assert r.status_code == 400
    assert "product is required" in r.json()["error"]


def test_a_runless_git_post_is_stored_and_reads_back(client):
    r = post(client, repo_identity="git:" + "1" * 40, product="library")
    assert r.status_code == 201, r.text
    assert r.json()["auth"] == "unverified"
    assert r.json()["auth_reason"] == "no run"

    conn = sqlite3.connect(store.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            f"SELECT {', '.join(store.REVIEW_RECORD_COLUMNS)} "
            f"FROM review_records WHERE id = ?", (r.json()["id"],)).fetchone()
    finally:
        conn.close()
    out = store.review_record_dict(row)
    assert out["repo_identity"] == "git:" + "1" * 40
    assert out["product"] == "library"
    assert out["auth"] == "unverified"


def test_the_forged_run_verified_record_the_reviews_found_is_now_unverified(
        client, facts):
    """A real, unpinned review run of `library` (`review_sha == ""`) posted
    against an unrelated `git:` repo and an arbitrary `reviewed_ref` used to
    mint `run-verified` for that arbitrary pair. It must not any more, on
    either of the two independent grounds the round-1 reviews found: the
    `git:` scheme is never `run-verified` regardless of the run, and a
    `git:` post never even reaches this run's facts as a repo match, because
    its product now always comes from the payload.
    """
    facts[RUN] = _run_verified_facts(repo="library", review_sha="")
    r = post(client, run_id=RUN, repo_identity="git:" + "9" * 40,
             reviewed_ref="b" * 40, product="library")
    assert r.status_code == 201, r.text
    assert r.json()["auth"] != "run-verified"
    assert r.json()["auth"] == "unverified"


def test_unreachable_facts_returns_503_and_writes_no_row(client, facts):
    facts[RUN] = {"available": False, "reason": "the Bosun is down"}
    r = post(client, run_id=RUN)
    assert r.status_code == 503, r.text
    assert record_count(store.DB_PATH) == 0


# --- product scope ------------------------------------------------------------

def test_an_unknown_github_repo_is_not_found(client):
    r = post(client, repo_identity="github:convivy/no-such-repo")
    assert r.status_code == 404


def test_a_mismatched_payload_product_is_refused(client):
    r = post(client, repo_identity="github:convivy/library", product="vivy")
    assert r.status_code == 400
    assert "product" in r.json()["error"]


def test_a_caller_out_of_scope_is_not_found(client):
    r = post(client, token=NARROW, repo_identity="github:convivy/library")
    assert r.status_code == 404


# --- validation ---------------------------------------------------------------

def test_a_bad_repo_identity_is_refused(client):
    r = post(client, repo_identity="not-a-valid-identity")
    assert r.status_code == 400


def test_a_bad_reviewed_ref_is_refused(client):
    r = post(client, reviewed_ref="too-short")
    assert r.status_code == 400


def test_an_oversized_field_is_refused(client):
    r = post(client, role="x" * (store.MAX_REVIEW_ROLE + 1))
    assert r.status_code == 400
    assert "longer than" in r.json()["error"]


@pytest.mark.parametrize("prose", [0, 0.0, False, [], {}])
def test_a_falsy_non_string_prose_is_refused_and_nothing_is_written(client, prose):
    """`or ""` once stored each of these as empty prose with a 201
    (issue_01M3XPJX701JSV60GNM5ZK0NJ0)."""
    r = post(client, prose=prose)
    assert r.status_code == 400
    assert r.json()["error"] == "prose must be a string"
    assert record_count(store.DB_PATH) == 0
    assert signal_count(store.DB_PATH) == 0


@pytest.mark.parametrize("prose", [None, ""])
def test_an_absent_or_null_prose_is_stored_empty(client, prose):
    r = post(client, prose=prose)
    assert r.status_code == 201, r.text
    assert r.json()["prose"] == ""


def test_an_unknown_auth_value_is_refused(client):
    r = post(client, auth="bogus-value")
    assert r.status_code == 400


def test_an_oversized_finding_is_refused(client):
    oversized = [{"summary": "x" * (store.MAX_TEXT + 1)}]
    r = post(client, findings=oversized)
    assert r.status_code == 400
    assert "findings[0]" in r.json()["error"]


# --- round trip ----------------------------------------------------------------

def test_findings_prose_paths_doc_ids_item_ids_survive_the_round_trip(client):
    findings = [{"severity": "high", "location": "app.py:10", "text": "a finding"}]
    r = post(client, findings=findings, prose="a long review",
            paths=["app.py", "store.py"], doc_ids=["projects/platform/decisions/0001"],
            item_ids=["issue_01ABC"])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["findings"] == findings
    assert body["prose"] == "a long review"
    assert body["paths"] == ["app.py", "store.py"]
    assert body["doc_ids"] == ["projects/platform/decisions/0001"]
    assert body["item_ids"] == ["issue_01ABC"]


# =============================================================================
# runs.run_facts, against a real UNIX socket — the failures worth catching
# here live in the transport, matching test_runs.py's own reasoning for
# `/runs`.
# =============================================================================

class FakeFactsServer:
    """A stand-in for the Bosun's `/runs/<id>/facts`, speaking the same
    one-shot HTTP-over-UNIX shape `test_runs.py`'s `FakeRegister` does."""

    def __init__(self, path, payload=None, *, behaviour="ok"):
        self.path = str(path)
        self.payload = payload
        self.behaviour = behaviour
        self.reads = 0
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(self.path)
        self._sock.listen(8)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            self.reads += 1
            try:
                conn.recv(65536)
                if self.behaviour == "garbage":
                    conn.sendall(b"HTTP/1.0 200 OK\r\n\r\nnot json at all")
                elif self.behaviour == "not-found":
                    conn.sendall(b"HTTP/1.0 404 Not Found\r\n\r\n")
                else:
                    conn.sendall(b"HTTP/1.0 200 OK\r\n\r\n" +
                                 json.dumps(self.payload).encode())
            except OSError:
                pass
            finally:
                conn.close()

    def close(self):
        self._stop.set()
        self._sock.close()


@pytest.fixture
def sock_dir():
    """A short directory for the socket — macOS caps an `AF_UNIX` path near
    104 characters, and `tmp_path` is comfortably past that on its own."""
    path = tempfile.mkdtemp(prefix="librrfacts-", dir="/tmp")
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def test_run_facts_refuses_a_malformed_run_id_without_touching_the_socket(
        sock_dir, monkeypatch):
    monkeypatch.setattr(runs, "SOCKET_PATH", Path(sock_dir) / "r.sock")
    reg = FakeFactsServer(Path(sock_dir) / "r.sock", {"found": True})
    try:
        assert runs.run_facts("not-a-run-id") == {"available": True, "found": False}
        assert reg.reads == 0
    finally:
        reg.close()


def test_run_facts_returns_available_false_on_no_socket(monkeypatch, sock_dir):
    monkeypatch.setattr(runs, "SOCKET_PATH", Path(sock_dir) / "missing.sock")
    result = runs.run_facts(RUN)
    assert result["available"] is False
    assert "no run register" in result["reason"]


def test_run_facts_reports_a_found_run(sock_dir, monkeypatch):
    monkeypatch.setattr(runs, "SOCKET_PATH", Path(sock_dir) / "r.sock")
    reg = FakeFactsServer(Path(sock_dir) / "r.sock", {
        "found": True, "agent": "coder", "repo": "library",
        "review_sha": "a" * 40, "pr": 42, "review_role": "reviewer"})
    try:
        assert runs.run_facts(RUN) == {
            "available": True, "found": True, "agent": "coder",
            "repo": "library", "review_sha": "a" * 40, "pr": 42,
            "review_role": "reviewer"}
    finally:
        reg.close()


def test_run_facts_reports_not_found(sock_dir, monkeypatch):
    monkeypatch.setattr(runs, "SOCKET_PATH", Path(sock_dir) / "r.sock")
    reg = FakeFactsServer(Path(sock_dir) / "r.sock", {"found": False})
    try:
        assert runs.run_facts(RUN) == {"available": True, "found": False}
    finally:
        reg.close()


def test_run_facts_a_private_runs_facts_carry_nothing_else(sock_dir, monkeypatch):
    """The allowlist is not re-opened downstream: a private run's payload
    naming `repo` and `agent` alongside `private: true` must not leak them —
    the same discipline `test_runs.py` pins for `/runs` itself."""
    monkeypatch.setattr(runs, "SOCKET_PATH", Path(sock_dir) / "r.sock")
    reg = FakeFactsServer(Path(sock_dir) / "r.sock", {
        "found": True, "private": True, "repo": "CFO", "agent": "secret-agent"})
    try:
        assert runs.run_facts(RUN) == {"available": True, "found": True,
                                       "private": True}
    finally:
        reg.close()


def test_run_facts_degrades_on_a_bad_body(sock_dir, monkeypatch):
    monkeypatch.setattr(runs, "SOCKET_PATH", Path(sock_dir) / "r.sock")
    reg = FakeFactsServer(Path(sock_dir) / "r.sock", None, behaviour="garbage")
    try:
        result = runs.run_facts(RUN)
        assert result["available"] is False
        assert "not JSON" in result["reason"]
    finally:
        reg.close()
