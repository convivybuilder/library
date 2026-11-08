"""`runs.py`'s remote transport: the Library reads the Bosun's run register
over mutual TLS when the Bosun runs on another host
(issue_01M5FMPE1Y2DQPBHYQ0P0J6VYW, the Library's part of the steward's
multi-machine install epic).

**Two layers, the same split `test_machine_identity_gate.py` uses for the
inbound gate.** The registry decision — active row, revoked row, a
fingerprint registered under a different machine name, no certificate at
all — is `_verify_remote_machine`'s own logic and needs no live socket, so
those run against a hand-built stand-in for the connection. What a real
handshake alone can prove — that a certificate failing the chain to the
Library's own root is refused before the registry is ever consulted, and
that a genuinely good certificate is answered end to end — runs against a
real TLS listener built the same way `tests/mtls_server.py` and `tls_ca.py`
already build one, over a real TCP socket this time rather than the Unix
one `test_runs.py`'s `FakeRegister` uses.

**A live second host is out of scope here**, the same carve-out
`test_mtls_remote_host.py` already states for this epic's prior Library
piece: this module can prove the transport works against a throwaway CA and
a loopback TCP listener standing in for the Bosun, not that a real second
machine answers. See the PR body for what remains live-only.
"""
from __future__ import annotations

import json
import shutil
import socket
import sqlite3
import ssl
import tempfile
import threading
import time
from pathlib import Path

import pytest

import machine_identity
import mtls
import runs
import store
import tls_ca

MACHINE = "host-b"


# --- shared fixtures --------------------------------------------------------

@pytest.fixture
def registry(tmp_path, monkeypatch):
    """A real `machine_certificates` table, the same DDL
    `test_machine_identity_gate.py`'s own fixture builds — this feature
    reads the identical table through `machine_registry.status_for`."""
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


def _register(db, machine, fingerprint, *, status="active"):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO machine_certificates "
        "(machine, fingerprint_sha256, not_after, status, created_at, custody) "
        "VALUES (?, ?, '2099-01-01', ?, '2026-01-01', 'hardware')",
        (machine, fingerprint, status))
    conn.commit()
    conn.close()


def _digest(der: bytes) -> str:
    return machine_identity.sha256_der(der)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    """Every test owns its own remote setting and its own cache — a stale
    snapshot or a leaked env var from one test must not decide another's
    transport."""
    monkeypatch.setattr(runs, "REMOTE_HOST", "")
    monkeypatch.setattr(runs, "REMOTE_MACHINE", "")
    monkeypatch.setattr(runs, "REMOTE_PORT", mtls.TLS_PORT)
    monkeypatch.setattr(runs, "CLIENT_CERT", "")
    monkeypatch.setattr(runs, "CLIENT_KEY", "")
    runs.reset_cache()
    yield
    runs.reset_cache()


# --- `_verify_remote_machine`, in isolation (no socket needed) -------------

class _StubConn:
    """Stands in for the connected `ssl.SSLSocket` `_verify_remote_machine`
    reads — only `getpeercert(binary_form=True)` is ever called on it, so a
    stub exposing exactly that is enough to exercise the registry decision
    without a live handshake."""

    def __init__(self, der: bytes | None):
        self._der = der

    def getpeercert(self, binary_form=False):
        assert binary_form is True
        return self._der


def test_verify_remote_machine_accepts_the_active_row_for_the_configured_machine(
        registry, monkeypatch, tmp_path):
    monkeypatch.setattr(runs, "REMOTE_MACHINE", MACHINE)
    ca_cert, ca_key = tls_ca.make_ca(tmp_path)
    cert, _ = tls_ca.make_cert(tmp_path, "bosun-host", ca_cert, ca_key,
                               ext_key_usage="serverAuth")
    der = ssl.PEM_cert_to_DER_cert(cert.read_text())
    _register(registry, MACHINE, _digest(der))
    runs._verify_remote_machine(_StubConn(der))  # does not raise


def test_verify_remote_machine_refuses_a_revoked_row(registry, monkeypatch,
                                                      tmp_path):
    monkeypatch.setattr(runs, "REMOTE_MACHINE", MACHINE)
    ca_cert, ca_key = tls_ca.make_ca(tmp_path)
    cert, _ = tls_ca.make_cert(tmp_path, "bosun-host", ca_cert, ca_key,
                               ext_key_usage="serverAuth")
    der = ssl.PEM_cert_to_DER_cert(cert.read_text())
    _register(registry, MACHINE, _digest(der), status="revoked")
    with pytest.raises(runs._RemoteRegisterRefused):
        runs._verify_remote_machine(_StubConn(der))


def test_verify_remote_machine_refuses_a_certificate_for_another_machine_name(
        registry, monkeypatch, tmp_path):
    """The fingerprint is registered and active — just not under the
    machine name this read is configured to expect. The exact shape
    platform/decisions/0066 names: a row is pinned to `(machine,
    fingerprint)` together, not the fingerprint alone."""
    monkeypatch.setattr(runs, "REMOTE_MACHINE", MACHINE)
    ca_cert, ca_key = tls_ca.make_ca(tmp_path)
    cert, _ = tls_ca.make_cert(tmp_path, "someone-elses-host", ca_cert, ca_key,
                               ext_key_usage="serverAuth")
    der = ssl.PEM_cert_to_DER_cert(cert.read_text())
    _register(registry, "a-different-machine", _digest(der))
    with pytest.raises(runs._RemoteRegisterRefused):
        runs._verify_remote_machine(_StubConn(der))


def test_verify_remote_machine_refuses_an_unregistered_fingerprint(
        registry, monkeypatch, tmp_path):
    monkeypatch.setattr(runs, "REMOTE_MACHINE", MACHINE)
    ca_cert, ca_key = tls_ca.make_ca(tmp_path)
    cert, _ = tls_ca.make_cert(tmp_path, "bosun-host", ca_cert, ca_key,
                               ext_key_usage="serverAuth")
    der = ssl.PEM_cert_to_DER_cert(cert.read_text())
    with pytest.raises(runs._RemoteRegisterRefused):
        runs._verify_remote_machine(_StubConn(der))


def test_verify_remote_machine_refuses_no_peer_certificate(monkeypatch):
    monkeypatch.setattr(runs, "REMOTE_MACHINE", MACHINE)
    with pytest.raises(runs._RemoteRegisterRefused):
        runs._verify_remote_machine(_StubConn(None))


def test_verify_remote_machine_fails_closed_on_an_unreadable_registry(
        monkeypatch, tmp_path):
    """The same fail-closed shape `machine_identity._registry_status` uses
    for an unreadable database — `machine_registry.status_for` itself does
    not catch `sqlite3.Error`, so this module's own call site must."""
    monkeypatch.setattr(runs, "REMOTE_MACHINE", MACHINE)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "does-not-exist.db")
    ca_cert, ca_key = tls_ca.make_ca(tmp_path)
    cert, _ = tls_ca.make_cert(tmp_path, "bosun-host", ca_cert, ca_key,
                               ext_key_usage="serverAuth")
    der = ssl.PEM_cert_to_DER_cert(cert.read_text())
    with pytest.raises(runs._RemoteRegisterRefused):
        runs._verify_remote_machine(_StubConn(der))


# --- the socket path, when the remote setting is absent --------------------

def test_the_socket_path_is_unchanged_when_the_setting_is_absent(
        monkeypatch):
    """`REMOTE_HOST`/`REMOTE_MACHINE` unset is the default this whole
    feature must leave alone: `_dial_register` takes the exact branch it
    took before this feature existed, never the mutual-TLS one.

    The socket lives in a short `/tmp` directory, as `test_runs.py`'s
    `sock_dir` does: under pytest's `tmp_path` macOS raises "AF_UNIX path
    too long" before it looks for the file."""
    assert runs._remote_configured() is False
    sock_dir = tempfile.mkdtemp(prefix="libruns-", dir="/tmp")
    try:
        monkeypatch.setattr(runs, "SOCKET_PATH", Path(sock_dir) / "missing.sock")
        with pytest.raises(FileNotFoundError):
            runs._dial_register()
    finally:
        shutil.rmtree(sock_dir, ignore_errors=True)


def test_a_configured_remote_never_falls_back_to_the_socket(
        monkeypatch, tmp_path):
    """No cleartext fallback: a remote that is configured but whose client
    certificate material is missing refuses outright rather than trying
    `SOCKET_PATH`, even when a register is listening there."""
    monkeypatch.setattr(runs, "REMOTE_HOST", "127.0.0.1")
    monkeypatch.setattr(runs, "REMOTE_MACHINE", MACHINE)
    monkeypatch.setattr(runs, "CLIENT_CERT", str(tmp_path / "no-such-cert.crt"))
    monkeypatch.setattr(runs, "CLIENT_KEY", str(tmp_path / "no-such-key.key"))
    monkeypatch.setattr(mtls, "TLS_CA", tmp_path / "no-such-ca.crt")
    monkeypatch.setattr(runs, "SOCKET_PATH", tmp_path / "unreachable.sock")
    with pytest.raises(runs._RemoteRegisterRefused):
        runs._dial_register()


def test_a_trust_anchor_failing_the_walk_builds_no_client_context(
        tmp_path, monkeypatch):
    """The outbound context holds `TLS_CA` to the same owner/group/mode walk
    the inbound listener does: with loadable material, the context is built
    when the walk passes and refused when it fails."""
    ca_cert, ca_key = tls_ca.make_ca(tmp_path)
    client_cert, client_key = _client_material(tmp_path, ca_cert, ca_key)
    monkeypatch.setattr(runs, "CLIENT_CERT", str(client_cert))
    monkeypatch.setattr(runs, "CLIENT_KEY", str(client_key))
    monkeypatch.setattr(mtls, "TLS_CA", ca_cert)
    monkeypatch.setattr(mtls, "_trust_anchor_walk_ok", lambda: True)
    assert runs._build_client_ssl_context() is not None
    monkeypatch.setattr(mtls, "_trust_anchor_walk_ok", lambda: False)
    assert runs._build_client_ssl_context() is None


def test_a_serverauth_client_certificate_builds_no_client_context(
        tmp_path, monkeypatch):
    """The Library's listener certificate is issued for `serverAuth` only,
    and a Bosun holding callers to `clientAuth` would refuse it, so the
    context refuses it first. The same chain with `clientAuth` is built."""
    ca_cert, ca_key = tls_ca.make_ca(tmp_path)
    monkeypatch.setattr(mtls, "TLS_CA", ca_cert)
    monkeypatch.setattr(mtls, "_trust_anchor_walk_ok", lambda: True)
    server_cert, server_key = tls_ca.make_cert(
        tmp_path, "library-listener", ca_cert, ca_key,
        ext_key_usage="serverAuth")
    monkeypatch.setattr(runs, "CLIENT_CERT", str(server_cert))
    monkeypatch.setattr(runs, "CLIENT_KEY", str(server_key))
    assert runs._build_client_ssl_context() is None
    client_cert, client_key = _client_material(tmp_path, ca_cert, ca_key)
    monkeypatch.setattr(runs, "CLIENT_CERT", str(client_cert))
    monkeypatch.setattr(runs, "CLIENT_KEY", str(client_key))
    assert runs._build_client_ssl_context() is not None


def test_the_client_context_is_built_off_the_request_path(
        tmp_path, monkeypatch):
    """The walk is a subprocess with a 30-second timeout, so a dial never
    runs it: the first call returns `None` at once and starts one
    background build; later calls return the built context without walking
    again; `reset_cache()` starts over."""
    ca_cert, ca_key = tls_ca.make_ca(tmp_path)
    client_cert, client_key = _client_material(tmp_path, ca_cert, ca_key)
    monkeypatch.setattr(runs, "CLIENT_CERT", str(client_cert))
    monkeypatch.setattr(runs, "CLIENT_KEY", str(client_key))
    monkeypatch.setattr(mtls, "TLS_CA", ca_cert)
    walks = []
    release = threading.Event()

    def slow_walk():
        walks.append(1)
        release.wait(5)
        return True

    monkeypatch.setattr(mtls, "_trust_anchor_walk_ok", slow_walk)
    assert runs._client_ssl_context() is None
    assert runs._client_ssl_context() is None
    release.set()
    _wait_for(lambda: runs._client_ssl_context() is not None)
    first = runs._client_ssl_context()
    assert runs._client_ssl_context() is first
    assert len(walks) == 1
    runs.reset_cache()
    assert runs._client_ssl_context() is None
    _wait_for(lambda: runs._client_ssl_context() is not None)
    assert len(walks) == 2


def test_a_refused_build_is_retried_after_the_back_off(tmp_path, monkeypatch):
    """A refusal is not kept for the process: within
    `CLIENT_CTX_RETRY_SECONDS` no new walk starts, and after it the next
    dial builds again."""
    ca_cert, ca_key = tls_ca.make_ca(tmp_path)
    client_cert, client_key = _client_material(tmp_path, ca_cert, ca_key)
    monkeypatch.setattr(runs, "CLIENT_CERT", str(client_cert))
    monkeypatch.setattr(runs, "CLIENT_KEY", str(client_key))
    monkeypatch.setattr(mtls, "TLS_CA", ca_cert)
    outcomes = [False, True]
    walks = []
    monkeypatch.setattr(mtls, "_trust_anchor_walk_ok",
                        lambda: walks.append(1) or outcomes[len(walks) - 1])
    runs._warm_client_ssl_context()
    assert runs._client_ssl_context() is None
    assert len(walks) == 1
    monkeypatch.setattr(runs, "CLIENT_CTX_RETRY_SECONDS", 0.0)
    assert runs._client_ssl_context() is None
    _wait_for(lambda: runs._client_ssl_context() is not None)
    assert len(walks) == 2


def test_a_build_that_raises_is_a_refusal_and_is_retried(monkeypatch):
    """An unexpected exception in the build (a file removed between the
    existence check and the load) must not leave a build marked running
    forever, which would stop every later dial from building."""
    def boom():
        raise OSError("removed mid-build")

    monkeypatch.setattr(runs, "_build_client_ssl_context", boom)
    runs._warm_client_ssl_context()
    monkeypatch.setattr(runs, "CLIENT_CTX_RETRY_SECONDS", 0.0)
    calls = []
    monkeypatch.setattr(runs, "_build_client_ssl_context",
                        lambda: calls.append(1) or None)
    runs._client_ssl_context()
    _wait_for(lambda: calls)


def _wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.01)


def test_a_configured_remote_with_no_client_certificate_set_refuses(
        monkeypatch):
    """No default client certificate: a remote configured without
    `LIBRARY_RUN_REGISTER_CLIENT_CERT` refuses to dial."""
    monkeypatch.setattr(runs, "REMOTE_HOST", "127.0.0.1")
    monkeypatch.setattr(runs, "REMOTE_MACHINE", MACHINE)
    with pytest.raises(runs._RemoteRegisterRefused):
        runs._dial_register()


def test_a_failed_socket_connect_closes_the_socket(monkeypatch):
    """A stale socket file (the Bosun crashed or is restarting) fails the
    connect; the socket `_dial_register` opened is closed then, as `main`'s
    `finally` closed it, rather than left for garbage collection."""
    opened = []
    real_socket = socket.socket

    def recording_socket(*args, **kwargs):
        s = real_socket(*args, **kwargs)
        opened.append(s)
        return s

    sock_dir = tempfile.mkdtemp(prefix="libruns-", dir="/tmp")
    try:
        monkeypatch.setattr(runs, "SOCKET_PATH", Path(sock_dir) / "missing.sock")
        monkeypatch.setattr(runs.socket, "socket", recording_socket)
        with pytest.raises(OSError):
            runs._dial_register()
    finally:
        shutil.rmtree(sock_dir, ignore_errors=True)
    assert len(opened) == 1
    assert opened[0].fileno() == -1


# --- real handshakes: what only a live TLS connection can prove ------------
#
# **Needs a bound TCP listener, the same way `test_machine_identity_gate.py`'s
# three real-handshake tests need one and `RunningServer` supplies it for the
# inbound direction.** `FakeRemoteRegister` below is this direction's
# equivalent: a real `ssl`-wrapped TCP server standing in for the Bosun, built
# the same way `tests/mtls_server.py` builds the Library's own listener for
# its tests, just serving raw HTTP-over-TLS the way `test_runs.py`'s
# `FakeRegister` serves HTTP-over-UNIX rather than running uvicorn.

class FakeRemoteRegister:
    """A stand-in for the Bosun's HTTPS register: a real TCP socket, TLS
    1.3, a client certificate required — speaking the same one-shot
    HTTP-over-a-connection shape `test_runs.py`'s `FakeRegister` does."""

    def __init__(self, ssl_context, payload=None, *, behaviour="ok"):
        self.payload = payload
        self.behaviour = behaviour
        self.reads = 0
        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw.bind(("127.0.0.1", 0))
        raw.listen(8)
        self.port = raw.getsockname()[1]
        self._sock = ssl_context.wrap_socket(raw, server_side=True)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            self.reads += 1
            try:
                conn.recv(65536)
                if self.behaviour == "garbage":
                    conn.sendall(b"HTTP/1.0 200 OK\r\n\r\nnot json at all")
                else:
                    conn.sendall(b"HTTP/1.0 200 OK\r\n\r\n" +
                                 json.dumps(self.payload).encode())
            except OSError:
                pass
            finally:
                conn.close()

    def close(self):
        self._stop.set()
        self._sock.close()


def _ca_and_server_ctx(dir_, *, name="bosun-host"):
    ca_cert, ca_key = tls_ca.make_ca(dir_)
    server_cert, server_key = tls_ca.make_cert(
        dir_, name, ca_cert, ca_key, ext_key_usage="serverAuth")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(server_cert), str(server_key))
    ctx.load_verify_locations(str(ca_cert))  # mutual: a client cert is required
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    return ca_cert, ca_key, server_cert, ctx


def _client_material(dir_, ca_cert, ca_key):
    client_cert, client_key = tls_ca.make_cert(
        dir_, "library-host", ca_cert, ca_key, ext_key_usage="clientAuth")
    return client_cert, client_key


def test_a_good_certificate_over_a_real_handshake_is_answered(
        tmp_path, registry, monkeypatch):
    """The positive case, end to end: a server certificate that chains to
    the configured root and whose fingerprint is the active registry row
    for the configured machine name is answered exactly as the Unix-socket
    transport already is."""
    ca_cert, ca_key, server_cert, server_ctx = _ca_and_server_ctx(tmp_path)
    client_cert, client_key = _client_material(tmp_path, ca_cert, ca_key)
    der = ssl.PEM_cert_to_DER_cert(server_cert.read_text())
    _register(registry, MACHINE, _digest(der))

    monkeypatch.setattr(runs, "CLIENT_CERT", str(client_cert))
    monkeypatch.setattr(runs, "CLIENT_KEY", str(client_key))
    monkeypatch.setattr(mtls, "TLS_CA", ca_cert)
    monkeypatch.setattr(runs, "REMOTE_HOST", "127.0.0.1")
    monkeypatch.setattr(runs, "REMOTE_MACHINE", MACHINE)
    # Built before the dial, so a refusal below is the handshake's and not
    # "no context yet".
    runs._warm_client_ssl_context()
    assert runs._client_ssl_context() is not None

    reg = FakeRemoteRegister(server_ctx, {
        "generated_at": "2026-10-23T00:00:00Z",
        "runs": [{"run_id": "run-aaaa1111", "agent": "coder"}]})
    monkeypatch.setattr(runs, "REMOTE_PORT", reg.port)
    try:
        body = runs.snapshot()
        assert body["available"] is True
        assert body["runs"][0]["run_id"] == "run-aaaa1111"
    finally:
        reg.close()


def test_a_certificate_not_signed_by_the_configured_root_is_refused(
        tmp_path, registry, monkeypatch):
    """Chains to a root the Library does NOT trust: refused before the
    registry is ever consulted — platform/decisions/0066's "a signature
    from the root is necessary" half, proven the other way round from
    `test_a_good_certificate_over_a_real_handshake_is_answered`'s "and not
    sufficient" half above."""
    trusted_ca_cert, trusted_ca_key = tls_ca.make_ca(tmp_path, cn="trusted CA")
    client_cert, client_key = _client_material(
        tmp_path, trusted_ca_cert, trusted_ca_key)

    # The server's own certificate comes from an UNRELATED CA — the
    # Library's configured trust anchor (`trusted_ca_cert`) never signed it.
    other_ca_cert, other_ca_key, server_cert, server_ctx = _ca_and_server_ctx(
        tmp_path, name="impostor-host")
    # The impostor's server context must still accept the real client
    # certificate to isolate this test to the SERVER side of the chain
    # check; it trusts the same CA the client's certificate chains to.
    server_ctx.load_verify_locations(str(trusted_ca_cert))

    monkeypatch.setattr(runs, "CLIENT_CERT", str(client_cert))
    monkeypatch.setattr(runs, "CLIENT_KEY", str(client_key))
    monkeypatch.setattr(mtls, "TLS_CA", trusted_ca_cert)
    monkeypatch.setattr(runs, "REMOTE_HOST", "127.0.0.1")
    monkeypatch.setattr(runs, "REMOTE_MACHINE", MACHINE)
    # Built before the dial, so a refusal below is the handshake's and not
    # "no context yet".
    runs._warm_client_ssl_context()
    assert runs._client_ssl_context() is not None

    # Registered as if it were trusted — proving the refusal is the chain
    # check, not an absent registry row.
    der = ssl.PEM_cert_to_DER_cert(server_cert.read_text())
    _register(registry, MACHINE, _digest(der))

    reg = FakeRemoteRegister(server_ctx, {"runs": []})
    monkeypatch.setattr(runs, "REMOTE_PORT", reg.port)
    try:
        body = runs.snapshot()
        assert body["available"] is False
    finally:
        reg.close()
