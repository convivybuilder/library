"""`POST /people/{person_id}/projects` (issue_01M2RSF4MMRT7PBHJJPQ2BKEC7) — the
attested administrator and the write route that changes a person's project set.

**The schema is the real one**, built by `alembic upgrade head`, because the
route's history guarantee lives in a table this file's own migration creates and
a hand-written `CREATE TABLE` would test a copy.

**Credentials come only from the client-token file**, the way `bootstrap_library`
mints them: an entry whose `name` is a person id. That name is the whole
attested channel, so the fixtures make one administrator, one person who holds a
credential and is not an administrator, and one client (`orient`) whose name
matches no person at all.

Four things are pinned first and hardest, each by its own test: a real
administrator's name in `acting_human` with no such credential is refused (the
field is off the authorization path); a non-administrator's refusal is
byte-identical to a missing person's, compared directly; a revocation leaves its
history behind; and an earlier record survives a later change.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as libapp
import bootstrap_library
import identity
import projects
import protocol
import store

REPO = Path(__file__).resolve().parent.parent

ADMIN = "person_admin"
TARGET = "person_target"
PLAIN = "person_plain"
GHOST = "person_never_created"

ADMIN_TOKEN = "admin-client-token"
PLAIN_TOKEN = "plain-person-client-token"
ORIENT_TOKEN = "orient-client-token"
SCOPED_ADMIN_TOKEN = "scoped-admin-client-token"
AGENT_TOKEN = "agent-token"
SYSTEM_TOKEN = "system-token"
FORGED_AGENT_TOKEN = "agent-token-named-for-an-admin"
FORGED_SYSTEM_TOKEN = "system-token-named-for-an-admin"

SCOPED_ADMIN = "person_scoped_admin"


def _migrate(db):
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=str(REPO),
        capture_output=True, text=True, check=False,
        env={"PATH": "/usr/bin:/bin", "HOME": str(REPO), "LIBRARY_DB": str(db)})
    assert result.returncode == 0, result.stderr


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = tmp_path / "library.db"
    _migrate(db)
    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO people (id, display_name, created_at, created_by, "
        "updated_at, updated_by) VALUES (?, ?, '2026-01-01', 'x', "
        "'2026-01-01', 'x')",
        [(ADMIN, "Ada Admin"), (TARGET, "Tara Target"), (PLAIN, "Pat Plain"),
         (SCOPED_ADMIN, "Sam Scoped")])
    conn.executemany(
        "INSERT INTO administrators (person_id, granted_at, granted_by) "
        "VALUES (?, '2026-01-01', 'x')", [(ADMIN,), (SCOPED_ADMIN,)])
    conn.commit()
    conn.close()

    client_tokens = tmp_path / "client-tokens.json"
    client_tokens.write_text(json.dumps({
        ADMIN_TOKEN: {"name": ADMIN, "products": ["*"]},
        PLAIN_TOKEN: {"name": PLAIN, "products": ["*"]},
        ORIENT_TOKEN: {"name": "orient", "products": ["*"]},
        SCOPED_ADMIN_TOKEN: {"name": SCOPED_ADMIN, "products": ["vivy"]},
    }))
    # Orient's agent file is a store this repo does not own. An entry there can
    # carry any `name`, including an administrator's person id.
    agent_tokens = tmp_path / "api-tokens.json"
    agent_tokens.write_text(json.dumps({
        AGENT_TOKEN: {"name": "an-agent", "products": ["*"]},
        SYSTEM_TOKEN: {"name": "capture", "products": ["*"], "kind": "system"},
        FORGED_AGENT_TOKEN: {"name": ADMIN, "products": ["*"]},
        FORGED_SYSTEM_TOKEN: {"name": ADMIN, "products": ["*"],
                              "kind": "system"},
    }))

    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "CLIENT_TOKENS", client_tokens)
    monkeypatch.setattr(identity, "TOKENS", agent_tokens)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    # A synthetic registry naming no creator, so every default is empty and
    # these tests keep asserting on explicit grants alone. Without one the
    # routes fail closed with 503 wherever no agent-platform checkout exists,
    # which is CI.
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps(
        {"vivy": {}, "hollis": {}, "constance": {}}))
    monkeypatch.setattr(projects, "REGISTRY_PATH", registry)
    projects.reload()
    test_client = TestClient(libapp.app)
    test_client.db = db
    yield test_client
    projects.reload()


def _h(token=None):
    headers = {protocol.HEADER: str(protocol.CURRENT)}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _post(client, token, body, person=TARGET):
    return client.post(f"/people/{person}/projects", json=body, headers=_h(token))


def _wire(response):
    """Everything a caller can observe about a response, for a direct equality."""
    return (response.status_code, response.content, dict(response.headers))


def _rows(db, sql):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql).fetchall()]
    finally:
        conn.close()


def _changes(db):
    return _rows(db, "SELECT * FROM person_product_changes ORDER BY id")


def _held(db, person=TARGET):
    return [r["product"] for r in _rows(
        db, f"SELECT product FROM person_products WHERE person_id = '{person}' "
            f"ORDER BY product")]


# --- the field is off the authorization path -------------------------------


def test_a_real_administrators_name_in_acting_human_and_nothing_else_is_refused(
        client):
    """`orient` holds `person.write` and is not an administrator. The body names
    a real administrator in `acting_human`, which is all `attested_human` needs
    to accept. If the route branched on that field this call would succeed."""
    r = _post(client, ORIENT_TOKEN,
              {"grant": ["vivy"], "acting_human": ADMIN})
    assert r.status_code == 404
    assert _held(client.db) == []
    assert _changes(client.db) == []


def test_acting_human_in_a_query_string_or_header_is_no_better(client):
    r = client.post(f"/people/{TARGET}/projects?acting_human={ADMIN}",
                    json={"grant": ["vivy"]},
                    headers={**_h(ORIENT_TOKEN), "X-Acting-Human": ADMIN})
    assert r.status_code == 404
    assert _held(client.db) == []


def test_acting_human_naming_someone_else_does_not_change_who_is_recorded(
        client):
    r = _post(client, ADMIN_TOKEN,
              {"grant": ["vivy"], "acting_human": "somebody-else"})
    assert r.status_code == 200
    assert r.json()["changed_by"] == ADMIN
    assert [c["changed_by"] for c in _changes(client.db)] == [ADMIN]
    assert "somebody-else" not in json.dumps(_changes(client.db))
    assert "somebody-else" not in json.dumps(
        _rows(client.db, "SELECT * FROM person_products"))


# --- refusals disclose nothing ----------------------------------------------


def test_a_non_administrator_is_refused_byte_for_byte_like_a_missing_person(
        client):
    body = {"grant": ["vivy"]}
    non_admin_names_a_real_person = _post(client, PLAIN_TOKEN, body, TARGET)
    admin_names_a_missing_person = _post(client, ADMIN_TOKEN, body, GHOST)
    assert non_admin_names_a_real_person.status_code == 404
    assert _wire(non_admin_names_a_real_person) == _wire(
        admin_names_a_missing_person)


def test_every_refusal_before_the_body_is_one_response(client):
    body = {"grant": ["vivy"]}
    reference = _wire(_post(client, PLAIN_TOKEN, body))
    callers = {
        "no credential": _post(client, None, body),
        "an unknown token": _post(client, "not-a-token", body),
        "an approved client that is nobody's": _post(client, ORIENT_TOKEN, body),
        "a person who is not an administrator": _post(client, PLAIN_TOKEN, body),
        "an administrator naming nobody": _post(client, ADMIN_TOKEN, body, GHOST),
        "an agent credential": _post(client, AGENT_TOKEN, body),
        "a system credential": _post(client, SYSTEM_TOKEN, body),
        "an agent-file entry named for an administrator":
            _post(client, FORGED_AGENT_TOKEN, body),
        "a system-file entry named for an administrator":
            _post(client, FORGED_SYSTEM_TOKEN, body),
    }
    for name, response in callers.items():
        assert _wire(response) == reference, name
    assert _held(client.db) == []
    assert _changes(client.db) == []


def test_a_malformed_body_never_tells_a_refused_caller_from_an_administrator(
        client):
    """The body is read after authorization, so a caller who is refused sees the
    same 404 whatever they sent, and never the 400 an administrator would."""
    for raw in (b"not json", b"[]", b'{"grant": 5}', b"{}"):
        refused = client.post(f"/people/{TARGET}/projects", content=raw,
                              headers={**_h(PLAIN_TOKEN),
                                       "Content-Type": "application/json"})
        reference = client.post(f"/people/{TARGET}/projects",
                                json={"grant": ["vivy"]},
                                headers=_h(PLAIN_TOKEN))
        assert _wire(refused) == _wire(reference), raw


# --- the two halves of the gate ---------------------------------------------


def test_an_administrator_credential_is_refused_without_person_write(
        client, monkeypatch):
    monkeypatch.setattr(identity, "ROLE_CAPS", {
        **identity.ROLE_CAPS,
        "approved_client": identity.ROLE_CAPS["approved_client"]
        - {"person.write"}})
    r = _post(client, ADMIN_TOKEN, {"grant": ["vivy"]})
    assert r.status_code == 404
    assert _held(client.db) == []


def test_the_capability_is_held_by_approved_clients_only():
    assert "person.write" in identity.ROLE_CAPS["approved_client"]
    for kind, caps in identity.ROLE_CAPS.items():
        if kind != "approved_client":
            assert "person.write" not in caps, kind


def test_an_administrators_name_on_an_agent_file_entry_is_refused_even_if_the_capability_were_misgranted(
        client, monkeypatch):
    """The route also requires the approved-client kind: a `name` in Orient's
    agent file is not something this repo controls, so it is not the attested
    channel even when a capability table is wrong."""
    monkeypatch.setattr(identity, "ROLE_CAPS", {
        **identity.ROLE_CAPS,
        "agent": identity.ROLE_CAPS["agent"] | {"person.write"}})
    r = _post(client, FORGED_AGENT_TOKEN, {"grant": ["vivy"]})
    assert r.status_code == 404
    assert _held(client.db) == []


# --- a verified administrator changes a person's set ------------------------


def test_an_administrator_changes_a_set_and_the_read_route_shows_it(client):
    r = _post(client, ADMIN_TOKEN, {"grant": ["vivy", "hollis"]})
    assert r.status_code == 200
    assert r.json() == {"person_id": TARGET, "granted": ["hollis", "vivy"],
                        "revoked": [], "changed_by": ADMIN}
    read = client.get(f"/people/{TARGET}/projects", headers=_h(ADMIN_TOKEN))
    assert read.json() == {"projects": ["hollis", "vivy"], "count": 2}

    r = _post(client, ADMIN_TOKEN, {"grant": ["constance"], "revoke": ["vivy"]})
    assert r.status_code == 200
    read = client.get(f"/people/{TARGET}/projects", headers=_h(ADMIN_TOKEN))
    assert read.json() == {"projects": ["constance", "hollis"], "count": 2}


def test_an_administrator_may_change_their_own_set(client):
    r = _post(client, ADMIN_TOKEN, {"grant": ["vivy"]}, ADMIN)
    assert r.status_code == 200
    assert _held(client.db, ADMIN) == ["vivy"]


def test_the_credential_bootstrap_mints_is_the_attested_channel(
        tmp_path, monkeypatch):
    """End to end from a first install: `bootstrap_library` mints the credential
    and the administrator row, and that credential alone, presented over HTTP,
    changes a person's set. Nothing here is written by hand except the second
    person."""
    db = tmp_path / "first.db"
    _migrate(db)
    tokens = tmp_path / "state" / "client-tokens.json"
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    monkeypatch.setattr(identity, "CLIENT_TOKENS", tokens)
    monkeypatch.setattr(identity, "TOKENS", tmp_path / "no-agent-tokens.json")
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({"vivy": {}}))
    monkeypatch.setattr(projects, "REGISTRY_PATH", registry)
    projects.reload()
    person_id, token = bootstrap_library.bootstrap("Ada Lovelace")
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO people (id, display_name, created_at, created_by, "
        "updated_at, updated_by) VALUES (?, 'Grace', '2026-01-01', 'x', "
        "'2026-01-01', 'x')", (TARGET,))
    conn.commit()
    conn.close()

    ident = identity.resolve(f"Bearer {token}")
    assert (ident.kind, ident.actor) == ("approved_client", person_id)

    web = TestClient(libapp.app)
    r = web.post(f"/people/{TARGET}/projects", json={"grant": ["vivy"]},
                 headers=_h(token))
    assert r.status_code == 200, r.text
    assert r.json()["changed_by"] == person_id
    assert _held(db) == ["vivy"]
    projects.reload()


# --- history --------------------------------------------------------------


def test_a_revocation_leaves_its_history_behind(client):
    _post(client, ADMIN_TOKEN, {"grant": ["vivy", "hollis"]})
    granted = _rows(client.db, "SELECT * FROM person_products "
                    "WHERE product = 'vivy'")
    assert len(granted) == 1
    r = _post(client, ADMIN_TOKEN, {"revoke": ["vivy"]})
    assert r.status_code == 200
    assert _held(client.db) == ["hollis"]
    # The current-state row, and its granted_by/granted_at with it, are gone...
    assert _rows(client.db, "SELECT * FROM person_products "
                 "WHERE product = 'vivy'") == []
    # ...and the record of the grant and of the revocation both remain.
    vivy = [c for c in _changes(client.db) if c["product"] == "vivy"]
    assert [(c["action"], c["changed_by"]) for c in vivy] == [
        ("grant", ADMIN), ("revoke", ADMIN)]
    assert all(c["changed_at"] for c in vivy)
    assert vivy[0]["person_id"] == vivy[1]["person_id"] == TARGET


def test_an_earlier_record_survives_a_later_change(client):
    _post(client, ADMIN_TOKEN, {"grant": ["vivy"]})
    first = _changes(client.db)
    assert len(first) == 1
    _post(client, ADMIN_TOKEN, {"revoke": ["vivy"]})
    _post(client, ADMIN_TOKEN, {"grant": ["vivy", "hollis"]})
    later = _changes(client.db)
    assert later[:1] == first, "the earlier record was rewritten"
    assert [(c["product"], c["action"]) for c in later] == [
        ("vivy", "grant"), ("vivy", "revoke"), ("hollis", "grant"),
        ("vivy", "grant")]
    assert [c["id"] for c in later] == sorted(c["id"] for c in later)


def test_a_request_that_changes_nothing_records_nothing(client):
    _post(client, ADMIN_TOKEN, {"grant": ["vivy"]})
    before = _changes(client.db)
    r = _post(client, ADMIN_TOKEN, {"grant": ["vivy"], "revoke": ["hollis"]})
    assert r.status_code == 200
    assert r.json()["granted"] == [] and r.json()["revoked"] == []
    assert _changes(client.db) == before


def test_a_regrant_is_stamped_with_the_regranting_administrator_and_time(client):
    _post(client, ADMIN_TOKEN, {"grant": ["vivy"]})
    _post(client, ADMIN_TOKEN, {"revoke": ["vivy"]})
    _post(client, SCOPED_ADMIN_TOKEN, {"grant": ["vivy"]})
    row = _rows(client.db, "SELECT * FROM person_products")[0]
    assert row["granted_by"] == SCOPED_ADMIN
    assert [c["changed_by"] for c in _changes(client.db)] == [
        ADMIN, ADMIN, SCOPED_ADMIN]


# --- validation, seen only by a verified administrator ----------------------


@pytest.mark.parametrize("body", [
    {}, {"grant": []}, {"grant": "vivy"}, {"grant": [5]}, {"grant": [""]},
    {"grant": ["  "]}, {"grant": [["vivy"]]}, {"revoke": {"a": 1}},
    {"grant": ["x" * (store.MAX_IDENT + 1)]}, {"grant": ["vivy"], "revoke": ["vivy"]},
])
def test_a_malformed_change_is_a_400_for_an_administrator(client, body):
    r = _post(client, ADMIN_TOKEN, body)
    assert r.status_code == 400, r.text
    assert _changes(client.db) == []


def test_a_body_that_is_not_an_object_is_a_400_for_an_administrator(client):
    r = client.post(f"/people/{TARGET}/projects", content=b"[1]",
                    headers={**_h(ADMIN_TOKEN),
                             "Content-Type": "application/json"})
    assert r.status_code == 400
    r = client.post(f"/people/{TARGET}/projects", content=b"nope",
                    headers={**_h(ADMIN_TOKEN),
                             "Content-Type": "application/json"})
    assert r.status_code == 400


def test_names_are_stripped_and_deduplicated(client):
    r = _post(client, ADMIN_TOKEN, {"grant": [" vivy ", "vivy"]})
    assert r.status_code == 200
    assert _held(client.db) == ["vivy"]
    assert len(_changes(client.db)) == 1


# --- product scope ------------------------------------------------------------


def test_a_scoped_administrator_cannot_name_a_product_outside_its_scope(client):
    r = _post(client, SCOPED_ADMIN_TOKEN, {"grant": ["vivy", "constance"]})
    assert r.status_code == 403
    assert r.json()["projects"] == ["constance"]
    assert _held(client.db) == [], "a refused request must write nothing"
    assert _changes(client.db) == []


def test_a_scoped_administrator_may_change_a_product_inside_its_scope(client):
    r = _post(client, SCOPED_ADMIN_TOKEN, {"grant": ["vivy"]})
    assert r.status_code == 200
    assert _held(client.db) == ["vivy"]


# --- the read-only shadow and the race -------------------------------------


def test_the_write_is_refused_while_the_library_is_read_only(client, monkeypatch):
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    r = _post(client, ADMIN_TOKEN, {"grant": ["vivy"]})
    assert r.status_code == 503
    assert _held(client.db) == []


def test_an_administrator_removed_before_the_write_lock_writes_nothing(
        client, monkeypatch):
    """The route's read-only check passes, then the row goes away before
    `BEGIN IMMEDIATE`. The store re-checks under the lock and the route answers
    with the same refusal as any other, so no change is recorded for someone who
    was no longer an administrator."""
    real = store.change_person_products

    def remove_then_write(*args):
        conn = sqlite3.connect(client.db)
        conn.execute("DELETE FROM administrators WHERE person_id = ?", (ADMIN,))
        conn.commit()
        conn.close()
        return real(*args)

    monkeypatch.setattr(store, "change_person_products", remove_then_write)
    r = _post(client, ADMIN_TOKEN, {"grant": ["vivy"]})
    monkeypatch.setattr(store, "change_person_products", real)
    assert _wire(r) == _wire(_post(client, PLAIN_TOKEN, {"grant": ["vivy"]}))
    assert _held(client.db) == []
    assert _changes(client.db) == []


def test_the_store_change_refuses_a_non_administrator_and_a_missing_person(
        client):
    assert store.change_person_products(PLAIN, TARGET, ["vivy"], []) is None
    assert store.change_person_products(ADMIN, GHOST, ["vivy"], []) is None
    assert _held(client.db) == []
    assert _changes(client.db) == []
