"""`GET /ci/tracked` (CQ·245): `ci_tracked.tracked()`'s rows, served so
the Bosun's CI capture reads the pairs the merge-admission evaluator
requires instead of keeping its own copy of the rule.

Fixture slugs and repos are synthetic (`oldmill`, `quill`) — no real crew
name. The registry is a fixture file reached through
`projects.REGISTRY_PATH`, the way `test_ci_tracked.py` reaches it.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import app as libapp
import ci_tracked
import identity
import projects
import protocol

AGENT = "agent-token-oldmill"
AGENT_UNGRANTED = "agent-token-ungranted"
SYSTEM_UNGRANTED = "system-token-ungranted"
CUSTOMER = "customer-reader-token"
ORCHESTRATOR = "orchestrator-token"

REGISTRY = {
    "oldmill": {"lifecycle": "active", "owner": "convivy",
                "repos": ["oldmill-app", "oldmill-site"],
                "ci_workflows": {"oldmill-site": []}},
    "quill": {"lifecycle": "active", "owner": "convivy", "private": True,
              "kb": "local", "repos": ["quill-app"]},
    "_meta": {},
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        AGENT: {"name": "oldmill-agent", "products": ["oldmill"],
                "grants": ["registry.read"]},
        AGENT_UNGRANTED: {"name": "plain-agent", "products": ["oldmill"]},
        SYSTEM_UNGRANTED: {"name": "plain-system", "products": ["oldmill"],
                           "kind": "system"},
        CUSTOMER: {"name": "customer", "products": ["oldmill"],
                   "kind": "customer_reader"},
        ORCHESTRATOR: {"name": "co", "products": ["oldmill"],
                       "kind": "orchestrator"},
    }))
    monkeypatch.setattr(identity, "TOKENS", tokens)
    yield TestClient(libapp.app), tmp_path, monkeypatch
    projects.reload()


def _use_registry(tmp_path, monkeypatch, document):
    path = tmp_path / "projects.json"
    path.write_text(json.dumps(document))
    monkeypatch.setattr(projects, "REGISTRY_PATH", path)
    projects.reload()


def _h(token=AGENT):
    return {"Authorization": f"Bearer {token}",
            protocol.HEADER: str(protocol.CURRENT)}


def test_serves_exactly_the_tracked_rows(client):
    c, tmp_path, monkeypatch = client
    _use_registry(tmp_path, monkeypatch, REGISTRY)
    r = c.get("/ci/tracked", headers=_h())
    assert r.status_code == 200
    body = r.json()
    assert body["tracked"] == ci_tracked.tracked()
    assert body["count"] == 1
    assert body["tracked"] == [{"repo": "convivy/oldmill-app",
                                "workflow_file": "ci.yml",
                                "product": "oldmill",
                                "on_pull_request": True}]


def test_serves_a_pair_outside_the_callers_products(client):
    """No per-caller scope: a pair dropped here is a pair the capture job
    never visits."""
    c, tmp_path, monkeypatch = client
    document = dict(REGISTRY)
    document["harrow"] = {"lifecycle": "active", "owner": "convivy",
                          "repos": ["harrow-app"]}
    _use_registry(tmp_path, monkeypatch, document)
    r = c.get("/ci/tracked", headers=_h())
    assert r.status_code == 200
    assert {row["product"] for row in r.json()["tracked"]} == {"oldmill", "harrow"}


def test_an_empty_list_is_a_200_when_the_registry_is_readable(client):
    c, tmp_path, monkeypatch = client
    _use_registry(tmp_path, monkeypatch, {
        "oldmill": {"lifecycle": "active", "owner": "convivy",
                    "repos": ["oldmill-app"],
                    "ci_workflows": {"oldmill-app": []}}})
    r = c.get("/ci/tracked", headers=_h())
    assert r.status_code == 200
    assert r.json() == {"tracked": [], "count": 0}


@pytest.mark.parametrize("document", [None, {"_meta": {}}])
def test_refuses_503_when_the_registry_is_unusable(client, document):
    c, tmp_path, monkeypatch = client
    if document is None:
        monkeypatch.setattr(projects, "REGISTRY_PATH",
                            tmp_path / "does-not-exist.json")
        projects.reload()
    else:
        _use_registry(tmp_path, monkeypatch, document)
    r = c.get("/ci/tracked", headers=_h())
    assert r.status_code == 503
    assert "tracked" not in r.json()


@pytest.mark.parametrize("token", [AGENT_UNGRANTED, SYSTEM_UNGRANTED,
                                   CUSTOMER, ORCHESTRATOR])
def test_refuses_every_credential_without_registry_read(client, token):
    c, tmp_path, monkeypatch = client
    _use_registry(tmp_path, monkeypatch, REGISTRY)
    r = c.get("/ci/tracked", headers=_h(token))
    assert r.status_code == 403
    assert "registry.read" in r.json()["error"]


def test_refuses_without_a_credential(client):
    c, tmp_path, monkeypatch = client
    _use_registry(tmp_path, monkeypatch, REGISTRY)
    r = c.get("/ci/tracked", headers={protocol.HEADER: str(protocol.CURRENT)})
    assert r.status_code == 401


def test_refuses_an_unknown_parameter(client):
    c, tmp_path, monkeypatch = client
    _use_registry(tmp_path, monkeypatch, REGISTRY)
    r = c.get("/ci/tracked?product=oldmill", headers=_h())
    assert r.status_code == 400
