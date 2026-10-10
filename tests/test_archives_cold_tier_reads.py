"""The Archives cold tier: every read path that must see a moved signal
(library/decisions/0001-what-the-archives-keep-embed-and-move-cold, memory-
retention epic clause 9, issue_01M35R5QKX7QDCM822K3J18SWX).

Mapped to the build plan's (projects/library/specs/archives-cold-tier.md)
own section 5:

- "Found by id" -> `test_found_by_id_*`
- "Found by search" -> `test_signals_search_finds_a_cold_row`,
  `test_vault_archives_finds_a_cold_row`
- "Interrupted move" -> `test_interrupted_move_*`
- "Concurrent reader" -> `test_a_concurrent_reader_never_sees_zero_copies`
- "Each path that must see cold" -> the list/facets/unreferenced_by/pr-state/
  acks/dedup tests below
- "Rollback" / "Interrupted restore" -> `test_restore_*`,
  `test_interrupted_restore_*`
- "Registry-hit fetch is read-only" ->
  `test_a_dedup_repost_of_a_moved_signal_returns_the_cold_row_unchanged`

Built on a REAL migrated `main` (`alembic upgrade head`) and a REAL cold
file (`init_cold.py`), the same reason `test_search.py` builds `control.db`
from the real migration: several of these routes depend on columns, CHECK
constraints and indexes a hand-written schema would have to duplicate and
could drift from.
"""
from __future__ import annotations

import datetime as _dt
import json
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as libapp
import cold_tier as ct
import identity
import projects
import protocol
import store
import vectors

REPO = Path(__file__).resolve().parent.parent

AGENT = "agent-token-vivy"

REGISTRY = {
    "_meta": {},
    "vivy": {"repos": []},
}


def _h():
    return {"Authorization": f"Bearer {AGENT}",
            protocol.HEADER: str(protocol.CURRENT)}


def _ts(**delta):
    return (_dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
            - _dt.timedelta(**delta)).isoformat(" ")


def _insert_signal(db_path, sig_id, kind, *, product="vivy", body="body",
                    refs=None, meta=None, ingested_at=None, embedded_at="x",
                    dedup_key=None, privacy_class="internal", stream=None):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO signals (id, source, kind, actor, ingested_by, "
        "product, stream, refs, body, meta, privacy_class, ingested_at, "
        "embedded_at, dedup_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sig_id, "test", kind, "someone", "someone", product, stream,
         json.dumps(refs or {}), body, json.dumps(meta or {}),
         privacy_class, ingested_at or _ts(days=1), embedded_at, dedup_key))
    conn.commit()
    conn.close()


@pytest.fixture
def env(tmp_path, monkeypatch, vector_index):
    db = tmp_path / "library.db"
    cold = tmp_path / "cold.db"
    env_vars = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
               "LIBRARY_DB": str(db)}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(REPO), capture_output=True, text=True, env=env_vars)
    assert result.returncode == 0, result.stderr
    result = subprocess.run(
        [sys.executable, "init_cold.py"], cwd=str(REPO),
        capture_output=True, text=True,
        env={**env_vars, "LIBRARY_COLD_DB": str(cold)})
    assert result.returncode == 0, result.stderr

    tokens = tmp_path / "api-tokens.json"
    tokens.write_text(json.dumps({
        AGENT: {"name": "vivy-agent", "products": ["vivy"]},
    }))

    vector_index(db)
    monkeypatch.setattr(store, "DB_PATH", db)
    monkeypatch.setattr(identity, "TOKENS", tokens)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    monkeypatch.setenv("LIBRARY_COLD_DB", str(cold))

    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps(REGISTRY))
    monkeypatch.setattr(projects, "REGISTRY_PATH", registry)
    projects.reload()
    yield db, cold
    projects.reload()


def _move(db_path, cold_path, sig_id, *, dedup_key=None):
    """Move one row cold by hand, the same two-phase shape `cold_tier.py`
    uses, without going through its own kind/window policy — these tests
    are about the read side, not the mover's own eligibility rules."""
    cols = ",".join(store.TWO_TIER_SIGNAL_COLUMNS)
    conn = sqlite3.connect(db_path)
    conn.execute("ATTACH DATABASE ? AS cold", (str(cold_path),))
    conn.execute(
        f"INSERT INTO cold.signals ({cols}) "
        f"SELECT {cols} FROM main.signals WHERE id = ?", (sig_id,))
    if dedup_key:
        conn.execute(
            "INSERT INTO cold_dedup_keys (dedup_key, signal_id) "
            "VALUES (?, ?)", (dedup_key, sig_id))
    conn.execute("DELETE FROM main.signals WHERE id = ?", (sig_id,))
    conn.commit()
    conn.close()


# --- LIBRARY_COLD_DB is required at this service's own startup -------------
#
# `store.resolve_cold_db_path` stays lazy at `store.py`'s own import
# (`test_db_resolution.py` holds the direct unit test for that function); this
# is `app.py`'s own import-time call, the one thing that makes a deploy
# missing the variable a refusal to start rather than a 503 nobody notices
# until the first request to a route that attaches cold unconditionally
# (archives-cold-tier.md, deploy step 5). Run in a subprocess because the
# refusal fires at `import app`, which already happened once in this test
# process when `conftest.py`'s placeholder `LIBRARY_COLD_DB` was in effect.


def test_the_service_refuses_to_start_without_LIBRARY_COLD_DB(tmp_path):
    blob_dir = tmp_path / "blobs"
    blob_dir.mkdir()
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
          "LIBRARY_DB": str(tmp_path / "library.db"),
          "LIBRARY_VECTORS_DB": str(tmp_path / "vectors.db"),
          "LIBRARY_BLOB_DIR": str(blob_dir)}
    result = subprocess.run([sys.executable, "-c", "import app"],
                            cwd=str(REPO), capture_output=True, text=True, env=env)
    assert result.returncode != 0
    assert "LIBRARY_COLD_DB" in result.stderr


def test_the_service_starts_once_LIBRARY_COLD_DB_is_set_too(tmp_path):
    """The positive control for the refusal above — without it, any OTHER
    broken import would also read as the refusal working."""
    blob_dir = tmp_path / "blobs"
    blob_dir.mkdir()
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
          "LIBRARY_DB": str(tmp_path / "library.db"),
          "LIBRARY_VECTORS_DB": str(tmp_path / "vectors.db"),
          "LIBRARY_BLOB_DIR": str(blob_dir),
          "LIBRARY_COLD_DB": str(tmp_path / "cold.db")}
    result = subprocess.run([sys.executable, "-c", "import app"],
                            cwd=str(REPO), capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr


# --- found by id ---------------------------------------------------------


def test_found_by_id_after_moving_cold(env):
    db, cold = env
    _insert_signal(db, "sig_a", "note", body="exact body")
    _move(db, cold, "sig_a")

    client = TestClient(libapp.app)
    r = client.get("/signals/sig_a", headers=_h())
    assert r.status_code == 200
    assert r.json()["body"] == "exact body"


def test_blob_still_serves_after_moving_cold(env):
    import blobs
    db, cold = env
    monkeypatch_dir = Path(db).parent / "blobs"
    blobs.BLOB_DIR = monkeypatch_dir
    ref = blobs.write_blob(b"the exact bytes")
    _insert_signal(db, "sig_a", "note", meta={"blob_ref": ref})
    _move(db, cold, "sig_a")

    client = TestClient(libapp.app)
    r = client.get("/signals/sig_a/blob", headers=_h())
    assert r.status_code == 200
    assert r.text == "the exact bytes"


def test_a_truly_missing_id_still_answers_404_after_checking_both_tiers(env):
    client = TestClient(libapp.app)
    r = client.get("/signals/sig_never_existed", headers=_h())
    assert r.status_code == 404


# --- found by search ------------------------------------------------------


def test_signals_search_finds_a_cold_row(env):
    db, cold = env
    _insert_signal(db, "sig_a", "note", body="a gasket signal", embedded_at=None)
    # Embedded WHILE HOT, matching the real mover's own rule
    # (`embedded_at IS NOT NULL` is required before a row ever moves) —
    # `embed_item_by_id` reads from `main.signals` alone, so embedding
    # after the move would silently do nothing.
    vectors.embed_item_by_id(vectors.VAULT_ARCHIVES, "sig_a")
    _move(db, cold, "sig_a")

    client = TestClient(libapp.app)
    r = client.get("/signals/search", params={"q": "gasket signal"},
                   headers=_h())
    assert r.status_code == 200
    ids = [s["id"] for s in r.json()["signals"]]
    assert "sig_a" in ids


def test_vault_archives_finds_a_cold_row(env):
    db, cold = env
    _insert_signal(db, "sig_a", "note", body="a gasket signal", embedded_at=None)
    vectors.embed_item_by_id(vectors.VAULT_ARCHIVES, "sig_a")
    _move(db, cold, "sig_a")

    client = TestClient(libapp.app)
    r = client.get("/search", params={"q": "gasket signal", "vault": "archives"},
                   headers=_h())
    assert r.status_code == 200
    ids = [row["id"] for row in r.json()["results"]]
    assert "sig_a" in ids


# --- list, cursor walk, facets --------------------------------------------


def test_list_signals_sees_a_cold_row(env):
    db, cold = env
    _insert_signal(db, "sig_a", "note")
    _move(db, cold, "sig_a")

    client = TestClient(libapp.app)
    r = client.get("/signals", headers=_h())
    assert r.status_code == 200
    ids = [s["id"] for s in r.json()["signals"]]
    assert "sig_a" in ids


def test_list_signals_stream_filter_sees_a_cold_row(env):
    """`stream` applies to the same two-tier source every other
    `_signal_filters` clause does — a moved row still matches it."""
    db, cold = env
    _insert_signal(db, "sig_a", "note", stream="kb-audit")
    _insert_signal(db, "sig_b", "note", stream="other-stream")
    _move(db, cold, "sig_a")

    client = TestClient(libapp.app)
    r = client.get("/signals", params={"stream": "kb-audit"}, headers=_h())
    assert r.status_code == 200
    ids = [s["id"] for s in r.json()["signals"]]
    assert ids == ["sig_a"]


def test_cursor_walk_crosses_the_tier_boundary_with_nothing_repeated_or_missed(
        env):
    db, cold = env
    for i in range(6):
        _insert_signal(db, f"sig_{i}", "note",
                       ingested_at=f"2020-01-01 00:00:0{i}.000000")
    for i in (1, 3, 5):
        _move(db, cold, f"sig_{i}")

    client = TestClient(libapp.app)
    seen = []
    cursor = None
    for _ in range(10):
        params = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        r = client.get("/signals", params=params, headers=_h())
        assert r.status_code == 200
        body = r.json()
        seen += [s["id"] for s in body["signals"]]
        cursor = body["next_cursor"]
        if not cursor:
            break
    assert sorted(seen) == [f"sig_{i}" for i in range(6)]
    assert len(seen) == len(set(seen)), "a cursor walk must not repeat a row"


def test_facets_counts_a_cold_row(env):
    db, cold = env
    _insert_signal(db, "sig_a", "note")
    _move(db, cold, "sig_a")

    client = TestClient(libapp.app)
    r = client.get("/signals/facets", headers=_h())
    assert r.status_code == 200
    body = r.json()
    assert body["total"] >= 1
    assert ["note", 1] in body["by_kind"] or any(
        k == "note" and n >= 1 for k, n in body["by_kind"])


# --- unreferenced_by crosses tiers both directions ------------------------


def test_unreferenced_by_a_cold_decision_still_retires_a_hot_proposal(env):
    db, cold = env
    _insert_signal(db, "sig_prop", "proposal")
    _insert_signal(db, "sig_dec", "proposal_decision",
                   refs={"target": "sig_prop"})
    _move(db, cold, "sig_dec")

    client = TestClient(libapp.app)
    r = client.get("/signals", params={
        "kind": "proposal", "unreferenced_by": "proposal_decision"},
        headers=_h())
    assert r.status_code == 200
    ids = [s["id"] for s in r.json()["signals"]]
    assert "sig_prop" not in ids


def test_unreferenced_by_a_hot_decision_still_retires_a_cold_proposal(env):
    db, cold = env
    _insert_signal(db, "sig_prop", "proposal")
    _insert_signal(db, "sig_dec", "proposal_decision",
                   refs={"target": "sig_prop"})
    _move(db, cold, "sig_prop")

    client = TestClient(libapp.app)
    r = client.get("/signals", params={
        "kind": "proposal", "unreferenced_by": "proposal_decision"},
        headers=_h())
    assert r.status_code == 200
    ids = [s["id"] for s in r.json()["signals"]]
    assert "sig_prop" not in ids


# --- pr-state --------------------------------------------------------------


def test_pr_state_lists_an_open_pr_whose_only_row_is_cold(env):
    db, cold = env
    _insert_signal(db, "sig_pr", "pr_state",
                   meta={"repo": "convivy/library", "number": 1,
                        "state": "OPEN", "title": "t"},
                   ingested_at=_ts(days=60))
    _move(db, cold, "sig_pr")

    client = TestClient(libapp.app)
    r = client.get("/signals/pr-state", params={"closed_within_days": 90},
                   headers=_h())
    assert r.status_code == 200
    numbers = [pr["number"] for pr in r.json()["prs"]]
    assert 1 in numbers


def test_pr_state_lists_a_closed_pr_inside_90_days_that_is_cold(env):
    db, cold = env
    _insert_signal(db, "sig_pr", "pr_state",
                   meta={"repo": "convivy/library", "number": 2,
                        "state": "MERGED", "title": "t"},
                   ingested_at=_ts(days=45))
    _move(db, cold, "sig_pr")

    client = TestClient(libapp.app)
    r = client.get("/signals/pr-state", params={"closed_within_days": 90},
                   headers=_h())
    assert r.status_code == 200
    numbers = [pr["number"] for pr in r.json()["prs"]]
    assert 2 in numbers


# --- acks --------------------------------------------------------------


def test_ack_a_cold_signal_succeeds(env, monkeypatch, tmp_path):
    db, cold = env
    _insert_signal(db, "sig_a", "note")
    _move(db, cold, "sig_a")

    # Approved-client credentials live in their own file, separate from
    # `identity.TOKENS` (`test_signal_acks.py`'s own fixture).
    client_tokens = tmp_path / "client-tokens.json"
    client_tokens.write_text(json.dumps({
        "client-token": {"name": "orient", "products": ["vivy"]}}))
    monkeypatch.setattr(identity, "CLIENT_TOKENS", client_tokens)

    client = TestClient(libapp.app)
    headers = {"Authorization": "Bearer client-token",
              protocol.HEADER: str(protocol.CURRENT)}
    r = client.post("/signals/sig_a/ack", json={"acting_human": "operator@example.com"},
                    headers=headers)
    assert r.status_code == 200


# --- the dedup registry, through the real write route ----------------------


def test_a_dedup_repost_of_a_moved_signal_returns_the_cold_row_unchanged(env):
    db, cold = env
    _insert_signal(db, "sig_a", "note", body="original", dedup_key="dk1")
    _move(db, cold, "sig_a", dedup_key="dk1")
    before = cold.read_bytes()

    client = TestClient(libapp.app)
    r = client.post("/signals", json={
        "source": "test", "kind": "note", "actor": "someone",
        "product": "vivy", "body": "a different body", "dedup_key": "dk1"},
        headers=_h())

    assert r.status_code == 200
    assert r.json()["idempotent"] is True
    assert r.json()["id"] == "sig_a"
    assert r.json()["body"] == "original"
    assert cold.read_bytes() == before, (
        "the registry-hit fetch must be read-only: the cold file must be "
        "byte-identical before and after")


def test_a_dedup_repost_of_a_moved_signal_answers_503_when_the_cold_file_is_gone(
        env):
    """Round 2 (reviewer run-4f4ac20c, security-reviewer run-a799154c):
    `_cold_dedup_existing`'s registry hit used to call `attach_cold` directly
    and let `FileNotFoundError` reach FastAPI's default handler as a bare
    500. It must answer the same 503 every other cold-tier read path does,
    and it must write nothing."""
    db, cold = env
    _insert_signal(db, "sig_a", "note", body="original", dedup_key="dk1")
    _move(db, cold, "sig_a", dedup_key="dk1")
    cold.unlink()

    client = TestClient(libapp.app)
    r = client.post("/signals", json={
        "source": "test", "kind": "note", "actor": "someone",
        "product": "vivy", "body": "a different body", "dedup_key": "dk1"},
        headers=_h())

    assert r.status_code == 503
    assert str(cold) not in r.text, "the path must not reach the response"
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT id FROM signals WHERE dedup_key = ?", ("dk1",)).fetchall()
    finally:
        conn.close()
    assert rows == [], "a refused dedup check must mint no new hot row"


def test_a_dedup_repost_of_a_moved_signal_answers_503_when_LIBRARY_COLD_DB_is_unset(
        env, monkeypatch):
    db, cold = env
    _insert_signal(db, "sig_a", "note", body="original", dedup_key="dk1")
    _move(db, cold, "sig_a", dedup_key="dk1")
    monkeypatch.delenv("LIBRARY_COLD_DB")

    client = TestClient(libapp.app)
    r = client.post("/signals", json={
        "source": "test", "kind": "note", "actor": "someone",
        "product": "vivy", "body": "a different body", "dedup_key": "dk1"},
        headers=_h())

    assert r.status_code == 503
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT id FROM signals WHERE dedup_key = ?", ("dk1",)).fetchall()
    finally:
        conn.close()
    assert rows == [], "a refused dedup check must mint no new hot row"


# --- interrupted move: a crash at any of the mover's three points ---------
#
# `sqlite3.Connection` is an immutable C type — an instance's `commit` or
# `execute` cannot be monkeypatched directly — so a subclass installed as
# the `factory` of every `sqlite3.connect` call is what lets these inject a
# failure at a chosen point, the same technique
# `test_discard_sweep.py`'s own commit-boundary test uses.


class _FlakyConnection(sqlite3.Connection):
    fail_commit_on_call = None
    fail_execute_prefix = None
    _commit_calls = 0
    _execute_fired = False

    def commit(self):
        self._commit_calls += 1
        if (self.fail_commit_on_call is not None
                and self._commit_calls == self.fail_commit_on_call):
            raise sqlite3.OperationalError("simulated crash")
        return super().commit()

    def execute(self, sql, *args, **kwargs):
        if (self.fail_execute_prefix is not None and not self._execute_fired
                and sql.startswith(self.fail_execute_prefix)):
            self._execute_fired = True
            raise sqlite3.OperationalError("simulated crash")
        return super().execute(sql, *args, **kwargs)


@pytest.fixture
def flaky_sqlite(monkeypatch):
    real_connect = sqlite3.connect

    def tracking_connect(*args, **kwargs):
        kwargs.setdefault("factory", _FlakyConnection)
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", tracking_connect)


def test_interrupted_move_before_the_cold_insert_commits_leaves_the_row_hot_only(
        env, flaky_sqlite):
    db, cold = env
    _insert_signal(db, "sig_a", "note")

    conn = store.connect_rw()
    store.attach_cold(conn, readonly=False)
    conn.fail_commit_on_call = 1
    with pytest.raises(sqlite3.OperationalError):
        ct._move_batch(conn, ["sig_a"])
    conn.close()

    assert _row_count(db, "sig_a") == 1
    assert _row_count(cold, "sig_a") == 0


def test_interrupted_move_between_the_two_transactions_leaves_the_row_in_both(
        env, flaky_sqlite):
    db, cold = env
    _insert_signal(db, "sig_a", "note", dedup_key="dk1")

    conn = store.connect_rw()
    store.attach_cold(conn, readonly=False)
    conn.fail_execute_prefix = "BEGIN IMMEDIATE"
    with pytest.raises(sqlite3.OperationalError):
        ct._move_batch(conn, ["sig_a"])
    conn.close()

    # Never in neither: present in hot, AND its cold copy already committed.
    assert _row_count(db, "sig_a") == 1
    assert _row_count(cold, "sig_a") == 1

    # Every read path shows it exactly once.
    client = TestClient(libapp.app)
    r = client.get("/signals/sig_a", headers=_h())
    assert r.status_code == 200
    r = client.get("/signals", headers=_h())
    assert [s["id"] for s in r.json()["signals"]].count("sig_a") == 1

    # The next pass completes the move: hot copy removed, cold copy stays.
    conn = store.connect_rw()
    store.attach_cold(conn, readonly=False)
    ct._move_batch(conn, ["sig_a"])
    conn.close()
    assert _row_count(db, "sig_a") == 0
    assert _row_count(cold, "sig_a") == 1


def test_interrupted_move_inside_step_2_before_commit_leaves_it_in_both(
        env, flaky_sqlite):
    db, cold = env
    _insert_signal(db, "sig_a", "note", dedup_key="dk1")

    conn = store.connect_rw()
    store.attach_cold(conn, readonly=False)
    conn.fail_commit_on_call = 2  # the SECOND commit is step 2's
    with pytest.raises(sqlite3.OperationalError):
        ct._move_batch(conn, ["sig_a"])
    conn.close()

    assert _row_count(db, "sig_a") == 1
    assert _row_count(cold, "sig_a") == 1


def _row_count(db_path, sig_id) -> int:
    conn = sqlite3.connect(db_path)
    n = conn.execute("SELECT COUNT(*) FROM signals WHERE id = ?",
                     (sig_id,)).fetchone()[0]
    conn.close()
    return n


# --- concurrent reader: total never changes, no read 404s -----------------


def test_a_concurrent_reader_never_sees_zero_copies_while_the_mover_runs(env):
    db, cold = env
    for i in range(30):
        _insert_signal(db, f"sig_{i:03d}", "note",
                       ingested_at=f"2020-01-01 00:00:{i % 60:02d}.000000")

    client = TestClient(libapp.app)
    totals = set()
    errors = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            r = client.get("/signals", headers=_h())
            if r.status_code != 200:
                errors.append(r.status_code)
                continue
            totals.add(r.json()["total"])
            for i in range(30):
                rr = client.get(f"/signals/sig_{i:03d}", headers=_h())
                if rr.status_code == 404:
                    errors.append(("404", f"sig_{i:03d}"))

    t = threading.Thread(target=reader)
    t.start()
    try:
        conn = store.connect_rw()
        store.attach_cold(conn, readonly=False)
        for i in range(30):
            ct._move_batch(conn, [f"sig_{i:03d}"])
        conn.close()
    finally:
        stop.set()
        t.join(timeout=10)

    assert not errors, f"a concurrent read saw a wrong answer: {errors[:5]}"
    assert totals == {30}, (
        f"total must never change across the move: saw {totals}")


# --- rollback: --restore ----------------------------------------------------


def test_restore_returns_every_row_to_hot_and_the_registry_empties(env):
    db, cold = env
    _insert_signal(db, "sig_a", "note", dedup_key="dk1")
    conn = store.connect_rw()
    store.attach_cold(conn, readonly=False)
    ct._move_batch(conn, ["sig_a"])
    conn.close()

    conn = store.connect_rw()
    store.attach_cold(conn, readonly=False)
    assert ct.restore(conn, dry_run=False) == 1
    conn.close()

    assert _row_count(db, "sig_a") == 1
    assert _row_count(cold, "sig_a") == 0
    reg_conn = sqlite3.connect(db)
    n = reg_conn.execute("SELECT COUNT(*) FROM cold_dedup_keys").fetchone()[0]
    reg_conn.close()
    assert n == 0


def test_interrupted_restore_before_step_1_commits_leaves_it_cold_only(
        env, flaky_sqlite):
    db, cold = env
    _insert_signal(db, "sig_a", "note", dedup_key="dk1")
    conn = store.connect_rw()
    store.attach_cold(conn, readonly=False)
    ct._move_batch(conn, ["sig_a"])
    conn.close()

    conn = store.connect_rw()
    store.attach_cold(conn, readonly=False)
    conn.fail_commit_on_call = 1
    with pytest.raises(sqlite3.OperationalError):
        ct._restore_batch(conn, ["sig_a"])
    conn.close()

    assert _row_count(db, "sig_a") == 0
    assert _row_count(cold, "sig_a") == 1

    # a repost with the same dedup_key still returns the existing signal
    client = TestClient(libapp.app)
    r = client.post("/signals", json={
        "source": "test", "kind": "note", "actor": "someone",
        "product": "vivy", "body": "x", "dedup_key": "dk1"}, headers=_h())
    assert r.status_code == 200
    assert r.json()["id"] == "sig_a"

    # the next restore pass completes it
    conn = store.connect_rw()
    store.attach_cold(conn, readonly=False)
    ct._restore_batch(conn, ["sig_a"])
    conn.close()
    assert _row_count(db, "sig_a") == 1
    assert _row_count(cold, "sig_a") == 0


def test_interrupted_restore_between_the_two_transactions_leaves_it_in_both(
        env, flaky_sqlite):
    db, cold = env
    _insert_signal(db, "sig_a", "note", dedup_key="dk1")
    conn = store.connect_rw()
    store.attach_cold(conn, readonly=False)
    ct._move_batch(conn, ["sig_a"])
    conn.close()

    conn = store.connect_rw()
    store.attach_cold(conn, readonly=False)
    conn.fail_execute_prefix = "DELETE FROM cold.signals"
    with pytest.raises(sqlite3.OperationalError):
        ct._restore_batch(conn, ["sig_a"])
    conn.close()

    # step 1 committed (hot insert + registry delete); step 2 (cold delete)
    # never ran — the row sits in both, read exactly once by id.
    assert _row_count(db, "sig_a") == 1
    assert _row_count(cold, "sig_a") == 1
    client = TestClient(libapp.app)
    r = client.get("/signals/sig_a", headers=_h())
    assert r.status_code == 200

    # the next pass removes the cold copy, leaving it in hot only
    conn = store.connect_rw()
    store.attach_cold(conn, readonly=False)
    ct._restore_batch(conn, ["sig_a"])
    conn.close()
    assert _row_count(db, "sig_a") == 1
    assert _row_count(cold, "sig_a") == 0
