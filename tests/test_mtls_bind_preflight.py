"""`mtls.build_tls_ssl_context`: the TLS listener refuses to bind rather than
serve degraded, and that now covers the revocation bound as well as the
material.

platform/decisions/0066. The four original conditions — a certificate, a
key, a trust anchor, `CERT_REQUIRED` — are what missing TLS material already
exercises; the fifth, added in the second security-review round, is that
`store.REQUEST_DEADLINE_SECONDS` — the revocation bound `app.RequestDeadline`
enforces — is a real, positive number. A deploy that zeroed it out would
otherwise pass every other check and bind a listener whose advertised
revocation bound does not exist.

**Checked for sanity, not merely presence, as of the round-2 finding on the
same review.** A value below `store.SLOWEST_MEASURED_ROUTE_SECONDS` is real
but useless — it would 504 this service's own slowest legitimate request
rather than bound a revoked one — and a value above
`store.REQUEST_DEADLINE_CEILING_SECONDS` passes a presence-only check while
no longer functioning as a per-request bound at all.

**The trust anchor also gets `serve.sh --walk`'s owner/group/mode/ACL walk**,
the same round-2 finding: `TLS_CA` was read straight off disk with no
integrity check of any kind.
"""
from __future__ import annotations

import subprocess
import sys

import pytest
import ssl

import mtls
import store
import tls_ca

_DARWIN_ONLY = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="serve.sh's walk is built on BSD stat, macOS ACLs and the admin group")


def _staged_material(tmp_path):
    ca_cert, ca_key = tls_ca.make_ca(tmp_path)
    server_cert, server_key = tls_ca.make_cert(
        tmp_path, "server", ca_cert, ca_key, dns_names=("localhost",),
        ip_names=("127.0.0.1",), ext_key_usage="serverAuth")
    return ca_cert, server_cert, server_key


def test_refuses_to_bind_with_no_material_staged(monkeypatch, tmp_path):
    monkeypatch.setattr(mtls, "TLS_CERT", tmp_path / "missing.crt")
    monkeypatch.setattr(mtls, "TLS_KEY", tmp_path / "missing.key")
    monkeypatch.setattr(mtls, "TLS_CA", tmp_path / "missing-ca.crt")
    assert mtls.build_tls_ssl_context() is None


def test_binds_when_material_is_staged_and_the_deadline_is_positive(
        monkeypatch, tmp_path):
    ca_cert, server_cert, server_key = _staged_material(tmp_path)
    monkeypatch.setattr(mtls, "TLS_CERT", server_cert)
    monkeypatch.setattr(mtls, "TLS_KEY", server_key)
    monkeypatch.setattr(mtls, "TLS_CA", ca_cert)
    monkeypatch.setattr(store, "REQUEST_DEADLINE_SECONDS", 60.0)

    ctx = mtls.build_tls_ssl_context()

    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_3


def test_refuses_to_bind_when_the_deadline_is_zero(monkeypatch, tmp_path):
    """A deploy that sets `LIBRARY_REQUEST_DEADLINE_SECONDS=0` meaning "no
    timeout" must not get a bound-free mutual-TLS listener silently — the
    revocation bound this listener exists to make good on would have quietly
    stopped existing while every other check kept passing."""
    ca_cert, server_cert, server_key = _staged_material(tmp_path)
    monkeypatch.setattr(mtls, "TLS_CERT", server_cert)
    monkeypatch.setattr(mtls, "TLS_KEY", server_key)
    monkeypatch.setattr(mtls, "TLS_CA", ca_cert)
    monkeypatch.setattr(store, "REQUEST_DEADLINE_SECONDS", 0.0)

    assert mtls.build_tls_ssl_context() is None


def test_refuses_to_bind_when_the_deadline_is_negative(monkeypatch, tmp_path):
    ca_cert, server_cert, server_key = _staged_material(tmp_path)
    monkeypatch.setattr(mtls, "TLS_CERT", server_cert)
    monkeypatch.setattr(mtls, "TLS_KEY", server_key)
    monkeypatch.setattr(mtls, "TLS_CA", ca_cert)
    monkeypatch.setattr(store, "REQUEST_DEADLINE_SECONDS", -1.0)

    assert mtls.build_tls_ssl_context() is None


def test_refuses_to_bind_when_the_deadline_is_too_small_to_bound_the_slowest_route(
        monkeypatch, tmp_path):
    """A real, positive value that is still too small to be a bound — the
    `LIBRARY_REQUEST_DEADLINE_SECONDS=1` case named in review: it passes a
    presence check but would 504 nearly every legitimate request, since this
    service's own slowest measured route already takes longer than that."""
    ca_cert, server_cert, server_key = _staged_material(tmp_path)
    monkeypatch.setattr(mtls, "TLS_CERT", server_cert)
    monkeypatch.setattr(mtls, "TLS_KEY", server_key)
    monkeypatch.setattr(mtls, "TLS_CA", ca_cert)
    monkeypatch.setattr(store, "REQUEST_DEADLINE_SECONDS", 1.0)

    assert mtls.build_tls_ssl_context() is None


def test_binds_at_exactly_the_slowest_measured_route(monkeypatch, tmp_path):
    ca_cert, server_cert, server_key = _staged_material(tmp_path)
    monkeypatch.setattr(mtls, "TLS_CERT", server_cert)
    monkeypatch.setattr(mtls, "TLS_KEY", server_key)
    monkeypatch.setattr(mtls, "TLS_CA", ca_cert)
    monkeypatch.setattr(store, "REQUEST_DEADLINE_SECONDS",
                        store.SLOWEST_MEASURED_ROUTE_SECONDS)

    assert mtls.build_tls_ssl_context() is not None


def test_refuses_to_bind_when_the_deadline_is_absurdly_large(
        monkeypatch, tmp_path):
    """`LIBRARY_REQUEST_DEADLINE_SECONDS=31536000` (one year) passes a
    presence-only check and would otherwise bind the listener with a
    revocation bound that, in practice, does not exist."""
    ca_cert, server_cert, server_key = _staged_material(tmp_path)
    monkeypatch.setattr(mtls, "TLS_CERT", server_cert)
    monkeypatch.setattr(mtls, "TLS_KEY", server_key)
    monkeypatch.setattr(mtls, "TLS_CA", ca_cert)
    monkeypatch.setattr(store, "REQUEST_DEADLINE_SECONDS", 31536000.0)

    assert mtls.build_tls_ssl_context() is None


def test_binds_at_exactly_the_ceiling(monkeypatch, tmp_path):
    ca_cert, server_cert, server_key = _staged_material(tmp_path)
    monkeypatch.setattr(mtls, "TLS_CERT", server_cert)
    monkeypatch.setattr(mtls, "TLS_KEY", server_key)
    monkeypatch.setattr(mtls, "TLS_CA", ca_cert)
    monkeypatch.setattr(store, "REQUEST_DEADLINE_SECONDS",
                        store.REQUEST_DEADLINE_CEILING_SECONDS)

    assert mtls.build_tls_ssl_context() is not None


@_DARWIN_ONLY
def test_refuses_to_bind_when_the_trust_anchor_fails_the_ownership_walk(
        monkeypatch, tmp_path):
    """A trust anchor reachable through a world-writable directory must not
    be trusted whatever it contains — the same hazard `serve.sh`'s own walk
    exists to close for the interpreter chain, now applied to `TLS_CA`."""
    unsafe_dir = tmp_path / "unsafe"
    unsafe_dir.mkdir()
    unsafe_dir.chmod(0o777)
    ca_cert, server_cert, server_key = _staged_material(tmp_path)
    unsafe_ca = unsafe_dir / "ca.crt"
    unsafe_ca.write_text(ca_cert.read_text())

    monkeypatch.setattr(mtls, "TLS_CERT", server_cert)
    monkeypatch.setattr(mtls, "TLS_KEY", server_key)
    monkeypatch.setattr(mtls, "TLS_CA", unsafe_ca)
    monkeypatch.setattr(store, "REQUEST_DEADLINE_SECONDS", 60.0)

    assert mtls.build_tls_ssl_context() is None


@_DARWIN_ONLY
def test_binds_when_the_trust_anchor_passes_the_ownership_walk(
        monkeypatch, tmp_path):
    """The positive control: material staged under `tmp_path`, which is
    owned by this user and not world- or group-writable, passes the walk —
    proving the check above refuses for the directory's permissions, not
    merely because it always refuses."""
    ca_cert, server_cert, server_key = _staged_material(tmp_path)
    monkeypatch.setattr(mtls, "TLS_CERT", server_cert)
    monkeypatch.setattr(mtls, "TLS_KEY", server_key)
    monkeypatch.setattr(mtls, "TLS_CA", ca_cert)
    monkeypatch.setattr(store, "REQUEST_DEADLINE_SECONDS", 60.0)

    assert mtls.build_tls_ssl_context() is not None


def test_trust_anchor_walk_is_skipped_rather_than_refused_off_darwin(
        monkeypatch):
    """Off Darwin, `serve.sh --walk` cannot mean anything — it is built on
    BSD `stat -f` — so the check must not silently refuse every non-macOS
    checkout; it skips instead, the same way `test_serve_guard.py` skips its
    own coverage rather than asserting behaviour the mechanism can't give."""
    monkeypatch.setattr(mtls.sys, "platform", "linux")
    monkeypatch.setattr(mtls, "TLS_CA", subprocess.__file__)  # any real file

    assert mtls._trust_anchor_walk_ok() is True


def test_the_deadline_check_does_not_touch_the_plain_listener():
    """`store.REQUEST_DEADLINE_SECONDS` gates only what `build_tls_ssl_context`
    returns; `serve_dual._build_plain_config` never calls it and never reads
    `mtls.build_tls_ssl_context` at all, so a deadline of zero cannot stop the
    plain listener from starting."""
    import serve_dual
    import inspect
    source = inspect.getsource(serve_dual._build_plain_config)
    assert "build_tls_ssl_context" not in source
    assert "REQUEST_DEADLINE_SECONDS" not in source
