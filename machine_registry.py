"""The write path for `machine_certificates` (platform/decisions/0066).

`enroll_machine.py` (the local admin command) and `POST /machines/{machine}/
certificates` / `POST /machines/{machine}/revoke` (the approved-client-gated
routes) all call into this module — the one place a `machine_certificates`
row is ever created or changed, the same shape `credential_binding.bind` is
for `credential_bindings`.

**Takes the fingerprint and expiry the signing tool already printed, not a
certificate file.** Issuance step 5 in platform/decisions/0066 is literally
"The operator adds the machine name and the new certificate's fingerprint to the
Library's registry" — the registry write is a record of what the signing
step already decided, not a re-derivation from a certificate this module has
no reason to hold. `machine_identity.sha256_der` is the function that
produces the fingerprint from the actual DER, on both the signing side and
the read side; this module only ever stores the string.

**The single-live-certificate invariant is enforced here, structurally, not
left to whoever calls this.** ADR-0066: updating a row is the act that
revokes the previous certificate, and the design turns on the two
certificates for one machine never coexisting as both active — "I lost my
key" is exactly what an attacker after a second certificate for an existing
name would say. `issue()` moves any existing active row for the same machine
to revoked and inserts the new one inside a single write transaction, so no
reader ever observes two active rows for one machine, and a crash between
the two statements leaves neither committed rather than leaving the old row
revoked with no replacement.
"""
from __future__ import annotations

import datetime as _dt
import re
import sqlite3

import store

# How a machine's private key is held, as the operator attests at issuance:
#
# - `hardware`: a non-exportable Secure Enclave or TPM key.
# - `file`: a software key in a file the operator account can read, so any
#   agent running as that account can read it too.
# - `root-file`: a software key in a file only root can read
#   (platform/decisions/0092). A launchd daemon cannot use the Secure Enclave
#   on macOS, so the Individuator's daemon holds one of these, and no agent
#   runs as root. It gives up export resistance against someone who already
#   has root on the machine.
#
# The registry records the attestation and serves it from machine verify;
# which custodies a client accepts (the Bosun's session registration, for
# one) is that client's decision, never this module's.
CUSTODY_VALUES = frozenset({"hardware", "file", "root-file"})
STATUS_ACTIVE = "active"
STATUS_REVOKED = "revoked"

# 64 lowercase hex characters: a SHA-256 digest as `hashlib.hexdigest()` and
# `machine_identity.sha256_der` both produce it, and as the schema's own
# `fingerprint_sha256` column is meant to hold it. Checked here rather than
# only at the two callers, matching `store.insert_signal`'s reasoning for
# re-checking `privacy_class` inside the store as well as at the route: the
# next write path that reaches this module must not be able to skip the
# check by not knowing it exists.
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")


class InvalidCertificateRow(ValueError):
    """A caller-supplied field is malformed, or collides with a constraint
    the schema enforces — distinct from `store.WritesDisabled` (the shadow
    gate), so both write paths can give a caller a clean 400 / CLI usage
    error. `issue()` raises this for a duplicate `fingerprint_sha256` too,
    translating the schema's `UNIQUE` constraint rather than letting a bare
    `sqlite3.IntegrityError` reach a caller as an unexplained 500."""


# **A `note` IS PROVENANCE, NOT A DOCUMENT.** Matched to `store.MAX_ACTOR`
# rather than to `MAX_COURSE_NOTE`, because what belongs here is who asked
# and when — `enroll_machine.py`'s own example is "renewal, 2026-09-04".
MAX_NOTE = store.MAX_ACTOR


def _validate_fingerprint(fingerprint_sha256) -> None:
    if not isinstance(fingerprint_sha256, str) or not _FINGERPRINT_RE.match(
            fingerprint_sha256):
        raise InvalidCertificateRow(
            "fingerprint_sha256 must be 64 lowercase hex characters (a "
            "SHA-256 digest over the certificate's DER encoding)")


def _validate_revoke(machine, note) -> None:
    """`revoke()`'s half of the bounds. It takes neither a fingerprint nor
    a custody value, so it cannot reuse `_validate` wholesale — but the
    two fields it does take are bounded on the same reasoning."""
    if not isinstance(machine, str) or not machine.strip() \
            or len(machine) > store.MAX_IDENT:
        raise InvalidCertificateRow(
            f"machine must be a non-empty string of at most "
            f"{store.MAX_IDENT} characters")
    if note is not None and (
            not isinstance(note, str) or len(note) > MAX_NOTE):
        raise InvalidCertificateRow(
            f"note must be a string of at most {MAX_NOTE} characters")


def _validate(fingerprint_sha256, custody, machine=None, note=None) -> None:
    """**Every bound lives HERE, not only on the HTTP route** — the module
    docstring's rule is that the next write path must not be able to skip a
    check by not knowing it exists, and `enroll_machine.py` is already a
    second caller that reaches these functions without passing through a
    route (reviewer and security-reviewer, convivy/library#91).

    `note` was the gap. It was type-checked and stored verbatim, so a
    caller holding `machine.write` could write up to the request body cap
    into one column, repeatedly. The body cap does not cover this:
    `RequestBodySizeLimit`'s own docstring says it "does not replace the
    per-field caps and must not be read as doing so", and the sibling
    `_validate_course_note` caps for exactly this reason. It matters more
    than the column: `store.connect_rw()` serialises every write in the
    Library through one SQLite writer on a 5-second `busy_timeout`, so one
    oversized write stalls every other writer, which this codebase has
    already paid for once.
    """
    if machine is not None and (
            not isinstance(machine, str) or not machine.strip()
            or len(machine) > store.MAX_IDENT):
        raise InvalidCertificateRow(
            f"machine must be a non-empty string of at most "
            f"{store.MAX_IDENT} characters")
    if note is not None and (
            not isinstance(note, str) or len(note) > MAX_NOTE):
        raise InvalidCertificateRow(
            f"note must be a string of at most {MAX_NOTE} characters")
    _validate_fingerprint(fingerprint_sha256)
    # **TYPE-CHECKED BEFORE THE MEMBERSHIP TEST.** `in` against a frozenset
    # hashes the probe, so an unhashable `custody` — a list or dict, both
    # plausible shapes for a caller-supplied JSON field — raises `TypeError`
    # here rather than the `InvalidCertificateRow` this function promises.
    # The route's caller would see a 500 in place of a 400 for exactly the
    # malformed input this function exists to catch cleanly; the same class
    # of bug PR #49 fixed in `identity.resolve` for `kind`.
    if not isinstance(custody, str) or custody not in CUSTODY_VALUES:
        raise InvalidCertificateRow(
            f"custody must be one of {sorted(CUSTODY_VALUES)}, not "
            f"{custody!r}")


def issue(machine: str, fingerprint_sha256: str, not_after: _dt.datetime,
          custody: str, *, note: str | None = None, now=None) -> dict:
    """Register a newly-signed certificate for `machine`, revoking whatever
    certificate was previously active for it. Returns the stored row.

    **`custody` is a required argument on every call, never inferred or
    defaulted.** ADR-0066's Handoff: "custody is reasserted at issuance and
    at renewal, never copied forward from the previous row" — the value is
    typed by a human (or, for the file-based tooling this PR ships, chosen
    deliberately by whoever runs it) on the same attended path that signs
    the certificate, and this function has no way to know it that isn't the
    caller telling it.

    Raises `InvalidCertificateRow` for a malformed fingerprint, an
    unrecognised custody value, or a `fingerprint_sha256` already registered
    to some row here or in `signing_certificates` (a plausible network retry
    of an issuance that already landed, or a caller bug — either way not a
    500), and
    `store.WritesDisabled` while the Library is in its read-only shadow —
    the registry write path is not a way around that gate.
    """
    _validate(fingerprint_sha256, custody, machine, note)
    now = now or _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    now_iso = now.isoformat(" ")
    # **NORMALISED TO NAIVE UTC, THE SAME WAY `store.parse_timestamp` IS**,
    # rather than stored with whatever offset the caller happened to pass.
    # Both write paths accept an ISO-8601 string from outside this process
    # (the CLI's argv, the route's JSON body) and either could carry a
    # timezone; storing it verbatim would put a non-naive value into a
    # column every other timestamp in this schema treats as naive UTC,
    # which is exactly the drift `store.parse_timestamp`'s own docstring
    # exists to prevent one column over.
    if isinstance(not_after, str):
        not_after = _dt.datetime.fromisoformat(not_after)
    if not_after.tzinfo is not None:
        not_after = not_after.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    not_after_iso = not_after.isoformat(" ")
    conn = store.connect_rw()
    try:
        # **`BEGIN IMMEDIATE` BEFORE THE CROSS-TABLE SELECT**, the same
        # reasoning as `signing_registry.issue`'s: without the write lock, a
        # concurrent signing-certificate issue of this fingerprint could commit
        # between the read and the insert, and one certificate would sit in
        # both tables. The check runs before the UPDATE, so a refusal here also
        # leaves the machine's currently active row untouched.
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        if conn.execute(
                "SELECT 1 FROM signing_certificates WHERE fingerprint_sha256 = ?",
                (fingerprint_sha256,)).fetchone() is not None:
            conn.rollback()
            raise InvalidCertificateRow(
                f"fingerprint_sha256 {fingerprint_sha256[:12]}… is already "
                f"registered as a signing certificate")
        # **The UPDATE and the INSERT commit together, inside the one
        # transaction opened above.** This is what makes "revoke the old row"
        # and "insert the new one" a single atomic act rather than two
        # statements a crash could split — the invariant this function
        # exists to hold, not merely to attempt.
        conn.execute(
            "UPDATE machine_certificates SET status = ?, revoked_at = ? "
            "WHERE machine = ? AND status = ?",
            (STATUS_REVOKED, now_iso, machine, STATUS_ACTIVE))
        try:
            conn.execute(
                "INSERT INTO machine_certificates "
                "(machine, fingerprint_sha256, not_after, status, note, "
                " created_at, custody) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (machine, fingerprint_sha256, not_after_iso, STATUS_ACTIVE,
                 note, now_iso, custody))
        except sqlite3.IntegrityError:
            # **ROLLS BACK THE UPDATE TOO.** `fingerprint_sha256` is UNIQUE,
            # so re-submitting one already on file — a network retry of an
            # issuance that already landed, or a caller bug — must not
            # leave the machine's real, previously-active certificate
            # revoked with no replacement inserted. The whole transaction
            # is one unit or it is nothing, matching the atomicity this
            # function's docstring already promises for the ordinary path.
            conn.rollback()
            raise InvalidCertificateRow(
                f"fingerprint_sha256 {fingerprint_sha256[:12]}… is "
                f"already registered") from None
        conn.commit()
        stored = conn.execute(
            "SELECT * FROM machine_certificates WHERE fingerprint_sha256 = ?",
            (fingerprint_sha256,)).fetchone()
        return dict(stored)
    finally:
        conn.close()


def revoke(machine: str, *, note: str | None = None, now=None) -> dict | None:
    """Revoke the certificate currently active for `machine`, or return
    `None` if it has none. The decommissioning path — unlike `issue()`, no
    replacement row is inserted.

    **`note` overwrites only when given.** A decommission note should not
    silently blank whatever provenance the issuing note already carried, so
    an absent `note` here leaves the stored one untouched.
    """
    now = now or _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    now_iso = now.isoformat(" ")
    _validate_revoke(machine, note)
    conn = store.connect_rw()
    try:
        # **`BEGIN IMMEDIATE` BEFORE THE SELECT, because this is a
        # check-then-act and the SELECT alone takes no write lock**
        # (reviewer, convivy/library#91). Racing a concurrent `issue()`
        # re-enrollment for the same machine, the read could return the
        # row that issue is about to supersede, and the UPDATE would then
        # revoke that already-superseded row — reporting success while the
        # actually-live certificate stayed active. That is the single-live
        # certificate invariant this module exists to hold, failing
        # silently and in the caller's favour.
        #
        # `issue()` needs no such guard: its first statement is the UPDATE,
        # so it takes the write lock immediately. The shape and the
        # reasoning are `store.py`'s, on its ledger-entry insert — "the
        # check-then-act that needs the lock".
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        active = conn.execute(
            "SELECT id FROM machine_certificates "
            "WHERE machine = ? AND status = ?", (machine, STATUS_ACTIVE)
        ).fetchone()
        if active is None:
            return None
        conn.execute(
            "UPDATE machine_certificates SET status = ?, revoked_at = ?, "
            "note = COALESCE(?, note) WHERE id = ?",
            (STATUS_REVOKED, now_iso, note, active["id"]))
        conn.commit()
        stored = conn.execute(
            "SELECT * FROM machine_certificates WHERE id = ?",
            (active["id"],)).fetchone()
        return dict(stored)
    finally:
        conn.close()


def status_for(fingerprint_sha256: str, *, now=None) -> dict:
    """The registry's answer for `GET /machine-certificates/
    {fingerprint_sha256}` (platform/decisions/0066), modelled on
    `signing_registry.status_for`: `{machine, fingerprint_sha256, status,
    active, custody, not_after}`, where `status` is one of `active`,
    `revoked`, `expired` or `unknown`, and `active` is `True` only when
    `status == "active"`.

    Raises `InvalidCertificateRow` for a fingerprint that is not 64
    lowercase hex — the route's 400 and this module's own validation are the
    same check, so a second caller cannot reach this table with a malformed
    fingerprint by not knowing the rule.

    **Read fresh on every call, never cached** — a revocation must take
    effect on the very next check, whatever a session was authenticated with
    before it.

    **`expired` is computed here, not stored** — a row that reads `active`
    in the schema but whose `not_after` has passed answers `expired`.
    `revoked` takes precedence over an expired `not_after`: a row the schema
    marks revoked answers `revoked` regardless of its `not_after`, because
    revocation is the more specific fact about why the certificate no longer
    verifies.

    **An unknown fingerprint returns only `{fingerprint_sha256, status,
    active}`**, with no `machine`, `custody` or `not_after` — there is no row
    to read any of those from, matching `signing_registry.status_for`'s own
    shape for the same case.
    """
    _validate_fingerprint(fingerprint_sha256)
    now = now or _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    conn = store.connect()
    try:
        row = conn.execute(
            "SELECT machine, status, not_after, custody FROM "
            "machine_certificates WHERE fingerprint_sha256 = ?",
            (fingerprint_sha256,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return {"fingerprint_sha256": fingerprint_sha256, "status": "unknown",
                "active": False}
    # **Read FOR `active`, never against `revoked`** — the direction
    # `machine_identity.resolve_machine` and decision 0066 use for this
    # table: a status value nobody anticipated must not read as active.
    if row["status"] != STATUS_ACTIVE:
        status = "revoked"
    elif _dt.datetime.fromisoformat(row["not_after"]) <= now:
        status = "expired"
    else:
        status = STATUS_ACTIVE
    return {"machine": row["machine"], "fingerprint_sha256": fingerprint_sha256,
            "status": status, "active": status == STATUS_ACTIVE,
            "custody": row["custody"], "not_after": row["not_after"]}
