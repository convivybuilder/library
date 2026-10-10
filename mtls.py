"""The mutual-TLS listener: peer-certificate plumbing uvicorn does not supply,
and the `uvicorn.Config` that binds `127.0.0.1:8443` on it.

platform/decisions/0066, stage B. uvicorn 0.42.0's h11 implementation never
reads `getpeercert()`, `ssl_object`, or any ASGI TLS extension — checked by
reading `uvicorn.protocols.http.h11_impl` directly, not assumed — so the
scope every route sees carries nothing about who a TLS handshake was with.
The supported extension point is `uvicorn.Config(http=<H11Protocol
subclass>)`: a protocol class the server constructs itself for every accepted
connection, which is what `MTLSH11Protocol` below is.

This module owns the listener's own configuration (which files, which port,
which TLS floor) and the plumbing that gets a peer certificate out of the
handshake and into the ASGI scope. What a certificate is allowed to identify
as, and the registry that can revoke it, are `machine_identity.py`'s.
"""
from __future__ import annotations

import os
import ssl
import subprocess
import sys
from pathlib import Path

from uvicorn.protocols.http.h11_impl import H11Protocol

import store

# `serve.sh --walk PATH` runs the same owner/group/mode/ACL walk the
# interpreter chain gets, against an arbitrary path — see that file for why
# this reuses it rather than a second implementation.
_SERVE_SH = Path(__file__).resolve().parent / "serve.sh"

# The ASGI scope extension key a request carries when it arrived over the
# mutual-TLS listener. **Its presence, not its content, is what every check
# downstream keys on** — the plain listener at 127.0.0.1:8100 runs the stock
# `H11Protocol` and never sets this, so nothing gated on this key can ever
# fire against a request that arrived there. That is what keeps the plain
# port's behaviour untouched by this file existing at all.
EXTENSION_KEY = "convivy.mtls"

HOST = "127.0.0.1"
PLAIN_PORT = 8100
TLS_PORT = 8443

# The Library's own external state area, alongside `identity.CLIENT_TOKENS`
# and `LIBRARY_DB`'s default — never the deploy area, so an atomic reinstall
# of the checkout does not touch them. The Library holds only the root
# certificate here, which is public; the private key behind it never leaves
# `macbook`'s Secure Enclave (platform/decisions/0066).
TLS_CERT = Path(os.environ.get(
    "LIBRARY_TLS_CERT", str(Path.home() / ".library" / "tls" / "server.crt")))
TLS_KEY = Path(os.environ.get(
    "LIBRARY_TLS_KEY", str(Path.home() / ".library" / "tls" / "server.key")))
TLS_CA = Path(os.environ.get(
    "LIBRARY_TLS_CA", str(Path.home() / ".library" / "tls" / "ca.crt")))


class MTLSH11Protocol(H11Protocol):
    """`H11Protocol`, plus the peer certificate stashed into every scope this
    connection builds — both the parsed form and the raw DER, since the
    registry pins a SHA-256 over the DER rather than over anything parsed.

    **A property override of `scope`, not a reimplementation of
    `handle_events`.** The base class rebuilds `self.scope` as a plain dict
    literal on every `h11.Request` event inside that method, which runs to
    about eighty lines in uvicorn 0.42.0 and is not this file's to own —
    duplicating it would tie this subclass to that method's exact shape
    across every future dependency bump. Overriding `scope` as a property
    intercepts every assignment the base class makes, however many times and
    wherever inside that method, and depends on nothing more than the ASGI
    protocol's own contract that a protocol instance exposes `self.scope` —
    which is far more stable than one method's internals.

    The certificate is read once, in `connection_made`, because it is a
    property of the TLS session rather than of any one request: a keep-alive
    connection carries one handshake and can carry many requests, and each of
    those requests' scopes gets the same certificate stamped into it. That is
    also what makes the revocation check on the next request meaningful over
    an already-open connection — the certificate travels with the
    connection, and only the registry lookup is re-done per request.
    """

    def connection_made(self, transport) -> None:  # type: ignore[override]
        super().connection_made(transport)
        ssl_object = transport.get_extra_info("ssl_object")
        peer_cert = None
        peer_cert_der = None
        if ssl_object is not None:
            # `getpeercert()` and its binary-form twin raise `ValueError` for
            # a handshake that has not completed yet. `connection_made` fires
            # on `accept()`, before the handshake runs, so this can
            # legitimately be called with nothing yet to read — the
            # cert-required listener guarantees a completed handshake by the
            # time any HTTP bytes arrive, not by the time this method runs.
            try:
                peer_cert = ssl_object.getpeercert()
            except ValueError:
                peer_cert = None
            try:
                peer_cert_der = ssl_object.getpeercert(binary_form=True)
            except ValueError:
                peer_cert_der = None
        self._convivy_peer_cert = peer_cert
        self._convivy_peer_cert_der = peer_cert_der

    @property
    def scope(self):
        return self._convivy_scope

    @scope.setter
    def scope(self, value):
        if value is not None:
            value.setdefault("extensions", {})
            value["extensions"][EXTENSION_KEY] = {
                "peer_cert": getattr(self, "_convivy_peer_cert", None),
                "peer_cert_der": getattr(self, "_convivy_peer_cert_der", None),
            }
        self._convivy_scope = value


def _log(msg: str) -> None:
    print(f"library.mtls: {msg}", file=sys.stderr)


def _trust_anchor_walk_ok() -> bool:
    """Whether `TLS_CA` passes `serve.sh --walk`'s owner/group/mode/ACL walk.

    **Fails closed on anything that stops this from getting a clean pass**:
    `serve.sh` missing, not executable, or the subprocess erroring outright
    all read as "not vouched for", the same direction every check in this
    function already takes for missing or invalid material.

    **Skipped, not refused, off Darwin.** The walk is built on BSD `stat -f`
    and macOS ACL semantics — `tests/test_serve_guard.py` already documents
    that it does not mean the same thing on Linux and skips its own coverage
    there rather than asserting behaviour the mechanism cannot deliver. This
    service deploys exclusively on macOS (the root key lives in a Secure
    Enclave, platform/decisions/0066), so refusing here on Linux would not
    protect a real deployment — it would only ever fire in CI or a
    non-macOS dev checkout, where it can't actually vouch for anything
    either way.
    """
    if sys.platform != "darwin":
        return True
    try:
        result = subprocess.run(
            [str(_SERVE_SH), "--walk", str(TLS_CA)],
            capture_output=True, text=True, timeout=30, check=False)
    except OSError as exc:
        _log("mutual-TLS listener not started — could not run serve.sh's "
             f"ownership walk against the trust anchor: {exc}")
        return False
    if result.returncode != 0:
        _log("mutual-TLS listener not started — the trust anchor at "
             f"{TLS_CA} failed serve.sh's ownership walk: "
             f"{result.stderr.strip()}")
        return False
    return True


def build_tls_ssl_context() -> ssl.SSLContext | None:
    """A `CERT_REQUIRED`, TLS-1.3-floor `SSLContext` built from `TLS_CERT`,
    `TLS_KEY` and `TLS_CA`, or `None` if the material is incomplete or
    invalid.

    **Every one of the four properties `platform/decisions/0066` requires is
    hardcoded here, not read from configuration**: `CERT_REQUIRED`, the TLS
    1.3 floor, and the cert/key/CA all named by this module's own constants.
    There is no environment variable that can weaken this listener short of
    replacing the files it reads — matching the shape `serve.sh`'s
    interpreter preflight already uses for "refuse rather than serve
    degraded".

    **A fifth check, added for the same reason** (security review,
    2026-09-01): `store.REQUEST_DEADLINE_SECONDS` is the revocation bound
    this listener exists to make good on — uvicorn gives a request no
    ceiling of its own, so a certificate revoked mid-connection is only
    bounded by `app.RequestDeadline`, and that middleware is only worth
    anything if the number behind it is a real deadline rather than a
    disabled one. A deploy that later set `LIBRARY_REQUEST_DEADLINE_SECONDS=0`
    to mean "no timeout" would leave the cert/key/CA/CERT_REQUIRED checks all
    passing while the bound this listener is FOR had quietly stopped
    existing; refusing here keeps that a loud failure, the same as the other
    four.

    **Checked for sanity, not merely presence** (round 2 of the same review,
    platform/decisions/0066's Handoff: "compare it against the slowest
    measured route and refuse a value that cannot be one"). A deadline below
    `store.SLOWEST_MEASURED_ROUTE_SECONDS` cannot bound even this service's
    own slowest legitimate request, so it would 504 ordinary traffic rather
    than protect it; a deadline above `store.REQUEST_DEADLINE_CEILING_SECONDS`
    is so far past every real request's duration that it stops functioning as
    a bound at all — `LIBRARY_REQUEST_DEADLINE_SECONDS=31536000` passing a
    presence-only check and binding the listener anyway is the shape this
    closes.

    **A sixth check, on the trust anchor's own file integrity** (round 2 of
    the same review, Handoff: "the owner, group and mode walk `serve.sh`
    performs on its interpreter chain, applied to the trust anchor"). `TLS_CA`
    is read straight off disk otherwise, with nothing stopping a
    world-writable component upstream of it from substituting a different
    root — exactly the hazard `serve.sh`'s own walk exists to close for the
    interpreter. `_trust_anchor_walk_ok()` runs the identical walk against
    `TLS_CA` by shelling out to `serve.sh --walk`, rather than a second
    implementation of a check already rewritten wrong four times.

    Returning `None` rather than raising is deliberate: the caller decides
    what "no TLS material yet" means for the process as a whole, and on this
    service today it means the plain listener still starts — see
    `serve_dual.py`.
    """
    missing = [str(p) for p in (TLS_CERT, TLS_KEY, TLS_CA) if not p.is_file()]
    if missing:
        _log(f"mutual-TLS listener not started — missing: {', '.join(missing)}")
        return None
    if not _trust_anchor_walk_ok():
        return None
    if store.REQUEST_DEADLINE_SECONDS < store.SLOWEST_MEASURED_ROUTE_SECONDS:
        _log("mutual-TLS listener not started — "
             f"LIBRARY_REQUEST_DEADLINE_SECONDS is "
             f"{store.REQUEST_DEADLINE_SECONDS:g}, below the "
             f"{store.SLOWEST_MEASURED_ROUTE_SECONDS:g}s slowest measured "
             "route, so it would time out legitimate requests rather than "
             "bound a revoked one")
        return None
    if store.REQUEST_DEADLINE_SECONDS > store.REQUEST_DEADLINE_CEILING_SECONDS:
        _log("mutual-TLS listener not started — "
             f"LIBRARY_REQUEST_DEADLINE_SECONDS is "
             f"{store.REQUEST_DEADLINE_SECONDS:g}, above the "
             f"{store.REQUEST_DEADLINE_CEILING_SECONDS:g}s ceiling, so it "
             "cannot plausibly be a per-request bound")
        return None
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        ctx.load_cert_chain(str(TLS_CERT), str(TLS_KEY))
        ctx.load_verify_locations(str(TLS_CA))
    except ssl.SSLError as exc:
        _log(f"mutual-TLS listener not started — {TLS_CERT} / {TLS_KEY} / "
             f"{TLS_CA} did not load as a certificate chain and trust "
             f"anchor: {exc}")
        return None
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx
