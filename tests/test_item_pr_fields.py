"""`pr`, `pr_url` and `pr_repo` on every route that serialises a work item
(issue_01M2P5BPTZEANEZNCK6JK8GY4H).

`GET /items/{id}` and every list route share one serialiser, `store.item_dict`,
so these three fields are added there once rather than per route. These tests
pin that to all four call sites Deck depends on — the detail route, the two
`/items` list shapes Deck's cockpit reads (`?product=&state=` and
`?parent_id=`), and a Course's resolved children — rather than trusting that a
fix to one carries to the rest.

Built on a database from the real migration, matching `test_course_position.py`'s
reason: a Course's `entries` carries a real foreign key against `issue_items`.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as libapp
import identity
import store

REPO = Path(__file__).resolve().parent.parent
AGENT = "agent-token-wide"
H = {"Library-Protocol": "1"}

LINKED_URL = "https://github.com/convivy/library/pull/9"


def _h(token=AGENT):
    return {**H, "Authorization": f"Bearer {token}"}


def _ts(**delta):
    return (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
            - dt.timedelta(**delta)).isoformat(" ")


def _insert_item(db_path, **fields):
    defaults = {
        "body": "b", "refs": "{}", "severity": "medium", "state": "open",
        "created_by": "seed", "created_at": _ts(days=1), "needs": "agent",
        "created_by_kind": "agent",
    }
    defaults.update(fields)
    conn = sqlite3.connect(db_path)
    try:
        cols = ", ".join(defaults)
        marks = ", ".join("?" * len(defaults))
        conn.execute(f"INSERT INTO issue_items ({cols}) VALUES ({marks})",
                    list(defaults.values()))
        conn.commit()
    finally:
        conn.close()


def _insert_course(db_path, *, id, product, entries, status=store.COURSE_LIVE,
                   title="the plan"):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO courses (id, product, title, entries, status, "
            "version, created_by, created_at) VALUES (?,?,?,?,?,1,?,?)",
            (id, product, title, json.dumps(entries), status, "seed",
             _ts(days=1)))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def db_path(tmp_path):
    db = tmp_path / "control.db"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO, capture_output=True, text=True, check=False,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LIBRARY_DB": str(db)})
    assert result.returncode == 0, result.stderr
    return db


@pytest.fixture
def client(db_path, tmp_path, monkeypatch):
    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        AGENT: {"name": "wide-agent", "products": ["*"]}}))
    monkeypatch.setattr(store, "DB_PATH", db_path)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    # NEVER the operator's real approved-client store: a plain `TOKENS` patch
    # leaves `identity.CLIENT_TOKENS` pointed at `~/.library/client-tokens.json`
    # (or `LIBRARY_CLIENT_TOKENS`), which `_client_tokens()` reads on every
    # request. Point it at a path under `tmp_path` that is never created —
    # `_client_tokens()` treats a missing file as an empty store, so this reads
    # as "no approved clients" without ever touching the real file.
    monkeypatch.setattr(identity, "CLIENT_TOKENS", tmp_path / "client-tokens.json")
    return TestClient(libapp.app)


def _item(resp_json: dict, item_id: str) -> dict:
    [item] = [i for i in resp_json["items"] if i["id"] == item_id]
    return item


def test_client_fixture_never_points_at_the_operators_real_credential_store(
        client, tmp_path):
    """The `client` fixture must patch `identity.CLIENT_TOKENS` to a path under
    `tmp_path`, never leave it at its default (`~/.library/client-tokens.json`
    or `LIBRARY_CLIENT_TOKENS`) — `_client_tokens()` reads that file on every
    request, and the default is the operator's real, plaintext approved-client
    credential store."""
    assert identity.CLIENT_TOKENS == tmp_path / "client-tokens.json"
    assert not identity.CLIENT_TOKENS.exists()


# --- the detail route, which every other assertion here is measured against ----


def test_detail_serves_pr_fields_for_a_linked_item(client, db_path):
    _insert_item(db_path, id="linked", product="vivy", title="linked", type="task",
                refs=json.dumps({"pr": 9, "url": LINKED_URL}))
    r = client.get("/items/linked", headers=_h())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pr"] == 9
    assert body["pr_url"] == LINKED_URL
    assert body["pr_repo"] == "convivy/library"


def test_detail_pr_fields_are_null_with_no_pr(client, db_path):
    _insert_item(db_path, id="bare", product="vivy", title="bare", type="task")
    r = client.get("/items/bare", headers=_h())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pr"] is None
    assert body["pr_url"] is None
    assert body["pr_repo"] is None


def test_detail_pr_fields_are_null_when_refs_is_not_an_object(client, db_path):
    """`refs` is valid JSON of any shape, not necessarily an object —
    `test_search.py`'s Chart House fixture seeds it as `'[]'`. A non-dict
    `refs` carries no `pr`/`url` rather than raising."""
    _insert_item(db_path, id="odd_refs", product="vivy", title="odd", type="task",
                refs="[]")
    r = client.get("/items/odd_refs", headers=_h())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pr"] is None
    assert body["pr_url"] is None
    assert body["pr_repo"] is None


def test_detail_pr_repo_is_null_for_a_url_naming_another_org(client, db_path):
    """`pr_repo` is org-pinned: a `pr_url` is trusted to name a repo only when
    it points at this org's own GitHub, never derived from whatever host or
    org a caller's `url` names."""
    _insert_item(db_path, id="offsite", product="vivy", title="offsite",
                type="task",
                refs=json.dumps({"pr": 9, "url": "https://example/pull/9"}))
    r = client.get("/items/offsite", headers=_h())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pr"] == 9
    assert body["pr_url"] == "https://example/pull/9"
    assert body["pr_repo"] is None


# --- `_pr_repo`'s own edge cases: end-anchoring and the repo-name class ----------
# Mirrors `agent-platform`'s `bin/issue-pr-reconcile` (`_PR_URL_RE`) test shape —
# the two regexes are meant to accept and reject the identical set of urls.


def test_pr_repo_rejects_a_url_with_a_trailing_query_string():
    assert store._pr_repo("https://github.com/convivy/library/pull/9?x=1") is None


def test_pr_repo_rejects_a_url_with_a_trailing_fragment():
    assert store._pr_repo("https://github.com/convivy/library/pull/9#files") is None


def test_pr_repo_rejects_a_url_with_an_extra_path_segment():
    assert store._pr_repo("https://github.com/convivy/library/pull/9/files") is None


def test_pr_repo_rejects_a_repo_name_starting_with_a_non_alphanumeric():
    assert store._pr_repo("https://github.com/convivy/-library/pull/9") is None


def test_pr_repo_accepts_a_trailing_slash():
    assert store._pr_repo("https://github.com/convivy/library/pull/9/") == \
        "convivy/library"


# --- GET /items?product=&state=, Deck's corrections queue -----------------------


def test_list_by_product_and_state_matches_the_detail_route(client, db_path):
    _insert_item(db_path, id="linked", product="vivy", title="linked", type="task",
                state="pr-open", refs=json.dumps({"pr": 9, "url": LINKED_URL}))
    _insert_item(db_path, id="bare", product="vivy", title="bare", type="task",
                state="pr-open")

    detail_linked = client.get("/items/linked", headers=_h()).json()
    detail_bare = client.get("/items/bare", headers=_h()).json()

    r = client.get("/items?product=vivy&state=pr-open", headers=_h())
    assert r.status_code == 200, r.text
    body = r.json()
    for field in ("pr", "pr_url", "pr_repo"):
        assert _item(body, "linked")[field] == detail_linked[field]
        assert _item(body, "bare")[field] == detail_bare[field]
    assert _item(body, "bare")["pr"] is None
    assert _item(body, "bare")["pr_url"] is None
    assert _item(body, "bare")["pr_repo"] is None


# --- GET /items?parent_id=, an epic's children -----------------------------------


def test_list_by_parent_id_matches_the_detail_route(client, db_path):
    _insert_item(db_path, id="epic1", product="vivy", title="epic", type="epic")
    _insert_item(db_path, id="linked", product="vivy", title="linked", type="task",
                parent_id="epic1", refs=json.dumps({"pr": 9, "url": LINKED_URL}))
    _insert_item(db_path, id="bare", product="vivy", title="bare", type="task",
                parent_id="epic1")

    detail_linked = client.get("/items/linked", headers=_h()).json()
    detail_bare = client.get("/items/bare", headers=_h()).json()

    r = client.get("/items?parent_id=epic1", headers=_h())
    assert r.status_code == 200, r.text
    body = r.json()
    for field in ("pr", "pr_url", "pr_repo"):
        assert _item(body, "linked")[field] == detail_linked[field]
        assert _item(body, "bare")[field] == detail_bare[field]


# --- the Course's resolved children ----------------------------------------------


def test_course_resolved_children_match_the_detail_route(client, db_path):
    _insert_item(db_path, id="linked", product="vivy", title="linked", type="epic",
                refs=json.dumps({"pr": 9, "url": LINKED_URL}))
    _insert_item(db_path, id="bare", product="vivy", title="bare", type="epic")
    _insert_course(db_path, id="course1", product="vivy",
                   entries=["linked", "bare"])

    detail_linked = client.get("/items/linked", headers=_h()).json()
    detail_bare = client.get("/items/bare", headers=_h()).json()

    r = client.get("/courses/course1", headers=_h())
    assert r.status_code == 200, r.text
    items = {i["id"]: i for i in r.json()["items"]}
    for field in ("pr", "pr_url", "pr_repo"):
        assert items["linked"][field] == detail_linked[field]
        assert items["bare"][field] == detail_bare[field]
