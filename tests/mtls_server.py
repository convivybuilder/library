"""A real `uvicorn.Server` running the mutual-TLS listener, in a background
thread, for the tests that a manufactured ASGI scope cannot stand in for.

**Three of the four required tests are about what a real TLS handshake does
or does not stop**, and about state that lives on a real, reused TCP
connection (`platform/decisions/0066`: revocation must reach the *next
request over an already-open connection*). Calling the ASGI app directly
with a hand-built scope cannot exercise either — it would prove the
refusal logic runs, not that `mtls.MTLSH11Protocol` actually captures a
handshake's certificate once and carries it across every request the
connection serves. This harness runs the real listener uvicorn builds so the
tests hit the thing that ships.

**Not `uvicorn.Server.run()` / `.serve()`.** Both wrap themselves in
`capture_signals()`, which calls `signal.signal()` — and `signal.signal` may
only be called from the interpreter's main thread, which this harness's
server is deliberately not on (the test body needs the main thread free to
act as the TLS client). This drives the same public `startup` / `main_loop`
/ `shutdown` sequence `serve_dual.py` uses for the identical reason, minus
any signal handling at all — tests stop the server by setting `should_exit`
directly.
"""
from __future__ import annotations

import asyncio
import threading
import time

import uvicorn

import mtls


class RunningServer:
    def __init__(self, config: uvicorn.Config):
        self.server = uvicorn.Server(config)
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._serve())

    async def _serve(self) -> None:
        # `Server._serve()`'s own first act, skipped by calling `startup()`
        # directly: it constructs `self.lifespan` from the config before
        # `startup()` ever reads it.
        self.server.lifespan = self.server.config.lifespan_class(
            self.server.config)
        await self.server.startup()
        if not self.server.should_exit:
            await self.server.main_loop()
        if self.server.started:
            await self.server.shutdown()

    def start(self) -> int:
        self._thread.start()
        deadline = time.monotonic() + 5.0
        while not self.server.started and self._thread.is_alive():
            if time.monotonic() > deadline:
                raise RuntimeError("server did not start within 5s")
            time.sleep(0.01)
        assert self.server.started, "server thread exited before starting"
        return self.server.servers[0].sockets[0].getsockname()[1]

    def stop(self) -> None:
        self.server.should_exit = True
        self._thread.join(timeout=5.0)


def tls_config(app, ssl_context, *, port: int = 0) -> uvicorn.Config:
    config = uvicorn.Config(app, host=mtls.HOST, port=port,
                             http=mtls.MTLSH11Protocol, log_level="critical")
    config.load()
    config.ssl = ssl_context
    return config
