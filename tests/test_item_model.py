"""An operator-set model choice on a work item (issue_01M38WTBNAS3NBGEYR8349RR52).

**The capability is `item.model`, and only an `approved_client` resolves to it.**
An agent, a system and an orchestrator credential are each refused, and a
client whose entry withholds it is too. **The choice is written by one route.**
`/correct`, `/revision` and `POST /items` refuse `model_choice` by name.
**`acting_human` is required and recorded as sent**, and the client recorded is
the authenticated credential's name, never a body field.

The fixture is `test_approve.py`'s: the same hand-built schema, tokens and
items, so the two human-reserved routes are tested against one world.
"""
from __future__ import annotations

import identity
import protocol
import pytest

import test_approve
from test_approve import (
    AGENT, BOSUN, HUMAN, OPERATOR, ORCH, SYSTEM, _db, _h, _notes, _row, _ts)

# The one fixture, shared rather than copied.
client = test_approve.client


def _set(client, item_id="epic_a", token=OPERATOR, **body):
    body.setdefault("acting_human", HUMAN)
    body.setdefault("model", "claude-opus-5-5")
    return client.post(f"/items/{item_id}/model", json=body, headers=_h(token))


def _untouched(item_id="epic_a"):
    assert _row(item_id)["model_choice"] is None
    assert _notes(item_id) == []


def test_the_capability_is_human_reserved():
    assert "item.model" in identity.HUMAN_RESERVED_CAPS
    assert "item.model" in identity.ROLE_CAPS["approved_client"]
    assert "item.model" not in identity.ROLE_CAPS["agent"]
    assert "item.model" not in identity.ROLE_CAPS["system"]


@pytest.mark.parametrize("token", [AGENT, SYSTEM, ORCH])
def test_an_agent_system_or_orchestrator_credential_is_refused(client, token):
    r = _set(client, token=token)
    assert r.status_code == 403 and "item.model" in r.json()["error"]
    _untouched()


def test_a_withheld_client_is_refused(client, tmp_path, monkeypatch):
    import json
    path = tmp_path / "client-tokens.json"
    path.write_text(json.dumps({
        BOSUN: {"name": "bosun", "products": ["*"],
                "withholds": ["item.model"]}}))
    monkeypatch.setattr(identity, "CLIENT_TOKENS", path)
    r = _set(client, token=BOSUN)
    assert r.status_code == 403 and "item.model" in r.json()["error"]
    _untouched()


def test_no_credential_is_refused(client):
    r = client.post("/items/epic_a/model",
                    json={"model": "m", "acting_human": HUMAN},
                    headers={protocol.HEADER: str(protocol.CURRENT)})
    assert r.status_code == 401
    _untouched()


def test_an_approved_client_sets_and_the_record_is_read_back(client):
    r = _set(client, model="claude-opus-5-5", reason="the roster pin is too weak")
    assert r.status_code == 200, r.text
    choice = r.json()["item"]["model_choice"]
    assert choice["model"] == "claude-opus-5-5"
    assert choice["reason"] == "the roster pin is too weak"
    assert choice["set_by_client"] == "deck"
    assert choice["acting_human"] == HUMAN
    assert choice["set_at"].endswith("Z")
    got = client.get("/items/epic_a", headers=_h(AGENT)).json()
    assert (got.get("item") or got)["model_choice"] == choice


def test_the_client_is_the_credential_and_the_human_is_as_sent(client):
    r = _set(client, acting_human="someone@example.com")
    choice = r.json()["item"]["model_choice"]
    assert choice["set_by_client"] == "deck"
    assert choice["acting_human"] == "someone@example.com"


def test_an_item_with_no_choice_reads_null(client):
    got = client.get("/items/epic_a", headers=_h(AGENT)).json()
    assert (got.get("item") or got)["model_choice"] is None


def test_a_clear_removes_the_choice_and_is_recorded(client):
    assert _set(client, reason="why").status_code == 200
    r = _set(client, model=None, reason="back to the pin")
    assert r.status_code == 200
    assert r.json()["item"]["model_choice"] is None
    row = _row("epic_a")
    assert all(row[c] is None for c in (
        "model_choice", "model_reason", "model_set_by_client",
        "model_acting_human", "model_set_at"))
    notes = _notes("epic_a")
    assert len(notes) == 2
    assert "cleared" in notes[1] and "deck" in notes[1] and HUMAN in notes[1]
    assert "back to the pin" in notes[1]


def test_every_set_writes_a_comment_naming_client_human_model_and_reason(client):
    _set(client, model="claude-sonnet-5", reason="cheaper for this one")
    (note,) = _notes("epic_a")
    for fragment in ("claude-sonnet-5", "deck", HUMAN, "cheaper for this one"):
        assert fragment in note


def test_a_second_set_replaces_the_first(client):
    _set(client, model="m1")
    r = _set(client, model="m2")
    assert r.json()["item"]["model_choice"]["model"] == "m2"
    assert len(_notes("epic_a")) == 2


@pytest.mark.parametrize("body", [
    {"acting_human": None}, {"acting_human": ""}, {"acting_human": "  "}])
def test_acting_human_is_required(client, body):
    r = client.post("/items/epic_a/model",
                    json={"model": "m", **body}, headers=_h(OPERATOR))
    assert r.status_code == 403
    _untouched()


def test_a_missing_acting_human_key_is_refused(client):
    r = client.post("/items/epic_a/model", json={"model": "m"},
                    headers=_h(OPERATOR))
    assert r.status_code == 403
    _untouched()


@pytest.mark.parametrize("body", [{}, {"model": ""}, {"model": 7},
                                  {"model": "x" * 500},
                                  {"model": "m", "reason": 3}])
def test_a_malformed_choice_is_refused_and_writes_nothing(client, body):
    r = client.post("/items/epic_a/model",
                    json={"acting_human": HUMAN, **body}, headers=_h(OPERATOR))
    assert r.status_code == 400
    _untouched()


def test_a_body_that_is_not_an_object_is_refused(client):
    r = client.post("/items/epic_a/model", json=["m"], headers=_h(OPERATOR))
    assert r.status_code == 400
    _untouched()


def test_an_unknown_item_and_a_terminal_item(client):
    assert _set(client, item_id="nope").status_code == 404
    assert _set(client, item_id="epic_done").status_code == 409
    _untouched("epic_done")


def test_an_item_outside_the_clients_scope_reads_as_absent(client, tmp_path,
                                                            monkeypatch):
    import json
    path = tmp_path / "scoped.json"
    path.write_text(json.dumps({OPERATOR: {"name": "deck",
                                           "products": ["constance"]}}))
    monkeypatch.setattr(identity, "CLIENT_TOKENS", path)
    assert _set(client, item_id="epic_a").status_code == 404
    _untouched()


def test_an_item_held_by_another_session_refuses_the_write(client):
    conn = _db()
    stamp = _ts(seconds=5)
    conn.execute(
        "INSERT INTO issue_leases (id, root_id, taken_for, holder, taken_at, "
        "heartbeat_at) VALUES ('l1','epic_a','epic_a','someone-else',?,?)",
        (stamp, stamp))
    conn.commit()
    conn.close()
    r = _set(client)
    assert r.status_code == 409 and r.json()["reason"] == "held"
    _untouched()
    assert _set(client, owner="someone-else").status_code == 200


def test_any_item_type_may_carry_a_choice(client):
    assert _set(client, item_id="a_bug").status_code == 200


# --- the other doors are shut --------------------------------------------------------


@pytest.mark.parametrize("token", [AGENT, OPERATOR])
def test_correct_refuses_the_field_by_name(client, token):
    r = client.post("/items/epic_a/correct",
                    json={"model_choice": {"model": "m"}}, headers=_h(token))
    assert r.status_code == 400
    assert r.json()["route"] == "POST /items/{item_id}/model"
    _untouched()


@pytest.mark.parametrize("token", [AGENT, OPERATOR])
def test_revision_refuses_the_field_by_name(client, token):
    r = client.post("/items/epic_a/revision",
                    json={"model_choice": "m", "title": "t", "owner": "me"},
                    headers=_h(token))
    assert r.status_code == 400
    assert r.json()["route"] == "POST /items/{item_id}/model"
    assert _row("epic_a")["title"] == "a title"
    _untouched()


def test_item_creation_refuses_the_field_by_name(client):
    r = client.post("/items", json={
        "product": "vivy", "title": "t", "severity": "low",
        "model_choice": "m"}, headers=_h(AGENT))
    assert r.status_code == 400
    assert r.json()["route"] == "POST /items/{item_id}/model"
    conn = _db()
    assert conn.execute("SELECT COUNT(*) FROM issue_items WHERE title = 't'"
                        ).fetchone()[0] == 0
    conn.close()
