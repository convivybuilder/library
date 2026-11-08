"""Both mutual-TLS listeners keep an idle connection open for 120 s; the
plain listener keeps uvicorn's default.

Deck's machine key is presence-gated (platform/decisions/0065), so every
TLS handshake made after its 300 s approval window has lapsed shows the
operator a prompt. Deck heartbeats every 60 s; the Library holding an idle
mutual-TLS connection for 120 s keeps that connection open, so the prompt
comes only on a real reconnect. The comment above
`serve_dual._TLS_IDLE_TIMEOUT_SECONDS` gives the reasoning and the scope.

These assert the value on the configs the builders return, as
`test_serve_dual_graceful_shutdown` does for its bound: uvicorn's own idle
timer is uvicorn's to test, and the wiring is what this repo owns.
"""
from __future__ import annotations

import ssl

import tls_ca
import uvicorn

import mtls
import serve_dual
import store


def _stage(monkeypatch, tmp_path):
    ca_cert, ca_key = tls_ca.make_ca(tmp_path)
    server_cert, server_key = tls_ca.make_cert(
        tmp_path, "server", ca_cert, ca_key, dns_names=("localhost",),
        ip_names=("127.0.0.1", "192.0.2.10"), ext_key_usage="serverAuth")
    monkeypatch.setattr(mtls, "TLS_CERT", server_cert)
    monkeypatch.setattr(mtls, "TLS_KEY", server_key)
    monkeypatch.setattr(mtls, "TLS_CA", ca_cert)
    monkeypatch.setattr(store, "REQUEST_DEADLINE_SECONDS", 60.0)


def test_the_idle_timeout_is_twice_decks_sixty_second_heartbeat():
    assert serve_dual._TLS_IDLE_TIMEOUT_SECONDS == 120


def test_loopback_tls_config_holds_an_idle_connection_for_the_tls_timeout(
        monkeypatch, tmp_path):
    _stage(monkeypatch, tmp_path)

    config = serve_dual._build_tls_config()

    assert config is not None
    assert isinstance(config.ssl, ssl.SSLContext)
    assert config.timeout_keep_alive == serve_dual._TLS_IDLE_TIMEOUT_SECONDS


def test_remote_tls_config_holds_an_idle_connection_for_the_tls_timeout(
        monkeypatch, tmp_path):
    _stage(monkeypatch, tmp_path)
    monkeypatch.setattr(mtls, "TLS_HOST", "192.0.2.10")

    loopback = serve_dual._build_tls_config()
    remote = serve_dual._build_tls_remote_config(loopback.ssl)

    assert remote is not None
    assert remote.timeout_keep_alive == serve_dual._TLS_IDLE_TIMEOUT_SECONDS


def test_plain_config_keeps_uvicorns_default_idle_timeout():
    """The plain listener has no handshake to save, so it is left alone.
    Compared against uvicorn's own default rather than a literal, so an
    upstream change to that default does not read as this repo's."""
    default = uvicorn.Config(app=lambda *a: None).timeout_keep_alive

    config = serve_dual._build_plain_config()

    assert config.timeout_keep_alive == default
    assert config.timeout_keep_alive != serve_dual._TLS_IDLE_TIMEOUT_SECONDS
