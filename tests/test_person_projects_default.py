"""The creator default and the administrator's view (issue_01M2RSFKJBEHD5Y76X0B6XAYZG),
the fourth slice of person identity in the Library.

A person's set defaults to the projects the REGISTRY names them as having
created (`projects.slugs_created_by`), never a table this repository keeps
itself. Every registry entry here is synthetic — a temporary file under
`tmp_path`, pointed to by `projects.REGISTRY_PATH` — so this never reads or
writes the real `agent-platform/config/projects.json`, and no real person's
name appears in it.

**The schema is the real one**, built by `alembic upgrade head`, the way
`tests/test_person_projects_write.py` builds it, because the removal
representation this file exercises
(`person_product_removals`) is a migration this slice adds.

Six things are pinned, one test group each: the default for a person who
created projects and for one who created none; a grant and a default's
removal each surfacing as their own deviation, and a re-grant of a removed
default leaving none; the effective set as default plus grant minus
removal, bounded by the caller's scope; an unknown person and an empty-set
person still answering identically; an unavailable registry refusing
rather than reading as "created nothing"; and a non-administrator's
refusal to the admin view being the one an unknown person gets.
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

ADMIN = "person_admin_default_test"
PLAIN = "person_plain_default_test"
CREATOR = "person_creator_default_test"
NOTHING = "person_nothing_default_test"
GHOST = "person_ghost_default_test"

ADMIN_TOKEN = "admin-default-test-token"
PLAIN_TOKEN = "plain-default-test-token"
ORIENT_TOKEN = "orient-default-test-token"
SCOPED_TOKEN = "scoped-default-test-token"
SCOPED_ADMIN_TOKEN = "scoped-admin-default-test-token"


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
        [(ADMIN, "Ada Admin"), (PLAIN, "Pat Plain"),
         (CREATOR, "Cora Creator"), (NOTHING, "Nico Nothing")])
    conn.execute(
        "INSERT INTO administrators (person_id, granted_at, granted_by) "
        "VALUES (?, '2026-01-01', 'x')", (ADMIN,))
    conn.commit()
    conn.close()

    client_tokens = tmp_path / "client-tokens.json"
    client_tokens.write_text(json.dumps({
        ADMIN_TOKEN: {"name": ADMIN, "products": ["*"]},
        PLAIN_TOKEN: {"name": PLAIN, "products": ["*"]},
        ORIENT_TOKEN: {"name": "orient", "products": ["*"]},
        SCOPED_TOKEN: {"name": "a-scoped-client", "products": ["alpha"]},
        SCOPED_ADMIN_TOKEN: {"name": ADMIN, "products": ["alpha", "gamma"]},
    }))
    agent_tokens = tmp_path / "api-tokens.json"
    agent_tokens.write_text(json.dumps({}))

    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({
        "alpha": {"created_by": CREATOR},
        "beta": {"created_by": CREATOR},
        "gamma": {},
    }))
    monkeypatch.setattr(projects, "REGISTRY_PATH", registry)
    projects.reload()

    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "CLIENT_TOKENS", client_tokens)
    monkeypatch.setattr(identity, "TOKENS", agent_tokens)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    test_client = TestClient(libapp.app)
    test_client.db = db
    yield test_client
    projects.reload()


def _h(token=None):
    headers = {protocol.HEADER: str(protocol.CURRENT)}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _get(client, token, person):
    return client.get(f"/people/{person}/projects", headers=_h(token))


def _report(client, token, person):
    return client.get(f"/people/{person}/projects/report", headers=_h(token))


def _post(client, token, body, person):
    return client.post(f"/people/{person}/projects", json=body, headers=_h(token))


def _wire(response):
    return (response.status_code, response.content, dict(response.headers))


def _rows(db, sql):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql).fetchall()]
    finally:
        conn.close()


# --- the default: a person who created, and one who created nothing -------


def test_the_default_for_a_person_who_created_projects(client):
    r = _report(client, ADMIN_TOKEN, CREATOR)
    assert r.status_code == 200, r.text
    assert r.json()["default"] == ["alpha", "beta"]
    assert r.json()["projects"] == ["alpha", "beta"]
    assert r.json()["deviations"] == []


def test_the_default_for_a_person_who_created_nothing(client):
    r = _report(client, ADMIN_TOKEN, NOTHING)
    assert r.status_code == 200, r.text
    assert r.json()["default"] == []
    assert r.json()["projects"] == []
    assert r.json()["deviations"] == []


def test_the_plain_read_route_also_serves_the_default(client):
    """`GET /people/{person_id}/projects` — the three-caller read route, not
    the admin view — reflects the default too: a person who created projects
    sees them with no grant ever made."""
    r = _get(client, ORIENT_TOKEN, CREATOR)
    assert r.status_code == 200
    assert r.json() == {"projects": ["alpha", "beta"], "count": 2}
    r = _get(client, ORIENT_TOKEN, NOTHING)
    assert r.status_code == 200
    assert r.json() == {"projects": [], "count": 0}


# --- deviations: a grant and a default's removal, and restoring one -------


def test_a_grant_and_a_removed_default_each_appear_as_their_own_deviation(
        client):
    grant = _post(client, ADMIN_TOKEN, {"grant": ["gamma"]}, CREATOR)
    assert grant.status_code == 200, grant.text
    revoke = _post(client, ADMIN_TOKEN, {"revoke": ["alpha"]}, CREATOR)
    assert revoke.status_code == 200, revoke.text

    r = _report(client, ADMIN_TOKEN, CREATOR)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["default"] == ["alpha", "beta"]
    assert body["projects"] == ["beta", "gamma"]
    deviations = {(d["project"], d["action"]) for d in body["deviations"]}
    assert deviations == {("gamma", "grant"), ("alpha", "removal")}
    for d in body["deviations"]:
        assert d["by"] == ADMIN
        assert d["at"]
    # beta was never touched, and must not be folded into either deviation.
    assert "beta" not in [d["project"] for d in body["deviations"]]


def test_a_regrant_of_a_removed_default_leaves_no_deviation(client):
    _post(client, ADMIN_TOKEN, {"revoke": ["alpha"]}, CREATOR)
    before = _report(client, ADMIN_TOKEN, CREATOR).json()
    assert before["projects"] == ["beta"]
    assert [d["project"] for d in before["deviations"]] == ["alpha"]

    regrant = _post(client, ADMIN_TOKEN, {"grant": ["alpha"]}, CREATOR)
    assert regrant.status_code == 200, regrant.text

    after = _report(client, ADMIN_TOKEN, CREATOR).json()
    assert after["projects"] == ["alpha", "beta"]
    assert after["deviations"] == []
    # No stray row in either table — the restore deleted the removal marker
    # and never touched person_products, since a default project is never an
    # explicit grant.
    assert _rows(client.db, "SELECT * FROM person_product_removals "
                 "WHERE person_id = 'person_creator_default_test'") == []
    assert _rows(client.db, "SELECT * FROM person_products "
                 "WHERE person_id = 'person_creator_default_test' "
                 "AND product = 'alpha'") == []


def test_the_removal_and_the_restore_are_each_recorded_with_who_and_when(
        client):
    _post(client, ADMIN_TOKEN, {"revoke": ["alpha"]}, CREATOR)
    _post(client, SCOPED_ADMIN_TOKEN, {"grant": ["alpha"]}, CREATOR)
    changes = [c for c in _rows(client.db, "SELECT * FROM "
               "person_product_changes ORDER BY id") if c["product"] == "alpha"]
    assert [(c["action"], c["changed_by"]) for c in changes] == [
        ("revoke", ADMIN), ("grant", ADMIN)]
    assert all(c["changed_at"] for c in changes)


# --- the effective set: default + grant - removal, bounded by scope -------


def test_the_effective_set_is_default_plus_grant_minus_removal(client):
    _post(client, ADMIN_TOKEN, {"grant": ["gamma"], "revoke": ["alpha"]},
          CREATOR)
    r = _get(client, ORIENT_TOKEN, CREATOR)
    assert r.json() == {"projects": ["beta", "gamma"], "count": 2}


def test_the_effective_set_stays_bounded_by_the_callers_scope(client):
    """`SCOPED_TOKEN` sees only `alpha`. Even though `CREATOR`'s true
    effective set after this change is `{beta, gamma}` (alpha was just
    removed), a caller scoped to `alpha` alone must see nothing — it must
    never learn `beta` or `gamma` exist, and it must not see a removed
    `alpha` reappear just because that is all it is scoped to."""
    _post(client, ADMIN_TOKEN, {"grant": ["gamma"], "revoke": ["alpha"]},
          CREATOR)
    r = _get(client, SCOPED_TOKEN, CREATOR)
    assert r.status_code == 200
    assert r.json() == {"projects": [], "count": 0}
    assert "beta" not in json.dumps(r.json())
    assert "gamma" not in json.dumps(r.json())

    wide = _get(client, ORIENT_TOKEN, CREATOR).json()
    assert wide == {"projects": ["beta", "gamma"], "count": 2}


def test_the_admin_view_is_also_bounded_by_scope(client):
    """`SCOPED_ADMIN_TOKEN` is an administrator scoped to `alpha` and
    `gamma` only. Asking for `CREATOR`'s report must not disclose `beta`,
    which that credential cannot see, anywhere in the response."""
    r = _report(client, SCOPED_ADMIN_TOKEN, CREATOR)
    assert r.status_code == 200, r.text
    assert "beta" not in json.dumps(r.json())
    assert r.json()["default"] == ["alpha"]


# --- unknown person and empty-set person are identical ---------------------


def test_unknown_person_and_empty_set_person_are_identical_on_the_read_route(
        client):
    known_empty = _get(client, ORIENT_TOKEN, NOTHING)
    unknown = _get(client, ORIENT_TOKEN, GHOST)
    assert known_empty.status_code == unknown.status_code == 200
    assert known_empty.json() == unknown.json() == {"projects": [], "count": 0}


def test_unknown_person_and_empty_set_person_are_identical_on_the_admin_view(
        client):
    known_empty = _report(client, ADMIN_TOKEN, NOTHING)
    unknown = _report(client, ADMIN_TOKEN, GHOST)
    # A missing person is refused outright on the admin view (see the
    # non-administrator test below) — so this compares the ADMINISTRATOR's
    # own two answers for "nothing to report", not the write route's
    # byte-identity contract, since `GHOST` is refused with 404 here rather
    # than described as empty.
    assert unknown.status_code == 404
    assert known_empty.status_code == 200
    assert known_empty.json() == {
        "person_id": NOTHING, "projects": [], "count": 0,
        "default": [], "deviations": []}


# --- an unavailable registry fails closed, not "created nothing" ----------


def test_an_unavailable_registry_fails_closed_on_the_read_route(
        client, monkeypatch):
    monkeypatch.setattr(projects, "REGISTRY_PATH",
                        Path("/nonexistent/projects.json"))
    projects.reload()
    r = _get(client, ORIENT_TOKEN, CREATOR)
    assert r.status_code == 503
    projects.reload()


def test_an_unavailable_registry_fails_closed_on_the_admin_view(
        client, monkeypatch):
    monkeypatch.setattr(projects, "REGISTRY_PATH",
                        Path("/nonexistent/projects.json"))
    projects.reload()
    r = _report(client, ADMIN_TOKEN, CREATOR)
    assert r.status_code == 503
    projects.reload()


def test_an_unavailable_registry_fails_closed_on_the_write_route(
        client, monkeypatch):
    monkeypatch.setattr(projects, "REGISTRY_PATH",
                        Path("/nonexistent/projects.json"))
    projects.reload()
    r = _post(client, ADMIN_TOKEN, {"grant": ["alpha"]}, CREATOR)
    assert r.status_code == 503
    projects.reload()


# --- a non-administrator is refused like an unknown person on the view ----


def test_a_non_administrator_calling_the_view_is_refused_like_an_unknown_person(
        client):
    non_admin_names_a_real_person = _report(client, PLAIN_TOKEN, CREATOR)
    admin_names_a_missing_person = _report(client, ADMIN_TOKEN, GHOST)
    assert non_admin_names_a_real_person.status_code == 404
    assert _wire(non_admin_names_a_real_person) == _wire(
        admin_names_a_missing_person)


def test_no_credential_is_refused_like_an_unknown_person_on_the_view(client):
    r = client.get(f"/people/{CREATOR}/projects/report", headers=_h(None))
    assert r.status_code == 404


def test_report_refuses_an_unrecognised_parameter_for_an_administrator(client):
    """issue_01M1MTCTH3PW0DWDEXAZA5KFET: the route reads no parameters."""
    r = client.get(f"/people/{CREATOR}/projects/report?detail=full",
                   headers=_h(ADMIN_TOKEN))
    assert r.status_code == 400
    assert r.json()["unknown"] == ["detail"]
    assert r.json()["supported"] == []


def test_report_still_answers_404_to_a_non_administrator_sending_one(client):
    """The guard runs after the administrator check, so a caller the 404
    hides the route from does not learn it exists from a 400."""
    plain = client.get(f"/people/{CREATOR}/projects/report?detail=full",
                       headers=_h(PLAIN_TOKEN))
    bare = _report(client, PLAIN_TOKEN, CREATOR)
    assert plain.status_code == bare.status_code == 404
    assert plain.json() == bare.json()
