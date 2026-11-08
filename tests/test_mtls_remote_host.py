"""The remote binding: `LIBRARY_TLS_HOST` adds a second mutual-TLS server
alongside the loopback one, and the plain listener's refusal to start
anywhere but loopback.

issue_01M5FMP5GSE61PVQ4H96RK23FN, the Library's part of the steward's
multi-machine install epic. The mutual-TLS listener already enforces
`machine_identity.MachineIdentityGate` on every request it serves
(platform/decisions/0066) — that gate is keyed on `mtls.EXTENSION_KEY`,
which `mtls.MTLSH11Protocol` sets on every connection it builds regardless
of which address the listener using that protocol class is bound to, so a
second TLS listener at a second address does not, by itself, change what
the gate enforces. What this file covers is the binding itself: which
host(s) each listener resolves to, which port the remote one binds, and the
plain listener's own startup refusal, which is the one piece of this change
that is new logic rather than an existing gate applied to a wider address.

A live second host, a real revocation over that binding, and
`lsof -iTCP -sTCP:LISTEN` against the running service are not reachable
from this suite — they need a second enrolled machine and the live
service, neither of which exists here. See the PR body for which
done-when bullets this file covers and which stay live-only.
"""
from __future__ import annotations

import pytest

import mtls
import serve_dual


# --- `mtls.is_loopback_host` -----------------------------------------------


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1", "localhost"])
def test_loopback_addresses_are_recognised(host):
    assert mtls.is_loopback_host(host) is True


@pytest.mark.parametrize("host", [
    "0.0.0.0",
    "192.0.2.10",      # a documentation address standing in for a remote host
    "192.168.1.5",
    "::",
    "example.com",      # a hostname is not loopback merely because it might
                         # resolve there — this check must not depend on DNS
    "not an address at all",
])
def test_non_loopback_hosts_are_not_recognised(host):
    assert mtls.is_loopback_host(host) is False


# --- `mtls.resolve_plain_host`: unset means loopback -----------------------


def test_plain_host_unset_means_loopback():
    assert mtls.resolve_plain_host({}) == mtls.HOST
    assert mtls.is_loopback_host(mtls.resolve_plain_host({})) is True


def test_plain_host_honours_the_environment_variable_when_set():
    assert mtls.resolve_plain_host({"LIBRARY_PLAIN_HOST": "127.0.0.2"}) == "127.0.0.2"


# --- `mtls.resolve_tls_host`: unset means no remote listener ---------------


def test_tls_host_unset_means_no_remote_listener():
    assert mtls.resolve_tls_host({}) is None


def test_tls_host_honours_the_environment_variable_when_set():
    assert mtls.resolve_tls_host(
        {"LIBRARY_TLS_HOST": "192.0.2.10"}) == "192.0.2.10"


def test_tls_host_keeps_an_explicitly_empty_value_distinct_from_unset():
    """`require_remote_tls_host` refuses an empty `LIBRARY_TLS_HOST`
    differently from an absent one — this is what lets it tell the two
    apart."""
    assert mtls.resolve_tls_host({"LIBRARY_TLS_HOST": ""}) == ""
    assert mtls.resolve_tls_host({}) is None


# --- `mtls.resolve_tls_remote_port`: unset means `TLS_PORT` -----------------


def test_tls_remote_port_unset_means_tls_port():
    assert mtls.resolve_tls_remote_port({}) == mtls.TLS_PORT


def test_tls_remote_port_honours_the_environment_variable_when_set():
    assert mtls.resolve_tls_remote_port(
        {"LIBRARY_TLS_REMOTE_PORT": "9443"}) == 9443


# --- `mtls.require_loopback_plain_host`: the plain listener's own refusal -


def test_require_loopback_plain_host_passes_loopback_silently():
    mtls.require_loopback_plain_host("127.0.0.1")  # no raise


def test_require_loopback_plain_host_passes_localhost_by_name():
    mtls.require_loopback_plain_host("localhost")  # no raise


def test_require_loopback_plain_host_refuses_a_non_loopback_address(capsys):
    """The done-when bullet this PR ships a test for: setting the plain
    listener's host to a non-loopback address stops the service with a
    message naming why."""
    with pytest.raises(mtls.NonLoopbackPlainHost) as exc:
        mtls.require_loopback_plain_host("192.0.2.10")
    assert "192.0.2.10" in str(exc.value)
    logged = capsys.readouterr().err
    assert "192.0.2.10" in logged
    assert "not a loopback address" in logged


def test_require_loopback_plain_host_refuses_the_unspecified_address():
    """`0.0.0.0` is the mistake this guard exists to catch first: it is not
    loopback, and binding it would serve cleartext on every interface."""
    with pytest.raises(mtls.NonLoopbackPlainHost):
        mtls.require_loopback_plain_host("0.0.0.0")


# --- `mtls.require_remote_tls_host`: the remote listener's own refusal ----


def test_require_remote_tls_host_passes_a_real_remote_address_silently():
    mtls.require_remote_tls_host("192.0.2.10")  # no raise


def test_require_remote_tls_host_refuses_a_loopback_address(capsys):
    """The loopback mutual-TLS listener already binds unconditionally, so a
    `LIBRARY_TLS_HOST` naming it too would only duplicate it."""
    with pytest.raises(mtls.NonRemoteTlsHost) as exc:
        mtls.require_remote_tls_host("127.0.0.1")
    assert "127.0.0.1" in str(exc.value)
    logged = capsys.readouterr().err
    assert "127.0.0.1" in logged
    assert "loopback" in logged


def test_require_remote_tls_host_refuses_an_empty_address(capsys):
    with pytest.raises(mtls.NonRemoteTlsHost) as exc:
        mtls.require_remote_tls_host("")
    assert "empty" in str(exc.value)
    logged = capsys.readouterr().err
    assert "empty" in logged


# --- `serve_dual._build_plain_config`: the refusal actually wired in ------


def test_build_plain_config_refuses_a_non_loopback_plain_host(monkeypatch):
    monkeypatch.setattr(mtls, "PLAIN_HOST", "192.0.2.10")
    with pytest.raises(mtls.NonLoopbackPlainHost):
        serve_dual._build_plain_config()


def test_build_plain_config_binds_plain_host_when_loopback(monkeypatch):
    monkeypatch.setattr(mtls, "PLAIN_HOST", "127.0.0.2")
    config = serve_dual._build_plain_config()
    assert config.host == "127.0.0.2"


def test_build_plain_config_binds_loopback_by_default():
    config = serve_dual._build_plain_config()
    assert config.host == mtls.HOST
    assert mtls.is_loopback_host(config.host)


# --- `serve_dual._build_tls_config`: loopback, unconditionally -------------


def test_build_tls_config_binds_loopback_regardless_of_tls_host(monkeypatch, tmp_path):
    """The loopback mutual-TLS listener binds `mtls.HOST` whether or not
    `LIBRARY_TLS_HOST` is set — setting it adds a second server
    (`_build_tls_remote_config` below) rather than moving this one."""
    import tls_ca
    ca_cert, ca_key = tls_ca.make_ca(tmp_path)
    server_cert, server_key = tls_ca.make_cert(
        tmp_path, "server", ca_cert, ca_key, dns_names=("localhost",),
        ip_names=("127.0.0.1",), ext_key_usage="serverAuth")
    monkeypatch.setattr(mtls, "TLS_CERT", server_cert)
    monkeypatch.setattr(mtls, "TLS_KEY", server_key)
    monkeypatch.setattr(mtls, "TLS_CA", ca_cert)
    monkeypatch.setattr(mtls, "TLS_HOST", "192.0.2.10")

    config = serve_dual._build_tls_config()

    assert config is not None
    assert config.host == mtls.HOST


def test_build_tls_config_binds_loopback_by_default(monkeypatch, tmp_path):
    import tls_ca
    ca_cert, ca_key = tls_ca.make_ca(tmp_path)
    server_cert, server_key = tls_ca.make_cert(
        tmp_path, "server", ca_cert, ca_key, dns_names=("localhost",),
        ip_names=("127.0.0.1",), ext_key_usage="serverAuth")
    monkeypatch.setattr(mtls, "TLS_CERT", server_cert)
    monkeypatch.setattr(mtls, "TLS_KEY", server_key)
    monkeypatch.setattr(mtls, "TLS_CA", ca_cert)

    config = serve_dual._build_tls_config()

    assert config is not None
    assert config.host == mtls.HOST


# --- `serve_dual._build_tls_remote_config`: the additive remote binding ----


def test_build_tls_remote_config_is_none_when_tls_host_unset(monkeypatch):
    monkeypatch.setattr(mtls, "TLS_HOST", None)
    assert serve_dual._build_tls_remote_config(object()) is None


def test_build_tls_remote_config_refuses_a_loopback_host(monkeypatch):
    monkeypatch.setattr(mtls, "TLS_HOST", "127.0.0.1")
    with pytest.raises(mtls.NonRemoteTlsHost):
        serve_dual._build_tls_remote_config(object())


def test_build_tls_remote_config_refuses_an_empty_host(monkeypatch):
    monkeypatch.setattr(mtls, "TLS_HOST", "")
    with pytest.raises(mtls.NonRemoteTlsHost):
        serve_dual._build_tls_remote_config(object())


def _server_cert(monkeypatch, tmp_path, *, dns_names=("localhost",),
                 ip_names=("127.0.0.1",)):
    """Stage a CA and a server certificate with the given names as
    `mtls.TLS_CERT`/`TLS_KEY`/`TLS_CA`."""
    import tls_ca
    ca_cert, ca_key = tls_ca.make_ca(tmp_path)
    server_cert, server_key = tls_ca.make_cert(
        tmp_path, "server", ca_cert, ca_key, dns_names=dns_names,
        ip_names=ip_names, ext_key_usage="serverAuth")
    monkeypatch.setattr(mtls, "TLS_CERT", server_cert)
    monkeypatch.setattr(mtls, "TLS_KEY", server_key)
    monkeypatch.setattr(mtls, "TLS_CA", ca_cert)


def test_build_tls_remote_config_binds_the_configured_host_and_port(
        monkeypatch, tmp_path):
    """The remote listener's config carries whatever `mtls.TLS_HOST` names,
    and `mtls.TLS_REMOTE_PORT` rather than `mtls.TLS_PORT` — the setting
    this issue adds so a reverse proxy already on the remote address's
    `TLS_PORT` (such as `tailscale serve` on the tailnet address) does not collide
    with it."""
    _server_cert(monkeypatch, tmp_path,
                 ip_names=("127.0.0.1", "192.0.2.10"))
    monkeypatch.setattr(mtls, "TLS_HOST", "192.0.2.10")
    monkeypatch.setattr(mtls, "TLS_REMOTE_PORT", 9443)
    sentinel_ctx = object()

    config = serve_dual._build_tls_remote_config(sentinel_ctx)

    assert config is not None
    assert config.host == "192.0.2.10"
    assert config.port == 9443
    assert config.ssl is sentinel_ctx
    assert config.http is mtls.MTLSH11Protocol


def test_loopback_and_remote_tls_configs_share_the_same_context_and_protocol(
        monkeypatch, tmp_path):
    """What `serve_dual.main` wires together: both mutual-TLS servers run
    `mtls.MTLSH11Protocol`, and the remote one reuses the one
    `ssl.SSLContext` `_build_tls_config` already built and validated for
    the loopback listener, rather than a second one built from the same
    files."""
    _server_cert(monkeypatch, tmp_path,
                 ip_names=("127.0.0.1", "192.0.2.10"))
    monkeypatch.setattr(mtls, "TLS_HOST", "192.0.2.10")

    loopback_config = serve_dual._build_tls_config()
    remote_config = serve_dual._build_tls_remote_config(loopback_config.ssl)

    assert remote_config is not None
    assert remote_config.ssl is loopback_config.ssl
    assert remote_config.http is loopback_config.http is mtls.MTLSH11Protocol
    assert loopback_config.host == mtls.HOST
    assert remote_config.host == "192.0.2.10"
    assert remote_config.port == mtls.TLS_REMOTE_PORT


def test_build_tls_remote_config_refuses_a_certificate_that_does_not_name_the_host(
        monkeypatch, tmp_path, capsys):
    """A certificate naming only loopback would make every direct remote
    client fail hostname verification, or turn it off to connect, so the
    remote listener does not start under one."""
    _server_cert(monkeypatch, tmp_path)
    monkeypatch.setattr(mtls, "TLS_HOST", "192.0.2.10")

    with pytest.raises(mtls.NonRemoteTlsHost):
        serve_dual._build_tls_remote_config(object())
    assert "does not name" in capsys.readouterr().err


def test_build_tls_remote_config_matches_a_dns_name_regardless_of_case(
        monkeypatch, tmp_path):
    _server_cert(monkeypatch, tmp_path,
                 dns_names=("localhost", "keel.host.example"))
    monkeypatch.setattr(mtls, "TLS_HOST", "Keel.Host.Example")

    assert serve_dual._build_tls_remote_config(object()) is not None


def test_build_tls_remote_config_refuses_a_non_integer_port(monkeypatch):
    monkeypatch.setattr(mtls, "TLS_HOST", "192.0.2.10")
    monkeypatch.setattr(mtls, "TLS_REMOTE_PORT", None)
    with pytest.raises(mtls.NonRemoteTlsHost):
        serve_dual._build_tls_remote_config(object())


def test_tls_remote_port_that_is_not_an_integer_resolves_to_none():
    """Read at import, so raising here would stop the whole service over
    the optional remote listener."""
    assert mtls.resolve_tls_remote_port(
        {"LIBRARY_TLS_REMOTE_PORT": "eighty"}) is None


def test_main_keeps_serving_when_the_remote_host_is_refused(
        monkeypatch, tmp_path):
    """A loopback `LIBRARY_TLS_HOST` stops only the remote listener: the
    plain and loopback mutual-TLS servers still run."""
    import asyncio
    import signal

    _server_cert(monkeypatch, tmp_path)
    monkeypatch.setattr(mtls, "TLS_HOST", "127.0.0.1")
    started = []

    async def _record(server, *label):
        started.append(server.config.host)

    monkeypatch.setattr(serve_dual, "_run", _record)
    monkeypatch.setattr(serve_dual, "_run_tls_survivably", _record)
    monkeypatch.setattr(signal, "signal", lambda *a: None)

    asyncio.run(serve_dual.main())

    assert started == [mtls.PLAIN_HOST, mtls.HOST]


def test_a_remote_listener_crash_does_not_cancel_the_loopback_tls_task(
        monkeypatch):
    import asyncio

    async def _run(server):
        if server == "remote":
            raise RuntimeError("remote accept() blew up")
        await asyncio.sleep(0)
        return None

    monkeypatch.setattr(serve_dual, "_run", _run)

    async def go():
        loopback = asyncio.ensure_future(
            serve_dual._run_tls_survivably("loopback", "127.0.0.1:8443"))
        await serve_dual._run_tls_survivably("remote", "192.0.2.10:9443")
        await loopback
        return loopback

    loopback = asyncio.run(go())
    assert loopback.done() and not loopback.cancelled()
