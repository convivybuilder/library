"""storage_report.py and `GET /signals/storage`: per-tier size and growth,
and `library_vectors`'s composition per kind, projected for the memory-
retention epic's own scale (clauses 10-11, issue_01M35R5XRDJBWZFJ87NTGJVTF5).

Built on a REAL migrated `main` (`alembic upgrade head`) and a REAL cold
file (`init_cold.py`), the same reason `test_archives_cold_tier_reads.py`
does — `dbstat`'s byte counts and the two-tier source both depend on the
real schema.
"""
from __future__ import annotations

import datetime as _dt
import json
import sqlite3
import struct
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
import storage_report
import vectors

sqlite_vec = pytest.importorskip("sqlite_vec")

REPO = Path(__file__).resolve().parent.parent

AGENT = "agent-token-vivy"
# `"*"` products, `signal.customer`, and (per the fixture's `REGISTRY`, no
# product bearing a stored `handoff` signal) nothing a handoff wall excludes
# — the three conditions `app._storage_report_wide` requires, so this token
# truly sees everything `storage_report.report`'s platform-wide block is
# computed over (security-reviewer, PR #203 round 2: `"*"` products alone,
# `kind` left unset and defaulting to `agent`, is NOT such a caller — see
# WIDE_NO_CUSTOMER below, which is what this token used to be).
WIDE = "agent-token-wide"
# `"*"` products, but `kind` left unset (`agent`), so it lacks
# `signal.customer` — a caller round 2 found still received the
# platform-wide ratio under the round-1 fix.
WIDE_NO_CUSTOMER = "agent-token-wide-no-customer"
NOSIGNAL = "agent-token-nosignal"

REGISTRY = {
    "_meta": {},
    "vivy": {"repos": []},
    "sous-chef": {"repos": []},
    # A black-box crew (ADR-0043) — registered so it counts toward
    # `len(projects.projects())`, which is exactly the figure a scoped
    # caller must not learn anything about (security-reviewer, PR #203
    # round 1).
    "cfo": {"private": True, "task_text_public": False, "repos": []},
}


def _h(token=AGENT):
    return {"Authorization": f"Bearer {token}",
            protocol.HEADER: str(protocol.CURRENT)}


def _ts(**delta):
    return (_dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
            - _dt.timedelta(**delta)).isoformat(" ")


def _insert_signal(db_path, sig_id, kind, *, product="vivy", body="body",
                   meta="{}", ingested_at=None, embedded_at=None,
                   privacy_class="internal"):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO signals (id, source, kind, actor, ingested_by, "
        "product, refs, body, meta, privacy_class, ingested_at, "
        "embedded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (sig_id, "test", kind, "someone", "someone", product,
         "{}", body, meta, privacy_class, ingested_at or _ts(days=1),
         embedded_at))
    conn.commit()
    conn.close()


def _vec(dominant: int) -> bytes:
    values = [0.0] * vectors.EMBED_DIM
    values[dominant % vectors.EMBED_DIM] = 1.0
    return struct.pack(f"{vectors.EMBED_DIM}f", *values)


def _insert_vector_row(table: str, item_id: str, *, vault="archives",
                       dominant: int = 0) -> None:
    conn = sqlite3.connect(vectors.VECTORS_DB_PATH)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    if table == vectors.VEC_TABLE:
        conn.execute(
            f"INSERT INTO {table}(embedding, vault, item_id) "
            f"VALUES (?, ?, ?)", (_vec(dominant), vault, item_id))
    else:
        conn.execute(
            f"INSERT INTO {table}(embedding, signal_id) VALUES (?, ?)",
            (_vec(dominant), item_id))
    conn.commit()
    conn.close()


def _move(db_path, cold_path, sig_id):
    cols = ",".join(store.TWO_TIER_SIGNAL_COLUMNS)
    conn = sqlite3.connect(db_path)
    conn.execute("ATTACH DATABASE ? AS cold", (str(cold_path),))
    conn.execute(
        f"INSERT INTO cold.signals ({cols}) "
        f"SELECT {cols} FROM main.signals WHERE id = ?", (sig_id,))
    conn.execute("DELETE FROM main.signals WHERE id = ?", (sig_id,))
    conn.commit()
    conn.close()


@pytest.fixture
def env(tmp_path, monkeypatch, vector_index):
    # Each test gets its own `library.db`/`cold.db`/`vectors.db`, but
    # `tier_sizes`'s and `_embedded_bytes_total`'s caches are module-level
    # globals keyed only on time, not on which database was measured — so a
    # cache a previous test left warm would otherwise serve this test a
    # stale measurement from a file that no longer exists.
    storage_report.reset_tier_sizes_cache()
    storage_report.reset_embedded_total_cache()
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
        WIDE: {"name": "wide-agent", "products": ["*"],
              "kind": "customer_reader"},
        WIDE_NO_CUSTOMER: {"name": "wide-no-customer-agent",
                          "products": ["*"]},
        NOSIGNAL: {"name": "nosignal-agent", "products": ["*"],
                  "withholds": ["signal.read"]},
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
    storage_report.reset_tier_sizes_cache()
    storage_report.reset_embedded_total_cache()


# --- GET /signals/storage: capability and shape -----------------------


def test_the_route_refuses_a_caller_without_signal_read(env):
    client = TestClient(libapp.app)
    r = client.get("/signals/storage", headers=_h(NOSIGNAL))
    assert r.status_code == 403


def test_the_route_answers_200_with_every_tier_and_by_kind(env):
    db, cold = env
    _insert_signal(db, "sig_a", "note", embedded_at=_ts(days=1))
    _insert_vector_row(vectors.VEC_TABLE, "sig_a")

    client = TestClient(libapp.app)
    r = client.get("/signals/storage", headers=_h(WIDE))
    assert r.status_code == 200
    body = r.json()
    for tier in (storage_report.TIER_HOT, storage_report.TIER_COLD,
                storage_report.TIER_VECTORS):
        assert tier in body["tiers"]
        t = body["tiers"][tier]
        assert set(t) == {"available", "reason", "size_bytes",
                          "growth_bytes_per_day",
                          "projected_size_bytes_at_horizon",
                          "threshold_bytes", "threshold_date"}
        assert t["available"] is True
        assert t["reason"] is None
        assert t["size_bytes"] >= 0
        assert t["threshold_bytes"] is None
        assert t["threshold_date"] is None
    assert isinstance(body["by_kind"], list)
    assert body["measurement"]["window_days"] == (
        storage_report.DEFAULT_GROWTH_WINDOW_DAYS)


def test_registered_before_signals_signal_id_so_storage_is_not_read_as_an_id(
        env):
    """`storage` must never resolve as a signal id — the same registration-
    order trap `facets`, `acks`, `pr-state` and `search` each carry a
    warning about."""
    client = TestClient(libapp.app)
    r = client.get("/signals/storage", headers=_h(WIDE))
    assert r.status_code == 200, (
        "a 404 here would mean /signals/{signal_id} caught this request "
        "first, with 'storage' read as a signal id")


# --- the vector index excludes signal_vectors, the dead legacy table ----


def test_vectors_tier_size_excludes_signal_vectors(env):
    conn = vectors.connect(store.DB_PATH)
    try:
        before = storage_report.tier_sizes(conn)[storage_report.TIER_VECTORS]
    finally:
        conn.close()

    _insert_vector_row(vectors.SIGNAL_VEC_TABLE, "sig_dead")
    storage_report.reset_tier_sizes_cache()

    conn = vectors.connect(store.DB_PATH)
    try:
        after_dead = storage_report.tier_sizes(conn)[storage_report.TIER_VECTORS]
    finally:
        conn.close()
    assert after_dead == before, (
        "a row in the legacy signal_vectors table must not change the "
        "measured vector-index size")

    _insert_vector_row(vectors.VEC_TABLE, "sig_live")
    storage_report.reset_tier_sizes_cache()

    conn = vectors.connect(store.DB_PATH)
    try:
        after_live = storage_report.tier_sizes(conn)[storage_report.TIER_VECTORS]
    finally:
        conn.close()
    assert after_live > before, (
        "a row in library_vectors must grow the measured vector-index size")


def test_hot_and_cold_tier_sizes_grow_with_their_own_rows(env):
    db, cold = env
    conn = store.connect()
    try:
        store.attach_cold(conn, readonly=True)
        before = storage_report.tier_sizes(conn)
    finally:
        conn.close()

    _insert_signal(db, "sig_a", "note", body="x" * 5000)
    storage_report.reset_tier_sizes_cache()

    conn = store.connect()
    try:
        store.attach_cold(conn, readonly=True)
        after_hot = storage_report.tier_sizes(conn)
    finally:
        conn.close()
    assert after_hot[storage_report.TIER_HOT] > before[storage_report.TIER_HOT]
    assert after_hot[storage_report.TIER_COLD] == before[storage_report.TIER_COLD]

    _move(db, cold, "sig_a")
    storage_report.reset_tier_sizes_cache()

    conn = store.connect()
    try:
        store.attach_cold(conn, readonly=True)
        after_move = storage_report.tier_sizes(conn)
    finally:
        conn.close()
    assert after_move[storage_report.TIER_COLD] > before[storage_report.TIER_COLD]
    assert after_move[storage_report.TIER_HOT] < after_hot[storage_report.TIER_HOT]


# --- per-kind share, across both tiers -----------------------------------


def test_kind_shares_computes_exact_fractions(env):
    db, cold = env
    for i in range(3):
        _insert_signal(db, f"sig_note_{i}", "note", embedded_at=_ts(days=1))
        _insert_vector_row(vectors.VEC_TABLE, f"sig_note_{i}", dominant=i)
    _insert_signal(db, "sig_finding", "finding", embedded_at=_ts(days=1))
    _insert_vector_row(vectors.VEC_TABLE, "sig_finding", dominant=9)

    conn = vectors.connect(store.DB_PATH)
    try:
        store.attach_cold(conn, readonly=True)
        source = store.signals_source()
        shares = storage_report.kind_shares(conn, source, "WHERE 1 = 1", [])
    finally:
        conn.close()

    by_kind = {kind: (n, share) for kind, n, share in shares}
    assert by_kind["note"] == (3, 0.75)
    assert by_kind["finding"] == (1, 0.25)


def test_kind_shares_counts_a_cold_signals_vectors_under_its_kind(env):
    """The cold-tier mover leaves vectors in place (archives-cold-tier.md
    §2), so a moved signal's vectors must still count under its kind."""
    db, cold = env
    _insert_signal(db, "sig_a", "finding", embedded_at=_ts(days=1))
    _insert_vector_row(vectors.VEC_TABLE, "sig_a")
    _move(db, cold, "sig_a")

    conn = vectors.connect(store.DB_PATH)
    try:
        store.attach_cold(conn, readonly=True)
        source = store.signals_source()
        shares = storage_report.kind_shares(conn, source, "WHERE 1 = 1", [])
    finally:
        conn.close()

    by_kind = {kind: n for kind, n, _ in shares}
    assert by_kind.get("finding") == 1


# --- the growth arithmetic -------------------------------------------


def test_content_bytes_matches_a_hand_computed_sum(env):
    db, cold = env
    now = _dt.datetime(2026, 10, 1, 12, 0, 0)
    inside = (now - _dt.timedelta(days=5)).isoformat(" ")
    outside = (now - _dt.timedelta(days=40)).isoformat(" ")
    # body + meta: 10 + 2 = 12 bytes for each row inside the window.
    _insert_signal(db, "sig_in_1", "note", body="x" * 10, meta="{}",
                  ingested_at=inside)
    _insert_signal(db, "sig_in_2", "note", body="y" * 10, meta="{}",
                  ingested_at=inside)
    _insert_signal(db, "sig_out", "note", body="z" * 10, meta="{}",
                  ingested_at=outside)

    conn = store.connect()
    try:
        store.attach_cold(conn, readonly=True)
        source = store.signals_source()
        since = (now - _dt.timedelta(days=30)).isoformat(" ")
        rows, content_bytes = storage_report._content_bytes(
            conn, source, "WHERE 1 = 1", [], since)
    finally:
        conn.close()
    assert rows == 2
    assert content_bytes == 24


def test_report_scales_ingestion_linearly_to_twenty_projects(env):
    db, cold = env
    now = _dt.datetime(2026, 10, 1, 12, 0, 0)
    since = (now - _dt.timedelta(days=5))
    # Two signals ingested inside a 10-day window, each with 100 bytes of
    # content (not an embedded kind, so no vector growth to account for):
    # 200 bytes / 10 days / 2 current projects * 20 projects = 200 bytes/day.
    for i in range(2):
        _insert_signal(db, f"sig_{i}", "pr_state", body="x" * 98, meta="{}",
                      ingested_at=since.isoformat(" "))

    conn = vectors.connect(store.DB_PATH)
    try:
        store.attach_cold(conn, readonly=True)
        source = store.signals_source()
        result = storage_report.report(
            conn, source=source, where="WHERE 1 = 1", params=[],
            current_project_count=2, window_days=10, now=now)
    finally:
        conn.close()

    assert result["measurement"]["bytes_ingested"] == 200
    assert result["tiers"][storage_report.TIER_HOT]["growth_bytes_per_day"] == 200
    assert result["tiers"][storage_report.TIER_COLD]["growth_bytes_per_day"] == 200


def test_report_returns_zero_growth_with_no_current_projects(env):
    conn = vectors.connect(store.DB_PATH)
    try:
        store.attach_cold(conn, readonly=True)
        source = store.signals_source()
        result = storage_report.report(
            conn, source=source, where="WHERE 1 = 1", params=[],
            current_project_count=0, window_days=30)
    finally:
        conn.close()
    for tier in storage_report.TIERS:
        assert result["tiers"][tier]["growth_bytes_per_day"] == 0


# --- threshold-date arithmetic, against _project_forward directly -------


def test_threshold_date_is_the_exact_crossing_day():
    now = _dt.datetime(2026, 1, 1)
    out = storage_report._project_forward(
        100, 10, now=now, threshold_bytes=150)
    assert out["threshold_date"] == "2026-01-06"  # (150-100)/10 = 5 days


def test_threshold_already_reached_dates_today():
    now = _dt.datetime(2026, 1, 1)
    out = storage_report._project_forward(
        200, 10, now=now, threshold_bytes=150)
    assert out["threshold_date"] == "2026-01-01"


def test_no_threshold_given_leaves_the_date_null():
    now = _dt.datetime(2026, 1, 1)
    out = storage_report._project_forward(
        100, 10, now=now, threshold_bytes=None)
    assert out["threshold_bytes"] is None
    assert out["threshold_date"] is None


def test_zero_growth_never_reaches_a_threshold_above_current_size():
    now = _dt.datetime(2026, 1, 1)
    out = storage_report._project_forward(
        100, 0, now=now, threshold_bytes=150)
    assert out["threshold_date"] is None


def test_projected_size_at_horizon_is_the_linear_extrapolation():
    now = _dt.datetime(2026, 1, 1)
    out = storage_report._project_forward(
        1000, 2, now=now, threshold_bytes=None)
    horizon_days = storage_report.PROJECTION_HORIZON_YEARS * storage_report.DAYS_PER_YEAR
    assert out["projected_size_bytes_at_horizon"] == 1000 + 2 * horizon_days


# --- threshold_bytes flows through the route's query params -------------


def test_threshold_bytes_query_params_produce_a_threshold_date(env):
    client = TestClient(libapp.app)
    r = client.get("/signals/storage",
                   params={"threshold_bytes_hot": 1},
                   headers=_h(WIDE))
    assert r.status_code == 200
    hot = r.json()["tiers"][storage_report.TIER_HOT]
    assert hot["threshold_bytes"] == 1
    # The hot tier already holds more than 1 byte once alembic's own schema
    # exists, so the threshold is already crossed.
    assert hot["threshold_date"] is not None


def test_an_invalid_threshold_bytes_is_refused(env):
    client = TestClient(libapp.app)
    r = client.get("/signals/storage",
                   params={"threshold_bytes_cold": "not-a-number"},
                   headers=_h(WIDE))
    assert r.status_code == 400


def test_a_negative_threshold_bytes_is_refused(env):
    client = TestClient(libapp.app)
    r = client.get("/signals/storage",
                   params={"threshold_bytes_vectors": -1},
                   headers=_h(WIDE))
    assert r.status_code == 400


def test_window_days_out_of_bounds_is_refused(env):
    client = TestClient(libapp.app)
    r = client.get("/signals/storage", params={"window_days": 0},
                   headers=_h(WIDE))
    assert r.status_code == 400
    r = client.get("/signals/storage", params={"window_days": 9999},
                   headers=_h(WIDE))
    assert r.status_code == 400


def test_storage_refuses_an_unrecognised_parameter(env):
    client = TestClient(libapp.app)
    r = client.get("/signals/storage", params={"kind": "note"}, headers=_h(WIDE))
    assert r.status_code == 400
    assert r.json()["unknown"] == ["kind"]
    assert r.json()["supported"] == sorted(
        ["threshold_bytes_cold", "threshold_bytes_hot",
         "threshold_bytes_vectors", "window_days"])


# --- scoping: a product-scoped caller sees only its own by_kind ----------


def test_a_scoped_caller_does_not_see_another_products_kind(env):
    db, cold = env
    _insert_signal(db, "sig_vivy", "note", product="vivy",
                   embedded_at=_ts(days=1))
    _insert_vector_row(vectors.VEC_TABLE, "sig_vivy")
    _insert_signal(db, "sig_sc", "finding", product="sous-chef",
                   embedded_at=_ts(days=1))
    _insert_vector_row(vectors.VEC_TABLE, "sig_sc", dominant=7)

    client = TestClient(libapp.app)
    r = client.get("/signals/storage", headers=_h(AGENT))  # scoped to vivy
    assert r.status_code == 200
    kinds = {kind for kind, _, _ in r.json()["by_kind"]}
    assert "note" in kinds
    assert "finding" not in kinds


# --- scoping: tiers and the project count are withheld, not scoped -------


def test_a_scoped_caller_sees_no_platform_wide_tier_size_or_project_count(
        env):
    """`tiers.*.size_bytes` and `measurement.current_project_count` are
    physical facts about the whole shared database files, with no per-
    product share to compute instead — so a single-product caller must get
    them withheld rather than the platform-wide total, which would include
    another product's bytes and a black-box crew's existence
    (security-reviewer, PR #203 round 1). `GET /metrics/librarian` already
    gates the identical disclosure shape for `current_project_count`'s own
    kind of figure.
    """
    db, cold = env
    _insert_signal(db, "sig_sc", "note", product="sous-chef",
                   body="y" * 5000, embedded_at=_ts(days=1))
    _insert_vector_row(vectors.VEC_TABLE, "sig_sc")

    client = TestClient(libapp.app)
    r = client.get("/signals/storage", headers=_h(AGENT))  # scoped to vivy
    assert r.status_code == 200
    body = r.json()
    for tier in storage_report.TIERS:
        t = body["tiers"][tier]
        assert t["available"] is False
        assert t["size_bytes"] is None, (
            "a scoped caller must not see any tier's byte total, which is "
            "computed over every product's rows, sous-chef's included")
        assert t["growth_bytes_per_day"] is None
        assert t["projected_size_bytes_at_horizon"] is None
        # The reason must name the gate THIS caller actually failed — not
        # a generic restatement, and not either of the other two gates it
        # was never tested against (reviewer, PR #203 round 4: the single
        # reason this used to return named only the "*"-products gate
        # regardless of which of the three actually withheld a caller).
        assert t["reason"] == libapp._WIDE_REASON_NOT_WILDCARD
        assert t["reason"] not in (
            libapp._WIDE_REASON_NO_CUSTOMER, libapp._WIDE_REASON_HANDOFF)
    assert body["measurement"]["current_project_count"] is None, (
        "a scoped caller must not learn how many projects are registered "
        "platform-wide, including the private crew this fixture holds")
    assert libapp._WIDE_REASON_NOT_WILDCARD in body["note"]

    # The wide caller sees real, non-null figures for the same database —
    # proving the scoped caller's nulls are a withholding, not an artifact
    # of an empty store.
    r_wide = client.get("/signals/storage", headers=_h(WIDE))
    assert r_wide.status_code == 200
    wide_body = r_wide.json()
    for tier in storage_report.TIERS:
        assert wide_body["tiers"][tier]["available"] is True
        assert wide_body["tiers"][tier]["size_bytes"] > 0
    assert wide_body["measurement"]["current_project_count"] == len(
        projects.projects())


# --- "*" products is necessary but not sufficient for the wide figures ---


def test_a_wildcard_caller_without_signal_customer_is_withheld_the_wide_figures(
        env):
    """`"*"` in `ident.products` alone is not full visibility — a caller
    without `signal.customer` cannot read a customer-class row through
    `_customer_wall`, so it must not receive `tiers`, the platform-wide
    `current_project_count`, or a vectors-growth ratio computed over rows it
    cannot read anywhere else (security-reviewer, PR #203 round 2). This is
    exactly `WIDE`'s own shape before this fix: `"*"` products, `kind` left
    unset and defaulting to `agent`, which `identity.ROLE_CAPS` does not
    grant `signal.customer`.
    """
    db, cold = env
    _insert_signal(db, "sig_customer", "note", product="vivy",
                   privacy_class="customer", body="x" * 5000,
                   embedded_at=_ts(days=1))
    _insert_vector_row(vectors.VEC_TABLE, "sig_customer")

    client = TestClient(libapp.app)
    r = client.get("/signals/storage", headers=_h(WIDE_NO_CUSTOMER))
    assert r.status_code == 200
    body = r.json()
    for tier in storage_report.TIERS:
        t = body["tiers"][tier]
        assert t["available"] is False
        assert t["size_bytes"] is None
        assert t["growth_bytes_per_day"] is None
        assert t["projected_size_bytes_at_horizon"] is None
        # This caller DOES satisfy the "*"-products gate — the reason must
        # name the signal.customer gate it actually fails, not restate the
        # gate it already passed (reviewer, PR #203 round 4).
        assert t["reason"] == libapp._WIDE_REASON_NO_CUSTOMER
        assert t["reason"] not in (
            libapp._WIDE_REASON_NOT_WILDCARD, libapp._WIDE_REASON_HANDOFF)
    assert body["measurement"]["current_project_count"] is None
    assert libapp._WIDE_REASON_NO_CUSTOMER in body["note"]


def test_a_wildcard_customer_reading_caller_behind_a_handoff_wall_is_withheld_too(
        env):
    """NOBODY is exempt from `_handoff_wall` (see its own docstring): a
    `kind = 'handoff'` row for a product the registry marks private is
    excluded for every caller, not scoped per identity. So a `"*"`-scoped,
    `signal.customer`-holding caller — `WIDE`, otherwise a full-visibility
    identity — still cannot read such a row through any route this service
    offers, and must get the wide figures withheld too once one exists,
    rather than a ratio or a project count derived from content it cannot
    read anywhere else (security-reviewer, PR #203 round 2; `cfo` is this
    fixture's private crew, ADR-0043).
    """
    db, cold = env
    _insert_signal(db, "sig_handoff", "handoff", product="cfo",
                   body="x" * 5000, embedded_at=_ts(days=1))
    _insert_vector_row(vectors.VEC_TABLE, "sig_handoff")

    client = TestClient(libapp.app)
    r = client.get("/signals/storage", headers=_h(WIDE))
    assert r.status_code == 200
    body = r.json()
    for tier in storage_report.TIERS:
        t = body["tiers"][tier]
        assert t["available"] is False
        assert t["size_bytes"] is None
        assert t["growth_bytes_per_day"] is None
        assert t["projected_size_bytes_at_horizon"] is None
        # `WIDE` satisfies both of the other two gates — the reason must
        # name the handoff wall, the one it actually fails, not either gate
        # it already passes (reviewer, PR #203 round 4).
        assert t["reason"] == libapp._WIDE_REASON_HANDOFF
        assert t["reason"] not in (
            libapp._WIDE_REASON_NOT_WILDCARD, libapp._WIDE_REASON_NO_CUSTOMER)
    assert body["measurement"]["current_project_count"] is None
    assert libapp._WIDE_REASON_HANDOFF in body["note"]


def test_the_handoff_admission_check_is_not_cached_unlike_tier_sizes(env):
    """`_storage_report_wide`'s own handoff-existence check must run fresh
    on every request — unlike `tier_sizes`/`_embedded_bytes_total`, which
    are deliberately cached for `TIER_SIZES_CACHE_TTL_SECONDS` because they
    are measurements, not admission decisions. Caching THIS check would let
    a newly-created handoff row leak the wide block to `WIDE` for up to the
    cache window, reopening the gap round 3's third gate exists to close
    (reviewer + security-reviewer, PR #203 round 3 non-blocking; round 4
    confirms it rather than just asserting it).

    Runs both calls back-to-back, well inside
    `storage_report.TIER_SIZES_CACHE_TTL_SECONDS`, so a pass here cannot be
    explained by the cache having merely expired between calls.
    """
    db, cold = env
    client = TestClient(libapp.app)

    r1 = client.get("/signals/storage", headers=_h(WIDE))
    assert r1.status_code == 200
    assert r1.json()["tiers"][storage_report.TIER_HOT]["available"] is True

    _insert_signal(db, "sig_handoff", "handoff", product="cfo",
                   body="x" * 5000, embedded_at=_ts(days=1))
    _insert_vector_row(vectors.VEC_TABLE, "sig_handoff")

    r2 = client.get("/signals/storage", headers=_h(WIDE))
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["tiers"][storage_report.TIER_HOT]["available"] is False
    assert body2["tiers"][storage_report.TIER_HOT]["reason"] == (
        libapp._WIDE_REASON_HANDOFF)


# --- the vectors tier's growth ratio shares one scope end-to-end ---------


def test_vectors_growth_rate_divides_by_the_platform_wide_embedded_total(
        env):
    """`bytes_per_index_byte` must divide the whole index by the bytes EVER
    embedded across every product, never by one product's own slice —
    scoping only the denominator inflates a minority-share product's rate
    by roughly (platform total ÷ that product's own total) (reviewer, PR
    #203 round 1, worked example: a 10x overstatement for a product holding
    a tenth of all embedded content, reproduced here with the same 1:9
    split).
    """
    db, cold = env
    now = _dt.datetime(2026, 10, 1, 12, 0, 0)
    since = (now - _dt.timedelta(days=5)).isoformat(" ")
    # vivy: 1,000 embedded bytes (998 body + 2 meta). sous-chef: 9,000
    # (8,998 body + 2 meta). Platform total: 10,000.
    _insert_signal(db, "sig_vivy", "note", product="vivy", body="x" * 998,
                  meta="{}", ingested_at=since, embedded_at=since)
    _insert_signal(db, "sig_sc", "note", product="sous-chef", body="y" * 8998,
                  meta="{}", ingested_at=since, embedded_at=since)
    _insert_vector_row(vectors.VEC_TABLE, "sig_vivy")
    _insert_vector_row(vectors.VEC_TABLE, "sig_sc", dominant=1)

    conn = vectors.connect(store.DB_PATH)
    try:
        store.attach_cold(conn, readonly=True)
        source = store.signals_source()
        index_bytes = storage_report.tier_sizes(
            conn)[storage_report.TIER_VECTORS]
        result = storage_report.report(
            conn, source=source, where="WHERE 1 = 1 AND product = ?",
            params=["vivy"], current_project_count=2, window_days=5, now=now)
    finally:
        conn.close()

    # Hand-worked from the PLATFORM-WIDE ratio (index bytes ÷ 10,000 total
    # embedded bytes ever), applied to vivy's own 1,000 window bytes, scaled
    # from today's 2 projects to the epic's 20.
    expected_ratio = index_bytes / 10_000
    expected_rate = round(1_000 * expected_ratio / 5 * (20 / 2))
    actual_rate = result["tiers"][storage_report.TIER_VECTORS][
        "growth_bytes_per_day"]
    assert actual_rate == expected_rate

    # The bug this guards against: scoping the ratio's denominator to
    # vivy's own 1,000 bytes instead of the platform's 10,000 inflates the
    # ratio roughly 10x — assert the fixed code does not reproduce it.
    buggy_ratio = index_bytes / 1_000
    buggy_rate = round(1_000 * buggy_ratio / 5 * (20 / 2))
    assert actual_rate != buggy_rate


def test_embedded_bytes_total_cache_is_keyed_by_source(env):
    """`_embedded_bytes_total`'s cache must key on `source`, not time
    alone — otherwise a caller passing a differently-scoped `source` than
    an earlier one would silently inherit the earlier call's total instead
    of its own (security-reviewer, PR #203 round 3 suggestion). Exercised
    directly: `GET /signals/storage` itself only ever passes one unscoped
    `source` today, so this is the only way to reach a second one.
    """
    db, cold = env
    # vivy: 1,000 embedded bytes (998 body + 2 meta). sous-chef: 9,000
    # (8,998 body + 2 meta) — same split the ratio test above uses.
    _insert_signal(db, "sig_vivy", "note", product="vivy", body="x" * 998,
                  meta="{}", embedded_at=_ts(days=1))
    _insert_signal(db, "sig_sc", "note", product="sous-chef",
                  body="y" * 8998, meta="{}", embedded_at=_ts(days=1))

    conn = vectors.connect(store.DB_PATH)
    try:
        store.attach_cold(conn, readonly=True)
        full_source = store.signals_source()
        vivy_source = store.signals_source("product = 'vivy'")

        # Populate the cache under `vivy_source` FIRST, so a cache keyed
        # on time alone (the pre-fix shape) would hand the full-platform
        # call vivy's own 1,000-byte total instead of recomputing.
        vivy_total = storage_report._embedded_bytes_total(conn, vivy_source)
        full_total = storage_report._embedded_bytes_total(conn, full_source)
        # And the reverse order must not cross-contaminate either.
        storage_report.reset_embedded_total_cache()
        full_total_2 = storage_report._embedded_bytes_total(
            conn, full_source)
        vivy_total_2 = storage_report._embedded_bytes_total(
            conn, vivy_source)
    finally:
        conn.close()

    assert vivy_total == 1_000
    assert full_total == 10_000
    assert full_total_2 == 10_000
    assert vivy_total_2 == 1_000
