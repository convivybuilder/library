"""`POST /bundles` and `POST /bundles/redeem` (issue_01M2VCWQXTF5EGHD1X1J3P4RTJ)
— the individuation bundle, the Library's side of `platform/decisions/0085`.

**The control database is the real schema**, built once by `alembic upgrade
head` and copied per test, because the token store's constraints are part of
what is under test and a hand-written `CREATE TABLE` would test a copy. The
knowledge index is a fixture, built to hold every status a bundle has to
handle. The last section of this file runs the same routes against the real
index and the real `library/daytime` profile, and skips where there is none
(CI has neither).

**The corpus is designed so each refusal has a document that trips it and a
neighbour that does not**, the discipline `test_reference_reads.py` uses:

* `p/skills/s-draft` is a `draft` skill for `coder`, selected by a profile whose
  skill rule names no `status` (the default is `current` and `draft`);
* `p/agents/retired-agent` is `deprecated`, so the roster names an agent whose
  only definition is retired;
* `platform-coder` has a definition and NO skill naming it, the live shape
  measured on 2026-09-18 (0 of 31 skills), so its bundle must refuse rather
  than come back holding only its definition.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

import app as libapp
import bundles
import crew_registry
import identity
import protocol
import reference
import black_box_tables
import store

REPO = Path(__file__).resolve().parent.parent

AGENT_TOKEN = "agent-token-bosun"
INDIVIDUATOR_TOKEN = "system-token-individuator"
SCOPED_TOKEN = "agent-token-scoped-to-q"

ORCHESTRATOR = identity.Identity(actor="co-library-test", kind="orchestrator")

ITEM_P = "issue_ITEM_IN_P"
ITEM_Q = "issue_ITEM_IN_Q"

# id, kind, project, status, audience
DOCS = [
    ("p/agents/coder", "agent", "p", "current", ["coder"]),
    ("p/agents/reviewer", "agent", "p", "current", ["reviewer"]),
    ("p/agents/platform-coder", "agent", "p", "current", ["platform-coder"]),
    ("p/agents/retired-agent", "agent", "p", "deprecated", ["retired-agent"]),
    ("p/skills/s-both", "skill", "p", "current", ["coder", "reviewer"]),
    ("p/skills/s-coder", "skill", "p", "current", ["coder"]),
    ("p/skills/s-reviewer", "skill", "p", "current", ["reviewer"]),
    ("p/skills/s-draft", "skill", "p", "draft", ["coder"]),
    ("p/skills/s-retired", "skill", "p", "deprecated", ["coder"]),
    ("other/skills/elsewhere", "skill", "other", "current", ["coder"]),
    # A read-only agent with no skill naming it, the same shape as
    # `platform-coder`, but free to declare `takes_none_of` in a given test —
    # left undeclared (frontmatter_extra defaults to `{}`) for the sweep and
    # the roster below.
    ("p/agents/scout", "agent", "p", "current", ["scout"]),
]
AGENTS_RULE = {"kind": "agent", "audience_contains": [
    "coder", "reviewer", "platform-coder", "retired-agent", "ghost", "scout"]}
ROSTER = ["coder", "reviewer", "platform-coder", "retired-agent", "ghost",
          "scout"]


def body_of(doc_id):
    return f"body of {doc_id}"


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture(scope="module")
def migrated_db(tmp_path_factory):
    db = tmp_path_factory.mktemp("migrated") / "library.db"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=str(REPO),
        capture_output=True, text=True, check=False,
        env={"PATH": "/usr/bin:/bin", "HOME": str(REPO),
             "LIBRARY_DB": str(db)})
    assert result.returncode == 0, result.stderr
    return db


def _build_index(path):
    iconn = sqlite3.connect(path)
    iconn.executescript("""
        CREATE TABLE docs (
            id TEXT PRIMARY KEY, source TEXT NOT NULL DEFAULT 'kb',
            source_ref TEXT, kind TEXT, project TEXT, title TEXT, status TEXT,
            topics TEXT DEFAULT '[]', audience TEXT DEFAULT '[]',
            last_reviewed TEXT, updated_at TEXT, body TEXT, body_hash TEXT,
            frontmatter_extra TEXT NOT NULL DEFAULT '{}');
    """)
    for doc_id, kind, project, status, audience in DOCS:
        iconn.execute(
            "INSERT INTO docs (id, source_ref, kind, project, title, status, "
            "audience, body, body_hash, last_reviewed, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (doc_id, f"{doc_id}.md", kind, project, f"title of {doc_id}",
             status, json.dumps(audience), body_of(doc_id),
             sha(body_of(doc_id)), "2026-08-01", "2026-08-02"))
    iconn.commit()
    iconn.close()


@pytest.fixture
def client(migrated_db, tmp_path, monkeypatch):
    db = tmp_path / "library.db"
    shutil.copy(migrated_db, db)
    index = tmp_path / "index.db"
    _build_index(index)
    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        AGENT_TOKEN: {"name": "bosun", "products": ["*"]},
        INDIVIDUATOR_TOKEN: {"name": "individuator", "kind": "system",
                             "products": ["*"]},
        SCOPED_TOKEN: {"name": "scoped", "products": ["q"]},
    }))
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(reference, "INDEX_DB", index)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(identity, "CLIENT_TOKENS", tmp_path / "none.json")
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    black_box_tables.use_registry(tmp_path, monkeypatch)
    for name in ROSTER:
        crew_registry.roster_add(ORCHESTRATOR, name)
    crew_registry.profile_put(
        ORCHESTRATOR, "p", "daytime",
        [AGENTS_RULE, {"kind": "skill", "project": ["p"],
                       "status": "current"}], [])
    # No `status` on the skill rule: the default selects current AND draft, so
    # `s-draft` is a document this profile names and a bundle cannot serve.
    crew_registry.profile_put(
        ORCHESTRATOR, "p", "loose",
        [AGENTS_RULE, {"kind": "skill", "project": ["p"]}], [])
    crew_registry.profile_put(
        ORCHESTRATOR, "p", "agents-only", [AGENTS_RULE], [])
    crew_registry.profile_put(
        ORCHESTRATOR, "p", "skills-only",
        [{"kind": "skill", "project": ["p"], "status": "current"}], [])
    crew_registry.profile_put(
        ORCHESTRATOR, "q", "daytime",
        [AGENTS_RULE, {"kind": "skill", "project": ["p"],
                       "status": "current"}], [])
    conn = sqlite3.connect(db)
    for item_id, product in ((ITEM_P, "p"), (ITEM_Q, "q")):
        conn.execute(
            "INSERT INTO issue_items (id, product, title, body, refs, "
            "severity, state, created_by, created_at) VALUES "
            "(?,?,?,?,?,?,?,?,?)",
            (item_id, product, "t", "b", "[]", "low", "open", "test",
             "2026-01-01 00:00:00"))
    conn.commit()
    conn.close()
    return TestClient(libapp.app)


def _h(token=AGENT_TOKEN):
    return {"Authorization": f"Bearer {token}",
            protocol.HEADER: str(protocol.CURRENT)}


def _issue(client, agent="coder", token=AGENT_TOKEN, **scope):
    body = {"agent": agent, **(scope or {"item_id": ITEM_P})}
    return client.post("/bundles", json=body, headers=_h(token))


def _redeem(client, token_id, token=INDIVIDUATOR_TOKEN):
    return client.post("/bundles/redeem", json={"token_id": token_id},
                       headers=_h(token))


def _ids(response):
    return [d["id"] for d in response.json()["documents"]]


def _token_row(client, token_id):
    conn = sqlite3.connect(store.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM bundle_tokens WHERE id = ?",
                            (token_id,)).fetchone()
    finally:
        conn.close()


def _token_count():
    conn = sqlite3.connect(store.DB_PATH)
    try:
        return conn.execute("SELECT COUNT(*) FROM bundle_tokens").fetchone()[0]
    finally:
        conn.close()


def _set_doc(doc_id, **cols):
    conn = sqlite3.connect(reference.INDEX_DB)
    sets = ", ".join(f"{c} = ?" for c in cols)
    conn.execute(f"UPDATE docs SET {sets} WHERE id = ?",
                 (*cols.values(), doc_id))
    conn.commit()
    conn.close()


# --- issue, then redeem once ---------------------------------------------------


def test_a_token_is_issued_for_an_agent_and_item_and_redeemed_for_its_documents(
        client):
    issued = _issue(client, "coder", item_id=ITEM_P)
    assert issued.status_code == 201, issued.text
    body = issued.json()
    assert body["token_id"].startswith("bundle_")
    assert body["agent"] == "coder"
    assert body["scope"] == {"kind": "item", "item_id": ITEM_P,
                             "project": "p", "variant": "daytime"}
    # The agent's own definition, and the skills naming `coder` under the
    # profile's rule: not `s-reviewer`, not the other project's skill, and not
    # the draft or the deprecated one, which `status: current` excludes.
    assert _ids(issued) == ["p/agents/coder", "p/skills/s-both",
                            "p/skills/s-coder"]
    assert "body" not in body["documents"][0]

    redeemed = _redeem(client, body["token_id"])
    assert redeemed.status_code == 200, redeemed.text
    assert _ids(redeemed) == _ids(issued)
    assert redeemed.json()["count"] == 3
    assert [d["body"] for d in redeemed.json()["documents"]] == [
        body_of(i) for i in _ids(redeemed)]


def test_a_repository_scoped_token_serves_the_same_way(client):
    issued = _issue(client, "reviewer", repo="p")
    assert issued.status_code == 201, issued.text
    assert issued.json()["scope"] == {"kind": "repo", "repo": "p",
                                      "project": "p", "variant": "daytime"}
    assert _ids(issued) == ["p/agents/reviewer", "p/skills/s-both",
                            "p/skills/s-reviewer"]
    redeemed = _redeem(client, issued.json()["token_id"])
    assert redeemed.status_code == 200
    assert redeemed.json()["scope"]["repo"] == "p"


def test_a_second_redemption_of_the_same_token_is_refused(client):
    token_id = _issue(client).json()["token_id"]
    assert _redeem(client, token_id).status_code == 200
    second = _redeem(client, token_id)
    assert second.status_code == 409
    assert "already been redeemed" in second.json()["error"]
    assert "documents" not in second.json(), (
        "a refused redemption must serve nothing")


def test_only_one_of_two_racing_redemptions_consumes_the_token(client):
    """A spent token cannot be spent again: `consume` returns False the second
    time and does not overwrite who won.

    **This pins sequential re-spending, NOT the atomicity of the UPDATE.** Both
    calls happen one after the other on one connection, so a read-then-write
    `consume` would pass this too — the second read plainly sees `redeemed_at`
    set. The window where the two shapes differ is inside `consume` itself and
    no test reaches it; see the test below for what IS pinned, which is the
    route's behaviour when it loses the race."""
    token_id = _issue(client).json()["token_id"]
    conn = sqlite3.connect(store.DB_PATH)
    try:
        assert bundles.consume(conn, token_id, redeemed_by="first") is True
        assert bundles.consume(conn, token_id, redeemed_by="second") is False
        conn.commit()
    finally:
        conn.close()
    assert _token_row(client, token_id)["redeemed_by"] == "first"


def test_a_redemption_that_loses_the_race_after_its_read_is_refused_and_serves_nothing(
        client, monkeypatch):
    """**The `consume` branch in `_do_redeem_bundle`, reached.** The route reads
    the row, reads the named documents, and only then consumes. Here the token
    is spent through a SECOND connection inside `read_named`, after the route's
    own unredeemed read and before its `consume`, so the route's conditional
    UPDATE finds nothing to change. Nothing is threaded and nothing sleeps: the
    patch fixes the interleaving rather than racing for it.

    **What this pins, exactly: that the route HONOURS a lost race.** Ignore the
    branch's result and the bundle is served twice, 200 with documents, and this
    fails — verified by replacing it with `if bundles.consume(...) and False`,
    which failed this test alone with 50 others passing.

    **What it does NOT pin is the atomicity of `consume` itself.** The rival
    commits before the route's `consume` runs, so a read-then-write `consume`
    would also see `redeemed_at` set and return False. That window is inside one
    function and no test reaches it without patching the function under test. An
    earlier version of this docstring claimed otherwise, which would have let a
    maintainer refactor `consume` believing it was covered (reviewer, PR #136
    round 2)."""
    token_id = _issue(client).json()["token_id"]
    real = bundles.read_named

    def read_then_lose_the_race(index_conn, named):
        rows = real(index_conn, named)
        rival = sqlite3.connect(store.DB_PATH)
        try:
            assert bundles.consume(rival, token_id, redeemed_by="winner")
            rival.commit()
        finally:
            rival.close()
        return rows

    monkeypatch.setattr(bundles, "read_named", read_then_lose_the_race)
    r = _redeem(client, token_id)
    assert r.status_code == 409, r.text
    assert "already been redeemed" in r.json()["error"]
    assert "documents" not in r.json(), (
        "the loser must be served nothing")
    row = _token_row(client, token_id)
    assert row["redeemed_by"] == "winner", (
        "the winner is the redemption that consumed the token, not the "
        "individuator whose request lost")


def test_redemption_records_the_authenticated_actor_and_not_a_claim(client):
    token_id = _issue(client).json()["token_id"]
    r = client.post("/bundles/redeem", headers=_h(INDIVIDUATOR_TOKEN),
                    json={"token_id": token_id, "redeemed_by": "someone-else",
                          "actor": "someone-else"})
    assert r.status_code == 200
    row = _token_row(client, token_id)
    assert row["redeemed_by"] == "individuator"
    assert row["issued_by"] == "bosun"


def test_an_expired_token_is_refused_and_serves_nothing(client):
    token_id = _issue(client).json()["token_id"]
    conn = sqlite3.connect(store.DB_PATH)
    conn.execute("UPDATE bundle_tokens SET expires_at = '2000-01-01 00:00:00' "
                 "WHERE id = ?", (token_id,))
    conn.commit()
    conn.close()
    r = _redeem(client, token_id)
    assert r.status_code == 409
    assert "expired" in r.json()["error"]
    assert _token_row(client, token_id)["redeemed_at"] is None


def test_the_expiry_is_a_stored_instant_set_once(client):
    """`call_slips`' rule, held here: nothing in `bundles` extends it."""
    token_id = _issue(client).json()["token_id"]
    before = _token_row(client, token_id)["expires_at"]
    _redeem(client, token_id)
    assert _token_row(client, token_id)["expires_at"] == before
    assert not [n for n in dir(bundles) if n in ("touch", "extend", "renew")]


# --- a document the profile names that is not current: REFUSED, never omitted --


def test_a_draft_the_profile_selects_refuses_the_whole_bundle_and_names_it(
        client):
    """THE CENTRAL PROPERTY. `p/loose` names `s-draft` for `coder`. A bundle
    that dropped it and served the other three would look identical to a
    successful one, which is the failure this epic exists to prevent."""
    r = _issue(client, "coder", item_id=ITEM_P, variant="loose")
    assert r.status_code == 409, r.text
    assert "documents" in r.json()
    named = {d["id"]: d for d in r.json()["documents"]}
    assert list(named) == ["p/skills/s-draft"]
    assert named["p/skills/s-draft"]["status"] == "draft"
    assert "p/skills/s-draft" in r.json()["error"]
    assert _token_count() == 0, "a refused bundle must mint nothing"


def test_the_refusal_names_every_such_document_at_once(client):
    _set_doc("p/skills/s-coder", status="draft")
    r = _issue(client, "coder", item_id=ITEM_P, variant="loose")
    assert r.status_code == 409
    assert [d["id"] for d in r.json()["documents"]] == [
        "p/skills/s-coder", "p/skills/s-draft"]


def test_a_neighbour_who_is_not_named_by_the_draft_is_still_served(client):
    """The refusal is scoped to the agent: `s-draft` names `coder` only."""
    r = _issue(client, "reviewer", item_id=ITEM_P, variant="loose")
    assert r.status_code == 201, r.text
    assert "p/skills/s-draft" not in _ids(r)


def test_an_explicit_current_filter_is_a_profile_choice_not_a_refusal(client):
    """`p/daytime` says `status: current`, so the draft and the deprecated
    skill are excluded by the profile's own rule. That is not a dropped
    document; the profile never selected them."""
    r = _issue(client, "coder", item_id=ITEM_P)
    assert r.status_code == 201
    assert not {"p/skills/s-draft", "p/skills/s-retired"} & set(_ids(r))


@pytest.mark.parametrize("status", ["deprecated", "archived", "draft"])
def test_a_document_retired_after_issue_refuses_the_redemption_naming_it(
        client, status):
    token_id = _issue(client).json()["token_id"]
    _set_doc("p/skills/s-coder", status=status)
    r = _redeem(client, token_id)
    assert r.status_code == 409, r.text
    assert "documents" in r.json() and r.json()["documents"] == [{
        "id": "p/skills/s-coder", "reason": "not-current", "status": status,
        "named_body_sha256": sha(body_of("p/skills/s-coder"))}]
    assert "p/skills/s-coder" in r.json()["error"]
    row = _token_row(client, token_id)
    assert row["redeemed_at"] is None, (
        "nothing was served, so the token must not be spent")


def test_a_document_deleted_after_issue_refuses_and_is_named_missing(client):
    token_id = _issue(client).json()["token_id"]
    conn = sqlite3.connect(reference.INDEX_DB)
    conn.execute("DELETE FROM docs WHERE id = 'p/skills/s-both'")
    conn.commit()
    conn.close()
    r = _redeem(client, token_id)
    assert r.status_code == 409
    assert [(d["id"], d["reason"]) for d in r.json()["documents"]] == [
        ("p/skills/s-both", "missing")]


def test_a_document_edited_after_issue_refuses_and_is_named_changed(client):
    token_id = _issue(client).json()["token_id"]
    _set_doc("p/skills/s-coder", body="edited", body_hash=sha("edited"))
    r = _redeem(client, token_id)
    assert r.status_code == 409
    [named] = r.json()["documents"]
    assert named["id"] == "p/skills/s-coder" and named["reason"] == "changed"
    assert named["named_body_sha256"] == sha(body_of("p/skills/s-coder"))
    assert named["body_sha256"] == sha("edited")


def test_a_refused_redemption_is_retryable_once_the_room_agrees_again(client):
    token_id = _issue(client).json()["token_id"]
    _set_doc("p/skills/s-coder", status="deprecated")
    assert _redeem(client, token_id).status_code == 409
    _set_doc("p/skills/s-coder", status="current")
    assert _redeem(client, token_id).status_code == 200
    assert _redeem(client, token_id).status_code == 409


def test_every_problem_is_reported_together(client):
    token_id = _issue(client).json()["token_id"]
    _set_doc("p/skills/s-coder", status="archived")
    _set_doc("p/skills/s-both", body="edited", body_hash=sha("edited"))
    r = _redeem(client, token_id)
    assert sorted((d["id"], d["reason"]) for d in r.json()["documents"]) == [
        ("p/skills/s-both", "changed"), ("p/skills/s-coder", "not-current")]


# --- the body hash, and a stale copy detected from it -------------------------


def test_every_document_carries_the_hash_of_its_body(client):
    issued = _issue(client)
    redeemed = _redeem(client, issued.json()["token_id"])
    for response in (issued, redeemed):
        for doc in response.json()["documents"]:
            assert doc["body_sha256"] == sha(body_of(doc["id"]))
    for doc in redeemed.json()["documents"]:
        assert sha(doc["body"]) == doc["body_sha256"], (
            "the hash must be of the body served, so a caller comparing what "
            "it fetched against what it was told is comparing like with like")


def test_a_stale_copy_on_disk_is_detected_from_the_hash(client, tmp_path):
    """What an individuator does with the field: hash the copy it holds and
    compare. Two copies, one current and one left over from before an edit."""
    redeemed = _redeem(client, _issue(client).json()["token_id"]).json()
    target = next(d for d in redeemed["documents"]
                  if d["id"] == "p/skills/s-coder")

    fresh = tmp_path / "fresh.md"
    fresh.write_text(target["body"], encoding="utf-8")
    stale = tmp_path / "stale.md"
    stale.write_text("body of p/skills/s-coder, before the edit",
                     encoding="utf-8")

    def matches(path):
        return sha(path.read_text(encoding="utf-8")) == target["body_sha256"]

    assert matches(fresh) is True
    assert matches(stale) is False


# --- an audience filter that matches nothing is a refusal ----------------------


def test_a_filter_that_matches_something_serves_it(client):
    r = _issue(client, "coder")
    assert r.status_code == 201
    assert r.json()["count"] > 0


def test_a_skill_filter_that_matches_nothing_is_refused_not_an_empty_bundle(
        client):
    """The live case: `platform-coder` has a definition and no skill names it,
    so returning its definition alone would ship an under-equipped agent."""
    r = _issue(client, "platform-coder", item_id=ITEM_P)
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["kind"] == "skill"
    assert body["audience"] == "platform-coder"
    assert body["matched"] == 0
    assert body["candidates"] == 3, (
        "the count is what tells a vocabulary error from an empty profile: "
        "three current skills in the profile, none naming this agent")
    assert body["audiences_present"] == ["coder", "reviewer"]
    assert "platform-coder" in body["error"] and "skill" in body["error"]
    assert _token_count() == 0


def test_a_definition_filter_that_matches_nothing_is_refused(client):
    """`ghost` is on the roster and no agent document names it."""
    r = _issue(client, "ghost")
    assert r.status_code == 409
    assert r.json()["kind"] == "agent" and r.json()["matched"] == 0


def test_an_agent_whose_only_definition_is_retired_is_refused_naming_it(client):
    r = _issue(client, "retired-agent")
    assert r.status_code == 409, r.text
    assert r.json()["kind"] == "agent"
    assert r.json()["documents"] == [{
        "id": "p/agents/retired-agent", "reason": "not-selected",
        "status": "deprecated", "project": "p"}]
    assert "p/agents/retired-agent" in r.json()["error"]


def test_a_profile_with_no_rule_for_a_kind_owes_that_kind_nothing(client):
    """A filter exists only where a rule does, so `agents-only` is a bundle of
    the definition and nothing else, and nothing was silently dropped."""
    r = _issue(client, "coder", repo="p", variant="agents-only")
    assert r.status_code == 201
    assert _ids(r) == ["p/agents/coder"]


def test_a_profile_with_no_agent_rule_is_refused_as_having_no_definition(
        client):
    """The opposite shape to `agents-only`: a profile of skills alone. The
    definition is always owed, so this is a 409 for kind `agent` and not a
    bundle of skills without the agent that uses them."""
    r = _issue(client, "coder", repo="p", variant="skills-only")
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["kind"] == "agent"
    assert body["audience"] == "coder"
    assert body["matched"] == 0 and body["candidates"] == 0
    assert body["documents"] == [{
        "id": "p/agents/coder", "reason": "not-selected",
        "status": "current", "project": "p"}], (
        "the refusal names the definition the index holds and the profile "
        "does not select")
    assert _token_count() == 0


# --- an agent that declares it takes none of a kind is exempted -------------


def test_an_agent_declaring_no_skills_gets_a_bundle_with_zero_skills(client):
    """The scout case (issue_01M3PZMW7Z7TBFQGWRN8JGZ70P): a read-only agent
    that is never given skills says so on its own definition, and the same
    empty skill match that refuses `platform-coder` is the design working
    for an agent that declared it."""
    _set_doc("p/agents/scout",
             frontmatter_extra=json.dumps({"takes_none_of": ["skill"]}))
    issued = _issue(client, "scout", item_id=ITEM_P)
    assert issued.status_code == 201, issued.text
    assert _ids(issued) == ["p/agents/scout"]
    assert issued.json()["count"] == 1
    redeemed = _redeem(client, issued.json()["token_id"])
    assert redeemed.status_code == 200, redeemed.text
    assert _ids(redeemed) == ["p/agents/scout"]


def test_an_agent_with_no_declaration_and_an_empty_skill_match_still_refuses(
        client):
    """Without a declaration the exemption never applies: the default is the
    refusal, unchanged."""
    r = _issue(client, "scout", item_id=ITEM_P)
    assert r.status_code == 409, r.text
    assert r.json()["kind"] == "skill" and r.json()["matched"] == 0
    assert _token_count() == 0


def test_a_declaration_naming_agent_does_not_remove_the_definition_requirement(
        client):
    """`agent` is a valid kind name and can be named, but it must never
    exempt the kind every bundle always carries. `skill` is declared
    alongside it here, and that exemption still applies."""
    _set_doc("p/agents/scout", frontmatter_extra=json.dumps(
        {"takes_none_of": ["agent", "skill"]}))
    r = _issue(client, "scout", item_id=ITEM_P)
    assert r.status_code == 201, r.text
    assert _ids(r) == ["p/agents/scout"], (
        "the agent's own definition is still served despite being named in "
        "its own takes_none_of")


def test_a_declaration_for_one_kind_does_not_excuse_an_empty_match_of_another(
        client):
    """Declaring `agent` alone does not spend over to `skill`: an empty skill
    match still refuses because nothing declared that kind."""
    _set_doc("p/agents/scout",
             frontmatter_extra=json.dumps({"takes_none_of": ["agent"]}))
    r = _issue(client, "scout", item_id=ITEM_P)
    assert r.status_code == 409, r.text
    assert r.json()["kind"] == "skill" and r.json()["matched"] == 0


@pytest.mark.parametrize("bad_value", [
    "skill",              # not a list at all
    ["skill", 5],         # not every entry is a string
    ["not-a-kind"],       # names no kind a bundle ever carries
])
def test_a_malformed_declaration_refuses(client, bad_value):
    _set_doc("p/agents/scout",
             frontmatter_extra=json.dumps({"takes_none_of": bad_value}))
    r = _issue(client, "scout", item_id=ITEM_P)
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["id"] == "p/agents/scout"
    assert body["field"] == "takes_none_of"
    assert body["value"] == bad_value
    assert "p/agents/scout" in body["error"] and "takes_none_of" in body["error"]
    assert _token_count() == 0, "a malformed declaration must not be read " \
        "as 'takes nothing' and must mint nothing"


def test_no_agent_and_profile_pair_ever_yields_an_empty_bundle(client):
    """Whatever the answer, it is never a 2xx with nothing in it."""
    seen = set()
    for agent in ROSTER:
        for variant in ("daytime", "loose", "agents-only"):
            r = _issue(client, agent, repo="p", variant=variant)
            seen.add(r.status_code)
            if r.status_code == 201:
                assert r.json()["count"] >= 1
                assert r.json()["documents"], (agent, variant)
                assert any(d["kind"] == "agent"
                           for d in r.json()["documents"]), (agent, variant)
            else:
                assert r.status_code == 409, (agent, variant, r.text)
    assert seen == {201, 409}, "the sweep exercised only one outcome"


# --- request validation ---------------------------------------------------------


def test_exactly_one_of_item_and_repo(client):
    both = _issue(client, "coder", item_id=ITEM_P, repo="p")
    neither = client.post("/bundles", json={"agent": "coder"}, headers=_h())
    assert both.status_code == 400 and neither.status_code == 400


@pytest.mark.parametrize("body", [
    {"item_id": ITEM_P},
    {"agent": "", "item_id": ITEM_P},
    {"agent": 5, "item_id": ITEM_P},
    {"agent": "x" * 65, "item_id": ITEM_P},
    {"agent": "coder", "item_id": ITEM_P, "variant": ""},
])
def test_a_malformed_request_is_a_400(client, body):
    assert client.post("/bundles", json=body, headers=_h()).status_code == 400


def test_a_body_that_is_not_an_object_is_a_400(client):
    assert client.post("/bundles", json=[1], headers=_h()).status_code == 400
    assert client.post("/bundles/redeem", json=[1],
                       headers=_h(INDIVIDUATOR_TOKEN)).status_code == 400
    assert client.post("/bundles/redeem", json={},
                       headers=_h(INDIVIDUATOR_TOKEN)).status_code == 400


def test_an_agent_off_the_roster_is_a_422(client):
    r = _issue(client, "not-on-the-roster")
    assert r.status_code == 422
    assert "roster" in r.json()["error"]


def test_a_profile_that_does_not_exist_is_a_422(client):
    r = _issue(client, "coder", repo="no-such-repo")
    assert r.status_code == 422
    assert "no-such-repo/daytime" in r.json()["error"]
    assert _issue(client, "coder", repo="p",
                  variant="no-such-variant").status_code == 422


def test_an_item_that_does_not_exist_and_one_out_of_scope_read_the_same(client):
    absent = _issue(client, "coder", token=SCOPED_TOKEN,
                    item_id="issue_NEVER_CREATED")
    hidden = _issue(client, "coder", token=SCOPED_TOKEN, item_id=ITEM_P)
    assert absent.status_code == hidden.status_code == 404
    assert absent.json() == hidden.json()
    assert _issue(client, "coder", token=SCOPED_TOKEN,
                  item_id=ITEM_Q).status_code == 201


def test_a_repository_bundle_is_not_product_scoped(client):
    """The Reference Room is not product-scoped, and a bundle is its documents."""
    r = _issue(client, "coder", token=SCOPED_TOKEN, repo="p")
    assert r.status_code == 201


def test_redeeming_an_out_of_scope_item_token_reads_as_no_such_token(client):
    token_id = _issue(client, "coder", item_id=ITEM_Q).json()["token_id"]
    r = _redeem(client, token_id, token=SCOPED_TOKEN)
    assert r.status_code == 200
    other = _issue(client, "coder", item_id=ITEM_P).json()["token_id"]
    hidden = _redeem(client, other, token=SCOPED_TOKEN)
    absent = _redeem(client, "bundle_NEVER_ISSUED", token=SCOPED_TOKEN)
    assert hidden.status_code == absent.status_code == 404
    assert hidden.json() == absent.json()


# --- authorization ---------------------------------------------------------------


def test_no_credential_is_a_401(client):
    assert client.post("/bundles", json={"agent": "coder", "repo": "p"},
                       headers={protocol.HEADER: "1"}).status_code == 401
    assert client.post("/bundles/redeem", json={"token_id": "x"},
                       headers={protocol.HEADER: "1"}).status_code == 401


@pytest.mark.parametrize("cap,path,body", [
    ("bundle.issue", "/bundles", {"agent": "coder", "repo": "p"}),
    ("bundle.redeem", "/bundles/redeem", {"token_id": "bundle_x"}),
])
def test_each_route_refuses_without_its_capability(client, monkeypatch, cap,
                                                   path, body):
    monkeypatch.setattr(identity, "ROLE_CAPS", {
        **identity.ROLE_CAPS,
        "agent": identity.ROLE_CAPS["agent"] - {cap}})
    r = client.post(path, json=body, headers=_h())
    assert r.status_code == 403
    assert cap in r.json()["error"]


def test_the_two_capabilities_are_granted_on_both_literal_role_sets():
    for kind in ("agent", "system"):
        assert {"bundle.issue", "bundle.redeem"} <= identity.ROLE_CAPS[kind]
    for kind in ("customer_reader", "orchestrator", "approved_client"):
        assert {"bundle.issue", "bundle.redeem"} <= identity.ROLE_CAPS[kind], (
            f"{kind!r} derives from a literal set and lost the grant")
    assert not {"bundle.issue", "bundle.redeem"} & identity.ROLE_CAPS.get(
        "a-role-nobody-defined", frozenset())


# --- this is not the call slip, and does not touch its refusal -----------------


def test_a_call_slip_id_is_not_a_bundle_token(client):
    r = _redeem(client, "slip_01ANYTHING")
    assert r.status_code == 404


def test_a_bundle_token_is_not_a_call_slip(client):
    token_id = _issue(client).json()["token_id"]
    r = client.post("/tokens/redeem", json={"slip_id": token_id},
                    headers=_h(INDIVIDUATOR_TOKEN))
    assert r.status_code == 403, (
        "POST /tokens/redeem refuses on the plain listener, where no "
        "machine is authenticated")
    assert _token_row(client, token_id)["redeemed_at"] is None


def test_the_write_shadow_refuses_both_routes(client, monkeypatch):
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    assert _issue(client).status_code == 503
    assert _redeem(client, "bundle_x").status_code == 503


def test_an_unreadable_index_is_a_503_and_mints_nothing(client, monkeypatch,
                                                        tmp_path):
    monkeypatch.setattr(reference, "INDEX_DB", tmp_path / "gone.db")
    assert _issue(client).status_code == 503
    assert _token_count() == 0


def _break_the_index(monkeypatch, tmp_path):
    """An index the watcher replaced mid-request: `connect()` succeeds on
    it and the first read fails (issue_01M4RD3919HN309R1S2KHR0JDQ)."""
    broken = tmp_path / "broken-index.db"
    broken.write_bytes(b"not a database, " * 256)
    monkeypatch.setattr(reference, "INDEX_DB", broken)
    return broken


def test_issuing_against_an_index_that_cannot_be_read_is_a_503_and_mints_nothing(
        client, monkeypatch, tmp_path):
    broken = _break_the_index(monkeypatch, tmp_path)
    r = _issue(client)
    assert r.status_code == 503, r.text
    assert str(broken) not in r.text
    assert _token_count() == 0


def test_redeeming_against_an_index_that_cannot_be_read_is_a_503_and_spends_nothing(
        client, monkeypatch, tmp_path):
    token_id = _issue(client).json()["token_id"]
    broken = _break_the_index(monkeypatch, tmp_path)
    r = _redeem(client, token_id)
    assert r.status_code == 503, r.text
    assert str(broken) not in r.text
    assert _token_row(client, token_id)["redeemed_at"] is None


def test_a_redeemed_bundle_carries_an_agent_documents_role_and_no_other(client):
    _set_doc("p/agents/coder",
             frontmatter_extra=json.dumps({"role": "implementer"}))
    _set_doc("p/skills/s-both",
             frontmatter_extra=json.dumps({"role": "not-an-agent-role"}))
    issued = _issue(client)
    redeemed = _redeem(client, issued.json()["token_id"])
    assert redeemed.status_code == 200, redeemed.text
    docs = {d["id"]: d for d in redeemed.json()["documents"]}
    assert docs["p/agents/coder"]["role"] == "implementer"
    assert "role" not in docs["p/skills/s-both"]


def test_a_redeemed_bundle_carries_a_skill_documents_trigger(client):
    """issue_01M34SV3BDRHBTXZPZ8C4AH9HD: knowledge-ctx writes a rendered
    SKILL.md's description from the skill's `trigger`."""
    _set_doc("p/skills/s-both",
             frontmatter_extra=json.dumps({"trigger": "when both apply"}))
    issued = _issue(client)
    redeemed = _redeem(client, issued.json()["token_id"])
    assert redeemed.status_code == 200, redeemed.text
    docs = {d["id"]: d for d in redeemed.json()["documents"]}
    assert docs["p/skills/s-both"]["trigger"] == "when both apply"


# --- the real Reference Room -------------------------------------------------

LIVE_INDEX = Path.home() / ".knowledge-index" / "index.db"
LIVE_PROFILE = (Path.home() / "Convivy" / "knowledge" / "projects" / "library"
                / "profiles" / "daytime.yaml")

live = pytest.mark.skipif(
    not (LIVE_INDEX.is_file() and LIVE_PROFILE.is_file()),
    reason="needs the live knowledge index and the knowledge checkout; the "
           "machine that serves the Library has both, CI has neither")


@pytest.fixture
def live_client(client, monkeypatch):
    """The routes, the real index and the real `library/daytime` profile."""
    monkeypatch.setattr(reference, "INDEX_DB", LIVE_INDEX)
    include = yaml.safe_load(LIVE_PROFILE.read_text())["include"]
    crew_registry.profile_put(ORCHESTRATOR, "library", "daytime", include, [])
    for name in ("reviewer", "platform-coder", "security-reviewer"):
        crew_registry.roster_add(ORCHESTRATOR, name)
    return client


def _live_skill_ids_for(agent, projects):
    """The expected set, computed by SQL against the index with none of this
    repository's selection code in the path."""
    conn = sqlite3.connect(f"file:{LIVE_INDEX}?mode=ro", uri=True)
    try:
        marks = ",".join("?" * len(projects))
        return sorted(r[0] for r in conn.execute(
            f"SELECT d.id FROM docs d WHERE d.kind = 'skill' "
            f"AND d.status = 'current' AND d.project IN ({marks}) "
            f"AND EXISTS (SELECT 1 FROM json_each(d.audience) je "
            f"WHERE je.value = ?)", (*projects, agent)))
    finally:
        conn.close()


@live
def test_the_live_room_serves_a_reviewer_and_refuses_a_second_redemption(
        live_client):
    projects = [p for r in yaml.safe_load(LIVE_PROFILE.read_text())["include"]
                if r["kind"] == "skill" for p in r["project"]]
    expected = _live_skill_ids_for("reviewer", projects)
    assert expected, "the live index has no skill naming `reviewer`"

    issued = _issue(live_client, "reviewer", repo="library")
    assert issued.status_code == 201, issued.text
    redeemed = _redeem(live_client, issued.json()["token_id"])
    assert redeemed.status_code == 200, redeemed.text

    docs = redeemed.json()["documents"]
    skills = sorted(d["id"] for d in docs if d["kind"] == "skill")
    assert skills == expected
    assert [d["id"] for d in docs if d["kind"] == "agent"] == [
        "agent-platform/agents/reviewer"]
    for doc in docs:
        assert doc["status"] == "current"
        assert doc["body_sha256"] == sha(doc["body"]), doc["id"]

    assert _redeem(live_client, issued.json()["token_id"]).status_code == 409


@live
def test_the_live_platform_coder_is_never_served_an_empty_skill_set(
        live_client):
    """Today no skill names `platform-coder`, so this refuses. When the
    vocabulary is corrected it serves skills instead. What it must never do,
    before or after, is answer 201 with a definition and no skills."""
    r = _issue(live_client, "platform-coder", repo="library")
    if r.status_code == 201:
        assert [d for d in r.json()["documents"] if d["kind"] == "skill"]
    else:
        assert r.status_code == 409, r.text
        assert r.json()["kind"] == "skill" and r.json()["matched"] == 0
        assert r.json()["candidates"] > 0
        assert "platform-coder" in r.json()["error"]


@live
def test_the_live_wb_reviewer_reads_back_its_declared_role(live_client):
    r = live_client.get("/reference/documents/write-better/agents/wb-reviewer",
                        headers=_h())
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "writing-reviewer"
