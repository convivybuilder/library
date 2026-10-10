"""Run the Library's two listeners — plain HTTP and mutual TLS — as two
`uvicorn.Server` instances sharing one process, one event loop, and one
SQLite writer.

platform/decisions/0066, stage B. `serve.sh` used to `exec python -m uvicorn
app:app --host 127.0.0.1 --port 8100` directly; it now execs this module
instead, so the mutual-TLS listener at `127.0.0.1:8443` can come up beside
the plain one without a second process contending for `store.connect_rw`'s
single SQLite writer — the ADR's own guess was that this is workable, with a
second launchd job over the same database as the named fallback if it is
not. It is: the risk this module actually has to manage is not the shared
event loop, it is that `uvicorn.Server.serve()` installs a process-wide
SIGTERM/SIGINT handler on entry, and two of those running concurrently would
have the second one silently replace the first's — see `main()`.

**The plain listener's own behaviour is exactly what it was before this file
existed**: same host, same port, same log level, the one FastAPI lifespan
that already ran under the old `python -m uvicorn` invocation. Everything
below that concerns the TLS listener is additive, and if its material is not
staged, the plain listener still starts — see `_build_tls_config`.
"""
from __future__ import annotations

import asyncio
import signal
import sys
import traceback

import uvicorn

import app as libapp
import machine_identity
import mtls

LOG_LEVEL = "warning"

# **THE GRACEFUL-SHUTDOWN BOUND — THE QUIESCE DRAIN'S OWN, BY REFERENCE,
# NOT A SECOND LITERAL.** Each server's `shutdown()` (uvicorn's, reached
# when `main()`'s shared signal handler sets `should_exit`) waits for its
# in-flight requests to finish before the process exits. Without a
# `timeout_graceful_shutdown` that wait is unbounded, and one connection
# held open by a client that never finishes — a raw SIGTERM can land while
# any request is part-way through, with no valid Library credential needed
# to hold one open — hangs the whole process, and the credentialed Library
# with it, until a `kill -9`. That wait is the same wait
# `app._restart_watcher` bounds with `_DRAIN_DEADLINE_SECONDS`: the same
# in-flight requests, the same slowest legitimate write behind them (32.2 s,
# an embedding commit queued behind SQLite's write lock — the derivation and
# the margin that sets that constant's value are in app.py's comment above
# it). So this names that constant rather than copying its value: two
# literals drift, and a graceful shutdown that outlived the drain it mirrors
# would re-open the lockout this whole mechanism exists to close. **Both
# listeners carry it**:
# the signal is process-wide, a wedged request may sit on either port, and
# each server's `shutdown()` waits on its own connection set, so a bound on
# only one leaves the other hanging the process anyway.
_GRACEFUL_SHUTDOWN_SECONDS = libapp._DRAIN_DEADLINE_SECONDS


def _build_plain_config() -> uvicorn.Config:
    config = uvicorn.Config(libapp.app, host=mtls.HOST, port=mtls.PLAIN_PORT,
                             log_level=LOG_LEVEL,
                             timeout_graceful_shutdown=_GRACEFUL_SHUTDOWN_SECONDS)
    config.load()
    return config


def _build_tls_config() -> uvicorn.Config | None:
    """A fully-armed mutual-TLS `uvicorn.Config`, or `None` if the TLS
    material at `mtls.TLS_CERT` / `TLS_KEY` / `TLS_CA` is missing or invalid.

    **`None` does not stop the process.** Stage B is explicitly "mutual TLS
    on 127.0.0.1:8443, and nobody is required to use it" — the listener is
    additive, and the hard constraint on this whole migration is that the
    plain listener keeps serving exactly as it does today. A machine with no
    TLS material staged (every dev checkout, this repo's own test suite, a
    mini that has not been through the stage-A enrollment yet) must still
    serve plain HTTP rather than refuse to start at all. What "refuse to
    bind that port unless it has a certificate, a key, a trust anchor and
    CERT_REQUIRED" governs is *how* the TLS port comes up when it does — full
    strength or not at all — not whether the rest of the process does.
    """
    ctx = mtls.build_tls_ssl_context()
    if ctx is None:
        return None
    config = uvicorn.Config(
        libapp.app, host=mtls.HOST, port=mtls.TLS_PORT, log_level=LOG_LEVEL,
        http=mtls.MTLSH11Protocol,
        # This process's own FastAPI lifespan already runs once, driven by
        # the plain listener's config below — starting it a second time here
        # would print every boot log line twice and start a second, redundant
        # `_restart_watcher` task racing the first over the same module-level
        # globals. One `Config` runs it; the other explicitly does not.
        lifespan="off",
        timeout_graceful_shutdown=_GRACEFUL_SHUTDOWN_SECONDS)
    # **NO `ssl_certfile` PASSED ABOVE, ON PURPOSE.** `Config.load()` builds
    # its own `SSLContext` from `ssl_certfile`/`ssl_keyfile`/etc whenever
    # either is set, unconditionally — passing `ssl_certfile` alone to make
    # `is_ssl` read `True` for the startup log line instead makes `load()`
    # call `load_cert_chain(certfile, None)`, which raises `ssl.SSLError` for
    # any cert file that does not also embed the private key. `mtls` already
    # built and validated the real context; `config.load()` runs with
    # `is_ssl` False so it leaves `self.ssl` alone, and this assigns the
    # validated context directly, once, the only place a certificate is read.
    config.load()
    config.ssl = ctx
    config.ssl_certfile = str(mtls.TLS_CERT)  # cosmetic: is_ssl for the log line
    return config


async def _run(server: uvicorn.Server) -> None:
    """`Server._serve()`'s own body, minus the signal handling `Server.serve()`
    wraps it in — see `main()` for why that wrapping is what this avoids."""
    # `_serve()`'s first act, otherwise skipped by calling `startup()`
    # directly: it builds `self.lifespan` from the config before `startup()`
    # ever reads it.
    server.lifespan = server.config.lifespan_class(server.config)
    await server.startup()
    if not server.should_exit:
        await server.main_loop()
    if server.started:
        await server.shutdown()


async def _run_tls_survivably(server: uvicorn.Server) -> None:
    """`_run(server)` for the TLS listener specifically, with anything that
    escapes it caught rather than left to propagate.

    **`asyncio.gather` cancels every other task the moment any one of them
    raises**, with the default `return_exceptions=False` `main()` uses below.
    An ordinary request-handler error never reaches here — Starlette already
    catches those — but something in the accept/handshake path itself could,
    and "one process, two listeners, and the plain one is unaffected" (this
    module's own docstring) is not true if that crash takes the plain
    listener's task down with it, since the plain listener is the one
    carrying every request until the fleet finishes migrating (security
    review, 2026-09-01). Catching here, once, for the one task that must not
    be allowed to cancel its sibling, keeps `_run` itself free of a
    try/except that would blur why the plain listener's own call site has
    none.

    **`SystemExit`, not just `Exception`.** `uvicorn.Server.startup()` does
    not raise an `Exception` when `loop.create_server()` fails to bind —
    it logs and calls `sys.exit(1)` directly (a lingering prior process on
    8443, or a restart racing the old one off the port, are the ordinary
    ways this happens). `SystemExit` derives from `BaseException`, so an
    `except Exception:` here does not stop it, and it propagates straight
    through `asyncio.gather` and takes the plain listener's task down too —
    the exact outage this wrapper exists to prevent. Nothing else awaited in
    `_run` — `startup`'s own lifespan step (this server runs `lifespan="off"`
    and reads no app-level startup events), `main_loop` (pure `on_tick` /
    `sleep`), `shutdown` — has a path to a genuine `Exception` here; a TLS
    handshake or accept failure is handled inside asyncio's transport
    machinery, outside anything `_run` awaits, so it never reaches this
    try/except regardless. This catches `SystemExit` for the one real
    failure mode and `Exception` for defense in depth against a future one.
    """
    try:
        await _run(server)
    except (Exception, SystemExit):  # noqa: BLE001 — anything here must not cancel the plain listener
        print("library: mutual-TLS listener crashed — "
              f"serving {mtls.HOST}:{mtls.PLAIN_PORT} only from here on",
              file=sys.stderr)
        traceback.print_exc(file=sys.stderr)


async def main() -> None:
    plain = uvicorn.Server(_build_plain_config())
    servers = [plain]
    # **`plain`'s task is bare `_run`, never `_run_tls_survivably`.** A
    # crash in the plain listener is not a survivable event to isolate —
    # there is nothing else left running to protect it from — so it keeps
    # `asyncio.gather`'s default propagate-and-cancel behaviour.
    tasks = [_run(plain)]

    tls_config = _build_tls_config()
    if tls_config is not None:
        tls_server = uvicorn.Server(tls_config)
        servers.append(tls_server)
        tasks.append(_run_tls_survivably(tls_server))
        print(f"library: mutual-TLS listener on "
              f"{mtls.HOST}:{mtls.TLS_PORT}, registry "
              f"{machine_identity.__name__} enforced", file=sys.stderr)
    else:
        print(f"library: mutual-TLS listener not started (see the line "
              f"above) — serving {mtls.HOST}:{mtls.PLAIN_PORT} only",
              file=sys.stderr)

    # **ONE SIGNAL HANDLER FOR BOTH SERVERS, INSTALLED ONCE, HERE — NOT
    # EACH `Server.serve()`'s OWN.** `Server.serve()` wraps itself in
    # `capture_signals()`, which calls `signal.signal(sig, self.handle_exit)`
    # — a process-wide registration, last-writer-wins. Two servers both
    # calling `.serve()` concurrently in this loop would have the second
    # one's handler silently replace the first's, so a SIGTERM would reach
    # only whichever server registered last; the other's `main_loop` would
    # never see `should_exit` flip and `asyncio.gather` below would hang on
    # shutdown forever. Driving each server through the public `startup` /
    # `main_loop` / `shutdown` methods instead of `.serve()` sidesteps that
    # registration entirely, and this is the one handler both share.
    #
    # This is also what makes `app._restart_watcher`'s self-sent SIGTERM
    # continue to work unchanged: it still just signals this process, and
    # this handler sets `should_exit` on every server that is actually
    # running, same as uvicorn's own handler would for the one server there
    # used to be.
    def _request_exit(signum, frame) -> None:
        for server in servers:
            server.should_exit = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _request_exit)

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    # Before anything writes: every line in `serve.log` gets a UTC timestamp,
    # including uvicorn's, whose handlers bind to `sys.stderr` when `main()`
    # builds its `uvicorn.Config`. Here and not at import, because tests
    # import this module. See `log_stamp`.
    import log_stamp
    log_stamp.install()
    asyncio.run(main())
