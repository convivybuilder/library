"""bin/cert_expiry_check.py — platform/decisions/0066's "the Library warns
when any registered certificate, including its own server certificate, is
inside 21 days of expiry" (issue_01M3XANTMTAED0P592KVX32D41).

**Do not mock the DB layer.** A real `machine_certificates` table and a real
`signals` table, the same shape `test_machine_registry.py` and
`test_discard_sweep.py` each already use for their own half of this.

**The served certificate is real TLS material, not a stand-in fingerprint.**
`tls_ca.py` builds an actual throwaway CA and leaf the same way
`test_machine_identity_gate.py` and `test_mtls_bind_preflight.py` do, so the
DER this test reads `not_after` and the fingerprint from is the same shape
`machine_identity.not_after_utc`/`sha256_der` parse in production.
"""
from __future__ import annotations

import datetime as _dt
import sqlite3
import ssl

import pytest

import cert_expiry_check as cec
import machine_identity
import mtls
import store
import tls_ca

SCHEMA = """
CREATE TABLE machine_certificates (
    id INTEGER PRIMARY KEY,
    machine TEXT NOT NULL,
    fingerprint_sha256 TEXT NOT NULL UNIQUE,
    not_after TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'revoked')),
    revoked_at TEXT,
    note TEXT,
    created_at TEXT NOT NULL,
    custody TEXT NOT NULL CHECK (custody IN ('hardware', 'file'))
);
CREATE INDEX ix_machine_certificates_lookup
    ON machine_certificates (machine, fingerprint_sha256);

CREATE TABLE signals (
    id TEXT PRIMARY KEY, source TEXT, stream TEXT, kind TEXT, actor TEXT,
    ingested_by TEXT, product TEXT, refs TEXT, body TEXT, meta TEXT,
    privacy_class TEXT, produced_at TEXT, ingested_at TEXT,
    embedded_at TEXT, dedup_key TEXT UNIQUE, acting_human TEXT);

-- `main`-only, the same shape `test_discard_sweep.py`'s MAIN_SCHEMA uses:
-- `insert_signal`'s dedup check falls through to this registry (and from
-- there to the cold file `_default_cold_db` stages) whenever a dedup_key
-- misses the hot table — every write this job makes carries one.
CREATE TABLE cold_dedup_keys (
    dedup_key TEXT PRIMARY KEY, signal_id TEXT NOT NULL);
"""

NOW = _dt.datetime(2026, 10, 2, tzinfo=_dt.timezone.utc)

FP_ACTIVE_INSIDE = "a" * 64
FP_ACTIVE_OUTSIDE = "b" * 64
FP_REVOKED_INSIDE = "c" * 64
FP_ACTIVE_SECOND = "d" * 64


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "library.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    monkeypatch.setattr(store, "DB_PATH", path)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    # No TLS material staged by default — `served_certificate_candidate`
    # must see "missing" and contribute no candidate at all.
    monkeypatch.setattr(mtls, "TLS_CERT", tmp_path / "no-tls" / "server.crt")
    monkeypatch.setattr(mtls, "TLS_KEY", tmp_path / "no-tls" / "server.key")
    monkeypatch.setattr(mtls, "TLS_CA", tmp_path / "no-tls" / "ca.crt")
    return path


def _register(db, machine, fingerprint, not_after, *, status="active",
              custody="hardware"):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO machine_certificates (machine, fingerprint_sha256, "
        "not_after, status, created_at, custody) VALUES (?,?,?,?,?,?)",
        (machine, fingerprint, not_after, status, "2026-01-01 00:00:00",
         custody))
    conn.commit()
    conn.close()


def _signals(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in
                conn.execute("SELECT * FROM signals").fetchall()]
    finally:
        conn.close()


def _staged_served_cert(tmp_path, monkeypatch, *, days):
    ca_cert, ca_key = tls_ca.make_ca(tmp_path)
    server_cert, server_key = tls_ca.make_cert(
        tmp_path, "mini", ca_cert, ca_key, dns_names=("localhost",),
        ip_names=("127.0.0.1",), ext_key_usage="serverAuth", days=days)
    monkeypatch.setattr(mtls, "TLS_CERT", server_cert)
    monkeypatch.setattr(mtls, "TLS_KEY", server_key)
    monkeypatch.setattr(mtls, "TLS_CA", ca_cert)
    return server_cert


# --- pure functions -----------------------------------------------------

def test_due_for_warning_true_inside_the_window():
    assert cec.due_for_warning(NOW + _dt.timedelta(days=20), NOW)


def test_due_for_warning_true_exactly_at_the_boundary():
    assert cec.due_for_warning(NOW + _dt.timedelta(days=21), NOW)


def test_due_for_warning_false_just_outside_the_window():
    assert not cec.due_for_warning(NOW + _dt.timedelta(days=22), NOW)


def test_due_for_warning_true_when_already_past():
    assert cec.due_for_warning(NOW - _dt.timedelta(days=5), NOW)


def test_alert_dedup_key_is_stable_within_a_day_and_changes_the_next():
    key_today = cec.alert_dedup_key(FP_ACTIVE_INSIDE, NOW)
    key_later_same_day = cec.alert_dedup_key(
        FP_ACTIVE_INSIDE, NOW + _dt.timedelta(hours=5))
    key_next_day = cec.alert_dedup_key(
        FP_ACTIVE_INSIDE, NOW + _dt.timedelta(days=1))
    assert key_today == key_later_same_day
    assert key_today != key_next_day


# --- the three-row scenario the work item names by name ------------------

def test_exactly_one_alert_names_the_active_row_inside_the_window(db):
    """One active row inside the window, one active row outside it, one
    revoked row inside it — exactly one alert, naming the first."""
    _register(db, "mini", FP_ACTIVE_INSIDE, "2026-10-15 00:00:00",
             status="active")
    _register(db, "macbook", FP_ACTIVE_OUTSIDE, "2027-06-01 00:00:00",
             status="active")
    _register(db, "decommissioned", FP_REVOKED_INSIDE, "2026-10-10 00:00:00",
             status="revoked")

    rc = cec.run(now=NOW)

    assert rc == 0
    rows = _signals(db)
    assert len(rows) == 1
    assert rows[0]["kind"] == "ops-alert"
    assert rows[0]["privacy_class"] == "internal"
    assert "mini" in rows[0]["body"]
    assert FP_ACTIVE_INSIDE in rows[0]["body"]
    assert FP_ACTIVE_OUTSIDE not in rows[0]["body"]
    assert FP_REVOKED_INSIDE not in rows[0]["body"]
    assert cec.RUNBOOK_ID in rows[0]["body"]


# --- the served certificate ------------------------------------------------

def test_a_served_certificate_inside_the_window_alerts(db, tmp_path,
                                                        monkeypatch):
    server_cert = _staged_served_cert(tmp_path, monkeypatch, days=10)
    der = ssl.PEM_cert_to_DER_cert(server_cert.read_text())
    fingerprint = machine_identity.sha256_der(der)

    rc = cec.run()  # real "now" — 10 real days out is inside the 21-day window

    assert rc == 0
    rows = _signals(db)
    assert len(rows) == 1
    assert rows[0]["kind"] == "ops-alert"
    assert fingerprint in rows[0]["body"]


def test_a_served_certificate_outside_the_window_does_not_alert(
        db, tmp_path, monkeypatch):
    _staged_served_cert(tmp_path, monkeypatch, days=365)
    rc = cec.run()
    assert rc == 0
    assert _signals(db) == []


def test_absent_tls_files_produce_no_alert(db):
    """`db`'s own fixture points `mtls.TLS_CERT`/`_KEY`/`_CA` at files that
    do not exist — the default shape every other scenario in this file
    builds on. A missing listener is `serve.log`'s report, not this job's."""
    rc = cec.run(now=NOW)
    assert rc == 0
    assert _signals(db) == []


def test_served_certificate_candidate_resolves_a_registered_machine_name(
        db, tmp_path, monkeypatch):
    server_cert = _staged_served_cert(tmp_path, monkeypatch, days=5)
    der = ssl.PEM_cert_to_DER_cert(server_cert.read_text())
    fingerprint = machine_identity.sha256_der(der)
    # Registered under a status other than active — the served check must
    # still name it, since what matters is what is actually being served.
    _register(db, "mini", fingerprint, "2099-01-01 00:00:00",
             status="revoked")

    conn = store.connect()
    try:
        candidate = cec.served_certificate_candidate(conn)
    finally:
        conn.close()
    assert candidate["machine"] == "mini"


def test_a_malformed_served_certificate_is_reported_not_skipped(
        db, tmp_path, monkeypatch):
    """A present-but-malformed `server.crt` must be this job's own finding
    (reported, exit non-zero), not silently skipped, and it must not stop
    the registry rows already read from being checked in the same run."""
    _register(db, "mini", FP_ACTIVE_INSIDE, "2026-10-15 00:00:00")
    cert_path = tmp_path / "server.crt"
    cert_path.write_text("this is not a PEM certificate")
    monkeypatch.setattr(mtls, "TLS_CERT", cert_path)
    monkeypatch.setattr(mtls, "TLS_KEY", tmp_path / "server.key")
    monkeypatch.setattr(mtls, "TLS_CA", tmp_path / "ca.crt")
    (tmp_path / "server.key").write_text("placeholder")
    (tmp_path / "ca.crt").write_text("placeholder")

    rc = cec.run(now=NOW)

    assert rc == 1
    rows = _signals(db)
    assert len(rows) == 1
    assert "mini" in rows[0]["body"]


def test_an_unreadable_served_certificate_is_reported_not_skipped(
        db, tmp_path, monkeypatch):
    """Same as above, for a `server.crt` that exists but cannot be read
    (EACCES) rather than one that reads fine but parses as garbage."""
    _register(db, "mini", FP_ACTIVE_INSIDE, "2026-10-15 00:00:00")
    cert_path = tmp_path / "server.crt"
    cert_path.write_text("placeholder")
    cert_path.chmod(0o000)
    monkeypatch.setattr(mtls, "TLS_CERT", cert_path)
    monkeypatch.setattr(mtls, "TLS_KEY", tmp_path / "server.key")
    monkeypatch.setattr(mtls, "TLS_CA", tmp_path / "ca.crt")
    (tmp_path / "server.key").write_text("placeholder")
    (tmp_path / "ca.crt").write_text("placeholder")

    try:
        rc = cec.run(now=NOW)
    finally:
        cert_path.chmod(0o600)  # tmp_path cleanup needs it readable again

    assert rc == 1
    rows = _signals(db)
    assert len(rows) == 1
    assert "mini" in rows[0]["body"]


def test_served_certificate_candidate_falls_back_when_unregistered(
        db, tmp_path, monkeypatch):
    _staged_served_cert(tmp_path, monkeypatch, days=5)

    conn = store.connect()
    try:
        candidate = cec.served_certificate_candidate(conn)
    finally:
        conn.close()
    assert candidate["machine"] == cec.UNREGISTERED_LABEL


# --- daily re-warning --------------------------------------------------

def test_a_same_day_rerun_does_not_duplicate_the_alert(db):
    """A same-day rerun's `dedup_key` collision must be a quiet idempotent
    no-op (exit 0, the existing row returned), never a refusal — passing
    `lambda _ex: False` as `can_return_existing` makes every collision a 409
    instead, which this asserts against by checking both runs' exit codes,
    not just the final row count."""
    _register(db, "mini", FP_ACTIVE_INSIDE, "2026-10-15 00:00:00")
    rc_first = cec.run(now=NOW)
    rc_second = cec.run(now=NOW + _dt.timedelta(hours=6))
    assert rc_first == 0
    assert rc_second == 0
    assert len(_signals(db)) == 1


def test_a_same_day_rerun_still_checks_a_newly_due_candidate_after_it(db):
    """One candidate's same-day collision must not stop the rest of that
    run's `due` list from being checked — the exact failure the dedup bug
    produced: looping `for candidate in due` and returning on the first
    non-2xx status would abandon every candidate after a collision,
    including one sorted after it that is due for the first time."""
    _register(db, "mini", FP_ACTIVE_INSIDE, "2026-10-15 00:00:00")
    rc_first = cec.run(now=NOW)
    assert rc_first == 0
    assert len(_signals(db)) == 1

    _register(db, "macbook", FP_ACTIVE_SECOND, "2026-10-20 00:00:00")
    rc_second = cec.run(now=NOW + _dt.timedelta(hours=6))

    assert rc_second == 0
    rows = _signals(db)
    assert len(rows) == 2
    assert any("macbook" in r["body"] for r in rows)


def test_a_next_day_rerun_warns_again(db):
    """"Keep warning on every run until the certificate is renewed" — a
    run on a later day is a fresh `dedup_key` and a fresh row."""
    _register(db, "mini", FP_ACTIVE_INSIDE, "2026-10-15 00:00:00")
    cec.run(now=NOW)
    cec.run(now=NOW + _dt.timedelta(days=1))
    assert len(_signals(db)) == 2


def test_writes_disabled_is_reported_rather_than_raised(db, monkeypatch):
    _register(db, "mini", FP_ACTIVE_INSIDE, "2026-10-15 00:00:00")
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    rc = cec.run(now=NOW)
    assert rc == 1
    assert _signals(db) == []
