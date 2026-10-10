"""`serve_dual._run_tls_survivably`: a crash in the TLS listener's own
accept/handshake path must not take the plain listener down with it.

platform/decisions/0066. `asyncio.gather`'s default `return_exceptions=False`
cancels every other task the moment any one of them raises — an ordinary
request-handler error never reaches that far (Starlette already catches
those), but something in `main_loop`/`startup`/`shutdown` itself could, and
the plain listener is the one carrying all live traffic until the fleet
finishes migrating (security review, 2026-09-01). These tests drive the
isolation directly with fake coroutines rather than real sockets — the
property under test is `asyncio.gather`'s own cancellation semantics, not
uvicorn's.
"""
from __future__ import annotations

import asyncio
import socket

import uvicorn

import serve_dual


async def _boom(server) -> None:
    raise RuntimeError("accept() blew up")


def test_run_tls_survivably_catches_and_logs_rather_than_raising(
        monkeypatch, capsys):
    monkeypatch.setattr(serve_dual, "_run", _boom)

    asyncio.run(serve_dual._run_tls_survivably(object()))

    err = capsys.readouterr().err
    assert "mutual-TLS listener crashed" in err
    assert "RuntimeError" in err
    assert "accept() blew up" in err


async def _plain_completes():
    await asyncio.sleep(0.01)
    return "plain-done"


def test_a_tls_crash_does_not_cancel_the_plain_listeners_task(monkeypatch):
    """The regression case: without the try/except in
    `_run_tls_survivably`, `asyncio.gather` would raise the instant the TLS
    task raises and cancel `_plain_completes()` before it ever finishes."""
    monkeypatch.setattr(serve_dual, "_run", _boom)

    async def go():
        return await asyncio.gather(
            _plain_completes(), serve_dual._run_tls_survivably(object()))

    results = asyncio.run(go())
    assert results[0] == "plain-done"


async def _noop_app(scope, receive, send) -> None:  # pragma: no cover — never invoked, lifespan="off"
    pass


def test_a_real_bind_conflict_is_survived_not_just_a_synthetic_exception():
    """The regression this PR's round-three fix addresses: uvicorn 0.42.0's
    own `Server.startup()` does not raise an `Exception` when
    `loop.create_server()` fails to bind — it calls `sys.exit(1)` directly
    (`uvicorn/server.py`), and `SystemExit` is a `BaseException`, not an
    `Exception`. `except Exception:` alone does not stop it. This drives the
    real path — an actual `OSError: address already in use` from a real
    socket — rather than a monkeypatched stand-in, so it would have failed
    against the pre-fix `except Exception:` exactly as it would in
    production."""
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    port = blocker.getsockname()[1]
    blocker.listen(1)
    try:
        config = uvicorn.Config(_noop_app, host="127.0.0.1", port=port,
                                 lifespan="off", log_level="critical")
        config.load()
        server = uvicorn.Server(config)

        async def go():
            return await asyncio.gather(
                _plain_completes(), serve_dual._run_tls_survivably(server))

        results = asyncio.run(go())
        assert results[0] == "plain-done"
    finally:
        blocker.close()


def test_a_plain_listener_crash_still_propagates(monkeypatch):
    """The isolation is one-directional: `_run_tls_survivably` protects the
    plain listener from the TLS listener, not the other way around — a
    genuine plain-listener failure is not something this module treats as
    survivable, so it must still raise out of `gather`."""
    async def _plain_boom():
        raise RuntimeError("plain listener blew up")

    async def _tls_ok(server):
        await asyncio.sleep(0.01)
        return "tls-done"

    monkeypatch.setattr(serve_dual, "_run", _tls_ok)

    async def go():
        return await asyncio.gather(
            _plain_boom(), serve_dual._run_tls_survivably(object()))

    try:
        asyncio.run(go())
        raised = False
    except RuntimeError as exc:
        raised = "plain listener blew up" in str(exc)
    assert raised, "a plain-listener crash must still propagate out of gather"
