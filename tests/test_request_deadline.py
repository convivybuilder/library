"""`app.RequestDeadline`: the revocation bound (platform/decisions/0066).

Scoped to the mutual-TLS listener the same way `machine_identity.
MachineIdentityGate` is — see that module's docstring and `app.py`'s
registration comment for why. These tests exercise the middleware directly
against a hand-built scope, the same shape `tests/test_body_size_cap.py`
uses for `RequestBodySizeLimit`; nothing here needs a real socket, since the
property under test is "does this bound wall-clock time", not "does a real
TLS handshake reach the scope" — `test_mtls_peer_cert.py` already covers
that.
"""
from __future__ import annotations

import asyncio

import app as libapp
import mtls


def _scope(*, on_tls: bool):
    return {
        "type": "http", "method": "GET", "path": "/", "raw_path": b"/",
        "query_string": b"", "headers": [], "client": ("127.0.0.1", 1),
        "server": ("127.0.0.1", 8443 if on_tls else 8100), "scheme": "http",
        "extensions": ({mtls.EXTENSION_KEY: {"peer_cert": None,
                                             "peer_cert_der": None}}
                       if on_tls else {}),
        "state": {},
    }


def _run(app, scope):
    sent = []

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    return sent


def _status(sent):
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


def _slow(seconds):
    async def inner(scope, receive, send):
        await asyncio.sleep(seconds)
        await send({"type": "http.response.start", "status": 200,
                    "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})
    return inner


async def _fast(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def test_a_request_over_the_plain_listener_is_never_bounded_by_this():
    """`extensions` carries no `mtls.EXTENSION_KEY` — the plain-port shape —
    so the deadline is not even installed around the call, whatever the
    configured number is. Proven with a `seconds=0` deadline, which would
    refuse instantly if it applied here at all."""
    deadline = libapp.RequestDeadline(_fast, seconds=0)
    sent = _run(deadline, _scope(on_tls=False))
    assert _status(sent) == 200


def test_a_request_finishing_within_the_deadline_over_tls_passes_through():
    deadline = libapp.RequestDeadline(_fast, seconds=5)
    sent = _run(deadline, _scope(on_tls=True))
    assert _status(sent) == 200


def test_a_request_over_the_deadline_over_tls_is_refused_504():
    deadline = libapp.RequestDeadline(_slow(0.2), seconds=0.01)
    sent = _run(deadline, _scope(on_tls=True))
    assert _status(sent) == 504


def test_the_default_is_stores_measured_constant():
    """`store.REQUEST_DEADLINE_SECONDS` is what `app.py` actually wires up —
    pinned here so a change to one without the other is a failing test
    rather than a silent divergence."""
    import store
    outer = None
    for mw in libapp.app.user_middleware:
        if mw.cls is libapp.RequestDeadline:
            outer = mw.kwargs
    assert outer is not None, "RequestDeadline is not registered on app"
    assert outer["seconds"] == store.REQUEST_DEADLINE_SECONDS


def test_the_deadline_is_inside_the_body_cap_and_outside_the_identity_gate():
    """Registration order, asserted by position rather than by reading the
    file — the same discipline `test_body_size_cap.py` holds the body cap
    to. The deadline must wrap the identity gate (a hung registry read is
    still bounded) and must sit inside the body cap (an oversized body is
    refused before either runs)."""
    import machine_identity
    classes = [mw.cls for mw in libapp.app.user_middleware]
    assert classes.index(libapp.RequestBodySizeLimit) < classes.index(
        libapp.RequestDeadline) < classes.index(
        machine_identity.MachineIdentityGate)
