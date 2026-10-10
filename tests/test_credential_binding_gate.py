"""`credential_binding.CredentialBindingGate`: the stage-C refusals
`platform/decisions/0066` names for the mutual-TLS listener.

**The bind-from-the-right-machine and bind-from-the-wrong-machine cases
need a real handshake and a real connection**, the same reasoning
`test_machine_identity_gate.py` gives for its own three real-handshake
tests: this gate reads `scope["state"]["machine_identity"]`, which only
`machine_identity.MachineIdentityGate` sets, and only once a real TLS
handshake has produced a certificate for it to resolve — a hand-built scope
can assert the gate's own logic in isolation, but not that the two gates
compose correctly wired into the real app. Both run here: the isolated
unit tests first, the real-handshake integration tests after.

**Do not mock the DB layer.** `credential_bindings` is built by executing
the migration's own DDL against a real SQLite file, matching `test_machine_
identity_gate.py`'s `registry` fixture, extended to also carry
`machine_certificates` so the integration tests can register a real
machine alongside a real binding.
"""
from __future__ import annotations

import asyncio
import hashlib
import http.client
import sqlite3
import ssl

import pytest

import credential_binding
import machine_identity
import mtls
import store
import tls_ca
from mtls_server import RunningServer, tls_config

MACHINE = "macbook"
OTHER_MACHINE = "keelboat"
MACHINE_URI = f"{machine_identity.MACHINE_URI_PREFIX}{MACHINE}"
OTHER_MACHINE_URI = f"{machine_identity.MACHINE_URI_PREFIX}{OTHER_MACHINE}"
TOKEN = "s3cr3t-bearer-token-do-not-log-me"


# --- shared fixtures --------------------------------------------------------

@pytest.fixture
def registry(tmp_path, monkeypatch):
    """A real `machine_certificates` and `credential_bindings` pair, built
    from each migration's own DDL."""
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
        CREATE TABLE credential_bindings (
            id INTEGER PRIMARY KEY,
            credential_sha256 TEXT NOT NULL UNIQUE,
            machine TEXT NOT NULL,
            note TEXT,
            created_at TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()
    monkeypatch.setattr(store, "DB_PATH", db)
    return db


def _digest(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest()


def _register_machine(db, machine, fingerprint, *, status="active"):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO machine_certificates "
        "(machine, fingerprint_sha256, not_after, status, created_at, custody) "
        "VALUES (?, ?, '2099-01-01', ?, '2026-01-01', 'hardware')",
        (machine, fingerprint, status))
    conn.commit()
    conn.close()


def _register_binding(db, token, machine, *, note=None):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO credential_bindings "
        "(credential_sha256, machine, note, created_at) "
        "VALUES (?, ?, ?, '2026-01-01')",
        (credential_binding.sha256_token(token), machine, note))
    conn.commit()
    conn.close()


# --- the binding lookup, in isolation ---------------------------------------

def test_sha256_token_is_the_plain_hex_digest_of_the_utf8_bytes():
    assert (credential_binding.sha256_token("hello")
            == hashlib.sha256(b"hello").hexdigest())


def test_bound_machine_is_none_for_an_unbound_credential(registry):
    assert credential_binding._bound_machine("deadbeef") is None


def test_bound_machine_reads_the_stored_row(registry):
    _register_binding(registry, TOKEN, MACHINE)
    assert (credential_binding._bound_machine(
        credential_binding.sha256_token(TOKEN)) == MACHINE)


def test_bound_machine_fails_closed_on_a_missing_database(tmp_path,
                                                           monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "does-not-exist.db")
    assert credential_binding._bound_machine("deadbeef") is None


def test_bound_machine_fails_closed_on_a_corrupt_database(tmp_path,
                                                          monkeypatch):
    """A file that EXISTS and is not a database is the case a
    connect-only catch misses: sqlite opens it lazily and raises at the
    query instead. The distinction matters because the two failures
    produce different evidence — `None` here becomes the gate's
    deliberate 401, while an escaping `sqlite3.DatabaseError` becomes a
    500 out of Starlette. Both refuse the request; only one of them is
    the refusal this module's docstring promises.
    """
    junk = tmp_path / "corrupt.db"
    junk.write_bytes(b"this is not a sqlite database" * 64)
    monkeypatch.setattr(store, "DB_PATH", junk)
    assert credential_binding._bound_machine("deadbeef") is None


def test_bound_machine_fails_closed_on_a_permission_error(monkeypatch):
    """`store.connect()` calls `DB_PATH.is_file()`, and under Python 3.11
    that raises `PermissionError` on EACCES rather than returning `False`
    — a directory whose permissions tightened, or a degraded network
    mount answering EACCES instead of ENOENT. Under 3.14 the same call
    returns `False` instead. Builder runs 3.11 throughout, so the raising
    branch is the live one; the catch covers both regardless.

    Raised from a stubbed `store.connect` rather than by chmod, so this
    pins the CATCH on whichever interpreter runs it instead of pinning
    one version's pathlib behaviour.
    """
    def boom():
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(store, "connect", boom)
    assert credential_binding._bound_machine("deadbeef") is None



# --- the gate, against a hand-built scope -----------------------------------

def _base_scope(*, headers=(), mtls_info=None, state=None):
    return {
        "type": "http", "method": "GET", "path": "/", "raw_path": b"/",
        "query_string": b"", "headers": list(headers),
        "client": ("127.0.0.1", 40000), "server": ("127.0.0.1", 8443),
        "scheme": "https",
        "extensions": ({} if mtls_info is None
                       else {mtls.EXTENSION_KEY: mtls_info}),
        "state": state if state is not None else {},
    }


def _auth_header(token):
    return (b"authorization", f"Bearer {token}".encode("latin-1"))


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
    """No `mtls.EXTENSION_KEY` in `extensions` — the shape every request
    over 127.0.0.1:8100 has — passes straight through, however unbound the
    presented token is and whatever `state` claims."""
    gate = credential_binding.CredentialBindingGate(_downstream_200)
    scope = _base_scope(headers=[_auth_header("anything-unbound")])
    assert _status(_run_gate(gate, scope)) == 200


def test_no_bearer_token_passes_through_unchanged(registry):
    """No `Authorization` header at all is not this gate's refusal to
    make — ordinary `_identify` answers that with its own 401."""
    gate = credential_binding.CredentialBindingGate(_downstream_200)
    scope = _base_scope(mtls_info={"peer_cert": None, "peer_cert_der": None},
                        state={"machine_identity": MACHINE})
    assert _status(_run_gate(gate, scope)) == 200


def test_a_non_bearer_authorization_header_passes_through_unchanged(registry):
    gate = credential_binding.CredentialBindingGate(_downstream_200)
    scope = _base_scope(
        headers=[(b"authorization", b"Basic dXNlcjpwYXNz")],
        mtls_info={"peer_cert": None, "peer_cert_der": None},
        state={"machine_identity": MACHINE})
    assert _status(_run_gate(gate, scope)) == 200


def test_an_unbound_token_is_refused(registry):
    gate = credential_binding.CredentialBindingGate(_downstream_200)
    scope = _base_scope(
        headers=[_auth_header(TOKEN)],
        mtls_info={"peer_cert": None, "peer_cert_der": None},
        state={"machine_identity": MACHINE})
    assert _status(_run_gate(gate, scope)) == 401


def test_a_corrupt_database_refuses_401_rather_than_raising(tmp_path,
                                                            monkeypatch):
    """An unreadable binding table must refuse BY DECISION. Without the
    query-side `sqlite3.Error` catch this raises out of raw ASGI
    middleware, which Starlette turns into a 500 — still a refusal, and
    not the one `_bound_machine`'s docstring promises. The two outcomes
    are what distinguish a fail-closed gate from one that happens to
    crash before admitting anything."""
    junk = tmp_path / "corrupt.db"
    junk.write_bytes(b"this is not a sqlite database" * 64)
    monkeypatch.setattr(store, "DB_PATH", junk)
    gate = credential_binding.CredentialBindingGate(_downstream_200)
    scope = _base_scope(
        headers=[_auth_header(TOKEN)],
        mtls_info={"peer_cert": None, "peer_cert_der": None},
        state={"machine_identity": MACHINE})
    assert _status(_run_gate(gate, scope)) == 401


def test_a_bound_token_presented_as_the_bound_machine_is_admitted(registry):
    _register_binding(registry, TOKEN, MACHINE)
    gate = credential_binding.CredentialBindingGate(_downstream_200)
    scope = _base_scope(
        headers=[_auth_header(TOKEN)],
        mtls_info={"peer_cert": None, "peer_cert_der": None},
        state={"machine_identity": MACHINE})
    assert _status(_run_gate(gate, scope)) == 200


def test_a_bound_token_presented_as_a_different_machine_is_refused(registry):
    _register_binding(registry, TOKEN, MACHINE)
    gate = credential_binding.CredentialBindingGate(_downstream_200)
    scope = _base_scope(
        headers=[_auth_header(TOKEN)],
        mtls_info={"peer_cert": None, "peer_cert_der": None},
        state={"machine_identity": OTHER_MACHINE})
    assert _status(_run_gate(gate, scope)) == 401


def test_a_bearer_token_with_no_resolved_machine_identity_is_refused(
        registry):
    """Defensive: if this gate somehow ran without `machine_identity.
    MachineIdentityGate` having set `scope["state"]["machine_identity"]`
    first — a middleware-ordering bug this file cannot see at runtime — it
    refuses rather than treating the absence as anything admitted."""
    _register_binding(registry, TOKEN, MACHINE)
    gate = credential_binding.CredentialBindingGate(_downstream_200)
    scope = _base_scope(
        headers=[_auth_header(TOKEN)],
        mtls_info={"peer_cert": None, "peer_cert_der": None},
        state={})
    assert _status(_run_gate(gate, scope)) == 401


# --- the write path ----------------------------------------------------------

def test_bind_creates_a_row_readable_by_the_gate(registry, monkeypatch):
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    digest = credential_binding.sha256_token(TOKEN)
    row = credential_binding.bind(digest, MACHINE, note="issued for a test")
    assert row["machine"] == MACHINE
    assert row["note"] == "issued for a test"
    assert credential_binding._bound_machine(digest) == MACHINE


def test_bind_rebinds_rather_than_colliding(registry, monkeypatch):
    """The lockout recovery path: binding an already-bound credential to a
    new machine overwrites the row instead of raising a uniqueness error."""
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    digest = credential_binding.sha256_token(TOKEN)
    credential_binding.bind(digest, MACHINE)
    credential_binding.bind(digest, OTHER_MACHINE)
    assert credential_binding._bound_machine(digest) == OTHER_MACHINE
    conn = sqlite3.connect(registry)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM credential_bindings "
            "WHERE credential_sha256 = ?", (digest,)).fetchone()[0]
    finally:
        conn.close()
    assert n == 1, "rebinding left more than one row for the same credential"


def test_bind_refuses_while_writes_are_disabled(registry, monkeypatch):
    monkeypatch.setattr(store, "WRITES_ENABLED", False)
    with pytest.raises(store.WritesDisabled):
        credential_binding.bind("deadbeef", MACHINE)


def test_the_raw_token_never_reaches_the_database(registry, monkeypatch):
    monkeypatch.setattr(store, "WRITES_ENABLED", True)
    credential_binding.bind(credential_binding.sha256_token(TOKEN), MACHINE,
                            note="a note, not the token")
    raw = registry.read_bytes()
    assert TOKEN.encode() not in raw


# --- the two refusals that need a real handshake and a real connection -----

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
    """The actual FastAPI app, the real middleware chain — including
    `CredentialBindingGate` and `MachineIdentityGate` exactly as
    `app.py` registers them, not either gate constructed standalone."""
    import app as libapp
    import identity
    tokens = tmp_path / "api-tokens.json"
    tokens.write_text("{}")
    monkeypatch.setattr(identity, "TOKENS", tokens)
    return libapp.app


def _get_health(port, ctx, *, bearer=None):
    headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
    conn = http.client.HTTPSConnection("127.0.0.1", port, timeout=5,
                                       context=ctx)
    conn.request("GET", "/health", headers=headers)
    resp = conn.getresponse()
    resp.read()
    status = resp.status
    conn.close()
    return status


def test_a_bound_credential_from_its_own_machine_is_admitted(
        tmp_path, real_app, registry):
    ca_cert, ca_key, server_ctx = _ca_and_server(tmp_path)
    leaf_cert, leaf_key = tls_ca.make_cert(
        tmp_path, MACHINE, ca_cert, ca_key, uri_names=(MACHINE_URI,))
    fingerprint = hashlib.sha256(
        ssl.PEM_cert_to_DER_cert(leaf_cert.read_text())).hexdigest()
    _register_machine(registry, MACHINE, fingerprint)
    _register_binding(registry, TOKEN, MACHINE)

    server = RunningServer(tls_config(real_app, server_ctx))
    port = server.start()
    try:
        status = _get_health(
            port, _client_ctx(ca_cert, leaf_cert, leaf_key), bearer=TOKEN)
        assert status == 200
    finally:
        server.stop()


def test_the_same_credential_from_a_different_machine_is_refused(
        tmp_path, real_app, registry):
    """The exact attack platform/decisions/0066 names: a credential lifted
    off one machine must fail from another, even though that other
    machine's own certificate is perfectly valid and registered."""
    ca_cert, ca_key, server_ctx = _ca_and_server(tmp_path)
    other_cert, other_key = tls_ca.make_cert(
        tmp_path, OTHER_MACHINE, ca_cert, ca_key,
        uri_names=(OTHER_MACHINE_URI,))
    other_fingerprint = hashlib.sha256(
        ssl.PEM_cert_to_DER_cert(other_cert.read_text())).hexdigest()
    _register_machine(registry, OTHER_MACHINE, other_fingerprint)
    _register_binding(registry, TOKEN, MACHINE)  # bound to MACHINE, not OTHER

    server = RunningServer(tls_config(real_app, server_ctx))
    port = server.start()
    try:
        status = _get_health(
            port, _client_ctx(ca_cert, other_cert, other_key), bearer=TOKEN)
        assert status == 401
    finally:
        server.stop()


def test_an_unbound_credential_over_a_real_handshake_is_refused(
        tmp_path, real_app, registry):
    ca_cert, ca_key, server_ctx = _ca_and_server(tmp_path)
    leaf_cert, leaf_key = tls_ca.make_cert(
        tmp_path, MACHINE, ca_cert, ca_key, uri_names=(MACHINE_URI,))
    fingerprint = hashlib.sha256(
        ssl.PEM_cert_to_DER_cert(leaf_cert.read_text())).hexdigest()
    _register_machine(registry, MACHINE, fingerprint)
    # No credential_bindings row for TOKEN at all.

    server = RunningServer(tls_config(real_app, server_ctx))
    port = server.start()
    try:
        status = _get_health(
            port, _client_ctx(ca_cert, leaf_cert, leaf_key), bearer=TOKEN)
        assert status == 401
    finally:
        server.stop()


def test_the_plain_listener_is_unaffected_by_a_bound_or_unbound_credential(
        real_app, registry, monkeypatch):
    """The migration's hard constraint: a request over the plain listener
    behaves exactly as it does today, whatever `credential_bindings` says.
    Driven through `TestClient`, which never sets `mtls.EXTENSION_KEY` —
    the same shape a request arriving over 127.0.0.1:8100 has."""
    from fastapi.testclient import TestClient
    _register_binding(registry, TOKEN, MACHINE)
    client = TestClient(real_app)
    bound = client.get("/health", headers={"Authorization": f"Bearer {TOKEN}"})
    unbound = client.get("/health",
                         headers={"Authorization": "Bearer never-bound"})
    assert bound.status_code == 200
    assert unbound.status_code == 200
