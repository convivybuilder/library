"""`machine_identity.MachineIdentityGate`: the refusals `platform/decisions/
0066` names for the mutual-TLS listener.

**Four scenarios are required by the decision record, and three of them are
not provable against a hand-built ASGI scope.** A chain-valid certificate
naming nobody, one naming two machines, and a revocation reaching an
already-open connection are all claims about what a real TLS handshake does
and about state that lives on a real, reused TCP connection — so those three
run against `mtls_server.RunningServer`, the same real listener the service
binds. The header-spoofing refusal and the registry's own status logic do
not depend on the handshake at all, so those run directly against the ASGI
middleware, which is faster and covers more shapes.

**Do not mock the DB layer.** `machine_certificates` here is built by
executing the migration's own DDL against a real SQLite file, the same
`ledger_tables`-fixture shape `tests/conftest.py` already uses for the
ledger tables, and every registry test drives `store.connect()` against
that real file.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib
import http.client
import sqlite3
import ssl
import subprocess

import pytest

import identity
import machine_identity
import mtls
import store
import tls_ca
from mtls_server import RunningServer, tls_config

MACHINE = "macbook"
MACHINE_URI = f"{machine_identity.MACHINE_URI_PREFIX}{MACHINE}"


# --- shared fixtures --------------------------------------------------------

@pytest.fixture
def registry(tmp_path, monkeypatch):
    """A real `machine_certificates` table, built from the migration's own
    DDL rather than a copy alembic never runs — `test_machine_certificates_
    migration.py` is what proves alembic's own DDL matches this shape."""
    db = tmp_path / "control.db"
    conn = sqlite3.connect(db)
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
    """)
    conn.commit()
    conn.close()
    monkeypatch.setattr(store, "DB_PATH", db)
    return db


def _digest(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest()


def _leaf_der(tmp_path, name="leaf", *, ext_key_usage="clientAuth") -> bytes:
    """The raw DER of a real, freshly-issued leaf certificate — needed
    wherever a test exercises `extended_key_usage_oids`, since that walk
    parses actual DER structure and cannot be handed opaque placeholder
    bytes the way fingerprint-only tests can."""
    ca_cert, ca_key = tls_ca.make_ca(tmp_path)
    cert, _ = tls_ca.make_cert(tmp_path, name, ca_cert, ca_key,
                               ext_key_usage=ext_key_usage)
    return ssl.PEM_cert_to_DER_cert(cert.read_text())


def _register(db, machine, fingerprint, *, status="active", custody="hardware"):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO machine_certificates "
        "(machine, fingerprint_sha256, not_after, status, created_at, custody) "
        "VALUES (?, ?, '2099-01-01', ?, '2026-01-01', ?)",
        (machine, fingerprint, status, custody))
    conn.commit()
    conn.close()


# --- the registry lookup, in isolation --------------------------------------

def test_uri_names_returns_empty_for_a_certificate_with_no_san_at_all():
    """`getpeercert()` omits `subjectAltName` entirely for a SAN-less
    certificate — there is no key, not an empty one — which is exactly the
    shape a chain-valid nameless leaf produces (platform/decisions/0066,
    measured)."""
    assert machine_identity.uri_names({"subject": (("CN", "x"),)}) == []
    assert machine_identity.uri_names(None) == []


def test_uri_names_reads_only_the_uri_kind():
    cert = {"subjectAltName": (("DNS", "localhost"),
                               ("URI", "convivy://machine/a"))}
    assert machine_identity.uri_names(cert) == ["convivy://machine/a"]


def test_sha256_der_is_the_plain_hex_digest():
    import hashlib
    assert (machine_identity.sha256_der(b"hello")
            == hashlib.sha256(b"hello").hexdigest())


def test_registry_status_is_none_for_an_unregistered_fingerprint(registry):
    assert machine_identity._registry_status("macbook", "deadbeef") is None


def test_registry_status_refuses_a_fingerprint_registered_under_another_name(
        registry):
    """The lookup is `(machine, fingerprint)` together, not the fingerprint
    alone — a row recorded under a different machine name must not
    authenticate a certificate merely because its fingerprint matches."""
    _register(registry, "a-different-machine", "abc123")
    assert machine_identity._registry_status("macbook", "abc123") is None


def test_registry_status_reads_the_stored_status(registry):
    _register(registry, "macbook", "abc123", status="active")
    assert machine_identity._registry_status("macbook", "abc123") == "active"


def test_registry_status_fails_closed_on_a_missing_database(tmp_path,
                                                             monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "does-not-exist.db")
    assert machine_identity._registry_status("macbook", "abc123") is None


def test_registry_status_fails_closed_on_a_corrupt_database(tmp_path,
                                                            monkeypatch):
    """The case a connect-only catch misses, and the one the missing-file
    test above cannot reach: sqlite opens a junk file lazily, so
    `connect()` succeeds and the first `execute()` raises. Without
    `sqlite3.Error` around the QUERY this returns by exception rather than
    by decision.
    """
    junk = tmp_path / "corrupt.db"
    junk.write_bytes(b"this is not a sqlite database" * 64)
    monkeypatch.setattr(store, "DB_PATH", junk)
    assert machine_identity._registry_status("macbook", "abc123") is None


def test_registry_status_fails_closed_on_a_permission_error(monkeypatch):
    """The EACCES case, which is `PermissionError` — an `OSError`, and
    neither a `FileNotFoundError` nor a `sqlite3.Error`. See
    `credential_binding`'s twin for why the interpreter version decides
    whether this path is reachable at all.
    """
    def boom():
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(store, "connect", boom)
    assert machine_identity._registry_status("macbook", "abc123") is None


def test_a_corrupt_registry_refuses_401_rather_than_raising(tmp_path,
                                                            monkeypatch):
    """An unreadable certificate registry must refuse BY DECISION. An
    exception escaping raw ASGI middleware becomes a 500 out of Starlette,
    which refuses the request too — so "refused" is not the property worth
    asserting here, and the status code is what separates the deliberate
    refusal from the incidental one.
    """
    junk = tmp_path / "corrupt.db"
    junk.write_bytes(b"this is not a sqlite database" * 64)
    monkeypatch.setattr(store, "DB_PATH", junk)
    der = _leaf_der(tmp_path)
    gate = machine_identity.MachineIdentityGate(_downstream_200)
    scope = _base_scope(mtls_info={
        "peer_cert": {"subjectAltName": (("URI", MACHINE_URI),)},
        "peer_cert_der": der})
    assert _status(_run_gate(gate, scope)) == 401


# --- the gate, against a hand-built scope -----------------------------------

def _base_scope(*, headers=(), mtls_info=None):
    return {
        "type": "http", "method": "GET", "path": "/", "raw_path": b"/",
        "query_string": b"", "headers": list(headers),
        "client": ("127.0.0.1", 40000), "server": ("127.0.0.1", 8443),
        "scheme": "https",
        "extensions": ({} if mtls_info is None
                       else {mtls.EXTENSION_KEY: mtls_info}),
        "state": {},
    }


def _run_gate(app, scope):
    sent = []

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    async def go():
        await app(scope, receive, send)

    asyncio.run(go())
    return sent


def _status(sent):
    starts = [m for m in sent if m["type"] == "http.response.start"]
    assert starts, f"no response started: {sent}"
    return starts[0]["status"]


async def _downstream_200(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def test_a_request_over_the_plain_listener_never_reaches_the_gates_logic():
    """No `mtls.EXTENSION_KEY` in `extensions` at all — the shape every
    request over 127.0.0.1:8100 has — passes straight through, whatever its
    headers claim."""
    gate = machine_identity.MachineIdentityGate(_downstream_200)
    scope = _base_scope(headers=[(b"x-client-cert", b"anything")])
    assert _status(_run_gate(gate, scope)) == 200


def test_x_client_cert_header_with_no_peer_certificate_is_refused():
    """**Required by platform/decisions/0066.** A re-originating TLS proxy
    that repairs the break by forwarding the certificate in a header is
    exactly what this refuses: the connection carries the mutual-TLS
    extension (so this is not the plain-listener case above) but no peer
    certificate, and the header is never consulted."""
    gate = machine_identity.MachineIdentityGate(_downstream_200)
    scope = _base_scope(
        headers=[(b"x-client-cert", b"-----BEGIN CERTIFICATE-----fake")],
        mtls_info={"peer_cert": None, "peer_cert_der": None})
    assert _status(_run_gate(gate, scope)) == 401


@pytest.mark.parametrize("header_value", [b"", b"anything-at-all",
                                          b"MIIBazCCAQ==spoofed"])
def test_the_header_content_never_changes_the_outcome(header_value, registry,
                                                       tmp_path):
    """However the header is filled in, an admitted certificate is admitted
    on its own terms and a missing one is refused on its own terms — proving
    the header is never read, not merely that one value of it is ignored."""
    der = _leaf_der(tmp_path)
    _register(registry, MACHINE, _digest(der))
    gate = machine_identity.MachineIdentityGate(_downstream_200)

    admitted = _base_scope(
        headers=[(b"x-client-cert", header_value)],
        mtls_info={"peer_cert": {"subjectAltName": (("URI", MACHINE_URI),)},
                  "peer_cert_der": der})
    assert _status(_run_gate(gate, admitted)) == 200

    refused = _base_scope(
        headers=[(b"x-client-cert", header_value)],
        mtls_info={"peer_cert": None, "peer_cert_der": None})
    assert _status(_run_gate(gate, refused)) == 401


def test_zero_uri_names_is_refused():
    gate = machine_identity.MachineIdentityGate(_downstream_200)
    scope = _base_scope(mtls_info={
        "peer_cert": {"subject": (("CN", MACHINE),)},
        "peer_cert_der": b"der"})
    assert _status(_run_gate(gate, scope)) == 401


def test_two_uri_names_is_refused_rather_than_resolved_to_the_first(registry):
    """Even when the first name IS registered and unrevoked — the failure
    mode this guards is picking a name to trust, not merely failing to find
    one."""
    der = b"known"
    _register(registry, MACHINE, _digest(der))
    gate = machine_identity.MachineIdentityGate(_downstream_200)
    scope = _base_scope(mtls_info={
        "peer_cert": {"subjectAltName": (
            ("URI", MACHINE_URI),
            ("URI", f"{machine_identity.MACHINE_URI_PREFIX}someone-else"))},
        "peer_cert_der": der})
    assert _status(_run_gate(gate, scope)) == 401


def test_an_unregistered_name_is_refused(registry):
    gate = machine_identity.MachineIdentityGate(_downstream_200)
    scope = _base_scope(mtls_info={
        "peer_cert": {"subjectAltName": (("URI", MACHINE_URI),)},
        "peer_cert_der": b"never-registered"})
    assert _status(_run_gate(gate, scope)) == 401


def test_a_revoked_row_is_refused(registry):
    der = b"revoked-fp"
    _register(registry, MACHINE, _digest(der), status="revoked")
    gate = machine_identity.MachineIdentityGate(_downstream_200)
    scope = _base_scope(mtls_info={
        "peer_cert": {"subjectAltName": (("URI", MACHINE_URI),)},
        "peer_cert_der": der})
    assert _status(_run_gate(gate, scope)) == 401


@pytest.mark.parametrize("status", ["decommissioned", "Active", "", "ACTIVE",
                                    "revoke", "pending"])
def test_an_unrecognised_status_is_refused_not_admitted(status, monkeypatch):
    """The gate reads FOR `active`, never against `revoked` — a typo, a case
    variant, or a future status value (`decommissioned`, which
    platform/decisions/0066 itself names as distinct from `revoked`) must
    refuse rather than pass by default. Exercised by monkeypatching the
    registry lookup directly, since the migration's own CHECK constraint
    would otherwise stop such a row from ever being written at all — this
    proves the application-level direction independently of that backstop.
    """
    monkeypatch.setattr(machine_identity, "_registry_status",
                        lambda machine, fingerprint: status)
    gate = machine_identity.MachineIdentityGate(_downstream_200)
    scope = _base_scope(mtls_info={
        "peer_cert": {"subjectAltName": (("URI", MACHINE_URI),)},
        "peer_cert_der": b"der"})
    assert _status(_run_gate(gate, scope)) == 401


def test_an_admitted_request_carries_the_machine_identity_forward(registry,
                                                                   tmp_path):
    der = _leaf_der(tmp_path)
    _register(registry, MACHINE, _digest(der))
    gate = machine_identity.MachineIdentityGate(_downstream_200)
    scope = _base_scope(mtls_info={
        "peer_cert": {"subjectAltName": (("URI", MACHINE_URI),)},
        "peer_cert_der": der})
    _run_gate(gate, scope)
    assert scope["state"]["machine_identity"] == MACHINE


# --- extended key usage -------------------------------------------------

def test_extended_key_usage_oids_reads_a_real_clientauth_certificate(
        tmp_path):
    der = _leaf_der(tmp_path, ext_key_usage="clientAuth")
    assert (machine_identity.extended_key_usage_oids(der)
            == {machine_identity.CLIENT_AUTH_OID})


def test_extended_key_usage_oids_reads_a_real_serverauth_certificate(
        tmp_path):
    der = _leaf_der(tmp_path, ext_key_usage="serverAuth")
    assert machine_identity.CLIENT_AUTH_OID not in (
        machine_identity.extended_key_usage_oids(der))


def test_extended_key_usage_oids_reads_both_purposes_on_one_certificate(
        tmp_path):
    der = _leaf_der(tmp_path, ext_key_usage="clientAuth,serverAuth")
    assert machine_identity.CLIENT_AUTH_OID in (
        machine_identity.extended_key_usage_oids(der))


def test_extended_key_usage_oids_is_empty_for_a_certificate_with_no_extension(
        tmp_path):
    """The CA's own self-signed certificate carries no `extKeyUsage`
    extension at all — the walk must return an empty set rather than
    raising."""
    ca_cert, _ = tls_ca.make_ca(tmp_path)
    der = ssl.PEM_cert_to_DER_cert(ca_cert.read_text())
    assert machine_identity.extended_key_usage_oids(der) == set()


@pytest.mark.parametrize("garbage", [b"", b"\x00", b"not-a-certificate",
                                     b"\x30\x80\xff\xff\xff\xff"])
def test_extended_key_usage_oids_fails_closed_on_malformed_der(garbage):
    assert machine_identity.extended_key_usage_oids(garbage) == set()


# --- notAfter -----------------------------------------------------------

def test_not_after_utc_matches_opensslS_own_reported_enddate(tmp_path):
    """The ground truth is `openssl x509 -enddate`'s own reading of the same
    certificate — not a value this test constructs, so a bug shared between
    the walk and the assertion cannot hide."""
    ca_cert, ca_key = tls_ca.make_ca(tmp_path)
    cert, _ = tls_ca.make_cert(tmp_path, "leaf", ca_cert, ca_key)
    der = ssl.PEM_cert_to_DER_cert(cert.read_text())
    reported = subprocess.run(
        ["openssl", "x509", "-noout", "-enddate", "-in", str(cert)],
        capture_output=True, text=True, check=True).stdout.strip()
    expected = _dt.datetime.strptime(
        reported.removeprefix("notAfter="), "%b %d %H:%M:%S %Y %Z"
    ).replace(tzinfo=_dt.timezone.utc)
    assert machine_identity.not_after_utc(der) == expected


def test_not_after_utc_reads_the_cas_own_self_signed_certificate_too(
        tmp_path):
    """The CA's own certificate carries no `extKeyUsage` extension at all
    (see the `extended_key_usage_oids` test above) — `notAfter` sits ahead
    of extensions in `tbsCertificate` and must not depend on one being
    present."""
    ca_cert, _ = tls_ca.make_ca(tmp_path)
    der = ssl.PEM_cert_to_DER_cert(ca_cert.read_text())
    reported = subprocess.run(
        ["openssl", "x509", "-noout", "-enddate", "-in", str(ca_cert)],
        capture_output=True, text=True, check=True).stdout.strip()
    expected = _dt.datetime.strptime(
        reported.removeprefix("notAfter="), "%b %d %H:%M:%S %Y %Z"
    ).replace(tzinfo=_dt.timezone.utc)
    assert machine_identity.not_after_utc(der) == expected


@pytest.mark.parametrize("garbage", [b"", b"\x00", b"not-a-certificate",
                                     b"\x30\x80\xff\xff\xff\xff"])
def test_not_after_utc_raises_rather_than_guessing_on_malformed_der(garbage):
    """Unlike `extended_key_usage_oids`'s empty-set fallback, there is no
    safe default expiry — `cert_expiry_check.py`'s caller must see the
    failure rather than silently treating an unparseable certificate as one
    with no expiry to warn about."""
    with pytest.raises((IndexError, ValueError)):
        machine_identity.not_after_utc(garbage)


def test_not_after_utc_reads_a_generalizedtime_notafter_past_2049(tmp_path):
    """RFC 5280 requires `GeneralizedTime`, not `UTCTime`, for any `notAfter`
    at or after 2050 — every other certificate in this file expires well
    before then, so `_parse_asn1_time`'s four-digit-year branch is otherwise
    never exercised."""
    ca_cert, ca_key = tls_ca.make_ca(tmp_path)
    cert, _ = tls_ca.make_cert(tmp_path, "leaf-2050s", ca_cert, ca_key,
                               days=10000)
    der = ssl.PEM_cert_to_DER_cert(cert.read_text())
    asn1 = subprocess.run(
        ["openssl", "asn1parse", "-in", str(cert)],
        capture_output=True, text=True, check=True).stdout
    assert "GENERALIZEDTIME" in asn1, (
        "this fixture must itself use GeneralizedTime, or the test below "
        "proves nothing about that branch")
    reported = subprocess.run(
        ["openssl", "x509", "-noout", "-enddate", "-in", str(cert)],
        capture_output=True, text=True, check=True).stdout.strip()
    expected = _dt.datetime.strptime(
        reported.removeprefix("notAfter="), "%b %d %H:%M:%S %Y %Z"
    ).replace(tzinfo=_dt.timezone.utc)
    assert expected.year >= 2050
    assert machine_identity.not_after_utc(der) == expected


def test_der_tlv_raises_on_a_der_truncated_past_its_declared_length(
        tmp_path):
    """Security-reviewer hardening (convivy/library#200): `_der_tlv`'s value
    slices do not raise on their own when a claimed length runs past the end
    of the buffer, unlike the byte-indexed tag/length reads above them. A
    real certificate's DER missing even its last byte (part of the
    signature, nothing `not_after_utc` itself reads) must now raise rather
    than silently parsing the fields it does need."""
    ca_cert, ca_key = tls_ca.make_ca(tmp_path)
    cert, _ = tls_ca.make_cert(tmp_path, "leaf", ca_cert, ca_key)
    der = ssl.PEM_cert_to_DER_cert(cert.read_text())
    assert machine_identity.not_after_utc(der) is not None  # sanity: valid

    with pytest.raises(ValueError):
        machine_identity.not_after_utc(der[:-1])


def test_a_server_auth_certificate_is_refused_despite_a_matching_registered_name(
        registry, tmp_path):
    """The exact attack the ADR's Handoff names: a chain-valid certificate
    issued for `serverAuth` — the shape the Library's own listener
    certificate has — must not authenticate as a machine merely because it
    carries a matching URI name and a registry row."""
    der = _leaf_der(tmp_path, ext_key_usage="serverAuth")
    _register(registry, MACHINE, _digest(der))
    gate = machine_identity.MachineIdentityGate(_downstream_200)
    scope = _base_scope(mtls_info={
        "peer_cert": {"subjectAltName": (("URI", MACHINE_URI),)},
        "peer_cert_der": der})
    assert _status(_run_gate(gate, scope)) == 401


# --- a signing certificate is not a machine certificate (contract §2.2) -----

# id-kp-signing, the purpose OID platform/contracts/library-protocol §2.2 pins.
# The dotted form is for openssl; the DER content bytes are the ones
# root-ca's `OIDs.idKpSigning` carries. The positive check on the fixture
# below asserts the two agree, so a fixture built from the wrong arc fails
# here rather than passing for the wrong reason.
SIGNING_OID_DOTTED = "2.25.332679538808142797592760871621889796493"
SIGNING_OID_DER = bytes((
    0x69, 0x83, 0xF4, 0xC7, 0xDF, 0xE8, 0xC3, 0xF8, 0xD2, 0xB4,
    0xFB, 0x8E, 0xC4, 0xB4, 0xF9, 0xAB, 0x9E, 0xAA, 0xD3, 0x0D,
))


def _signing_der(tmp_path, *, uri_names=(), email_names=(),
                 subject_cn=None) -> bytes:
    """A root-signed certificate with exactly the signing certificate's
    extensions: `keyUsage` digitalSignature only (critical) and an EKU of
    the signing purpose alone (critical), no clientAuth, no serverAuth."""
    ca_cert, ca_key = tls_ca.make_ca(tmp_path)
    cert, _ = tls_ca.make_cert(
        tmp_path, "signer", ca_cert, ca_key, uri_names=uri_names,
        email_names=email_names, subject_cn=subject_cn,
        ext_key_usage=f"critical,{SIGNING_OID_DOTTED}",
        key_usage="critical,digitalSignature")
    return ssl.PEM_cert_to_DER_cert(cert.read_text())


def test_the_signing_fixture_carries_the_pinned_purpose_oid(tmp_path):
    """Guards the fixture itself: the EKU the other signing tests present is
    exactly the purpose OID the contract pins, and nothing else."""
    der = _signing_der(tmp_path)
    assert machine_identity.extended_key_usage_oids(der) == {SIGNING_OID_DER}


def test_a_signing_certificate_is_not_admitted_under_a_registered_machine_name(
        registry, tmp_path):
    """Only the EKU check stands between this certificate and admission: the
    URI name matches the machine prefix, the row is registered and active,
    and the fingerprint matches. The control in the same test shows that the
    identical certificate issued with clientAuth IS admitted, so the refusal
    is the purpose OID and nothing else.

    **Removing the `CLIENT_AUTH_OID` check in `machine_identity.resolve_machine`
    makes the first assertion fail** — this test is what pins that check."""
    signer_der = _signing_der(tmp_path, uri_names=(MACHINE_URI,))
    _register(registry, MACHINE, _digest(signer_der))
    mtls_info = {"peer_cert": {"subjectAltName": (("URI", MACHINE_URI),)},
                 "peer_cert_der": signer_der}
    assert machine_identity.resolve_machine(mtls_info) is None

    ca_cert, ca_key = tls_ca.make_ca(tmp_path)
    client_cert, _ = tls_ca.make_cert(
        tmp_path, "client", ca_cert, ca_key, uri_names=(MACHINE_URI,))
    client_der = ssl.PEM_cert_to_DER_cert(client_cert.read_text())
    _register(registry, MACHINE, _digest(client_der))
    client_info = {"peer_cert": {"subjectAltName": (("URI", MACHINE_URI),)},
                   "peer_cert_der": client_der}
    assert machine_identity.resolve_machine(client_info) == MACHINE


def test_a_signing_certificate_with_its_real_subject_is_refused_by_the_gate(
        registry, tmp_path):
    """The shape root-ca's `LeafCertificate.issue(kind: .signing)` produces:
    subject `CN=<email>`, SAN `rfc822Name` the email plus `URI
    convivy://signer/<machine>`, and no `convivy://machine/` name. No
    registry row exists for it, so this is refused on the URI and the
    purpose OID together, before any registry lookup."""
    signer_der = _signing_der(
        tmp_path, uri_names=("convivy://signer/" + MACHINE,),
        email_names=("operator@example.com",), subject_cn="operator@example.com")
    gate = machine_identity.MachineIdentityGate(_downstream_200)
    scope = _base_scope(mtls_info={
        "peer_cert": {"subjectAltName": (
            ("email", "operator@example.com"),
            ("URI", "convivy://signer/" + MACHINE))},
        "peer_cert_der": signer_der})
    assert _status(_run_gate(gate, scope)) == 401


# --- the three refusals that need a real handshake and a real connection ---

def _ca_and_server(dir_):
    ca_cert, ca_key = tls_ca.make_ca(dir_)
    server_cert, server_key = tls_ca.make_cert(
        dir_, "server", ca_cert, ca_key, dns_names=("localhost",),
        ip_names=("127.0.0.1",), ext_key_usage="serverAuth")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(server_cert), str(server_key))
    ctx.load_verify_locations(str(ca_cert))
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    return ca_cert, ca_key, ctx


def _client_ctx(ca_cert, leaf_cert, leaf_key):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(str(ca_cert))
    ctx.load_cert_chain(str(leaf_cert), str(leaf_key))
    return ctx


@pytest.fixture
def real_app(tmp_path, monkeypatch):
    """The actual FastAPI app, wired to a throwaway token file and control
    database — the real middleware chain, including `MachineIdentityGate` as
    it is actually registered, rather than the gate constructed standalone."""
    import app as libapp
    tokens = tmp_path / "api-tokens.json"
    tokens.write_text("{}")
    monkeypatch.setattr(identity, "TOKENS", tokens)
    return libapp.app


def test_a_chain_valid_certificate_with_no_uri_name_is_refused_over_a_real_handshake(
        tmp_path, real_app, registry):
    """**Not stopped by the handshake.** A chain-valid leaf with no SAN at
    all completes a real TLS 1.3 mutual handshake — measured directly in
    platform/decisions/0066 — so the refusal has to be the Library's own."""
    ca_cert, ca_key, server_ctx = _ca_and_server(tmp_path)
    nameless_cert, nameless_key = tls_ca.make_cert(
        tmp_path, "nameless", ca_cert, ca_key)  # no uri_names at all

    server = RunningServer(tls_config(real_app, server_ctx))
    port = server.start()
    try:
        conn = http.client.HTTPSConnection(
            "127.0.0.1", port, timeout=5,
            context=_client_ctx(ca_cert, nameless_cert, nameless_key))
        conn.request("GET", "/health")
        resp = conn.getresponse()
        assert resp.status == 401, resp.read()
        conn.close()
    finally:
        server.stop()


def test_a_certificate_with_two_uri_names_is_refused_over_a_real_handshake(
        tmp_path, real_app, registry):
    _register(registry, "a", "will-not-be-reached")
    ca_cert, ca_key, server_ctx = _ca_and_server(tmp_path)
    two_name_cert, two_name_key = tls_ca.make_cert(
        tmp_path, "twoname", ca_cert, ca_key,
        uri_names=(f"{machine_identity.MACHINE_URI_PREFIX}a",
                   f"{machine_identity.MACHINE_URI_PREFIX}b"))

    server = RunningServer(tls_config(real_app, server_ctx))
    port = server.start()
    try:
        conn = http.client.HTTPSConnection(
            "127.0.0.1", port, timeout=5,
            context=_client_ctx(ca_cert, two_name_cert, two_name_key))
        conn.request("GET", "/health")
        resp = conn.getresponse()
        assert resp.status == 401, resp.read()
        conn.close()
    finally:
        server.stop()


def test_a_revoked_row_refuses_the_next_request_over_an_already_open_connection(
        tmp_path, real_app, registry):
    """**The property that matters, and the one a fresh connection cannot
    prove.** One TLS connection, kept open across two HTTP requests: the
    first request succeeds while the row is active, the row is then flipped
    to revoked with the connection never closed, and the second request over
    THAT SAME connection is refused — proving the registry is read per
    request rather than cached at the handshake, which is what a test that
    reconnected after revoking would not be able to tell apart from a
    connection-level cache."""
    import hashlib
    ca_cert, ca_key, server_ctx = _ca_and_server(tmp_path)
    leaf_cert, leaf_key = tls_ca.make_cert(
        tmp_path, MACHINE, ca_cert, ca_key, uri_names=(MACHINE_URI,))
    fingerprint = hashlib.sha256(
        ssl.PEM_cert_to_DER_cert(leaf_cert.read_text())).hexdigest()
    _register(registry, MACHINE, fingerprint)

    server = RunningServer(tls_config(real_app, server_ctx))
    port = server.start()
    try:
        conn = http.client.HTTPSConnection(
            "127.0.0.1", port, timeout=5,
            context=_client_ctx(ca_cert, leaf_cert, leaf_key))

        conn.request("GET", "/health")
        first = conn.getresponse()
        assert first.status == 200, first.read()
        first.read()

        db_conn = sqlite3.connect(store.DB_PATH)
        db_conn.execute(
            "UPDATE machine_certificates SET status = 'revoked' "
            "WHERE machine = ?", (MACHINE,))
        db_conn.commit()
        db_conn.close()

        conn.request("GET", "/health")
        second = conn.getresponse()
        assert second.status == 401, (
            f"a revocation did not reach the next request over an "
            f"already-open connection: {second.read()}")
        conn.close()
    finally:
        server.stop()
