"""The chain pull: the Library reads the Bosun's register and serves it near-live.

**Against a real UNIX socket rather than a mocked `_fetch`.** The failures worth
catching here live in the transport — a socket that is absent, one that accepts
and never answers, one that returns a non-200, one that returns JSON of the
wrong shape — and a fixture that patches `runs._fetch` proves none of them.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as libapp
import identity
import projects
import runs
import store

AGENT = "agent-token"
NARROW = "narrow-token"
H = {"Library-Protocol": "1"}


def _headers(token=AGENT):
    return {**H, "Authorization": f"Bearer {token}"}


class FakeRegister:
    """A stand-in for the Bosun, speaking the same one-shot HTTP-over-UNIX shape.

    `behaviour` picks what it does: answer normally, answer a status, answer
    unparseable bytes, or accept the connection and never reply.
    """

    def __init__(self, path, payload=None, *, behaviour="ok"):
        self.path = str(path)
        self.payload = payload
        self.behaviour = behaviour
        self.reads = 0
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(self.path)
        self._sock.listen(8)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            self.reads += 1
            try:
                conn.recv(65536)
                if self.behaviour == "hang":
                    self._stop.wait(10)
                elif self.behaviour == "not-found":
                    conn.sendall(b"HTTP/1.0 404 Not Found\r\n\r\n")
                elif self.behaviour == "garbage":
                    conn.sendall(b"HTTP/1.0 200 OK\r\n\r\nnot json at all")
                elif self.behaviour == "trickle":
                    # A byte at a time, each well inside a per-call timeout, so
                    # only a TOTAL deadline stops this.
                    conn.sendall(b"HTTP/1.0 200 OK\r\n\r\n")
                    for _ in range(2000):
                        if self._stop.is_set():
                            break
                        conn.sendall(b" ")
                        self._stop.wait(0.05)
                elif self.behaviour == "huge":
                    conn.sendall(b"HTTP/1.0 200 OK\r\n\r\n")
                    conn.sendall(b"x" * (runs.MAX_BODY_BYTES + 1024))
                elif self.behaviour == "wrong-shape":
                    conn.sendall(b"HTTP/1.0 200 OK\r\n\r\n" +
                                 json.dumps({"runs": "not a list"}).encode())
                else:
                    conn.sendall(b"HTTP/1.0 200 OK\r\n\r\n" +
                                 json.dumps(self.payload).encode())
            except OSError:
                pass
            finally:
                conn.close()

    def close(self):
        self._stop.set()
        self._sock.close()


def _seed_lease(*, product, released_at=None, run_id="run-aaaa1111"):
    """A live lease on a real item, since the holder join scopes THROUGH
    `issue_items` — a lease pointing at no item yields no holder, correctly."""
    conn = sqlite3.connect(store.DB_PATH)
    conn.execute(
        "INSERT INTO issue_items (id, product, title, state) "
        "VALUES ('issue_root', ?, 't', 'claimed')", (product,))
    conn.execute(
        "INSERT INTO issue_leases (id, root_id, taken_for, holder, run_id, "
        " taken_at, heartbeat_at, expires_at, released_at) "
        "VALUES ('lease_1','issue_root','issue_root','co-x',?,'x','x','x',?)",
        (run_id, released_at))
    conn.commit()
    conn.close()


def a_run(**over):
    row = {"run_id": "run-aaaa1111", "agent": "coder", "task": "do a thing",
           "started_at": "2026-08-24T20:00:00Z", "repo": "library",
           "worktree": "/tmp/wt", "pid": 4242, "parent_run": None}
    row.update(over)
    return row


@pytest.fixture
def sock_dir():
    """A SHORT directory for the socket.

    macOS caps an `AF_UNIX` path near 104 characters and pytest's `tmp_path` is
    comfortably past that on its own, before a filename. This is the standard
    way round it and it is worth a fixture rather than a comment in ten places.
    """
    path = tempfile.mkdtemp(prefix="libruns-", dir="/tmp")
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def client(tmp_path, sock_dir, monkeypatch):
    db = tmp_path / "l.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE issue_items (
            id TEXT PRIMARY KEY, product TEXT, title TEXT, body TEXT, refs TEXT,
            severity TEXT, state TEXT, claimed_by TEXT, claimed_at TEXT,
            created_by TEXT, created_at TEXT, type TEXT, needs TEXT,
            parent_id TEXT, run_id TEXT);
        CREATE TABLE issue_leases (
            id TEXT PRIMARY KEY, root_id TEXT, taken_for TEXT, holder TEXT,
            run_id TEXT, taken_at TEXT, heartbeat_at TEXT, expires_at TEXT,
            released_at TEXT, release_reason TEXT, released_by TEXT, note TEXT);
    """)
    conn.commit()
    conn.close()
    tokens = tmp_path / "t.json"
    tokens.write_text(json.dumps({
        AGENT: {"name": "wide", "products": ["*"]},
        NARROW: {"name": "vivy-only", "products": ["vivy"]}}))
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(runs, "SOCKET_PATH", Path(sock_dir) / "r.sock")
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({
        "_meta": {}, "library": {"repos": ["library"]}, "vivy": {"repos": ["vivy"]},
        "cfo": {"private": True, "task_text_public": False, "repos": ["CFO"]}}))
    monkeypatch.setattr(projects, "REGISTRY_PATH", registry)
    projects.reload()
    yield TestClient(libapp.app)
    runs.reset_cache()
    projects.reload()


# --- the ordinary case -------------------------------------------------------

def test_it_serves_what_the_register_reports(client, sock_dir):
    reg = FakeRegister(os.path.join(sock_dir, "r.sock"),
                       {"generated_at": "2026-08-24T20:00:01Z",
                        "runs": [a_run(), a_run(run_id="run-bbbb2222",
                                              agent="reviewer")]})
    try:
        r = client.get("/runs", headers=_headers())
        assert r.status_code == 200
        body = r.json()
        assert body["available"] is True
        assert body["count"] == 2
        assert {x["agent"] for x in body["runs"]} == {"coder", "reviewer"}
        assert body["generated_at"] == "2026-08-24T20:00:01Z"
    finally:
        reg.close()


def test_the_freshness_bound_is_stated_rather_than_left_implicit(client, sock_dir):
    """The Bosun's read is uncached, so the Library's bound IS the freshness a
    client sees. A client left to guess how old this is will guess wrong."""
    reg = FakeRegister(os.path.join(sock_dir, "r.sock"), {"runs": []})
    try:
        body = client.get("/runs", headers=_headers()).json()
        assert body["stale_after_seconds"] == runs.TTL_SECONDS
    finally:
        reg.close()


# --- absence is a normal state ----------------------------------------------

def test_a_missing_register_is_an_empty_answer_not_an_error(client):
    """The Bosun's job ships deliberately inactive, so the socket may simply not
    exist. A 503 here would take out a crew view over a dependency that is
    optional by design."""
    r = client.get("/runs", headers=_headers())
    assert r.status_code == 200
    assert r.json()["available"] is False
    assert r.json()["runs"] == []
    assert "no run register" in r.json()["reason"]


def test_a_register_that_accepts_and_never_answers_degrades(
        client, sock_dir, monkeypatch):
    """A hung upstream must not hold the caller open — this sits inside a
    client's request."""
    reg = FakeRegister(os.path.join(sock_dir, "r.sock"), None, behaviour="hang")
    try:
        monkeypatch.setattr(runs, "TIMEOUT_SECONDS", 0.3)
        r = client.get("/runs", headers=_headers())
        assert r.status_code == 200
        assert r.json()["available"] is False
        assert "did not answer" in r.json()["reason"]
    finally:
        reg.close()


@pytest.mark.parametrize("behaviour,fragment", [
    ("not-found", "answered"),
    ("garbage", "not JSON"),
    ("wrong-shape", "not the agreed shape"),
])
def test_a_bad_answer_degrades_rather_than_raising(client, sock_dir,
                                                   behaviour, fragment):
    """Shape, not only parseability — a payload that parses and is not the
    agreed shape would otherwise reach the allowlist loop as an AttributeError."""
    reg = FakeRegister(os.path.join(sock_dir, "r.sock"), None, behaviour=behaviour)
    try:
        r = client.get("/runs", headers=_headers())
        assert r.status_code == 200
        assert r.json()["available"] is False
        assert fragment in r.json()["reason"]
    finally:
        reg.close()


def test_a_recursion_error_from_json_loads_degrades_rather_than_raising(
        client, sock_dir, monkeypatch):
    """`json.loads` raises `RecursionError`, not `ValueError`, on a sufficiently
    deep body, and round 1 added the extra `except` clause for it
    (security-reviewer, PR #51) — but nothing exercised the branch.

    Not built as a genuinely deep payload over the socket: the nesting depth
    that actually trips the C `json` scanner's `RecursionError` does not fit
    inside `MAX_BODY_BYTES`, so a byte-budget version of this test would either
    silently exercise the size-cap branch instead or depend on a depth threshold
    that is itself Python-build-dependent. Forcing `json.loads` to raise is what
    exercises this exact except clause regardless of build — scoped to THIS
    payload only, since `json` is one shared module and a blanket patch of
    `json.loads` also breaks the token lookup this same request makes.
    """
    marker = "runs-marker-deep-nest"
    real_loads = json.loads
    def _raise_for_marker(s, *a, **k):
        text = s if isinstance(s, str) else s.decode("utf-8", "replace")
        if marker in text:
            raise RecursionError("maximum recursion depth exceeded")
        return real_loads(s, *a, **k)
    reg = FakeRegister(os.path.join(sock_dir, "r.sock"),
                       {"runs": [], marker: True})
    monkeypatch.setattr(runs.json, "loads", _raise_for_marker)
    try:
        r = client.get("/runs", headers=_headers())
        assert r.status_code == 200
        assert r.json()["available"] is False
        assert "not JSON" in r.json()["reason"]
    finally:
        reg.close()


# --- the allowlist is not re-opened downstream -------------------------------

def test_a_field_the_bosun_adds_does_not_reach_a_client_by_itself(client, sock_dir):
    """The Bosun's response is an explicit allowlist, added after a `reserved`
    field was found leaking mid-spawn. An allowlist upstream only holds if
    nothing downstream widens it back, so a new field arrives at clients when
    somebody adds it here, deliberately."""
    reg = FakeRegister(os.path.join(sock_dir, "r.sock"),
                       {"runs": [a_run(secret="a credential", reserved="x")]})
    try:
        row = client.get("/runs", headers=_headers()).json()["runs"][0]
        assert "secret" not in row
        assert "reserved" not in row
        assert set(row) == set(runs.RUN_FIELDS) | {"holds"}
    finally:
        reg.close()


# --- scoping -----------------------------------------------------------------

def test_a_scoped_caller_does_not_see_another_products_run(client, sock_dir):
    """The register carries `task` text and `worktree` paths, which for a
    private crew is exactly the content ADR-0043's wall exists to keep in."""
    reg = FakeRegister(os.path.join(sock_dir, "r.sock"),
                       {"runs": [a_run(repo="library"),
                                 a_run(run_id="run-cccc3333", repo="vivy")]})
    try:
        wide = client.get("/runs", headers=_headers()).json()
        assert wide["count"] == 2
        narrow = client.get("/runs", headers=_headers(NARROW)).json()
        assert narrow["count"] == 1
        assert narrow["runs"][0]["repo"] == "vivy"
    finally:
        reg.close()


# --- the holder join ---------------------------------------------------------

def test_a_run_holding_a_lease_says_which_epic_it_holds(client, sock_dir):
    """The third part of the served view, and the one needing no new plumbing —
    a lease has carried `run_id` since Keel Phase 2."""
    _seed_lease(product="library")
    reg = FakeRegister(os.path.join(sock_dir, "r.sock"),
                       {"runs": [a_run(), a_run(run_id="run-bbbb2222")]})
    try:
        rows = {r["run_id"]: r for r in
                client.get("/runs", headers=_headers()).json()["runs"]}
        assert rows["run-aaaa1111"]["holds"]["item_id"] == "issue_root"
        assert rows["run-bbbb2222"]["holds"] is None
    finally:
        reg.close()


def test_a_released_lease_is_not_reported_as_held(client, sock_dir):
    _seed_lease(product="library", released_at="2026-08-24")
    reg = FakeRegister(os.path.join(sock_dir, "r.sock"), {"runs": [a_run()]})
    try:
        row = client.get("/runs", headers=_headers()).json()["runs"][0]
        assert row["holds"] is None
    finally:
        reg.close()


# --- the cache ---------------------------------------------------------------

def test_a_burst_of_requests_costs_the_register_one_read(client, sock_dir):
    """The reason the bound is not zero: a page render makes several requests
    and should cost the socket one read, not one each."""
    reg = FakeRegister(os.path.join(sock_dir, "r.sock"), {"runs": [a_run()]})
    try:
        for _ in range(5):
            assert client.get("/runs", headers=_headers()).status_code == 200
        assert reg.reads == 1
    finally:
        reg.close()


def test_the_snapshot_is_re_read_once_the_bound_has_passed(client, sock_dir):
    reg = FakeRegister(os.path.join(sock_dir, "r.sock"), {"runs": [a_run()]})
    try:
        runs.snapshot(now=1000.0)
        runs.snapshot(now=1000.0 + runs.TTL_SECONDS - 0.01)
        assert reg.reads == 1
        runs.snapshot(now=1000.0 + runs.TTL_SECONDS + 0.01)
        assert reg.reads == 2
    finally:
        reg.close()


# --- what the reviewers found ------------------------------------------------

def test_a_black_box_crews_run_is_not_served_at_all(client, sock_dir):
    """ADR-0043: a private crew is registered "with none of its content reaching
    the Library and none of its agents visible to Orient."

    **Its agents, not merely its task text** — so the row is dropped rather than
    a field redacted. `cfo` is `private: true` with `task_text_public: false`,
    which agrees rather than softening it. The register carries live `task` text
    and `worktree` paths, so this route was one wildcard token away from handing
    a black box's working content to a client.
    """
    reg = FakeRegister(os.path.join(sock_dir, "r.sock"),
                       {"runs": [a_run(repo="library"),
                                 a_run(run_id="run-dddd4444", repo="CFO",
                                       task="something confidential",
                                       worktree="/srv/convivy/worktrees/CFO")]})
    try:
        body = client.get("/runs", headers=_headers()).json()
        assert body["count"] == 1
        assert [r["repo"] for r in body["runs"]] == ["library"]
        # Not merely absent from the list — absent from the response entirely.
        assert "confidential" not in json.dumps(body)
        assert "CFO" not in json.dumps(body)
    finally:
        reg.close()


def test_an_unreadable_registry_serves_nothing_rather_than_everything(
        client, sock_dir, monkeypatch):
    """This route fails CLOSED, inverting `projects.py`'s usual trade.

    Everywhere else an unreadable registry means "carry on", because a service
    that cannot accept a filing is a work stoppage. Here the question is "may I
    show this to somebody", and the cost of guessing is a private crew's live
    task text — which no later edit can un-disclose.
    """
    reg = FakeRegister(os.path.join(sock_dir, "r.sock"),
                       {"runs": [a_run(repo="library")]})
    try:
        monkeypatch.setattr(projects, "REGISTRY_PATH", Path("/nonexistent/reg.json"))
        projects.reload()
        body = client.get("/runs", headers=_headers()).json()
        assert body["count"] == 0
        assert body["available"] is False
        assert "private crews cannot be identified" in body["reason"]
    finally:
        reg.close()


def test_the_holder_join_does_not_leak_an_item_outside_the_callers_scope(
        client, sock_dir):
    """A run's `repo` and an item's `product` are different fields and nothing
    makes them agree.

    So scoping the RUN and then returning whatever item is leased against it
    would let a caller learn the id of an item it may not see, and hand it a
    working link. `GET /items/{id}` refuses that id on the next request — by
    which point the id and its existence have already left, which is the
    existence oracle `get_item`'s own comment exists to prevent.
    """
    _seed_lease(product="vivy")           # the ITEM is vivy's
    reg = FakeRegister(os.path.join(sock_dir, "r.sock"),
                       {"runs": [a_run(repo="library")]})   # the RUN is library's
    try:
        wide = client.get("/runs", headers=_headers()).json()["runs"][0]
        assert wide["holds"]["item_id"] == "issue_root"

        # NARROW is scoped to vivy: it cannot see the library RUN at all.
        narrow = client.get("/runs", headers=_headers(NARROW)).json()
        assert narrow["count"] == 0
    finally:
        reg.close()


def test_an_item_the_caller_cannot_see_yields_no_holder(client, sock_dir,
                                                        tmp_path, monkeypatch):
    """The other half: the run IS visible and the item behind it is not."""
    _seed_lease(product="a-product-nobody-is-scoped-to")
    tokens = tmp_path / "t2.json"
    tokens.write_text(json.dumps({
        "scoped": {"name": "lib-only", "products": ["library"]}}))
    monkeypatch.setattr(identity, "TOKENS", tokens)
    reg = FakeRegister(os.path.join(sock_dir, "r.sock"),
                       {"runs": [a_run(repo="library")]})
    try:
        body = client.get("/runs", headers={**H, "Authorization": "Bearer scoped"}).json()
        assert body["count"] == 1
        assert body["runs"][0]["holds"] is None
    finally:
        reg.close()


def test_the_cached_snapshot_is_not_mutated_by_serving_it(client, sock_dir):
    """`snapshot()` hands back the cached object itself, so writing `holds` onto
    its rows wrote a route-specific field into a module-owned cache shared by
    every request inside the TTL.

    It happened not to leak while `holds` was recomputed unconditionally — and
    the holder-scope fix makes `holds` depend on the CALLER, which is exactly
    when a shared object becomes a real cross-request leak.
    """
    _seed_lease(product="library")
    reg = FakeRegister(os.path.join(sock_dir, "r.sock"),
                       {"runs": [a_run(repo="library")]})
    try:
        assert client.get("/runs", headers=_headers()).json()["runs"][0]["holds"]
        cached = runs.snapshot()["runs"][0]
        assert "holds" not in cached, "the route wrote into the shared cache"
    finally:
        reg.close()


def test_a_trickling_register_is_bounded_by_a_total_deadline(client, sock_dir,
                                                             monkeypatch):
    """The blocking finding: `settimeout` bounds each `recv`, not the whole read.

    A sender returning a byte just inside the per-call timeout never trips it,
    so the loop — and the module lock, and a thread from the pool every other
    route shares — could be held open indefinitely.
    """
    reg = FakeRegister(os.path.join(sock_dir, "r.sock"), None, behaviour="trickle")
    try:
        monkeypatch.setattr(runs, "TIMEOUT_SECONDS", 0.5)
        started = time.monotonic()
        body = client.get("/runs", headers=_headers()).json()
        elapsed = time.monotonic() - started
        assert body["available"] is False
        # **THE BOUND IS THE ASSERTION, not which of two paths reported it.**
        # `_fetch` re-arms `settimeout` to the remaining budget on every pass, so
        # a trickle is usually caught by that `recv` raising one pass before the
        # explicit deadline check fires. Both are the same deadline doing the
        # same job; asserting only the explicit wording would fail on a correct
        # implementation, which is what the first version of this test did.
        assert ("did not finish answering" in body["reason"]
                or "did not answer" in body["reason"]), body["reason"]
        assert elapsed < 3.0, f"the read ran {elapsed:.1f}s past a 0.5s bound"
    finally:
        reg.close()


def test_an_oversized_answer_is_refused_rather_than_read_into_memory(client,
                                                                     sock_dir):
    reg = FakeRegister(os.path.join(sock_dir, "r.sock"), None, behaviour="huge")
    try:
        body = client.get("/runs", headers=_headers()).json()
        assert body["available"] is False
        assert "larger than" in body["reason"]
    finally:
        reg.close()
