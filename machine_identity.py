"""Machine identity from a peer certificate, and the registry that revokes it.

platform/decisions/0066. **A signature from the root is necessary and not
sufficient.** The Library's `machine_certificates` table pins the SHA-256 of
each machine's leaf certificate over its DER encoding, so a certificate the
root signed but this table does not carry — or carries with `status =
'revoked'` — is refused whatever the handshake itself accepted.

**Read fresh on every request, never cached**, the same shape
`identity._tokens()` already uses so a bearer-token grant or revocation takes
effect with no restart. Here it is what makes a certificate revocation take
effect on the very next request over an already-open keep-alive connection:
the TLS session and its certificate persist for the connection's lifetime
(`mtls.MTLSH11Protocol` stamps it once, at `connection_made`), and only this
lookup runs again for each request that connection carries.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import sqlite3

import mtls
import refusals
import store

MACHINE_URI_PREFIX = "convivy://machine/"

# id-kp-clientAuth, 1.3.6.1.5.5.7.3.2, DER-encoded per X.690's OID rule (first
# two arcs combine as 40*1+3, every later arc under 128 so single-byte).
CLIENT_AUTH_OID = bytes((0x2B, 0x06, 0x01, 0x05, 0x05, 0x07, 0x03, 0x02))

# id-ce-extKeyUsage, 2.5.29.37, same encoding (40*2+5, then 29, then 37).
_EXT_KEY_USAGE_OID = bytes((0x55, 0x1D, 0x25))
_EXTENSIONS_TAG = 0xA3  # tbsCertificate's [3] EXPLICIT extensions field


def sha256_der(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest()


def _der_tlv(data: bytes, offset: int) -> tuple[int, int, int, int]:
    """`(tag, value_start, value_end, next_offset)` for one definite-length
    DER TLV at `offset`. Low-tag-number only (tag < 31) — every tag this
    module ever reads (SEQUENCE, OID, OCTET STRING, the context-specific
    extensions field) fits in one tag byte, so the multi-byte tag form is
    never needed here.

    **Bounds-checked against `len(data)` (security-reviewer, convivy/
    library#200).** `data[offset]`/`data[offset + 1]` already raise
    `IndexError` on a truncated tag or length octet, but the length-of-length
    and value slices below them are Python slices, which do not raise on a
    claimed length that runs past the end of `data` — they silently return
    whatever bytes remain. A caller that trusted such a `value_end` (a plain
    slice, as `not_after_utc`'s final `der[time_vs:time_ve]` is) would read a
    truncated value rather than raising, so both the length-of-length read
    and the final value extent are checked here, once, for every caller.
    """
    tag = data[offset]
    length_octet = data[offset + 1]
    if length_octet & 0x80:
        n = length_octet & 0x7F
        if offset + 2 + n > len(data):
            raise ValueError(
                f"DER TLV at offset {offset} claims a {n}-byte length "
                f"field beyond the {len(data)}-byte buffer")
        length = int.from_bytes(data[offset + 2:offset + 2 + n], "big")
        value_start = offset + 2 + n
    else:
        length = length_octet
        value_start = offset + 2
    value_end = value_start + length
    if value_end > len(data):
        raise ValueError(
            f"DER TLV at offset {offset} claims a value ending at "
            f"{value_end}, beyond the {len(data)}-byte buffer")
    return tag, value_start, value_end, value_end


def _der_children(data: bytes, start: int, end: int
                   ) -> list[tuple[int, int, int]]:
    """Every top-level `(tag, value_start, value_end)` TLV in `data[start:end]`
    — the immediate children of a constructed value already unwrapped by the
    caller."""
    children = []
    offset = start
    while offset < end:
        tag, vs, ve, nxt = _der_tlv(data, offset)
        children.append((tag, vs, ve))
        offset = nxt
    return children


def extended_key_usage_oids(der: bytes) -> set[bytes]:
    """The DER-encoded OID of every `KeyPurposeId` in a certificate's
    `extKeyUsage` extension, or an empty set if the certificate carries no
    such extension.

    **A minimal DER walk, not the `cryptography` package** — that library is
    not a dependency of this service (`tests/tls_ca.py`'s own docstring: not
    even the test fixtures use it), and adding one for this alone is a
    dependency change out of scope for a refusal check. `getpeercert()`
    parses everything else this service reads from a certificate without
    one; this walks the one field it omits — `Certificate ::= SEQUENCE {
    tbsCertificate, ... }`, `tbsCertificate`'s children scanned for the `[3]`
    extensions field, each `Extension ::= SEQUENCE { extnID, critical
    OPTIONAL, extnValue OCTET STRING }` scanned for `id-ce-extKeyUsage`, and
    that extension's OCTET STRING content parsed again as the `SEQUENCE OF
    OBJECT IDENTIFIER` it DER-encodes.

    **Fails closed on anything this walk cannot make sense of** — a
    malformed or truncated `der` returns an empty set rather than raising,
    the same fail-closed shape `_registry_status` uses for an unreadable
    database. An empty set matches no `KeyPurposeId`, including
    `clientAuth`, so the caller refuses exactly as it would for a
    certificate that completed the handshake but genuinely carries none.
    """
    try:
        _, cert_vs, _, _ = _der_tlv(der, 0)
        _, tbs_vs, tbs_ve, _ = _der_tlv(der, cert_vs)
        for tag, cvs, cve in _der_children(der, tbs_vs, tbs_ve):
            if tag != _EXTENSIONS_TAG:
                continue
            # `[3]` is EXPLICIT: its content is one child, the actual
            # `SEQUENCE OF Extension`.
            _, seq_vs, seq_ve, _ = _der_tlv(der, cvs)
            for _, ext_vs, ext_ve in _der_children(der, seq_vs, seq_ve):
                fields = _der_children(der, ext_vs, ext_ve)
                if not fields:
                    continue
                _, oid_vs, oid_ve = fields[0]
                if der[oid_vs:oid_ve] != _EXT_KEY_USAGE_OID:
                    continue
                # `critical` is OPTIONAL, so `extnValue` is always the last
                # field regardless of whether it is present.
                _, octet_vs, _ = fields[-1]
                _, purposes_vs, purposes_ve, _ = _der_tlv(der, octet_vs)
                return {der[p_vs:p_ve] for _, p_vs, p_ve
                        in _der_children(der, purposes_vs, purposes_ve)}
        return set()
    except (IndexError, ValueError):
        return set()


# UTCTime (`YYMMDDHHMMSSZ`) and GeneralizedTime (`YYYYMMDDHHMMSSZ`), the two
# forms X.509's `Time` CHOICE allows (RFC 5280 §4.1.2.5).
_UTCTIME_TAG = 0x17
_GENERALIZEDTIME_TAG = 0x18


def _parse_asn1_time(tag: int, raw: bytes) -> _dt.datetime:
    """A `UTCTime` or `GeneralizedTime` value as a timezone-aware UTC
    datetime. `UTCTime`'s two-digit year follows RFC 5280's own rule: 50-99
    is 19xx, 00-49 is 20xx — the rule every certificate this walk reads is
    bound by, not a guess specific to this module."""
    text = raw.decode("ascii")
    if tag == _UTCTIME_TAG:
        year = int(text[0:2])
        year += 1900 if year >= 50 else 2000
        rest = text[2:]
    elif tag == _GENERALIZEDTIME_TAG:
        year = int(text[0:4])
        rest = text[4:]
    else:
        raise ValueError(f"unsupported ASN.1 time tag {tag:#x}")
    month, day, hour, minute, second = (
        int(rest[i:i + 2]) for i in (0, 2, 4, 6, 8))
    return _dt.datetime(year, month, day, hour, minute, second,
                        tzinfo=_dt.timezone.utc)


def not_after_utc(der: bytes) -> _dt.datetime:
    """A certificate's `notAfter` field (`Validity`'s second `Time`), as a
    timezone-aware UTC datetime.

    **The same minimal DER walk `extended_key_usage_oids` uses, for the same
    reason** — `cryptography` is not a dependency of this service, and
    `getpeercert()` has no local-file counterpart that parses this field
    without a live handshake. `tbsCertificate`'s children in RFC 5280's fixed
    order are `version [0] OPTIONAL`, `serialNumber`, `signature`, `issuer`,
    `validity`, `subject`, ... — the optional EXPLICIT `[0]` is stripped when
    present (every certificate this design mints is X.509v3 and carries it,
    confirmed against a real self-signed leaf with `openssl asn1parse`,
    2026-10-02) rather than assumed, so `validity` is found by position
    among the fields that are always there instead of trusting an absent
    field to still be at a fixed offset.

    Raises `IndexError` or `ValueError` on a `der` this walk cannot make
    sense of — unlike `extended_key_usage_oids`'s empty-set fallback, there
    is no safe default expiry to fail closed to, so the caller sees the
    failure rather than silently skipping a certificate it could not read.
    """
    _, cert_vs, _, _ = _der_tlv(der, 0)
    _, tbs_vs, tbs_ve, _ = _der_tlv(der, cert_vs)
    children = _der_children(der, tbs_vs, tbs_ve)
    if children and children[0][0] == 0xA0:
        children = children[1:]
    # children: serialNumber, signature AlgorithmIdentifier, issuer,
    # validity, subject, ...
    _, validity_vs, validity_ve = children[3]
    tag, time_vs, time_ve = _der_children(der, validity_vs, validity_ve)[1]
    return _parse_asn1_time(tag, der[time_vs:time_ve])


def uri_names(cert: dict | None) -> list[str]:
    """Every `URI` general name a peer certificate carries.

    **`getpeercert()` omits `subjectAltName` from its result entirely for a
    certificate with no SAN extension at all** — there is no key to find,
    rather than an empty one — and a chain-valid leaf can be exactly that and
    still complete the handshake (measured against a real TLS 1.3 handshake,
    platform/decisions/0066). So this returns an empty list rather than
    raising on a missing key, which is what lets the caller refuse a
    chain-valid-but-nameless certificate instead of crashing on it.
    """
    if not cert:
        return []
    return [value for kind, value in cert.get("subjectAltName", ())
            if kind == "URI"]


def _registry_status(machine: str, fingerprint: str) -> str | None:
    """The `status` of the registry row naming both this machine and this
    exact certificate, or `None` if no such row exists.

    **Both fields, not the fingerprint alone.** The fingerprint is what makes
    the row unique — SHA-256 collisions are not a real risk here — but
    checking `machine` too is what refuses a certificate whose SAN claims a
    name the registry recorded that exact fingerprint UNDER A DIFFERENT ONE,
    which is the shape a hand-edited registry row could produce and a
    fingerprint-only lookup would silently paper over.

    Fails closed on anything that stops this from reading the database at
    all: a missing file answers "no such row" rather than raising, the same
    fail-closed shape `identity._tokens()` uses for an unreadable token file.

    **The query is inside the same `sqlite3.Error` catch as the connect**
    (security-reviewer, on convivy/library#89). A locked or corrupt database
    raises at `execute` rather than at `connect`, and an exception escaping
    raw ASGI middleware becomes a 500 instead of this gate's 401 — still a
    refusal, but an incidental one, and this docstring promises a
    deliberate one.

    **`OSError` rather than `FileNotFoundError`**: `store.connect()` calls
    `DB_PATH.is_file()`, which raises `PermissionError` on EACCES under
    Python 3.11 and returns `False` under 3.14. Builder runs 3.11
    throughout, so the raising branch is the live one. See
    `credential_binding._bound_machine` for the measurement.
    """
    try:
        conn = store.connect()
    except (store.MisconfiguredDatabase, OSError, sqlite3.Error):
        return None
    try:
        row = conn.execute(
            "SELECT status FROM machine_certificates "
            "WHERE machine = ? AND fingerprint_sha256 = ?",
            (machine, fingerprint)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return row[0] if row else None


def _refuse() -> refusals.JSONResponse:
    return refusals.refuse(
        401, "no resolvable identity",
        hint="present a client certificate the Library's machine registry "
             "recognises")


def resolve_machine(mtls_info: dict | None) -> str | None:
    """The one registered, unrevoked machine the peer certificate in
    `mtls_info` (the `mtls.EXTENSION_KEY` scope extension) authenticates, or
    `None` for anything else.

    **The single place a certificate becomes a machine name.**
    `MachineIdentityGate` calls it to admit a request, and
    `credential_binding.redemption_binding_holds` calls it again inside
    `POST /tokens/redeem`, so redemption does not rest on the middleware
    having run first.

    Reads for the one value that permits and fails closed on everything
    else: no extension, no DER, zero or several URI names, a name outside
    `MACHINE_URI_PREFIX`, a registry row that is not exactly `active`, or a
    certificate without `clientAuth`.
    """
    if mtls_info is None:
        return None
    der = mtls_info.get("peer_cert_der")
    if not der:
        return None
    names = uri_names(mtls_info.get("peer_cert"))
    if len(names) != 1 or not names[0].startswith(MACHINE_URI_PREFIX):
        # Zero names: nothing to check. More than one: refused rather than
        # resolved to the first — the registry names one machine per row,
        # and picking which of several claimed names to trust is not a
        # decision this gate makes (platform/decisions/0066).
        return None
    machine = names[0][len(MACHINE_URI_PREFIX):]
    # Read FOR the one value that permits, never against the one that
    # denies — a typo, a case variant, a hand-edited row, or a future status
    # (`decommissioned`, distinct from `revoked`) must all refuse, the same
    # fail-closed shape `identity.resolve()` uses for an unrecognised `kind`
    # (platform/decisions/0066).
    if _registry_status(machine, sha256_der(der)) != "active":
        return None
    # A chain-valid certificate issued for `serverAuth` (the Library's own
    # listener certificate is one) must not authenticate as a machine merely
    # because it carries a matching URI name and a registry row —
    # platform/decisions/0066 requires `clientAuth` on every machine
    # certificate, enforced here rather than left to the handshake, which
    # does not check key usage at all.
    if CLIENT_AUTH_OID not in extended_key_usage_oids(der):
        return None
    return machine


class MachineIdentityGate:
    """Refuse a request over the mutual-TLS listener whose certificate does
    not resolve to exactly one registered, unrevoked machine.

    **Never runs on the plain listener.** This is keyed on
    `mtls.EXTENSION_KEY` in the ASGI scope's `extensions`, which only
    `mtls.MTLSH11Protocol` — the protocol class the TLS listener alone uses —
    ever sets. A request that arrived over `127.0.0.1:8100` carries no such
    key, so this middleware passes it straight through on the very first
    line, unchanged from how it behaved before this file existed.

    **Never reads a header for identity, ever, on any path through this
    class.** `X-Client-Cert`, `X-Forwarded-*`, or anything else a
    re-originating TLS proxy might carry a certificate in are not consulted
    anywhere below — the only source of truth is the peer certificate the
    TLS handshake itself established, which is what makes "a proxy that
    re-originates TLS and repairs the breakage with a header" fail here
    exactly like a bare unauthenticated connection does (platform/decisions/
    0066).
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        mtls_info = scope.get("extensions", {}).get(mtls.EXTENSION_KEY)
        if mtls_info is None:
            await self.app(scope, receive, send)
            return

        machine = resolve_machine(mtls_info)
        if machine is None:
            await _refuse()(scope, receive, send)
            return

        scope.setdefault("state", {})
        scope["state"]["machine_identity"] = machine
        await self.app(scope, receive, send)
