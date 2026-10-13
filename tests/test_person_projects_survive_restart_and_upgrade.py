"""Clause 10 of the person-identity epic (issue_01M2QWC5NZW824FV6DKNFSEJEY):
"A person's set survives a Library restart and an Alembic upgrade, shown by
reading it back after each." (issue_01M35R7YXVJRZ1CJF4WQ647NXM)

**Upgrades to `0652cc6b2cee`, not to `76bfd9912739` where `person_products`
itself is created.** `person_products` alone is not enough to drive the write
path the app and `test_person_projects_write.py` use: the route also checks
`administrators` (`b59b16050865`) before it will act, and
`store.change_person_products` records every change in
`person_product_changes` (`0652cc6b2cee`) in the same transaction. `0652cc6b2cee`
is the earliest revision where all three exist, so it is the earliest point this
test can write through the real path rather than by hand. Four ordinary
migrations still sit between it and head — bundle tokens, `item_model_choice`,
`signing_certificates`, and `epic_id` on ledger entries, none of which touch a
`person_*` table — so the later `alembic upgrade head` below genuinely runs
migrations over the rows this test writes, which the revision-identity
assertions confirm directly rather than trusting the choice of revision.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

import app as libapp
import identity
import projects
import protocol
import store

REPO = Path(__file__).resolve().parent.parent

# The earliest revision with `person_products`, `administrators` and
# `person_product_changes` all present — see the module docstring.
EARLIEST_FULL_WRITE_PATH = "0652cc6b2cee"

ADMIN = "person_admin"
ADMIN_TOKEN = "admin-client-token"
WITH_PROJECTS = "person_with_projects"
WITH_NOTHING = "person_with_nothing"


def _alembic(*args, db):
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(REPO), capture_output=True, text=True, check=False,
        env={"PATH": "/usr/bin:/bin", "HOME": str(REPO), "LIBRARY_DB": str(db)})


def _seed_people_and_administrator(db):
    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO people (id, display_name, created_at, created_by, "
        "updated_at, updated_by) VALUES (?, ?, '2026-01-01', 'x', "
        "'2026-01-01', 'x')",
        [(ADMIN, "Ada Admin"), (WITH_PROJECTS, "Pat Projects"),
         (WITH_NOTHING, "Nico Nothing")])
    conn.execute(
        "INSERT INTO administrators (person_id, granted_at, granted_by) "
        "VALUES (?, '2026-01-01', 'x')", (ADMIN,))
    conn.commit()
    conn.close()


def _app_instance(db, tmp_path, monkeypatch):
    """A fresh `TestClient` over the same file — the restart. `store` and
    `app` hold no cached connection or schema state between requests (every
    route opens its own `sqlite3.Connection` against `store.DB_PATH` when
    it runs), so a new client is what a new process would see too."""
    tokens = tmp_path / "client-tokens.json"
    tokens.write_text(json.dumps({
        ADMIN_TOKEN: {"name": ADMIN, "products": ["*"]},
    }))
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "CLIENT_TOKENS", tokens)
    monkeypatch.setattr(identity, "TOKENS", tmp_path / "no-agent-tokens.json")
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    return TestClient(libapp.app)


def _headers():
    return {"Authorization": f"Bearer {ADMIN_TOKEN}",
            protocol.HEADER: str(protocol.CURRENT)}


def _read(client, person):
    r = client.get(f"/people/{person}/projects", headers=_headers())
    assert r.status_code == 200, r.text
    return r.json()


def test_a_persons_project_set_survives_an_alembic_upgrade_and_a_restart(
        tmp_path, monkeypatch):
    db = tmp_path / "library.db"

    built = _alembic("upgrade", EARLIEST_FULL_WRITE_PATH, db=db)
    assert built.returncode == 0, built.stderr
    current = _alembic("current", db=db)
    assert EARLIEST_FULL_WRITE_PATH in current.stdout, current.stdout
    head_id = _alembic("heads", db=db).stdout.split()[0]
    assert head_id != EARLIEST_FULL_WRITE_PATH, (
        "no migration sits between this revision and head, so upgrading to "
        "head below would not exercise a real migration over existing rows")

    _seed_people_and_administrator(db)
    # A synthetic registry naming no creator, so every default is empty and
    # this test keeps asserting on explicit grants alone. Without one the
    # routes fail closed with 503 wherever no agent-platform checkout exists,
    # which is CI.
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({"vivy": {}, "hollis": {}}))
    monkeypatch.setattr(projects, "REGISTRY_PATH", registry)
    projects.reload()
    writer = _app_instance(db, tmp_path, monkeypatch)

    grant = writer.post(f"/people/{WITH_PROJECTS}/projects",
                        json={"grant": ["vivy", "hollis"]}, headers=_headers())
    assert grant.status_code == 200, grant.text
    # `WITH_NOTHING` gets no call at all: the route refuses a grant/revoke
    # naming nothing (`test_a_malformed_change_is_a_400_for_an_administrator`),
    # so "a person with an empty set" is a person the write path has never
    # touched, read back through the same route as the one that was.

    written = {WITH_PROJECTS: _read(writer, WITH_PROJECTS),
               WITH_NOTHING: _read(writer, WITH_NOTHING)}
    assert written[WITH_PROJECTS] == {"projects": ["hollis", "vivy"], "count": 2}
    assert written[WITH_NOTHING] == {"projects": [], "count": 0}

    upgraded = _alembic("upgrade", "head", db=db)
    assert upgraded.returncode == 0, upgraded.stderr
    current = _alembic("current", db=db)
    assert head_id in current.stdout, (
        "the upgrade did not reach head: " + current.stdout + current.stderr)

    reader = _app_instance(db, tmp_path, monkeypatch)
    after = {WITH_PROJECTS: _read(reader, WITH_PROJECTS),
             WITH_NOTHING: _read(reader, WITH_NOTHING)}
    assert after == written
    projects.reload()
