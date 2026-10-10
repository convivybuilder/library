"""`machine_registry.py`: the write path for `machine_certificates`
(platform/decisions/0066), and specifically the invariant the ADR says has
to be structural — a machine never has two active certificates at once.

**Do not mock the DB layer.** A real `machine_certificates` table, built
from the migration's own DDL, the same shape `test_machine_identity_gate.py`
already uses for the read side.
"""
from __future__ import annotations

import datetime as _dt
import sqlite3

import pytest

import machine_registry
import store

MACHINE = "macbook"
FP_A = "a" * 64
FP_B = "b" * 64
NOT_AFTER = _dt.datetime(2026, 12, 1)


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "control.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
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
                conn.execute("SELECT * FROM machine_certificates "
                             "ORDER BY id").fetchall()]
    finally:
        conn.close()


def _register_signing_certificate(db, fingerprint):
    """A `signing_certificates` row, written straight into the table: the
    cross-table refusal is about what is on file, not how it got there."""
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO signing_certificates "
        "(fingerprint_sha256, person, machine, not_after, status, issued_at) "
        "VALUES (?, 'operator@example.com', 'macbook', '2099-01-01', 'active', "
        "'2026-01-01')", (fingerprint,))
    conn.commit()
    conn.close()


# --- issue: the single-live-certificate invariant ----------------------------


def test_issuing_the_first_certificate_for_a_machine(db):
    row = machine_registry.issue(MACHINE, FP_A, NOT_AFTER, "hardware")
    assert row["machine"] == MACHINE
    assert row["fingerprint_sha256"] == FP_A
    assert row["status"] == "active"
    assert row["custody"] == "hardware"
    assert row["revoked_at"] is None


def test_re_issuing_revokes_the_previous_active_row(db):
    """"A machine that lost its key is indistinguishable from a new machine
    and is treated as one. ... Updating the registry row replaces the old
    fingerprint, which revokes the previous certificate in the same act"
    (platform/decisions/0066)."""
    machine_registry.issue(MACHINE, FP_A, NOT_AFTER, "hardware")
    machine_registry.issue(MACHINE, FP_B, NOT_AFTER, "hardware")

    rows = {r["fingerprint_sha256"]: r for r in _rows(db)}
    assert rows[FP_A]["status"] == "revoked"
    assert rows[FP_A]["revoked_at"] is not None
    assert rows[FP_B]["status"] == "active"


def test_two_certificates_for_one_machine_never_both_read_active(db):
    """The invariant stated as a property over every row, not just the two
    the happy path produces: at most one row per machine ever reads
    'active', at any point after any sequence of issues."""
    machine_registry.issue(MACHINE, FP_A, NOT_AFTER, "hardware")
    machine_registry.issue(MACHINE, FP_B, NOT_AFTER, "file")
    machine_registry.issue(MACHINE, "c" * 64, NOT_AFTER, "hardware")
    active = [r for r in _rows(db) if r["status"] == "active"]
    assert len(active) == 1


def test_issuing_for_a_different_machine_does_not_touch_the_first(db):
    machine_registry.issue(MACHINE, FP_A, NOT_AFTER, "hardware")
    machine_registry.issue("deck", FP_B, NOT_AFTER, "hardware")
    rows = {r["machine"]: r for r in _rows(db)}
    assert rows[MACHINE]["status"] == "active"
    assert rows["deck"]["status"] == "active"


def test_custody_is_reasserted_not_carried_forward(db):
    """ADR-0066's Handoff: "custody is reasserted at every issuance and
    renewal, never copied forward from the previous row." A machine
    reissued with a file-based key must not inherit a stale 'hardware'
    reading from the row it replaces."""
    machine_registry.issue(MACHINE, FP_A, NOT_AFTER, "hardware")
    machine_registry.issue(MACHINE, FP_B, NOT_AFTER, "file")
    active = [r for r in _rows(db) if r["status"] == "active"][0]
    assert active["custody"] == "file"


def test_not_after_is_normalised_to_naive_utc(db):
    """Matching `store.parse_timestamp`'s own convention — every other
    timestamp in this schema is naive UTC, and a caller-supplied offset
    stored verbatim would put the one column that isn't next to all the
    ones that are."""
    aware = _dt.datetime(2026, 12, 1, 14, 0, 0, tzinfo=_dt.timezone(
        _dt.timedelta(hours=2)))
    row = machine_registry.issue(MACHINE, FP_A, aware, "hardware")
    assert row["not_after"] == "2026-12-01 12:00:00"


def test_not_after_accepts_an_iso_string_directly(db):
    row = machine_registry.issue(MACHINE, FP_A, "2026-12-01T00:00:00",
                                 "hardware")
    assert row["not_after"] == "2026-12-01 00:00:00"


def test_a_note_is_recorded_on_the_new_row(db):
    row = machine_registry.issue(MACHINE, FP_A, NOT_AFTER, "hardware",
                                 note="first enrollment")
    assert row["note"] == "first enrollment"


# --- issue: validation --------------------------------------------------------


@pytest.mark.parametrize("bad_fp", [
    "", "not-hex", "a" * 63, "a" * 65, "A" * 64, None, 12345,
])
def test_an_invalid_fingerprint_is_refused(db, bad_fp):
    with pytest.raises(machine_registry.InvalidCertificateRow,
                        match="fingerprint_sha256"):
        machine_registry.issue(MACHINE, bad_fp, NOT_AFTER, "hardware")
    assert _rows(db) == []


@pytest.mark.parametrize("bad_custody", [
    "usb-stick", "", None, "HARDWARE", ["hardware"], {"custody": "hardware"},
])
def test_an_invalid_custody_is_refused(db, bad_custody):
    """**Type-checked before the membership test** (matching PR #49's fix to
    `identity.resolve`'s `kind` check): `custody not in CUSTODY_VALUES`
    hashes the probe, so an unhashable value like a list must not reach
    that line unguarded — it would raise `TypeError` in place of the
    `InvalidCertificateRow` this function promises callers."""
    with pytest.raises(machine_registry.InvalidCertificateRow,
                        match="custody"):
        machine_registry.issue(MACHINE, FP_A, NOT_AFTER, bad_custody)
    assert _rows(db) == []


def test_a_duplicate_fingerprint_is_refused_cleanly(db):
    """`fingerprint_sha256` is UNIQUE in the schema; re-submitting one
    already on file — a plausible network retry — must answer
    `InvalidCertificateRow`, not a bare `sqlite3.IntegrityError` reaching
    the caller as an unexplained 500."""
    machine_registry.issue(MACHINE, FP_A, NOT_AFTER, "hardware")
    with pytest.raises(machine_registry.InvalidCertificateRow,
                        match="already registered"):
        machine_registry.issue("deck", FP_A, NOT_AFTER, "hardware")


def test_a_duplicate_fingerprint_does_not_revoke_the_existing_active_row(db):
    """The failed re-issuance must not have revoked the machine's real
    certificate on its way to being refused — the UPDATE and the failed
    INSERT roll back together."""
    machine_registry.issue(MACHINE, FP_A, NOT_AFTER, "hardware")
    with pytest.raises(machine_registry.InvalidCertificateRow):
        machine_registry.issue(MACHINE, FP_A, NOT_AFTER, "hardware")
    row = _rows(db)[0]
    assert row["status"] == "active"


def test_a_fingerprint_registered_as_a_signing_certificate_is_refused(db):
    """The other direction: a signing certificate cannot be enrolled as a
    machine's mTLS certificate. The refusal names the other table."""
    _register_signing_certificate(db, FP_A)
    with pytest.raises(machine_registry.InvalidCertificateRow,
                        match="signing certificate"):
        machine_registry.issue(MACHINE, FP_A, NOT_AFTER, "hardware")
    assert _rows(db) == []


def test_a_signing_refusal_does_not_revoke_the_machines_active_row(db):
    """The cross-table check runs before the revoke UPDATE, so a refused
    re-issuance leaves the machine's real active certificate active."""
    machine_registry.issue(MACHINE, FP_A, NOT_AFTER, "hardware")
    _register_signing_certificate(db, FP_B)
    with pytest.raises(machine_registry.InvalidCertificateRow):
        machine_registry.issue(MACHINE, FP_B, NOT_AFTER, "hardware")
    rows = _rows(db)
    assert [r["status"] for r in rows] == ["active"]
    assert rows[0]["fingerprint_sha256"] == FP_A


def test_issue_takes_the_write_lock_before_the_cross_table_read(db,
                                                                monkeypatch):
    """`issue()` now reads `signing_certificates` before its UPDATE, so the
    write lock has to come first, the same as `revoke()`'s check-then-act.
    Removing `BEGIN IMMEDIATE` fails this check."""
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
    machine_registry.issue(MACHINE, FP_A, NOT_AFTER, "hardware")

    assert seen[0].startswith("BEGIN IMMEDIATE"), (
        f"the write lock must be taken before the cross-table read; first "
        f"statement was {seen[0]!r}")
    assert seen[1].startswith("SELECT 1 FROM signing_certificates"), seen


def test_invalid_input_does_not_revoke_the_existing_active_row(db):
    """Validation runs before any write — a malformed second issuance must
    not revoke a machine's real, working certificate on its way to being
    refused."""
    machine_registry.issue(MACHINE, FP_A, NOT_AFTER, "hardware")
    with pytest.raises(machine_registry.InvalidCertificateRow):
        machine_registry.issue(MACHINE, "not-hex", NOT_AFTER, "hardware")
    row = _rows(db)[0]
    assert row["status"] == "active"


# --- issue: the shadow gate ---------------------------------------------------


def test_issue_refuses_while_writes_are_disabled(db, monkeypatch):
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    with pytest.raises(store.WritesDisabled):
        machine_registry.issue(MACHINE, FP_A, NOT_AFTER, "hardware")


def test_revoke_refuses_while_writes_are_disabled(db, monkeypatch):
    machine_registry.issue(MACHINE, FP_A, NOT_AFTER, "hardware")
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    with pytest.raises(store.WritesDisabled):
        machine_registry.revoke(MACHINE)


# --- revoke --------------------------------------------------------------------


def test_revoking_the_active_certificate(db):
    machine_registry.issue(MACHINE, FP_A, NOT_AFTER, "hardware")
    row = machine_registry.revoke(MACHINE, note="decommissioned")
    assert row["status"] == "revoked"
    assert row["revoked_at"] is not None
    assert row["note"] == "decommissioned"


def test_revoking_a_machine_with_no_active_certificate_returns_none(db):
    assert machine_registry.revoke(MACHINE) is None


def test_revoking_does_not_insert_a_replacement_row(db):
    machine_registry.issue(MACHINE, FP_A, NOT_AFTER, "hardware")
    machine_registry.revoke(MACHINE)
    assert len(_rows(db)) == 1


def test_revoke_with_no_note_leaves_the_existing_note_untouched(db):
    machine_registry.issue(MACHINE, FP_A, NOT_AFTER, "hardware",
                           note="first enrollment")
    row = machine_registry.revoke(MACHINE)
    assert row["note"] == "first enrollment"


def test_revoke_only_touches_the_named_machine(db):
    machine_registry.issue(MACHINE, FP_A, NOT_AFTER, "hardware")
    machine_registry.issue("deck", FP_B, NOT_AFTER, "hardware")
    machine_registry.revoke(MACHINE)
    rows = {r["machine"]: r for r in _rows(db)}
    assert rows[MACHINE]["status"] == "revoked"
    assert rows["deck"]["status"] == "active"


def test_a_machine_can_be_re_issued_after_revocation(db):
    """Revocation is not a dead end — a decommissioned machine name can be
    reused by a later, distinct issuance, and the new row is the one that
    reads active."""
    machine_registry.issue(MACHINE, FP_A, NOT_AFTER, "hardware")
    machine_registry.revoke(MACHINE)
    machine_registry.issue(MACHINE, FP_B, NOT_AFTER, "hardware")
    active = [r for r in _rows(db) if r["status"] == "active"]
    assert len(active) == 1
    assert active[0]["fingerprint_sha256"] == FP_B


# --- bounds on the free-text and identifier fields ---------------------------
# Round 1 of convivy/library#91: `note` was type-checked and stored verbatim,
# so a caller holding `machine.write` could write up to the request body cap
# into one column. The body cap does not cover this — its own docstring says it
# "does not replace the per-field caps" — and it matters beyond the column,
# because `store.connect_rw()` serialises every write in the Library through one
# SQLite writer. The bounds live in `machine_registry` rather than on the route
# so `enroll_machine.py`, already a second caller, cannot skip them.

def test_an_oversized_note_is_refused_on_issue(db):
    with pytest.raises(machine_registry.InvalidCertificateRow, match="note"):
        machine_registry.issue(
            MACHINE, FP_A, NOT_AFTER, "file",
            note="x" * (machine_registry.MAX_NOTE + 1))
    assert _rows(db) == []


def test_an_oversized_note_is_refused_on_revoke(db):
    machine_registry.issue(MACHINE, FP_A, NOT_AFTER, "file")
    with pytest.raises(machine_registry.InvalidCertificateRow, match="note"):
        machine_registry.revoke(
            MACHINE, note="x" * (machine_registry.MAX_NOTE + 1))
    assert [r["status"] for r in _rows(db)] == ["active"], (
        "a refused revoke must leave the active certificate alone")


def test_a_non_string_note_is_refused_rather_than_stored(db):
    with pytest.raises(machine_registry.InvalidCertificateRow, match="note"):
        machine_registry.issue(
            MACHINE, FP_A, NOT_AFTER, "file", note={"who": "operator"})
    assert _rows(db) == []


def test_an_oversized_machine_name_is_refused_by_the_module(db):
    """The HTTP route bounds `machine`; `enroll_machine.py` did not, and the
    module is where a bound has to live for both callers to get it."""
    long_name = "m" * (store.MAX_IDENT + 1)
    with pytest.raises(machine_registry.InvalidCertificateRow, match="machine"):
        machine_registry.issue(long_name, FP_A, NOT_AFTER, "file")
    with pytest.raises(machine_registry.InvalidCertificateRow, match="machine"):
        machine_registry.revoke(long_name)
    assert _rows(db) == []


# --- status_for: the registry read the route serves -------------------------


def test_status_for_an_active_certificate(db):
    machine_registry.issue(MACHINE, FP_A, NOT_AFTER, "hardware")
    result = machine_registry.status_for(FP_A, now=_dt.datetime(2026, 6, 1))
    assert result == {"machine": MACHINE, "fingerprint_sha256": FP_A,
                      "status": "active", "active": True,
                      "custody": "hardware", "not_after": "2026-12-01 00:00:00"}


def test_status_for_a_revoked_certificate(db):
    machine_registry.issue(MACHINE, FP_A, NOT_AFTER, "hardware")
    machine_registry.revoke(MACHINE)
    result = machine_registry.status_for(FP_A, now=_dt.datetime(2026, 6, 1))
    assert result["status"] == "revoked"
    assert result["active"] is False


def test_status_for_an_expired_certificate(db):
    machine_registry.issue(MACHINE, FP_A, _dt.datetime(2026, 1, 1), "hardware")
    result = machine_registry.status_for(FP_A, now=_dt.datetime(2026, 6, 1))
    assert result["status"] == "expired"
    assert result["active"] is False


def test_status_for_an_unknown_fingerprint(db):
    result = machine_registry.status_for(FP_A)
    assert result == {"fingerprint_sha256": FP_A, "status": "unknown",
                      "active": False}


def test_revoked_takes_precedence_over_expired(db):
    """A row the schema marks revoked answers `revoked` even past its
    `not_after` — revocation is the more specific fact."""
    machine_registry.issue(MACHINE, FP_A, _dt.datetime(2026, 1, 1), "hardware")
    machine_registry.revoke(MACHINE)
    result = machine_registry.status_for(FP_A, now=_dt.datetime(2026, 6, 1))
    assert result["status"] == "revoked"


def test_status_for_reads_fresh_each_call(db):
    """A revocation must take effect on the very next check — no caching
    inside `status_for`."""
    machine_registry.issue(MACHINE, FP_A, NOT_AFTER, "hardware")
    assert machine_registry.status_for(FP_A)["status"] == "active"
    machine_registry.revoke(MACHINE)
    assert machine_registry.status_for(FP_A)["status"] == "revoked"


@pytest.mark.parametrize("bad_fp", [
    "", "not-hex", "a" * 63, "a" * 65, "A" * 64, None, 12345,
])
def test_status_for_an_invalid_fingerprint_is_refused(db, bad_fp):
    with pytest.raises(machine_registry.InvalidCertificateRow,
                        match="fingerprint_sha256"):
        machine_registry.status_for(bad_fp)


def test_revoke_takes_the_write_lock_before_it_reads(db, monkeypatch):
    """`revoke()` is a check-then-act: it SELECTs the active row's id, then
    UPDATEs by that id. A bare SELECT takes no write lock, so racing a
    concurrent `issue()` re-enrollment the read can return the row issue is
    about to supersede, and the UPDATE then revokes an already-superseded
    row — reporting success while the actually-live certificate stays
    active. That is the single-live-certificate invariant failing silently
    and in the caller's favour (reviewer, convivy/library#91).

    **This pins the lock acquisition rather than reproducing the race**,
    and the distinction is worth stating: a genuine interleaving test would
    have to win a timing window against SQLite's own locking to fail, so it
    would pass for the wrong reason on a slow run. What makes this a check
    rather than a ritual is that removing the `BEGIN IMMEDIATE` fails it —
    `issue()` needs no such guard, because its first statement is already
    the UPDATE.
    """
    machine_registry.issue(MACHINE, FP_A, NOT_AFTER, "hardware")

    seen = []
    real_connect = store.connect_rw

    class Recording:
        """`sqlite3.Connection.execute` is read-only, so the recorder wraps
        the connection rather than patching a method onto it."""

        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *a, **kw):
            seen.append(" ".join(sql.split())[:40])
            return self._conn.execute(sql, *a, **kw)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    monkeypatch.setattr(store, "connect_rw",
                        lambda: Recording(real_connect()))
    machine_registry.revoke(MACHINE)

    assert seen, "revoke issued no statements"
    assert seen[0].startswith("BEGIN IMMEDIATE"), (
        f"the write lock must be taken before the read; first statement "
        f"was {seen[0]!r}")
    assert any(s.startswith("SELECT") for s in seen[1:]), (
        "the read must still happen, after the lock")
