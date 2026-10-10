"""`POST /reference/promotions` — the epic's one non-pull-request path into the
Reference Room (issue_01M19QQZ76GSZYNH11JBDMA2EM, ADR-0048).

**Most of this file proves a boundary rather than a behaviour**, which is what
the definition of done asks for: the route refuses an agent credential, tested
by PRESENTING one rather than by reading the check, and the capability it
requires is structurally absent from the `agent` and `system` sets. Both halves
are here, because either alone is a control that can be removed without a test
failing.
"""
from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

import app as libapp
import identity
import protocol
import store

AGENT = "agent-token-vivy"
SYSTEM = "system-token"
CLIENT = "orient-token"

# A definition that passes `reference_admission.validate_frontmatter`, so a
# refusal in these tests is always the promotion clause rather than the schema.
DEFINITION = {
    "id": "platform/agents/scout",
    "kind": "agent",
    "project": "platform",
    "title": "Scout",
    "status": "current",
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = tmp_path / "control.db"
    conn = sqlite3.connect(db)
    conn.executescript("CREATE TABLE issue_items (id TEXT PRIMARY KEY);")
    conn.commit()
    conn.close()

    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        AGENT: {"name": "vivy-agent", "products": ["vivy"]},
        SYSTEM: {"name": "capture", "products": ["*"], "kind": "system"},
    }))
    client_tokens = tmp_path / "client-tokens.json"
    client_tokens.write_text(json.dumps({
        CLIENT: {"name": "orient", "products": ["*"]},
    }))

    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(identity, "CLIENT_TOKENS", client_tokens)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    return TestClient(libapp.app)


def _h(token):
    return {"Authorization": f"Bearer {token}",
            protocol.HEADER: str(protocol.CURRENT)}


def _promote(client, token=CLIENT, **body):
    payload = {"acting_human": "operator", "frontmatter": DEFINITION}
    payload.update(body)
    return client.post("/reference/promotions", json=payload, headers=_h(token))


# --- the boundary, proved by presenting credentials ---------------------------


@pytest.mark.parametrize("token", [AGENT, SYSTEM])
def test_an_agent_and_a_system_credential_are_both_refused(client, token):
    """**The definition of done's own wording: tested by presenting one rather
    than by reading the check.** A test that asserted `"reference.promote" not
    in ROLE_CAPS["agent"]` and stopped would pass against a route that never
    checked the capability at all."""
    r = _promote(client, token=token)
    assert r.status_code == 403
    assert "reference.promote" in r.json()["error"]


def test_the_capability_is_absent_from_the_agent_and_system_sets():
    """The structural half, which the presenting test above cannot prove. A
    route check alone is a control one edit removes; a capability absent from
    the sets is one no edit to this route can restore."""
    assert "reference.promote" not in identity.ROLE_CAPS["agent"]
    assert "reference.promote" not in identity.ROLE_CAPS["system"]
    assert "reference.promote" in identity.ROLE_CAPS["approved_client"]


def test_every_kind_a_token_file_can_name_is_refused(client, tmp_path,
                                                      monkeypatch):
    """**`NAMEABLE_KINDS` is what a token file may claim to be**, so a kind
    added there later must not inherit this route. Enumerated from the live
    set rather than listed, so a new kind is covered the day it exists rather
    than the day someone remembers this test."""
    for kind in sorted(identity.NAMEABLE_KINDS):
        tokens = tmp_path / f"api-tokens-{kind}.json"
        tokens.write_text(json.dumps({
            "t": {"name": "n", "products": ["*"], "kind": kind}}))
        monkeypatch.setattr(identity, "TOKENS", tokens)
        r = _promote(client, token="t")
        assert r.status_code == 403, f"{kind} reached the promotion route"


def test_human_is_not_a_kind_this_service_can_produce():
    """**The reason the item's specified check would have built a dead route.**
    It asked for `if ident.kind != "human"`, and `resolve()` never produces
    `human` — ADR-0038 settled that no human calls the Library directly. A
    route gated on it refuses every caller forever while reading as a live
    control. This pins the fact the design rests on, so a later reader who
    finds the item and the code disagreeing knows which one is right."""
    assert "human" not in identity.ROLE_CAPS
    assert "human" not in identity.NAMEABLE_KINDS


# --- the attested human -------------------------------------------------------


def test_the_acting_human_is_required(client):
    r = client.post("/reference/promotions",
                    json={"frontmatter": DEFINITION}, headers=_h(CLIENT))
    assert r.status_code == 403
    assert "acting_human" in r.json()["error"]


def test_the_response_records_the_human_and_the_client_separately(client):
    """The Bosun writes the commit, so it needs the person who authorized the
    promotion AND the client that carried it — recording only `attested_by`
    would put Orient in a commit a person authorized."""
    body = _promote(client).json()
    assert body["acting_human"] == "operator"
    assert body["attested_by"] == "orient"


# --- what may be promoted -----------------------------------------------------


def test_an_agent_definition_may_land(client):
    body = _promote(client).json()
    assert body["may_land"] is True
    assert body["errors"] == []
    assert body["clauses_refused"] == []


@pytest.mark.parametrize("kind", ["decision", "skill", "reference", "bead"])
def test_any_other_kind_is_refused_as_a_clause_not_a_400(client, kind):
    """**The narrowing that keeps this from being a general bypass of review.**
    The epic authorizes one document class to skip the pull request; a route
    taking any `kind` would let the whole Reference Room skip it. Answered as a
    clause rather than a 400, matching how `/reference/admission` answers every
    other content problem."""
    r = _promote(client, frontmatter={**DEFINITION, "kind": kind})
    assert r.status_code == 200
    body = r.json()
    assert body["may_land"] is False
    assert "agent_definition" in body["clauses_refused"]


def test_a_promotion_is_validated_as_hard_as_a_pull_request(client):
    """Skipping review must not also skip the schema: the same frontmatter
    clause `/reference/admission` runs, so a document a PR would have rejected
    is refused here too."""
    r = _promote(client, frontmatter={"kind": "agent"})
    body = r.json()
    assert body["may_land"] is False
    assert "frontmatter" in body["clauses_refused"]


def test_both_clauses_refuse_together_rather_than_one_at_a_time(client):
    """A caller fixing the schema should not then discover the document was
    never eligible. Both reasons arrive at once."""
    body = _promote(client, frontmatter={"kind": "decision"}).json()
    assert set(body["clauses_refused"]) == {"frontmatter", "agent_definition"}
    assert len(body["errors"]) > 1


def test_a_non_dict_frontmatter_is_a_may_land_false_not_a_500(client):
    """Matching `/reference/admission`'s own invariant: a content problem is
    answered `200 {may_land: false}`, never a crash. Reading `kind` off a
    non-dict must not raise."""
    r = _promote(client, frontmatter=["not", "a", "mapping"])
    assert r.status_code == 200
    assert r.json()["may_land"] is False


def test_the_checkout_clause_never_runs_here(client):
    """A promotion moves a definition that is not in the Reference Room yet, so
    there is no checkout to hold on it. Asserted rather than left implicit, so
    a later edit copying admission's clause wholesale fails instead of
    refusing every promotion."""
    assert "checkout" not in _promote(client).json()["clauses_checked"]
