"""A credential that may do exactly one thing: write a review record.

`review_record_writer` (CQ·237, issue_01M5QE987V342E9NX422BTWW8J) is the
Bosun's credential for a private crew's own dispatch — a run under ADR-0043's
wall has neither the general verdict MCP server nor a `system`-kind token,
because `system` also reaches `signal.read` and everything else that role
carries, and a private crew's run must not be able to read anything back
through the credential that posts its own stamp.

Unlike `customer_reader` and `orchestrator`, this role is not a union over
`agent` — it is a literal one-capability set, the same shape `agent` and
`system` themselves are. These tests pin the capability delta directly and
then sweep every other route on the live app to confirm the token this role
produces is refused everywhere else, which is the half `test_orchestrator_role
.py`'s capability-set assertions alone cannot show.
"""
from __future__ import annotations

import json
import re
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

WRITER = "writer-token"
H = {"Library-Protocol": str(protocol.CURRENT)}


def _h(token=WRITER):
    return {**H, "Authorization": f"Bearer {token}"}


# --- the capability delta, asserted directly ----------------------------------


def test_the_role_holds_exactly_one_capability():
    """Not a union over `agent` or `system` — a literal, one-member set. If
    this ever reports a second capability, something widened the role."""
    assert identity.ROLE_CAPS["review_record_writer"] == frozenset(
        {"review_record.create"})


def test_the_role_cannot_even_read_a_review_record():
    """The whole point: this credential posts a private crew's own verdict
    and may do nothing else with it, not even read it back."""
    assert "review_record.read" not in identity.ROLE_CAPS["review_record_writer"]


def test_the_role_is_nameable_by_a_token_file(tmp_path, monkeypatch):
    tokens = tmp_path / "t.json"
    tokens.write_text(json.dumps({
        "tok": {"name": "private-dispatch", "products": ["fennick"],
               "kind": "review_record_writer"}}))
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(identity, "CLIENT_TOKENS", tmp_path / "absent.json")
    ident = identity.resolve("Bearer tok")
    assert ident.kind == "review_record_writer"
    assert identity.authorized(ident, "review_record.create") is True


def test_the_nameable_set_still_excludes_the_client_role():
    assert "review_record_writer" in identity.NAMEABLE_KINDS
    assert "approved_client" not in identity.NAMEABLE_KINDS
    assert identity.NAMEABLE_KINDS <= set(identity.ROLE_CAPS)


# --- refused on every other route ---------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch, vector_index):
    """A fully migrated database, because the sweep below calls every route
    on the live app — including ones that would 500 on a bare-bones schema
    before ever reaching the capability check this test is about."""
    db = tmp_path / "sweep.db"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LIBRARY_DB": str(db)})
    assert result.returncode == 0, result.stderr
    vector_index(db)

    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        WRITER: {"name": "private-dispatch", "products": ["fennick"],
                "kind": "review_record_writer"}}))
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({"_meta": {}, "fennick": {"repos": ["fennick"]}}))

    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    monkeypatch.setattr(projects, "REGISTRY_PATH", registry)
    projects.reload()
    yield TestClient(libapp.app)
    projects.reload()


# Every route this app DEFINES (not FastAPI's own auto-added `/docs`,
# `/redoc` and `/openapi.json`, which carry no credential check at all),
# except three this role must reach differently: `/health` needs no
# credential, `/whoami` and `/whoami/capabilities` are "gated on no
# capability" by their own docstrings — the whole point is that a narrow
# credential can read its own grant — and `POST /review-records` is the one
# route this role exists to reach. Enumerated from the app rather than
# hand-copied, matching `test_read_surface.py`'s own
# `test_every_route_requires_the_protocol_header_except_health` — a route
# added next month is swept automatically rather than silently skipped.
_EXEMPT = {("GET", "/health"), ("POST", "/review-records"),
          ("GET", "/whoami"), ("GET", "/whoami/capabilities")}


def _all_routes():
    seen = []
    for r in libapp.app.routes:
        methods = getattr(r, "methods", None)
        if not methods or not hasattr(r, "endpoint"):
            continue
        if getattr(r.endpoint, "__module__", "") != libapp.__name__:
            continue  # FastAPI's own docs/openapi/redoc routes
        concrete = re.sub(r"\{[^}]+\}", "nope", r.path)
        for method in sorted(methods):
            if method == "HEAD":
                continue
            if (method, r.path) in _EXEMPT:
                continue
            seen.append((method, concrete))
    return seen


@pytest.mark.parametrize("method,path", _all_routes())
def test_the_writer_token_is_refused_on_every_route_but_review_records(
        client, method, path):
    """**"Refused" means anything but success here, not a pinned status
    code.** A handful of routes check capability only after confirming the
    path's resource exists — `/items/{id}/claim` and its siblings answer a
    missing item with 404 before ever reaching `item.claim`, deliberately,
    so a capability check can never disclose whether an id exists
    (`heartbeat_item`'s own docstring makes the same trade for leases). This
    sweep's path parameters are a literal `"nope"`, so those routes are
    refused by that 404 rather than by `identity.authorized`'s 403 — still a
    full refusal of the action, which is what this test is about. A handful
    more validate the body or query first and answer 400. Any 2xx would mean
    this one-capability token did something only a wider credential should
    be able to."""
    r = client.request(method, path, headers=_h(), json={})
    assert r.status_code >= 400, f"{method} {path}: {r.status_code} {r.text}"


def test_the_writer_token_can_reach_review_records(client):
    """The other half of the sweep above: the one route this role exists
    for must actually accept it, not merely fail to be refused by it."""
    r = client.post(
        "/review-records", headers=_h(),
        json={"repo_identity": "github:convivy/fennick",
             "reviewed_ref": "a" * 40, "role": "reviewer", "verdict": "APPROVE"})
    assert r.status_code == 201, r.text
