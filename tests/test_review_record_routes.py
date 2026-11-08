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
        NARROW: {"name": "fennick-only", "products": ["fennick"], "kind": "system"}}))
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({
        "_meta": {}, "library": {"repos": ["library"]},
        "fennick": {"repos": ["fennick", "qh-shared"]},
        "quillhouse": {"repos": ["quillhouse-site", "qh-shared"], "private": True},
        "ledgerwick": {"repos": [], "private": True}}))

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

    **Models `include_private_facts` the way the real function does**
    (CQ·237): a run seeded with `private: True` reads as
    `{"available", "found", "private"}` alone unless the keyword is passed.
    A fake that handed every caller full facts is how round 1 shipped a
    route that fetched a private run's facts for any post (security-reviewer
    and reviewer, CQ·237 round 1). `seeded.calls` records each call's
    keyword so a test can assert which path the route took.
    """
    class Seeded(dict):
        calls: list

    seeded = Seeded()
    seeded.calls = []

    def fake(run_id, *, include_private_facts=False):
        seeded.calls.append((run_id, include_private_facts))
        got = seeded.get(run_id, {"available": True, "found": False})
        if got.get("private") and not include_private_facts:
            return {"available": True, "found": True, "private": True}
        return got

    monkeypatch.setattr(runs, "run_facts", fake)
    return seeded


def _run_verified_facts(**over):
    """Defaults to matching `_body()`'s own defaults (`verdict="APPROVE"`,
    no `pr`) through `posted_verdict`/`posted_pr`, so a test that overrides
    neither still reaches `run-verified` under CQ·217's rule without having
    to restate them."""
    facts = {"available": True, "found": True, "agent": "coder",
            "repo": "library", "review_sha": "a" * 40, "pr": None,
            "review_role": "reviewer", "posted_verdict": "APPROVE",
            "posted_pr": None}
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
        NARROW: {"name": "fennick-only", "products": ["fennick"], "kind": "system"}}))
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({
        "_meta": {}, "library": {"repos": ["library"]},
        "fennick": {"repos": ["fennick"]}}))

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


# --- private crews (ADR-0043, CQ·237, issue_01M5QE987V342E9NX422BTWW8J) -----


@pytest.mark.parametrize("field,value", [
    ("prose", "a review of the OpenAI key fix"),
    ("findings", [{"severity": "blocking", "note": "x"}]),
    ("paths", ["app.py"]),
    ("doc_ids", ["projects/platform/decisions/0001"]),
    ("item_ids", ["issue_01ABC"]),
    ("source_comment_id", "3061234567"),
])
def test_a_private_githubs_text_field_is_refused_400_naming_the_field(
        client, field, value):
    r = post(client, repo_identity="github:examplecorp/quillhouse-site",
             **{field: value})
    assert r.status_code == 400, r.text
    assert field in r.json()["error"]
    assert record_count(store.DB_PATH) == 0


def test_a_private_products_text_field_is_refused_400_via_a_git_identity(client):
    """The epic names both forms: a private `github:` repo, and a private
    PRODUCT named through a `git:` identity's own `product` field — `ledgerwick`
    here carries no `repos` at all, so only the slug names it."""
    r = post(client, repo_identity="git:" + "e" * 40, product="ledgerwick",
             findings=[{"severity": "blocking"}])
    assert r.status_code == 400, r.text
    assert "findings" in r.json()["error"]
    assert record_count(store.DB_PATH) == 0


def test_a_verdict_only_post_for_a_private_crew_stores_a_row_with_no_archives_signal(
        client):
    r = post(client, repo_identity="github:examplecorp/quillhouse-site")
    assert r.status_code == 201, r.text
    assert r.json()["prose"] == ""
    assert r.json()["findings"] == []
    assert record_count(store.DB_PATH) == 1
    assert signal_count(store.DB_PATH) == 0


def test_a_self_registered_black_box_crew_is_refused_even_when_the_registry_does_not_mark_it_private(
        client):
    """The third union member, `store.black_box_registration_crews()`
    (`create_signal`'s own reasoning): a crew that registered itself as a
    black box is one whether or not the project registry's `private` flag
    also says so. `fennick` here is an ordinary, non-private registry
    entry — only the self-registration makes it private for this route."""
    conn = sqlite3.connect(store.DB_PATH)
    conn.execute(
        "INSERT INTO black_box_registrations "
        "(crew, model, gateway, created_by, created_at, updated_by, updated_at) "
        "VALUES ('fennick', 'gpt-4', 'openai', 'tester', "
        "'2026-10-24T00:00:00Z', 'tester', '2026-10-24T00:00:00Z')")
    conn.commit()
    conn.close()
    r = post(client, repo_identity="github:convivy/fennick", prose="hi")
    assert r.status_code == 400, r.text
    assert "prose" in r.json()["error"]


def test_an_unreadable_registry_gives_503_and_stores_nothing(client, monkeypatch):
    """`_review_record_product`'s own `projects.available()` gate already
    404s a repository the registry cannot resolve at all, so the only way
    this check's own 503 is reachable is a registry that answers `resolve()`
    but not `private_repos()` — the live case is a race: the file is edited
    out from under the second read within the same request. Simulated
    directly at the function this check calls, rather than by deleting the
    registry file, which `_review_record_product` would already have
    refused with a 404 before this check ever ran."""
    monkeypatch.setattr(projects, "private_repos", lambda: None)
    r = post(client)
    assert r.status_code == 503, r.text
    assert record_count(store.DB_PATH) == 0


def test_a_non_private_post_is_unchanged(client, facts):
    """The protocol effect this change states: an ordinary repository's
    review record still writes prose and findings and still mints an
    Archives signal, exactly as before."""
    facts[RUN] = _run_verified_facts()
    r = post(client, run_id=RUN, prose="an ordinary review",
             findings=[{"severity": "blocking"}])
    assert r.status_code == 201, r.text
    assert r.json()["prose"] == "an ordinary review"
    assert r.json()["findings"] == [{"severity": "blocking"}]
    assert signal_count(store.DB_PATH) == 1


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


def test_a_changed_verdict_on_the_same_run_role_ref_is_refused_once_bound(
        client, facts):
    """CQ·217 (issue_01M5FM2A102PKMQVKFE7HM30F0): before this build, a
    second post on the same run/role/commit superseded the record into
    whatever verdict it named and kept it `run-verified` — exactly the
    shape the vulnerability this item fixes exploited, since nothing bound
    the stored verdict to what the run actually posted. A real reviewer's
    `REQUEST_CHANGES`, re-posted as `APPROVE` under the same `run_id`, is
    now refused 409, and the stored record is left exactly as it was."""
    facts[RUN] = _run_verified_facts(posted_verdict="REQUEST_CHANGES")
    first = post(client, run_id=RUN, verdict="REQUEST_CHANGES")
    assert first.status_code == 201, first.text
    assert first.json()["auth"] == "run-verified"
    assert first.json()["verdict_bound"] is True
    record_id = first.json()["id"]

    second = post(client, run_id=RUN, verdict="APPROVE")
    assert second.status_code == 409, second.text
    assert second.json()["existing"]["id"] == record_id
    assert second.json()["existing"]["verdict"] == "REQUEST_CHANGES"
    assert second.json()["existing"]["auth"] == "run-verified"

    conn = sqlite3.connect(store.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            f"SELECT {', '.join(store.REVIEW_RECORD_COLUMNS)} "
            f"FROM review_records WHERE id = ?", (record_id,)).fetchone()
    finally:
        conn.close()
    out = store.review_record_dict(row)
    assert out["verdict"] == "REQUEST_CHANGES"
    assert out["auth"] == "run-verified"
    assert out["version"] == 1
    assert record_count(store.DB_PATH) == 1
    assert signal_count(store.DB_PATH) == 1


def test_a_forged_repost_whose_facts_also_match_is_refused_through_the_route(
        client, facts):
    """CQ·217, round 1 (reviewer): the test above reaches the older
    `ReviewRecordWouldDowngrade` refusal, because its second post computes
    `unverified`. This one drives `ReviewRecordVerdictBound` through the
    route: a same-account process that also rewrites the run's `meta.json`
    so `posted_verdict` reads `APPROVE` makes the forged post compute
    `run-verified` too, and the bound verdict still does not move."""
    facts[RUN] = _run_verified_facts(posted_verdict="REQUEST_CHANGES")
    first = post(client, run_id=RUN, verdict="REQUEST_CHANGES")
    assert first.status_code == 201, first.text
    assert first.json()["verdict_bound"] is True
    record_id = first.json()["id"]

    facts[RUN] = _run_verified_facts(posted_verdict="APPROVE")
    second = post(client, run_id=RUN, verdict="APPROVE")
    assert second.status_code == 409, second.text
    assert "bound to its run" in second.json()["error"]
    assert second.json()["existing"]["id"] == record_id
    assert second.json()["existing"]["verdict"] == "REQUEST_CHANGES"

    assert record_count(store.DB_PATH) == 1
    assert signal_count(store.DB_PATH) == 1
    assert version_rows(store.DB_PATH, record_id) == []


def test_a_matching_post_is_run_verified_and_verdict_bound(client, facts):
    """CQ·217: the done-when case — a genuine post whose verdict and pr
    match what the run register attests it posted still reaches
    `run-verified`, and is now `verdict_bound` as well."""
    facts[RUN] = _run_verified_facts(pr="7", posted_verdict="APPROVE",
                                     posted_pr="7")
    r = post(client, run_id=RUN, pr=7, verdict="APPROVE")
    assert r.status_code == 201, r.text
    assert r.json()["auth"] == "run-verified"
    assert r.json()["auth_reason"] is None
    assert r.json()["verdict_bound"] is True


def test_an_empty_posted_verdict_is_unverified(client, facts):
    """CQ·217: a run the register has no attested post for — `posted_verdict`
    empty or absent — stays unverified rather than comparing the claimed
    verdict against nothing."""
    facts[RUN] = _run_verified_facts(posted_verdict="")
    r = post(client, run_id=RUN)
    assert r.status_code == 201, r.text
    assert r.json()["auth"] == "unverified"
    assert r.json()["auth_reason"] == "verdict not recorded by the run"
    assert r.json()["verdict_bound"] is False


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


def test_a_private_runs_matching_facts_reach_run_verified(client, facts):
    """CQ·237: the write route calls `runs.run_facts(...,
    include_private_facts=True)`, the internal path that gives
    `_review_record_auth` a private run's full `RUN_FACTS_FIELDS` rather
    than the `{"found", "private"}` shape `GET /runs` still gets. Before
    this, `facts.get("private")` short-circuited every private run straight
    to `unverified, "private run"`, no matter how well the claim matched —
    making `run-verified` permanently unreachable for a private crew's own
    review. Posted under the private crew's own repository, the only
    record the full facts are fetched for."""
    facts[RUN] = _run_verified_facts(private=True, repo="quillhouse-site")
    r = post(client, run_id=RUN, repo_identity="github:examplecorp/quillhouse-site")
    assert r.status_code == 201, r.text
    assert r.json()["auth"] == "run-verified"
    assert r.json()["auth_reason"] is None
    assert facts.calls[-1] == (RUN, True)


def test_an_ordinary_record_naming_a_private_run_never_reads_its_facts(
        client, facts):
    """Security-reviewer and reviewer, CQ·237 round 1. A post under an
    ordinary repository that names a private crew's run sees only
    `{"found", "private"}`. It stays unverified, gets no mismatch reason that
    would act as an oracle, and the private repository's name reaches no
    stored row or Archives signal."""
    facts[RUN] = _run_verified_facts(private=True, repo="quillhouse-site")
    r = post(client, run_id=RUN, repo_identity="git:" + "f" * 40,
             product="library", prose="an ordinary review")
    assert r.status_code == 201, r.text
    assert r.json()["auth"] == "unverified"
    assert all(flag is False for _, flag in facts.calls)
    conn = sqlite3.connect(store.DB_PATH)
    try:
        dump = "\n".join(conn.iterdump())
    finally:
        conn.close()
    assert "quillhouse-site" not in dump


def test_a_private_record_naming_another_private_crews_run_learns_nothing(
        client, facts):
    """A private crew's record that names a different private crew's run gets
    the same answer as an ordinary record would: unverified, with the reason
    the `{found, private}` shape produces, so no mismatch reason describes the
    other crew's run."""
    facts[RUN] = _run_verified_facts(private=True, repo="ledgerwick-books")
    r = post(client, run_id=RUN, repo_identity="github:examplecorp/quillhouse-site")
    assert r.status_code == 201, r.text
    assert r.json()["auth"] == "unverified"
    seeded_shape = {"available": True, "found": True, "private": True}
    facts[RUN + "x"] = seeded_shape
    baseline = post(client, run_id=RUN + "x",
                    repo_identity="github:examplecorp/quillhouse-site",
                    reviewed_ref="b" * 40)
    assert r.json()["auth_reason"] == baseline.json()["auth_reason"]


def test_a_git_product_spelling_shared_with_a_private_crew_is_private(client):
    """Reviewer, CQ·237 round 1. `qh-shared` is listed by the ordinary
    `fennick` first and by the private `quillhouse` second, so
    `projects.resolve` answers `fennick`. The raw spelling still names a
    private crew, and the route treats it as one."""
    r = post(client, repo_identity="git:" + "d" * 40, product="qh-shared",
             prose="text a private crew may not store")
    assert r.status_code == 400, r.text
    assert "prose" in r.json()["error"]
    assert record_count(store.DB_PATH) == 0


def test_a_private_run_with_incomplete_facts_is_unverified_on_the_missing_field(
        client, facts):
    """The removed `"private run"` shortcut is not replaced by a free pass:
    a private run whose facts carry no `review_role` at all (the shape a
    stale or partial register entry could still produce) is refused on that
    missing field exactly as a non-private run would be."""
    facts[RUN] = {"available": True, "found": True, "private": True}
    r = post(client, run_id=RUN)
    assert r.status_code == 201, r.text
    assert r.json()["auth"] == "unverified"
    assert r.json()["auth_reason"] == "role mismatch"


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
    facts[RUN] = _run_verified_facts(pr="42", posted_pr="42")
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
    facts[RUN] = _run_verified_facts(pr="42", posted_verdict="REQUEST_CHANGES",
                                     posted_pr="42")
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
    facts[RUN] = _run_verified_facts(pr="42", posted_verdict="REQUEST_CHANGES",
                                     posted_pr="42")
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


# `run-verified` over `run-verified` with a DIFFERENT verdict, the fourth
# pairing, is no longer free to supersede once the existing record's verdict
# is bound (CQ·217) — see
# `test_a_changed_verdict_on_the_same_run_role_ref_is_refused_once_bound`.


def test_a_git_identity_is_never_run_verified_even_when_every_fact_matches(
        client, facts):
    facts[RUN] = _run_verified_facts(repo="library", review_sha="c" * 40)
    r = post(client, run_id=RUN, repo_identity="git:" + "c" * 40,
             reviewed_ref="c" * 40, product="library")
    assert r.status_code == 201, r.text
    assert r.json()["auth"] == "unverified"
    assert r.json()["auth_reason"] == "git identity cannot be verified against a run"


def test_a_git_identity_with_a_private_run_is_unverified_not_404(client, facts):
    """A `git:` identity is still never `run-verified` — the check right
    after `facts.get("found")` catches it before any role/repo/sha/pr
    comparison runs — and a private run behind it must not turn into a 404;
    it is simply unverified for the `git:`-cannot-verify reason, same as a
    non-private run would be."""
    facts[RUN] = {"available": True, "found": True, "private": True}
    r = post(client, run_id=RUN, repo_identity="git:" + "d" * 40,
             product="library")
    assert r.status_code == 201, r.text
    assert r.json()["auth"] == "unverified"
    assert r.json()["auth_reason"] == "git identity cannot be verified against a run"


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
    r = post(client, repo_identity="github:convivy/library", product="fennick")
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
        "review_sha": "a" * 40, "pr": 42, "review_role": "reviewer",
        "posted_verdict": "APPROVE", "posted_pr": 42})
    try:
        assert runs.run_facts(RUN) == {
            "available": True, "found": True, "agent": "coder",
            "repo": "library", "review_sha": "a" * 40, "pr": 42,
            "review_role": "reviewer", "posted_verdict": "APPROVE",
            "posted_pr": 42}
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
    the same discipline `test_runs.py` pins for `/runs` itself. The default
    (`include_private_facts` unset) is every caller-facing read's shape,
    `GET /runs` included."""
    monkeypatch.setattr(runs, "SOCKET_PATH", Path(sock_dir) / "r.sock")
    reg = FakeFactsServer(Path(sock_dir) / "r.sock", {
        "found": True, "private": True, "repo": "QUILL", "agent": "secret-agent"})
    try:
        assert runs.run_facts(RUN) == {"available": True, "found": True,
                                       "private": True}
    finally:
        reg.close()


def test_run_facts_include_private_facts_is_the_internal_path_for_the_write_route(
        sock_dir, monkeypatch):
    """CQ·237: `include_private_facts=True` is the one way past the
    allowlist above, and it is a keyword a caller never gets from a request
    — `create_review_record` is the only caller in this process that passes
    it, so `_review_record_auth` can verify a private crew's own review
    against the run's real facts."""
    monkeypatch.setattr(runs, "SOCKET_PATH", Path(sock_dir) / "r.sock")
    reg = FakeFactsServer(Path(sock_dir) / "r.sock", {
        "found": True, "private": True, "agent": "coder", "repo": "library",
        "review_sha": "a" * 40, "pr": 42, "review_role": "reviewer",
        "posted_verdict": "APPROVE", "posted_pr": 42})
    try:
        assert runs.run_facts(RUN, include_private_facts=True) == {
            "available": True, "found": True, "private": True,
            "agent": "coder", "repo": "library", "review_sha": "a" * 40,
            "pr": 42, "review_role": "reviewer", "posted_verdict": "APPROVE",
            "posted_pr": 42}
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
