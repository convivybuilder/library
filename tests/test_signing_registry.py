"""`signing_registry.py`: the write path for `signing_certificates`
(platform/contracts/library-protocol §2.2), and specifically the invariant
that differs from `machine_registry.py`'s — a person may hold several
active certificates at once, so `issue()` never revokes a sibling row.

**Do not mock the DB layer.** A real `signing_certificates` table, built
from the migration's own DDL, the same shape `test_machine_registry.py`
already uses for the sibling registry.
"""
from __future__ import annotations

import datetime as _dt
import sqlite3

import pytest

import signing_registry
import store

PERSON = "operator@example.com"
MACHINE = "macbook"
FP_A = "a" * 64
FP_B = "b" * 64
NOT_AFTER = _dt.datetime(2026, 12, 1)


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "control.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE signing_certificates (
            id INTEGER PRIMARY KEY,
            fingerprint_sha256 TEXT NOT NULL UNIQUE,
            person TEXT NOT NULL,
            machine TEXT NOT NULL,
            not_after TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'revoked')),
            issued_at TEXT NOT NULL,
            revoked_at TEXT,
            note TEXT
        );
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
    """)
    conn.commit()
    conn.close()
    monkeypatch.setattr(store, "DB_PATH", path)
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    return path


def _rows(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in
                conn.execute("SELECT * FROM signing_certificates "
                             "ORDER BY id").fetchall()]
    finally:
        conn.close()


def _register_machine_certificate(db, fingerprint):
    """A `machine_certificates` row, written straight into the table: the
    cross-table refusal is about what is on file, not how it got there."""
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO machine_certificates "
        "(machine, fingerprint_sha256, not_after, status, created_at, custody) "
        "VALUES (?, ?, '2099-01-01', 'active', '2026-01-01', 'hardware')",
        (MACHINE, fingerprint))
    conn.commit()
    conn.close()


# --- issue ---------------------------------------------------------------


def test_issuing_the_first_certificate(db):
    row = signing_registry.issue(FP_A, PERSON, MACHINE, NOT_AFTER)
    assert row["fingerprint_sha256"] == FP_A
    assert row["person"] == PERSON
    assert row["machine"] == MACHINE
    assert row["status"] == "active"
    assert row["revoked_at"] is None


def test_person_is_lowercased(db):
    row = signing_registry.issue(FP_A, "Operator@Example.COM", MACHINE, NOT_AFTER)
    assert row["person"] == "operator@example.com"


def test_a_person_can_hold_two_active_certificates_at_once(db):
    """The invariant this module deliberately does NOT enforce, unlike
    `machine_registry.issue` — see the module docstring."""
    signing_registry.issue(FP_A, PERSON, "macbook", NOT_AFTER)
    signing_registry.issue(FP_B, PERSON, "deck", NOT_AFTER)
    active = [r for r in _rows(db) if r["status"] == "active"]
    assert len(active) == 2
    assert {r["machine"] for r in active} == {"macbook", "deck"}


def test_a_note_is_recorded_on_the_new_row(db):
    row = signing_registry.issue(FP_A, PERSON, MACHINE, NOT_AFTER,
                                 note="root ceremony")
    assert row["note"] == "root ceremony"


def test_not_after_is_normalised_to_naive_utc(db):
    aware = _dt.datetime(2026, 12, 1, 14, 0, 0, tzinfo=_dt.timezone(
        _dt.timedelta(hours=2)))
    row = signing_registry.issue(FP_A, PERSON, MACHINE, aware)
    assert row["not_after"] == "2026-12-01 12:00:00"


def test_not_after_accepts_an_iso_string_directly(db):
    row = signing_registry.issue(FP_A, PERSON, MACHINE,
                                 "2026-12-01T00:00:00")
    assert row["not_after"] == "2026-12-01 00:00:00"


# --- issue: validation -----------------------------------------------------


@pytest.mark.parametrize("bad_fp", [
    "", "not-hex", "a" * 63, "a" * 65, "A" * 64, None, 12345,
])
def test_an_invalid_fingerprint_is_refused(db, bad_fp):
    with pytest.raises(signing_registry.InvalidCertificateRow,
                        match="fingerprint_sha256"):
        signing_registry.issue(bad_fp, PERSON, MACHINE, NOT_AFTER)
    assert _rows(db) == []


@pytest.mark.parametrize("bad_person", ["", "   ", None, 12345])
def test_an_invalid_person_is_refused(db, bad_person):
    with pytest.raises(signing_registry.InvalidCertificateRow,
                        match="person"):
        signing_registry.issue(FP_A, bad_person, MACHINE, NOT_AFTER)
    assert _rows(db) == []


def test_an_oversized_person_is_refused(db):
    long_person = "j" * (signing_registry.MAX_PERSON + 1) + "@example.com"
    with pytest.raises(signing_registry.InvalidCertificateRow,
                        match="person"):
        signing_registry.issue(FP_A, long_person, MACHINE, NOT_AFTER)
    assert _rows(db) == []


@pytest.mark.parametrize("bad_machine", ["", "   ", None, 12345])
def test_an_invalid_machine_is_refused(db, bad_machine):
    with pytest.raises(signing_registry.InvalidCertificateRow,
                        match="machine"):
        signing_registry.issue(FP_A, PERSON, bad_machine, NOT_AFTER)
    assert _rows(db) == []


def test_an_oversized_machine_name_is_refused(db):
    long_name = "m" * (store.MAX_IDENT + 1)
    with pytest.raises(signing_registry.InvalidCertificateRow,
                        match="machine"):
        signing_registry.issue(FP_A, PERSON, long_name, NOT_AFTER)
    assert _rows(db) == []


def test_an_oversized_note_is_refused(db):
    with pytest.raises(signing_registry.InvalidCertificateRow, match="note"):
        signing_registry.issue(FP_A, PERSON, MACHINE, NOT_AFTER,
                               note="x" * (signing_registry.MAX_NOTE + 1))
    assert _rows(db) == []


def test_a_non_string_note_is_refused(db):
    with pytest.raises(signing_registry.InvalidCertificateRow, match="note"):
        signing_registry.issue(FP_A, PERSON, MACHINE, NOT_AFTER,
                               note={"who": "operator"})
    assert _rows(db) == []


def test_a_duplicate_fingerprint_is_refused_cleanly(db):
    signing_registry.issue(FP_A, PERSON, MACHINE, NOT_AFTER)
    with pytest.raises(signing_registry.InvalidCertificateRow,
                        match="already registered"):
        signing_registry.issue(FP_A, PERSON, "deck", NOT_AFTER)


def test_a_fingerprint_registered_as_a_machine_certificate_is_refused(db):
    """A machine's mTLS certificate is not a signing certificate, even though
    both tables key on the same fingerprint: the refusal names the other table,
    and nothing is inserted here."""
    _register_machine_certificate(db, FP_A)
    with pytest.raises(signing_registry.InvalidCertificateRow,
                        match="machine certificate"):
        signing_registry.issue(FP_A, PERSON, MACHINE, NOT_AFTER)
    assert _rows(db) == []


def test_issue_takes_the_write_lock_before_the_cross_table_read(db,
                                                                monkeypatch):
    """The cross-table SELECT is a check-then-act, so `BEGIN IMMEDIATE` must
    come before it. Removing the lock fails this check; a race is not needed
    to show it, which is why this pins the statement order instead (the same
    reasoning as `test_machine_registry`'s lock test for `revoke`)."""
    seen = []
    real_connect = store.connect_rw

    class Recording:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *a, **kw):
            seen.append(" ".join(sql.split())[:40])
            return self._conn.execute(sql, *a, **kw)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    monkeypatch.setattr(store, "connect_rw",
                        lambda: Recording(real_connect()))
    signing_registry.issue(FP_A, PERSON, MACHINE, NOT_AFTER)

    assert seen[0].startswith("BEGIN IMMEDIATE"), (
        f"the write lock must be taken before the cross-table read; first "
        f"statement was {seen[0]!r}")
    assert seen[1].startswith("SELECT 1 FROM machine_certificates"), seen


def test_invalid_input_does_not_disturb_an_existing_row(db):
    signing_registry.issue(FP_A, PERSON, MACHINE, NOT_AFTER)
    with pytest.raises(signing_registry.InvalidCertificateRow):
        signing_registry.issue("not-hex", PERSON, MACHINE, NOT_AFTER)
    row = _rows(db)[0]
    assert row["status"] == "active"


# --- issue: the shadow gate -------------------------------------------------


def test_issue_refuses_while_writes_are_disabled(db, monkeypatch):
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    with pytest.raises(store.WritesDisabled):
        signing_registry.issue(FP_A, PERSON, MACHINE, NOT_AFTER)


def test_revoke_refuses_while_writes_are_disabled(db, monkeypatch):
    signing_registry.issue(FP_A, PERSON, MACHINE, NOT_AFTER)
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    with pytest.raises(store.WritesDisabled):
        signing_registry.revoke(FP_A)


# --- revoke ------------------------------------------------------------------


def test_revoking_by_fingerprint(db):
    signing_registry.issue(FP_A, PERSON, MACHINE, NOT_AFTER)
    row = signing_registry.revoke(FP_A, note="lost the key")
    assert row["status"] == "revoked"
    assert row["revoked_at"] is not None
    assert row["note"] == "lost the key"


def test_revoking_an_unknown_fingerprint_returns_none(db):
    assert signing_registry.revoke(FP_A) is None


def test_revoking_an_already_revoked_fingerprint_returns_none(db):
    signing_registry.issue(FP_A, PERSON, MACHINE, NOT_AFTER)
    signing_registry.revoke(FP_A)
    assert signing_registry.revoke(FP_A) is None


def test_revoke_with_no_note_leaves_the_existing_note_untouched(db):
    signing_registry.issue(FP_A, PERSON, MACHINE, NOT_AFTER,
                           note="root ceremony")
    row = signing_registry.revoke(FP_A)
    assert row["note"] == "root ceremony"


def test_revoking_one_certificate_does_not_touch_the_persons_other_one(db):
    """The property distinguishing this registry from `machine_registry`'s:
    revoking one of a person's certificates must leave every other active
    certificate of theirs untouched."""
    signing_registry.issue(FP_A, PERSON, "macbook", NOT_AFTER)
    signing_registry.issue(FP_B, PERSON, "deck", NOT_AFTER)
    signing_registry.revoke(FP_A)
    rows = {r["fingerprint_sha256"]: r for r in _rows(db)}
    assert rows[FP_A]["status"] == "revoked"
    assert rows[FP_B]["status"] == "active"


def test_revoke_validates_the_fingerprint_shape(db):
    with pytest.raises(signing_registry.InvalidCertificateRow,
                        match="fingerprint_sha256"):
        signing_registry.revoke("not-hex")


def test_an_oversized_note_is_refused_on_revoke(db):
    signing_registry.issue(FP_A, PERSON, MACHINE, NOT_AFTER)
    with pytest.raises(signing_registry.InvalidCertificateRow, match="note"):
        signing_registry.revoke(
            FP_A, note="x" * (signing_registry.MAX_NOTE + 1))
    assert [r["status"] for r in _rows(db)] == ["active"], (
        "a refused revoke must leave the active certificate alone")


# --- status_for: the registry read the route serves -------------------------


def test_status_for_an_active_certificate(db):
    signing_registry.issue(FP_A, PERSON, MACHINE, NOT_AFTER)
    result = signing_registry.status_for(
        FP_A, now=_dt.datetime(2026, 6, 1))
    assert result == {"fingerprint_sha256": FP_A, "status": "active",
                      "active": True}


def test_status_for_a_revoked_certificate(db):
    signing_registry.issue(FP_A, PERSON, MACHINE, NOT_AFTER)
    signing_registry.revoke(FP_A)
    result = signing_registry.status_for(
        FP_A, now=_dt.datetime(2026, 6, 1))
    assert result["status"] == "revoked"
    assert result["active"] is False


def test_status_for_an_expired_certificate(db):
    signing_registry.issue(FP_A, PERSON, MACHINE,
                           _dt.datetime(2026, 1, 1))
    result = signing_registry.status_for(
        FP_A, now=_dt.datetime(2026, 6, 1))
    assert result["status"] == "expired"
    assert result["active"] is False


def test_status_for_an_unknown_fingerprint(db):
    result = signing_registry.status_for(FP_A)
    assert result == {"fingerprint_sha256": FP_A, "status": "unknown",
                      "active": False}


def test_status_for_a_machine_certificate_fingerprint_is_unknown(db):
    """An active machine certificate must not answer `active` here: the
    signing registry reads only `signing_certificates`, so a machine's mTLS
    fingerprint has no signing row and reads as unknown."""
    _register_machine_certificate(db, FP_A)
    result = signing_registry.status_for(FP_A)
    assert result["status"] == "unknown"
    assert result["active"] is False


def test_revoked_takes_precedence_over_expired(db):
    """A row the schema marks revoked answers `revoked` even past its
    `not_after` — revocation is the more specific fact."""
    signing_registry.issue(FP_A, PERSON, MACHINE, _dt.datetime(2026, 1, 1))
    signing_registry.revoke(FP_A)
    result = signing_registry.status_for(FP_A, now=_dt.datetime(2026, 6, 1))
    assert result["status"] == "revoked"


def test_status_for_reads_fresh_each_call(db):
    """A revocation must take effect on the very next check — no caching
    inside `status_for`."""
    signing_registry.issue(FP_A, PERSON, MACHINE, NOT_AFTER)
    assert signing_registry.status_for(FP_A)["status"] == "active"
    signing_registry.revoke(FP_A)
    assert signing_registry.status_for(FP_A)["status"] == "revoked"


@pytest.mark.parametrize("bad_fp", [
    "", "not-hex", "a" * 63, "a" * 65, "A" * 64, None, 12345,
])
def test_status_for_an_invalid_fingerprint_is_refused(db, bad_fp):
    with pytest.raises(signing_registry.InvalidCertificateRow,
                        match="fingerprint_sha256"):
        signing_registry.status_for(bad_fp)
