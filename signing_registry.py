"""The write path for `signing_certificates`, and the read the registry
route answers with (platform/contracts/library-protocol §2.2).

`enroll_signing_cert.py` (the local admin command) and `POST /signing-
certificates` / `POST /signing-certificates/{fingerprint}/revoke` (the
approved-client-gated routes) all call into this module — the one place a
`signing_certificates` row is ever created or changed, the same shape
`machine_registry.py` is for `machine_certificates`.

**`issue()` never revokes anything, unlike `machine_registry.issue`.** A
person may hold several active signing certificates at once, one per Mac,
so there is no single-live-certificate invariant here to enforce — see the
migration's own docstring for why a person is not a machine name. Every call
to `issue()` is a plain insert.

**`revoke()` takes a fingerprint, not a person.** Given several active rows
for one person, "revoke this person" does not name which Mac's key it
means; the fingerprint is the value that names exactly one row, and it is
what the signing ceremony already printed to whoever runs the admin
command.
"""
from __future__ import annotations

import datetime as _dt
import re
import sqlite3

import store

STATUS_ACTIVE = "active"
STATUS_REVOKED = "revoked"

# 64 lowercase hex characters, matching `machine_registry`'s own regex and
# the reasoning beside it: a SHA-256 digest as `hashlib.hexdigest()` and
# `machine_identity.sha256_der` both produce it, and as this schema's
# `fingerprint_sha256` column is meant to hold it. Checked here rather than
# only at the callers, so the next write or read path that reaches this
# module cannot skip the check by not knowing it exists.
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")


class InvalidCertificateRow(ValueError):
    """A caller-supplied field is malformed, or collides with a constraint
    the schema enforces — distinct from `store.WritesDisabled` (the shadow
    gate), so both write paths can give a caller a clean 400 / CLI usage
    error. `issue()` raises this for a duplicate `fingerprint_sha256` too,
    translating the schema's `UNIQUE` constraint rather than letting a bare
    `sqlite3.IntegrityError` reach a caller as an unexplained 500."""


# **`note` IS PROVENANCE, NOT A DOCUMENT**, matching `machine_registry.
# MAX_NOTE`'s own reasoning — what belongs here is who asked and when.
MAX_NOTE = store.MAX_ACTOR

# `person` is an email address; bounded the same as `machine_registry`'s
# `note` and the ledger's own `person` column (`sa.String(200)` in the
# migration) rather than `store.MAX_IDENT`, which is sized for a short
# machine-name slug and would refuse a legitimate long address.
MAX_PERSON = store.MAX_ACTOR


def _validate_fingerprint(fingerprint_sha256) -> None:
    if not isinstance(fingerprint_sha256, str) or not _FINGERPRINT_RE.match(
            fingerprint_sha256):
        raise InvalidCertificateRow(
            "fingerprint_sha256 must be 64 lowercase hex characters (a "
            "SHA-256 digest over the certificate's DER encoding)")


def _validate(fingerprint_sha256, person, machine, note=None) -> None:
    """Every bound lives HERE, not only on the HTTP route — matching
    `machine_registry._validate`'s own reasoning, so the next write path
    (an admin command, say) cannot reach this table without them."""
    _validate_fingerprint(fingerprint_sha256)
    if not isinstance(person, str) or not person.strip() \
            or len(person) > MAX_PERSON:
        raise InvalidCertificateRow(
            f"person must be a non-empty string of at most {MAX_PERSON} "
            f"characters")
    if not isinstance(machine, str) or not machine.strip() \
            or len(machine) > store.MAX_IDENT:
        raise InvalidCertificateRow(
            f"machine must be a non-empty string of at most "
            f"{store.MAX_IDENT} characters")
    if note is not None and (
            not isinstance(note, str) or len(note) > MAX_NOTE):
        raise InvalidCertificateRow(
            f"note must be a string of at most {MAX_NOTE} characters")


def _validate_revoke(note) -> None:
    if note is not None and (
            not isinstance(note, str) or len(note) > MAX_NOTE):
        raise InvalidCertificateRow(
            f"note must be a string of at most {MAX_NOTE} characters")


def _normalise(value: _dt.datetime | str) -> tuple[_dt.datetime, str]:
    """Naive UTC, the same way `machine_registry.issue` normalises
    `not_after` and `store.parse_timestamp` normalises everything else in
    this schema — a caller-supplied offset stored verbatim would put one
    column next to a dozen that are all naive UTC."""
    if isinstance(value, str):
        value = _dt.datetime.fromisoformat(value)
    if value.tzinfo is not None:
        value = value.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    return value, value.isoformat(" ")


def issue(fingerprint_sha256: str, person: str, machine: str,
          not_after: _dt.datetime | str, *, note: str | None = None,
          now=None) -> dict:
    """Register a newly-signed signing certificate. Returns the stored row.

    **A plain insert — see the module docstring for why this never revokes
    another row the way `machine_registry.issue` does.**

    Raises `InvalidCertificateRow` for a malformed fingerprint, an empty or
    oversized `person` or `machine`, or a `fingerprint_sha256` already
    registered to some row in this table or in `machine_certificates`, and
    `store.WritesDisabled` while the Library is in its read-only shadow.
    """
    _validate(fingerprint_sha256, person, machine, note)
    person = person.strip().lower()
    machine = machine.strip()
    now = now or _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    now_iso = now.isoformat(" ")
    _, not_after_iso = _normalise(not_after)
    conn = store.connect_rw()
    try:
        # **`BEGIN IMMEDIATE` BEFORE THE CROSS-TABLE SELECT**, the same shape as
        # `machine_registry.revoke`'s check-then-act. Without the write lock, a
        # concurrent `machine_registry.issue` of this fingerprint could commit
        # between this read and the insert, and the same certificate would then
        # be registered in both tables. The lock makes the two writers serialise.
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        if conn.execute(
                "SELECT 1 FROM machine_certificates WHERE fingerprint_sha256 = ?",
                (fingerprint_sha256,)).fetchone() is not None:
            conn.rollback()
            raise InvalidCertificateRow(
                f"fingerprint_sha256 {fingerprint_sha256[:12]}… is already "
                f"registered as a machine certificate")
        try:
            conn.execute(
                "INSERT INTO signing_certificates "
                "(fingerprint_sha256, person, machine, not_after, status, "
                " issued_at, note) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (fingerprint_sha256, person, machine, not_after_iso,
                 STATUS_ACTIVE, now_iso, note))
        except sqlite3.IntegrityError:
            conn.rollback()
            raise InvalidCertificateRow(
                f"fingerprint_sha256 {fingerprint_sha256[:12]}… is "
                f"already registered") from None
        conn.commit()
        stored = conn.execute(
            "SELECT * FROM signing_certificates WHERE fingerprint_sha256 = ?",
            (fingerprint_sha256,)).fetchone()
        return dict(stored)
    finally:
        conn.close()


def revoke(fingerprint_sha256: str, *, note: str | None = None,
           now=None) -> dict | None:
    """Revoke the signing certificate named by `fingerprint_sha256`, or
    return `None` if no row, or none still active, carries it.

    **`note` overwrites only when given**, matching `machine_registry.
    revoke`'s own reasoning: a decommission note should not silently blank
    whatever provenance the issuing note already carried.
    """
    _validate_fingerprint(fingerprint_sha256)
    _validate_revoke(note)
    now = now or _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    now_iso = now.isoformat(" ")
    conn = store.connect_rw()
    try:
        # **No `BEGIN IMMEDIATE` before this SELECT, unlike `machine_registry.
        # revoke`.** That guard exists there because a concurrent `issue()`
        # supersedes — revokes and replaces — the exact row a racing
        # `revoke()` is about to act on, so the read could return an
        # already-superseded row and silently leave the real active
        # certificate untouched. This module's `issue()` never supersedes
        # anything: a fingerprint identifies one immutable row for its whole
        # life, so the only race here is two `revoke()` calls for the same
        # fingerprint reaching the same, correct row — idempotent, not a
        # lost update.
        row = conn.execute(
            "SELECT id FROM signing_certificates "
            "WHERE fingerprint_sha256 = ? AND status = ?",
            (fingerprint_sha256, STATUS_ACTIVE)).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE signing_certificates SET status = ?, revoked_at = ?, "
            "note = COALESCE(?, note) WHERE id = ?",
            (STATUS_REVOKED, now_iso, note, row["id"]))
        conn.commit()
        stored = conn.execute(
            "SELECT * FROM signing_certificates WHERE id = ?",
            (row["id"],)).fetchone()
        return dict(stored)
    finally:
        conn.close()


def status_for(fingerprint_sha256: str, *, now=None) -> dict:
    """The registry's answer for `GET /signing-certificates/{fingerprint}`
    (contract §2.2): `{fingerprint_sha256, status, active}`, where `status`
    is one of `active`, `revoked`, `expired` or `unknown`, and `active` is
    `True` only when `status == "active"`.

    Raises `InvalidCertificateRow` for a fingerprint that is not 64
    lowercase hex — the route's 400 and this module's own validation are
    the same check, so a second caller cannot reach this table with a
    malformed fingerprint by not knowing the rule.

    **Read fresh on every call, never cached**, the same shape
    `machine_identity._registry_status` uses: a revocation must take effect
    on the very next check, whatever a save was signed with before it.

    **`expired` is computed here, not stored** — a row that reads `active`
    in the schema but whose `not_after` has passed answers `expired`, per
    the contract's own wording. `revoked` takes precedence over an expired
    `not_after`: a row the schema marks revoked answers `revoked` regardless
    of its `not_after`, because revocation is the more specific fact about
    why the certificate no longer verifies.
    """
    _validate_fingerprint(fingerprint_sha256)
    now = now or _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    conn = store.connect()
    try:
        row = conn.execute(
            "SELECT status, not_after FROM signing_certificates "
            "WHERE fingerprint_sha256 = ?", (fingerprint_sha256,)).fetchone()
    finally:
        conn.close()
    if row is None:
        status = "unknown"
    elif row["status"] == STATUS_REVOKED:
        status = STATUS_REVOKED
    elif _dt.datetime.fromisoformat(row["not_after"]) <= now:
        status = "expired"
    else:
        status = STATUS_ACTIVE
    return {"fingerprint_sha256": fingerprint_sha256, "status": status,
            "active": status == STATUS_ACTIVE}
