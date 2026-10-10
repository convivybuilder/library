"""`mtls.MTLSH11Protocol`: the peer certificate reaches the ASGI scope, on a
real TLS 1.3 mutual handshake, for both the parsed form and the raw DER.

platform/decisions/0066. uvicorn 0.42.0 never reads `getpeercert()` on its
own, so this is the one piece of plumbing this whole feature is unbuildable
without — see the module docstring in `mtls.py` for the verification that
established that.
"""
from __future__ import annotations

import http.client
import ssl

import mtls
import tls_ca
from mtls_server import RunningServer, tls_config


def _serving_pair(dir_):
    """A CA and a `serverAuth` certificate for `localhost`/`127.0.0.1`, the
    two names a tunnelled client dials (platform/decisions/0066)."""
    ca_cert, ca_key = tls_ca.make_ca(dir_)
    server_cert, server_key = tls_ca.make_cert(
        dir_, "server", ca_cert, ca_key, dns_names=("localhost",),
        ip_names=("127.0.0.1",), ext_key_usage="serverAuth")
    return ca_cert, ca_key, server_cert, server_key


def _server_context(ca_cert, server_cert, server_key):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(server_cert), str(server_key))
    ctx.load_verify_locations(str(ca_cert))
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    return ctx


def _client_context(ca_cert, leaf_cert, leaf_key):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(str(ca_cert))
    ctx.load_cert_chain(str(leaf_cert), str(leaf_key))
    return ctx


def test_the_property_setter_stashes_both_forms_without_touching_handle_events():
    """The mechanism in isolation, with no socket at all: assigning `.scope`
    the way `H11Protocol.handle_events` does — a plain dict literal — comes
    back with both the parsed certificate and its DER under
    `mtls.EXTENSION_KEY`, however many times the attribute is assigned."""
    proto = mtls.MTLSH11Protocol.__new__(mtls.MTLSH11Protocol)
    proto._convivy_peer_cert = {"subjectAltName": (("URI", "x"),)}
    proto._convivy_peer_cert_der = b"raw-der-bytes"

    proto.scope = {"type": "http", "method": "GET"}
    stashed = proto.scope["extensions"][mtls.EXTENSION_KEY]
    assert stashed["peer_cert"] == {"subjectAltName": (("URI", "x"),)}
    assert stashed["peer_cert_der"] == b"raw-der-bytes"

    # A second request over the same (simulated) keep-alive connection reuses
    # the one handshake's certificate, unchanged.
    proto.scope = {"type": "http", "method": "POST"}
    assert (proto.scope["extensions"][mtls.EXTENSION_KEY]["peer_cert_der"]
            == b"raw-der-bytes")


def test_a_none_scope_assignment_is_left_alone():
    """`H11Protocol.__init__` does `self.scope = None` before any connection
    exists — the property setter must not explode on that, and must not
    invent an `extensions` dict for a scope that is not a request at all."""
    proto = mtls.MTLSH11Protocol.__new__(mtls.MTLSH11Protocol)
    proto.scope = None
    assert proto.scope is None


def test_the_der_and_parsed_certificate_both_reach_the_scope_over_a_real_handshake(
        tmp_path):
    """**The full path, not the property alone**: a real TLS 1.3 mutual
    handshake against `mtls.MTLSH11Protocol`, through `uvicorn.Server`,
    ending at an ASGI app that reads `scope['extensions'][mtls.EXTENSION_KEY]`
    the same way `machine_identity.MachineIdentityGate` does.
    """
    ca_cert, ca_key, server_cert, server_key = _serving_pair(tmp_path)
    leaf_cert, leaf_key = tls_ca.make_cert(
        tmp_path, "macbook", ca_cert, ca_key,
        uri_names=("convivy://machine/macbook",))

    seen = {}

    async def probe_app(scope, receive, send):
        seen.update(scope.get("extensions", {}).get(mtls.EXTENSION_KEY, {}))
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"ok"})

    server = RunningServer(tls_config(
        probe_app, _server_context(ca_cert, server_cert, server_key)))
    port = server.start()
    try:
        conn = http.client.HTTPSConnection(
            "127.0.0.1", port, timeout=5,
            context=_client_context(ca_cert, leaf_cert, leaf_key))
        conn.request("GET", "/")
        resp = conn.getresponse()
        assert resp.status == 200
        resp.read()
        conn.close()
    finally:
        server.stop()

    assert seen["peer_cert"]["subjectAltName"] == (
        ("URI", "convivy://machine/macbook"),)
    assert isinstance(seen["peer_cert_der"], bytes) and seen["peer_cert_der"]
