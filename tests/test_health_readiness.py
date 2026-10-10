"""`GET /health` reporting whether semantic search can actually serve.

The 2026-08-24 incident: `vectors.available()` started returning False on every
call after `.venv` was deleted out from under the running process, and
`/signals/search` refused every request with 503 for twenty minutes while
`/health` kept answering ready — it read a static literal that touched neither
the database nor the vector index. These tests hold the fix: the CURRENT
readiness, cached rather than checked on every call, and never able to make
`/health` itself fail.
"""
from __future__ import annotations

import dis
import json

import pytest
from fastapi.testclient import TestClient

import app as libapp
import identity
import store
import vectors

AGENT = "agent-token"


@pytest.fixture(autouse=True)
def _reset_readiness_cache():
    """The cache is a module-level global in `app.py`, shared across every test
    in this process — reset it so one test's readiness answer cannot leak into
    the next."""
    libapp._readiness_cache = None
    yield
    libapp._readiness_cache = None


@pytest.fixture
def client(tmp_path, monkeypatch, vector_index):
    db = tmp_path / "control.db"
    vector_index(db)
    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({AGENT: {"name": "an-agent", "products": ["*"]}}))
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    return TestClient(libapp.app)


def _auth():
    return {"Authorization": f"Bearer {AGENT}"}


def test_health_still_answers_status_and_protocol(client):
    """Backward compatible: the two existing fields are unchanged."""
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["protocol"]["current"] == libapp.protocol.CURRENT


def _boot_stderr(capsys) -> str:
    """The stderr `lifespan` writes at boot, the way test_merge_admission does."""
    import asyncio

    async def _run():
        async with libapp.lifespan(libapp.app):
            pass

    asyncio.run(_run())
    return capsys.readouterr().err


def test_boot_line_reports_the_commit(capsys, monkeypatch):
    """The other two identity lines at boot — protocol and database — each have
    a test. This one is the third, and it exists for the same reason they do: a
    line nobody pins is a line a later edit silently breaks."""
    monkeypatch.setattr(libapp, "RUNNING_COMMIT", "a" * 40)
    assert "library: code " + "a" * 40 in _boot_stderr(capsys)


def test_boot_line_says_unresolved_without_asserting_a_cause(capsys, monkeypatch):
    """**"unresolved", not "not a git checkout"** (reviewer, convivy/library#59).
    The resolver returns None for at least four different reasons — no git on
    PATH, a directory outside any checkout, a git that hangs past the timeout,
    and a git that exits 0 printing something that is not a hash. A boot line
    naming one of them as though it were the only one sends the next operator
    to check the wrong thing."""
    monkeypatch.setattr(libapp, "RUNNING_COMMIT", None)
    err = _boot_stderr(capsys)
    assert "library: code unresolved" in err
    assert "not a git checkout" not in err


def test_boot_line_reports_the_archive_tier_ready(capsys, monkeypatch):
    """The cold tier's own startup line, beside the database and semantic-
    search lines above — `store.cold_db_available`'s non-fatal readiness
    check (archives-cold-tier.md, round 2: `LIBRARY_COLD_DB` itself is
    required at `app.py`'s own import, tested in
    `test_archives_cold_tier_reads.py`; the FILE's existence is this
    non-fatal line instead, matching `vectors.available`'s own shape)."""
    monkeypatch.setattr(store, "cold_db_available", lambda: (True, "ready"))
    assert "library: archive tier ready" in _boot_stderr(capsys)


def test_boot_line_reports_the_archive_tier_unavailable(capsys, monkeypatch):
    monkeypatch.setattr(
        store, "cold_db_available",
        lambda: (False, "the cold tier's database is missing or could not "
                        "be opened"))
    err = _boot_stderr(capsys)
    assert ("library: archive tier UNAVAILABLE — the cold tier's database "
           "is missing or could not be opened") in err


def test_a_git_that_exits_zero_with_junk_is_refused(monkeypatch):
    """A wrapper, alias or hook that exits 0 while printing something else must
    not reach the probe. The argv is fixed and no request touches it, so this is
    about a value that is WRONG rather than one that is hostile — which is the
    failure this whole route exists to end."""
    import subprocess as _sp

    class _Out:
        returncode = 0
        stdout = "Already up to date.\n"

    monkeypatch.setattr(_sp, "run", lambda *a, **k: _Out())
    assert libapp._running_commit() is None


def test_health_reports_the_running_commit(client):
    """The identity of the code that is answering, which nothing on the wire
    could distinguish before (`issue_01M0K44XH8FP83MVBSXBGRTM96`)."""
    body = client.get("/health").json()
    assert body["commit"] == libapp.RUNNING_COMMIT
    assert len(body["commit"]) == 40


def test_the_commit_is_stamped_at_import_and_never_re_read(client, monkeypatch):
    """**The property the whole fix turns on.** Resolving the commit inside the
    handler would report what the tree says NOW, which is precisely the value
    that is wrong during the failure this exists to end: on 2026-08-21 the
    service served 19-hour-stale code while the checkout said otherwise, and a
    per-request read would have reported the new commit throughout.

    So: break `git` after import and the answer must not change. If this test
    ever fails, the resolution has moved back onto the request path and the
    route has started reporting the tree rather than the process.
    """
    before = client.get("/health").json()["commit"]

    # Make the resolver return something different AND make calling it fatal.
    # Either alone is weaker: a changed return value proves the handler is not
    # re-reading, and the exception proves it is not calling the resolver at all.
    def _explode(*a, **k):
        raise AssertionError("the handler resolved the commit per request")

    monkeypatch.setattr(libapp, "_running_commit", _explode)
    assert client.get("/health").json()["commit"] == before


def test_the_commit_key_is_absent_rather_than_null_when_unresolvable(client,
                                                                    monkeypatch):
    """A `commit: null` invites a client to render "unknown" as an answer. An
    absent key makes a client that needs it say so — and a checkout with no git,
    or a tarball deploy, must still get a 200 from the liveness probe."""
    monkeypatch.setattr(libapp, "RUNNING_COMMIT", None)
    r = client.get("/health")
    assert r.status_code == 200
    assert "commit" not in r.json()
    assert r.json()["status"] == "ok"


def test_health_reports_ready_when_the_vector_index_is_healthy(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["semantic_search"]["ready"] is True


def test_health_reports_not_ready_when_vectors_available_is_false(client, monkeypatch):
    monkeypatch.setattr(
        vectors, "available",
        lambda path=None, timeout=None: (False, "the vector index is unreadable (x)"))
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["semantic_search"]["ready"] is False


def test_health_reports_not_ready_when_the_vectors_file_is_missing(
        client, monkeypatch, tmp_path):
    """The real path, not a mocked `available()`: a missing `LIBRARY_VECTORS_DB`
    file has to be visible here exactly as the deploy runbook and the plist
    comment promise (reviewer + security-reviewer,
    issue_01M35R5GA1SKRG2CMQRF189NDE round 1)."""
    monkeypatch.setattr(vectors, "VECTORS_DB_PATH", tmp_path / "no-such-vectors.db")
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["semantic_search"]["ready"] is False


def test_health_stays_200_when_the_readiness_check_raises(client, monkeypatch):
    def boom(path=None, timeout=None):
        raise RuntimeError("the venv is gone")
    monkeypatch.setattr(vectors, "available", boom)
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "supported" in body["protocol"]
    assert body["semantic_search"]["ready"] is False


def test_health_caches_readiness_within_the_ttl(client, monkeypatch):
    calls = []

    def counting(path=None, timeout=None):
        calls.append(1)
        return True, "ready"
    monkeypatch.setattr(vectors, "available", counting)
    client.get("/health")
    client.get("/health")
    assert len(calls) == 1, "a second call inside the TTL must not re-check"


def test_health_rechecks_once_the_cache_expires(client, monkeypatch):
    calls = []

    def counting(path=None, timeout=None):
        calls.append(1)
        return True, "ready"
    monkeypatch.setattr(vectors, "available", counting)
    now = [1000.0]
    monkeypatch.setattr(libapp.time, "monotonic", lambda: now[0])
    client.get("/health")
    now[0] += libapp._READINESS_TTL_SECONDS + 1
    client.get("/health")
    assert len(calls) == 2, "a call past the TTL must re-check"


def test_the_reason_is_never_in_the_response_unauthenticated(client, monkeypatch):
    monkeypatch.setattr(
        vectors, "available",
        lambda path=None, timeout=None: (False, "the vector index is unreadable (a detail)"))
    r = client.get("/health")
    assert "reason" not in r.json()["semantic_search"]


def test_the_reason_is_never_in_the_response_authenticated(client, monkeypatch):
    monkeypatch.setattr(
        vectors, "available",
        lambda path=None, timeout=None: (False, "the vector index is unreadable (a detail)"))
    r = client.get("/health", headers=_auth())
    assert "reason" not in r.json()["semantic_search"]


def test_a_not_ready_result_reaches_the_log(client, monkeypatch, capsys):
    monkeypatch.setattr(
        vectors, "available",
        lambda path=None, timeout=None: (False, "the vector index is unreadable (a detail)"))
    client.get("/health")
    err = capsys.readouterr().err
    assert "the vector index is unreadable (a detail)" in err


def test_a_healthy_result_writes_no_log_line(client, monkeypatch, capsys):
    monkeypatch.setattr(
        vectors, "available", lambda path=None, timeout=None: (True, "ready"))
    client.get("/health")
    err = capsys.readouterr().err
    assert "semantic search not ready" not in err


def test_readiness_cache_hit_reads_the_global_exactly_once():
    """A prior version read `_readiness_cache` four separate times on the
    cache-hit path — once for `is not None`, once for the TTL compare, once
    each for the two returned fields — each compiling to its own
    `LOAD_GLOBAL`. CPython's GIL makes a single name rebind atomic but does
    nothing for a *sequence* of reads: a recompute racing in on another thread
    between any two of those loads could hand back a `ready` from one
    generation paired with a `reason` from another that never coexisted. This
    can't be pinned by racing real threads without flakiness, so it's pinned
    structurally: the cache-hit path must bind the global to a local exactly
    once and read the tuple through that local from there on.
    """
    loads = [
        i for i in dis.get_instructions(libapp._readiness_cache_hit)
        if i.opname == "LOAD_GLOBAL" and i.argval == "_readiness_cache"
    ]
    assert len(loads) == 1


def test_health_does_not_touch_the_threadpool_on_a_cache_hit(client, monkeypatch):
    """A 15s TTL against a watchdog polling every second means most calls hit
    the cache; each of those should be a pure in-memory lookup on the event
    loop, not a threadpool round-trip."""
    client.get("/health")  # populates the cache

    def fail_if_called(*args, **kwargs):
        raise AssertionError("cache hit must not go through run_in_threadpool")
    monkeypatch.setattr(libapp, "run_in_threadpool", fail_if_called)

    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["semantic_search"]["ready"] is True
