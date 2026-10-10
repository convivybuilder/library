"""The per-submitter rate limit on `POST /signals` (the operator, 2026-09-30): 120
signals per 60-second sliding window per `Identity.actor`, refused with a 429
naming the limit, the window and `Retry-After`, before anything is written.

Split in two. `store.check_signal_rate_limit` is exercised directly, with an
injected clock rather than a sleep, for the sliding-window arithmetic and the
concurrency guarantee a thread pool cannot exercise through one `TestClient`.
The HTTP-level tests exercise the route's wiring — the refusal shape, that
only an accepted submission spends a slot, and that one actor's exhaustion
does not touch another's — with the module constants patched down to a size
a test can drive without 120 real requests.
"""
from __future__ import annotations

import json
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

import app as libapp
import identity
import projects
import protocol
import store

AGENT = "agent-token-vivy"
OTHER = "agent-token-other"

REGISTRY = {
    "_meta": {},
    "vivy": {"repos": []},
}


@pytest.fixture
def client(tmp_path, monkeypatch, vector_index):
    db = tmp_path / "control.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE signals (
            id TEXT PRIMARY KEY, source TEXT, stream TEXT, kind TEXT, actor TEXT,
            ingested_by TEXT, product TEXT, refs TEXT, body TEXT, meta TEXT,
            privacy_class TEXT, produced_at TEXT, ingested_at TEXT,
            embedded_at TEXT, dedup_key TEXT UNIQUE, acting_human TEXT);
        CREATE TABLE black_box_registrations (
            id INTEGER PRIMARY KEY, crew TEXT NOT NULL UNIQUE,
            model TEXT NOT NULL, gateway TEXT NOT NULL,
            created_by TEXT NOT NULL, created_at TEXT NOT NULL,
            updated_by TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE cold_dedup_keys (
            dedup_key TEXT PRIMARY KEY, signal_id TEXT NOT NULL);
    """)
    conn.commit()
    conn.close()

    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        AGENT: {"name": "vivy-agent", "products": ["vivy"]},
        OTHER: {"name": "vivy-agent-two", "products": ["vivy"]},
    }))
    vector_index(db)
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)

    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps(REGISTRY))
    monkeypatch.setattr(projects, "REGISTRY_PATH", registry)
    projects.reload()
    yield TestClient(libapp.app)
    projects.reload()


def _h(token=AGENT):
    return {"Authorization": f"Bearer {token}",
            protocol.HEADER: str(protocol.CURRENT)}


def _body(**over):
    out = {"source": "github", "kind": "note", "actor": "someone",
           "product": "vivy", "body": "a thing happened"}
    out.update(over)
    return out


@pytest.fixture(autouse=True)
def _clean_rate_state(monkeypatch):
    """`_signal_rate_windows` is process-global state, so every test in this
    file — including the ones below that call `store.check_signal_rate_limit`
    directly, with no `client` fixture in play — starts from an empty table
    rather than whatever a previous test or a previous actor name left behind."""
    monkeypatch.setattr(store, "_signal_rate_windows", {})


def _signal_count():
    conn = sqlite3.connect(store.DB_PATH)
    try:
        return conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    finally:
        conn.close()


# --- `store.check_signal_rate_limit` directly: the sliding window itself ------


def test_the_limit_th_lands_and_the_next_is_refused(monkeypatch):
    monkeypatch.setattr(store, "SIGNAL_RATE_LIMIT", 120)
    monkeypatch.setattr(store, "SIGNAL_RATE_WINDOW_SECONDS", 60)
    for _ in range(120):
        allowed, retry_after = store.check_signal_rate_limit("actor-a", now=1000.0)
        assert allowed
        assert retry_after == 0
    allowed, retry_after = store.check_signal_rate_limit("actor-a", now=1000.0)
    assert not allowed
    assert retry_after == 60


def test_a_second_actor_is_unaffected_by_the_firsts_exhaustion():
    for _ in range(store.SIGNAL_RATE_LIMIT):
        assert store.check_signal_rate_limit("actor-a", now=1000.0)[0]
    assert not store.check_signal_rate_limit("actor-a", now=1000.0)[0]
    assert store.check_signal_rate_limit("actor-b", now=1000.0)[0]


def test_a_slot_frees_once_its_timestamp_ages_out_of_the_window(monkeypatch):
    monkeypatch.setattr(store, "SIGNAL_RATE_LIMIT", 3)
    for i in range(3):
        assert store.check_signal_rate_limit(
            "actor-a", now=1000.0 + i)[0]
    allowed, retry_after = store.check_signal_rate_limit("actor-a", now=1002.0)
    assert not allowed
    # The oldest of the three reserved slots was at 1000.0, so it ages out at
    # 1000.0 + 60; two seconds have elapsed, so 58 remain.
    assert retry_after == 58
    # Once the oldest slot's own window has fully elapsed, it is freed.
    assert store.check_signal_rate_limit("actor-a", now=1060.1)[0]


def test_an_idle_actors_expired_timestamps_are_dropped_not_accumulated(
        monkeypatch):
    """An actor's list is trimmed to in-window timestamps on every call, so it
    never grows past `SIGNAL_RATE_LIMIT` entries no matter how many calls —
    spread out past the window, so none of them ever collide — that actor has
    made over the life of the process."""
    monkeypatch.setattr(store, "SIGNAL_RATE_LIMIT", 5)
    for i in range(50):
        allowed, _ = store.check_signal_rate_limit(
            "actor-idle", now=1000.0 + i * 61)
        assert allowed, "each call is 61s after the last, always outside " \
                         "the 60s window, so none should ever be refused"
        assert len(store._signal_rate_windows["actor-idle"]) == 1


def test_concurrent_submissions_from_one_actor_never_admit_more_than_the_limit(
        monkeypatch):
    monkeypatch.setattr(store, "SIGNAL_RATE_LIMIT", 50)
    results = []
    results_lock = threading.Lock()

    def _attempt():
        allowed, _ = store.check_signal_rate_limit("actor-concurrent", now=5000.0)
        with results_lock:
            results.append(allowed)

    threads = [threading.Thread(target=_attempt) for _ in range(200)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(results) == 50


# --- the route's wiring: `POST /signals` ---------------------------------------


def test_the_refusal_names_the_limit_the_window_and_retry_after(client, monkeypatch):
    monkeypatch.setattr(store, "SIGNAL_RATE_LIMIT", 1)
    monkeypatch.setattr(store, "SIGNAL_RATE_WINDOW_SECONDS", 60)
    first = client.post("/signals", json=_body(), headers=_h())
    assert first.status_code == 201
    r = client.post("/signals", json=_body(), headers=_h())
    assert r.status_code == 429
    body = r.json()
    assert body["limit"] == 1
    assert body["window_seconds"] == 60
    assert body["retry_after"] == 60
    assert r.headers["Retry-After"] == "60"
    assert _signal_count() == 1, "the refused 121st-equivalent wrote nothing"


def test_a_request_refused_for_another_reason_spends_no_slot(client, monkeypatch):
    monkeypatch.setattr(store, "SIGNAL_RATE_LIMIT", 1)
    # Missing `source`: refused for a bad body, long before the rate gate.
    bad = client.post("/signals", json=_body(source=""), headers=_h())
    assert bad.status_code == 400
    # The one slot the limit allows is still there.
    ok = client.post("/signals", json=_body(), headers=_h())
    assert ok.status_code == 201
    exhausted = client.post("/signals", json=_body(), headers=_h())
    assert exhausted.status_code == 429


def test_a_second_actor_is_unaffected_by_the_first_at_the_route(client, monkeypatch):
    monkeypatch.setattr(store, "SIGNAL_RATE_LIMIT", 1)
    assert client.post("/signals", json=_body(), headers=_h(AGENT)).status_code == 201
    assert client.post("/signals", json=_body(), headers=_h(AGENT)).status_code == 429
    assert client.post("/signals", json=_body(), headers=_h(OTHER)).status_code == 201


def test_an_idempotent_dedup_key_resubmission_still_spends_a_slot(client, monkeypatch):
    """Deliberate: whether a `dedup_key` collision is a fresh row (201) or a
    repeat (200) is only known inside `store.insert_signal`, which still runs a
    real query against the write connection either way. The rate gate bounds
    how often a submitter reaches the write path, not how many distinct rows
    resulted, so a repeat spends a slot exactly like a fresh row does."""
    monkeypatch.setattr(store, "SIGNAL_RATE_LIMIT", 1)
    first = client.post("/signals", json=_body(dedup_key="k1"), headers=_h())
    assert first.status_code == 201
    repeat = client.post("/signals", json=_body(dedup_key="k1"), headers=_h())
    assert repeat.status_code == 429, (
        "the resubmission is refused by the rate gate before it ever reaches "
        "the dedup check, because the gate is keyed on the actor and runs "
        "before any write regardless of what this particular body contains")


def test_a_slot_frees_after_the_window_passes_at_the_route(client, monkeypatch):
    monkeypatch.setattr(store, "SIGNAL_RATE_LIMIT", 1)
    monkeypatch.setattr(store, "SIGNAL_RATE_WINDOW_SECONDS", 60)
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(store.time, "monotonic", lambda: clock["now"])

    assert client.post("/signals", json=_body(), headers=_h()).status_code == 201
    assert client.post("/signals", json=_body(), headers=_h()).status_code == 429
    clock["now"] += 60.0
    assert client.post("/signals", json=_body(), headers=_h()).status_code == 201


def test_the_120th_lands_and_the_121st_is_refused_over_http(client):
    """The ruling's own numbers, driven once through the real route and the
    real (unpatched) constants, so the patched-constant tests above are not
    the only evidence the shipped limit is 120 per 60 seconds."""
    assert store.SIGNAL_RATE_LIMIT == 120
    assert store.SIGNAL_RATE_WINDOW_SECONDS == 60
    for _ in range(120):
        r = client.post("/signals", json=_body(), headers=_h())
        assert r.status_code == 201
    r = client.post("/signals", json=_body(), headers=_h())
    assert r.status_code == 429
    assert r.json()["limit"] == 120
    assert _signal_count() == 120
