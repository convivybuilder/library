"""`PUT /people/{person_id}/email` (issue_01M4QQZ7VTANJ5ZGF630DCB0Y7) — the
administrator route that sets or clears a person's sign-in email.

**Gated exactly as `POST /people/{person_id}/projects` is**, so this file's
fixture and its first block of tests are the email route's counterpart to
`test_person_projects_write.py`'s own: the credential comes only from the
client-token file, a real administrator's name in `acting_human` with no
such credential changes nothing, and a non-administrator's refusal is
byte-identical to a missing person's.

Every email in this file is a synthetic `example.com` address.
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
import identity
import projects
import protocol
import store

REPO = Path(__file__).resolve().parent.parent

ADMIN = "person_admin"
TARGET = "person_target"
OTHER = "person_other"
PLAIN = "person_plain"
GHOST = "person_never_created"

ADMIN_TOKEN = "admin-client-token"
PLAIN_TOKEN = "plain-person-client-token"
ORIENT_TOKEN = "orient-client-token"
AGENT_TOKEN = "agent-token"
FORGED_AGENT_TOKEN = "agent-token-named-for-an-admin"


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
        [(ADMIN, "Ada Admin"), (TARGET, "Tara Target"),
         (OTHER, "Oleg Other"), (PLAIN, "Pat Plain")])
    conn.execute(
        "INSERT INTO administrators (person_id, granted_at, granted_by) "
        "VALUES (?, '2026-01-01', 'x')", (ADMIN,))
    conn.execute(
        "UPDATE people SET sign_in_email = ? WHERE id = ?",
        ("oleg@example.com", OTHER))
    conn.commit()
    conn.close()

    client_tokens = tmp_path / "client-tokens.json"
    client_tokens.write_text(json.dumps({
        ADMIN_TOKEN: {"name": ADMIN, "products": ["*"]},
        PLAIN_TOKEN: {"name": PLAIN, "products": ["*"]},
        ORIENT_TOKEN: {"name": "orient", "products": ["*"]},
    }))
    agent_tokens = tmp_path / "api-tokens.json"
    agent_tokens.write_text(json.dumps({
        AGENT_TOKEN: {"name": "an-agent", "products": ["*"]},
        FORGED_AGENT_TOKEN: {"name": ADMIN, "products": ["*"]},
    }))

    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "CLIENT_TOKENS", client_tokens)
    monkeypatch.setattr(identity, "TOKENS", agent_tokens)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({"vivy": {}}))
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


def _put(client, token, body, person=TARGET):
    return client.put(f"/people/{person}/email", json=body, headers=_h(token))


def _wire(response):
    return (response.status_code, response.content, dict(response.headers))


def _email(db, person=TARGET):
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT sign_in_email FROM people WHERE id = ?", (person,)
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _changes(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM person_email_changes ORDER BY id").fetchall()]
    finally:
        conn.close()


# --- the field is off the authorization path --------------------------------


def test_a_real_administrators_name_in_acting_human_and_nothing_else_is_refused(
        client):
    r = _put(client, ORIENT_TOKEN,
             {"email": "tara@example.com", "acting_human": ADMIN})
    assert r.status_code == 404
    assert _email(client.db) is None
    assert _changes(client.db) == []


def test_acting_human_naming_someone_else_does_not_change_who_is_recorded(
        client):
    r = _put(client, ADMIN_TOKEN,
             {"email": "tara@example.com", "acting_human": "somebody-else"})
    assert r.status_code == 200
    assert r.json()["changed_by"] == ADMIN
    assert [c["changed_by"] for c in _changes(client.db)] == [ADMIN]
    assert "somebody-else" not in json.dumps(_changes(client.db))


# --- refusals disclose nothing -----------------------------------------------


def test_a_non_administrator_is_refused_byte_for_byte_like_a_missing_person(
        client):
    body = {"email": "tara@example.com"}
    non_admin_names_a_real_person = _put(client, PLAIN_TOKEN, body, TARGET)
    admin_names_a_missing_person = _put(client, ADMIN_TOKEN, body, GHOST)
    assert non_admin_names_a_real_person.status_code == 404
    assert _wire(non_admin_names_a_real_person) == _wire(
        admin_names_a_missing_person)


def test_every_refusal_before_the_body_is_one_response(client):
    body = {"email": "tara@example.com"}
    reference = _wire(_put(client, PLAIN_TOKEN, body))
    callers = {
        "no credential": _put(client, None, body),
        "an unknown token": _put(client, "not-a-token", body),
        "an approved client that is nobody's": _put(client, ORIENT_TOKEN, body),
        "a person who is not an administrator": _put(client, PLAIN_TOKEN, body),
        "an administrator naming nobody": _put(client, ADMIN_TOKEN, body, GHOST),
        "an agent credential": _put(client, AGENT_TOKEN, body),
        "an agent-file entry named for an administrator":
            _put(client, FORGED_AGENT_TOKEN, body),
    }
    for name, response in callers.items():
        assert _wire(response) == reference, name
    assert _email(client.db) is None
    assert _changes(client.db) == []


def test_a_malformed_body_never_tells_a_refused_caller_from_an_administrator(
        client):
    for raw in (b"not json", b"[]", b'{"email": 5}', b"{}"):
        refused = client.put(f"/people/{TARGET}/email", content=raw,
                             headers={**_h(PLAIN_TOKEN),
                                      "Content-Type": "application/json"})
        reference = client.put(f"/people/{TARGET}/email",
                               json={"email": "tara@example.com"},
                               headers=_h(PLAIN_TOKEN))
        assert _wire(refused) == _wire(reference), raw


def test_a_conflict_refusal_never_names_the_other_person(client):
    r = _put(client, ADMIN_TOKEN, {"email": "oleg@example.com"})
    assert r.status_code == 409
    assert OTHER not in json.dumps(r.json())
    assert "Oleg" not in json.dumps(r.json())
    assert _email(client.db) is None


# --- setting and clearing ----------------------------------------------------


def test_an_administrator_sets_an_email_and_it_is_stored_trimmed_and_lowered(
        client):
    r = _put(client, ADMIN_TOKEN, {"email": "  Tara@Example.COM  "})
    assert r.status_code == 200
    assert r.json() == {"person_id": TARGET, "email": "tara@example.com",
                        "changed_by": ADMIN}
    assert _email(client.db) == "tara@example.com"


def test_an_administrator_clears_an_email(client):
    _put(client, ADMIN_TOKEN, {"email": "tara@example.com"})
    r = _put(client, ADMIN_TOKEN, {"email": None})
    assert r.status_code == 200
    assert r.json() == {"person_id": TARGET, "email": None,
                        "changed_by": ADMIN}
    assert _email(client.db) is None


def test_an_administrator_may_set_their_own_email(client):
    r = _put(client, ADMIN_TOKEN, {"email": "ada@example.com"}, ADMIN)
    assert r.status_code == 200
    assert _email(client.db, ADMIN) == "ada@example.com"


def test_two_people_may_not_hold_the_same_email_under_any_casing(client):
    r = _put(client, ADMIN_TOKEN, {"email": "OLEG@EXAMPLE.COM"})
    assert r.status_code == 409
    assert _email(client.db) is None


def test_re_setting_a_persons_own_email_is_not_a_conflict(client):
    _put(client, ADMIN_TOKEN, {"email": "tara@example.com"})
    r = _put(client, ADMIN_TOKEN, {"email": "TARA@example.com"})
    assert r.status_code == 200
    assert _email(client.db) == "tara@example.com"


# --- validation, seen only by a verified administrator -----------------------


@pytest.mark.parametrize("raw", [
    "no-at-sign.example.com", "two@at@example.com", "has space@example.com",
    "internal\ttab@example.com", "", "   ", 5, ["a@example.com"],
])
def test_an_implausible_or_wrong_typed_email_is_a_400(client, raw):
    """`None` is deliberately absent from this list — `{"email": null}` is
    the documented way to CLEAR an email, not a malformed value, which is
    exactly what distinguishes this route's validation from `POST
    /people/by-email/projects`'s, where every one of these same values
    including `None` is a 400."""
    r = _put(client, ADMIN_TOKEN, {"email": raw})
    assert r.status_code == 400, (raw, r.text)
    assert _changes(client.db) == []


def test_surrounding_whitespace_is_trimmed_rather_than_refused(client):
    """The negative control for the parametrize list above: a space WITHIN
    the address is a 400, but one around it is not an address defect at
    all — `_parse_email` strips it before checking for whitespace."""
    r = _put(client, ADMIN_TOKEN, {"email": "  tara@example.com  "})
    assert r.status_code == 200
    assert r.json()["email"] == "tara@example.com"


def test_an_email_over_the_bound_is_a_400(client):
    too_long = "a" * (store.MAX_EMAIL - len("@example.com") + 1) + "@example.com"
    assert len(too_long) > store.MAX_EMAIL
    r = _put(client, ADMIN_TOKEN, {"email": too_long})
    assert r.status_code == 400
    assert _changes(client.db) == []


def test_a_body_missing_the_email_field_is_a_400(client):
    r = _put(client, ADMIN_TOKEN, {})
    assert r.status_code == 400


# --- history ------------------------------------------------------------------


def test_each_change_is_recorded_with_its_administrator_and_time(client):
    r = _put(client, ADMIN_TOKEN, {"email": "tara@example.com"})
    assert r.status_code == 200
    rows = _changes(client.db)
    assert len(rows) == 1
    assert rows[0]["person_id"] == TARGET
    assert rows[0]["email"] == "tara@example.com"
    assert rows[0]["changed_by"] == ADMIN
    assert rows[0]["changed_at"]


def test_an_earlier_record_survives_a_later_change(client):
    _put(client, ADMIN_TOKEN, {"email": "tara@example.com"})
    first = _changes(client.db)
    _put(client, ADMIN_TOKEN, {"email": None})
    later = _changes(client.db)
    assert later[:1] == first, "the earlier record was rewritten"
    assert [c["email"] for c in later] == ["tara@example.com", None]


def test_a_request_that_changes_nothing_is_still_recorded(client):
    # `store.set_person_email` records every request an administrator makes,
    # including one that leaves the value as it was: clearing an email that
    # is already clear, and re-setting the email already held.
    _put(client, ADMIN_TOKEN, {"email": None})
    _put(client, ADMIN_TOKEN, {"email": "tara@example.com"})
    _put(client, ADMIN_TOKEN, {"email": "TARA@example.com"})
    assert [c["email"] for c in _changes(client.db)] == [
        None, "tara@example.com", "tara@example.com"]
    assert all(c["changed_by"] == ADMIN for c in _changes(client.db))


# --- the read-only shadow -----------------------------------------------------


def test_the_write_is_refused_while_the_library_is_read_only(client, monkeypatch):
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    r = _put(client, ADMIN_TOKEN, {"email": "tara@example.com"})
    assert r.status_code == 503
    assert _email(client.db) is None


def test_the_store_change_refuses_a_non_administrator_and_a_missing_person(
        client):
    assert store.set_person_email(PLAIN, TARGET, "tara@example.com") is None
    assert store.set_person_email(ADMIN, GHOST, "tara@example.com") is None
    assert _email(client.db) is None
