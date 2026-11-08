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

import ipaddress
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


def resolve_plain_host(env=None) -> str:
    """`LIBRARY_PLAIN_HOST`, or `HOST` (loopback) when it is unset.

    Takes an explicit `env` the way `store.resolve_db` does, so a test can
    drive "unset" and "set" without needing to re-import this module."""
    env = os.environ if env is None else env
    return env.get("LIBRARY_PLAIN_HOST", HOST)


def resolve_tls_host(env=None) -> str | None:
    """`LIBRARY_TLS_HOST`, naming a second address the mutual-TLS listener
    also binds, or `None` when it is unset — the multi-machine epic's
    Library clause (issue_01M5FMP5GSE61PVQ4H96RK23FN). The loopback
    mutual-TLS listener binds `HOST` either way; this only ever adds a
    second listener, never replaces that one.

    `env.get` returns `None` only when the key is absent, keeping an
    explicitly empty value distinct from unset so `require_remote_tls_host`
    can refuse the former rather than silently treating it like the
    latter."""
    env = os.environ if env is None else env
    return env.get("LIBRARY_TLS_HOST")


def resolve_tls_remote_port(env=None) -> int | None:
    """`LIBRARY_TLS_REMOTE_PORT`, or `TLS_PORT` when it is unset — the port
    the remote mutual-TLS listener binds, read the same way as `TLS_HOST`.
    `None` when the value is not an integer, which
    `serve_dual._build_tls_remote_config` refuses; raising here, at import,
    would take the plain listener down over the optional remote one.

    Kept separate from `TLS_PORT` because the loopback listener keeps that
    port regardless of what else already owns it on the remote address: when
    the Library runs on a tailnet, `tailscale serve` may already listen on the
    tailnet address at 8443 for an unrelated service, so a remote listener
    sharing that address needs a port of its own."""
    env = os.environ if env is None else env
    try:
        return int(env.get("LIBRARY_TLS_REMOTE_PORT", str(TLS_PORT)))
    except ValueError:
        return None


# Resolved once, at import, the same way `store.DB_PATH` is: a configuration
# that is wrong when the process starts should be a refusal to start, not a
# value re-read per request. `PLAIN_HOST`'s own validity — loopback or
# nothing — is `require_loopback_plain_host`'s job, and `TLS_HOST`'s is
# `require_remote_tls_host`'s, both run from `serve_dual.py` rather than
# here, so that importing this module for a unit test never itself raises.
PLAIN_HOST = resolve_plain_host()
TLS_HOST = resolve_tls_host()
TLS_REMOTE_PORT = resolve_tls_remote_port()

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


class NonLoopbackPlainHost(RuntimeError):
    """Raised at startup for a `PLAIN_HOST` (`LIBRARY_PLAIN_HOST`) that is
    not loopback — the plain listener's own refusal to start.

    The plain listener carries no TLS of its own, so there is no value of
    this variable under which it may serve cleartext off this host; a
    remote host reaches the Library through `TLS_HOST`
    (`LIBRARY_TLS_HOST`) and `machine_identity.MachineIdentityGate`
    instead.
    """


class NonRemoteTlsHost(RuntimeError):
    """Raised for a `TLS_HOST` (`LIBRARY_TLS_HOST`) that is empty or itself
    loopback — the remote mutual-TLS listener's own refusal to build.

    The loopback mutual-TLS listener binds `HOST` unconditionally, so a
    `LIBRARY_TLS_HOST` naming that same address, or naming none at all,
    would only duplicate or no-op it rather than reach a second machine.
    """


def is_loopback_host(host: str) -> bool:
    """Whether `host` is `localhost` by name, or an IPv4/IPv6 literal in the
    loopback range.

    A hostname other than `localhost` is not treated as loopback even if an
    operator's own `/etc/hosts` happens to resolve it there — this check has
    to hold without depending on how, or whether, a name resolves on the
    host running it.
    """
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def require_loopback_plain_host(host: str) -> None:
    """Refuse to start rather than bind the plain listener anywhere but
    loopback — `serve_dual._build_plain_config`'s own preflight.

    Raises `NonLoopbackPlainHost`, naming the offending value, for anything
    `is_loopback_host` does not recognise as loopback. Nothing here can be
    weakened by configuration short of changing `host` itself to a loopback
    address — the same "refuse rather than serve degraded" shape
    `build_tls_ssl_context` already uses for the mutual-TLS listener's own
    material.
    """
    if is_loopback_host(host):
        return
    _log(f"plain listener not started — LIBRARY_PLAIN_HOST={host!r} is not "
         "a loopback address; the plain listener serves no transport "
         "security of its own and refuses to serve cleartext off this "
         "host. Set LIBRARY_TLS_HOST instead to reach this service "
         "remotely, over mutual TLS.")
    raise NonLoopbackPlainHost(
        f"LIBRARY_PLAIN_HOST={host!r} is not a loopback address")


def require_remote_tls_host(host: str) -> None:
    """Refuse to build the remote mutual-TLS listener for a `host` that is
    empty or loopback — `serve_dual._build_tls_remote_config`'s own
    preflight.

    Raises `NonRemoteTlsHost`, naming the offending value, for either case:
    the loopback mutual-TLS listener already binds `HOST` unconditionally,
    so neither value reaches a second machine.
    """
    if not host:
        _log("remote mutual-TLS listener not started — LIBRARY_TLS_HOST is "
             "empty, and the loopback mutual-TLS listener already binds "
             f"{HOST} unconditionally")
        raise NonRemoteTlsHost("LIBRARY_TLS_HOST is empty")
    if is_loopback_host(host):
        _log(f"remote mutual-TLS listener not started — "
             f"LIBRARY_TLS_HOST={host!r} is a loopback address, and the "
             f"loopback mutual-TLS listener already binds {HOST} "
             "unconditionally")
        raise NonRemoteTlsHost(
            f"LIBRARY_TLS_HOST={host!r} is a loopback address")


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
    except (OSError, subprocess.SubprocessError) as exc:
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
