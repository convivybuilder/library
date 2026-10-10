"""`serve_dual`'s two `uvicorn.Config`s carry `timeout_graceful_shutdown`,
both of them.

The bug (security review, 2026-09-01, run-26526dae): a same-user process
sends the Library's pid a plain SIGTERM while one local request is held open
— no valid Library credential needed, since the body is readable after
admission but before auth — and uvicorn's graceful shutdown waits for the
in-flight request with no deadline, hanging the whole process (and the
credentialed Library with it) until a `kill -9`. `serve_dual` drives each
listener through uvicorn's own `shutdown()`, whose wait is bounded only by
`config.timeout_graceful_shutdown`; neither config set one.

The fix wires both configs to `app._DRAIN_DEADLINE_SECONDS` — the same bound
the quiesce drain already uses for the same wait over the same in-flight
requests (the derivation is in app.py's comment above that constant). This
asserts the wiring on the real configs the builders return. A timing test —
spawn the real process, hold a request open, SIGTERM, measure that it exits
inside that bound — would test uvicorn's own timeout machinery against a
wall clock; asserting the config value is what this repo owns, and it fails
meaningfully (it failed pre-fix: the value was None on both configs).

The TLS listener only exists when material is staged, so the TLS half stages
it exactly as `test_mtls_bind_preflight` does; with no material the builder
returns None by design and there is no second listener to hang anything.
"""
from __future__ import annotations

import ssl

import app
import mtls
import serve_dual
import store
import tls_ca

# `app._DRAIN_DEADLINE_SECONDS` is the single source the builders are wired
# to; asserting the built configs carry exactly it means a second literal
# anywhere drifts loudly rather than silently.
_BOUND = app._DRAIN_DEADLINE_SECONDS


def _staged_material(tmp_path):
    ca_cert, ca_key = tls_ca.make_ca(tmp_path)
    server_cert, server_key = tls_ca.make_cert(
        tmp_path, "server", ca_cert, ca_key, dns_names=("localhost",),
        ip_names=("127.0.0.1",), ext_key_usage="serverAuth")
    return ca_cert, server_cert, server_key


def test_plain_config_carries_the_graceful_shutdown_bound():
    config = serve_dual._build_plain_config()

    assert config.timeout_graceful_shutdown is not None
    assert config.timeout_graceful_shutdown == _BOUND


def test_tls_config_carries_the_graceful_shutdown_bound(monkeypatch, tmp_path):
    ca_cert, server_cert, server_key = _staged_material(tmp_path)
    monkeypatch.setattr(mtls, "TLS_CERT", server_cert)
    monkeypatch.setattr(mtls, "TLS_KEY", server_key)
    monkeypatch.setattr(mtls, "TLS_CA", ca_cert)
    monkeypatch.setattr(store, "REQUEST_DEADLINE_SECONDS", 60.0)

    config = serve_dual._build_tls_config()

    assert config is not None
    assert isinstance(config.ssl, ssl.SSLContext)  # armed, not a None path
    assert config.timeout_graceful_shutdown is not None
    assert config.timeout_graceful_shutdown == _BOUND
