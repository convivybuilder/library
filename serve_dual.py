"""Run the Library's listeners — plain HTTP, and mutual TLS on loopback
plus, when configured, a second remote address — as `uvicorn.Server`
instances sharing one process, one event loop, and one SQLite writer.

platform/decisions/0066, stage B. `serve.sh` used to `exec python -m uvicorn
app:app --host 127.0.0.1 --port 8100` directly; it now execs this module
instead, so the mutual-TLS listener at `127.0.0.1:8443` can come up beside
the plain one without a second process contending for `store.connect_rw`'s
single SQLite writer — the ADR's own guess was that this is workable, with a
second launchd job over the same database as the named fallback if it is
not. It is: the risk this module actually has to manage is not the shared
event loop, it is that `uvicorn.Server.serve()` installs a process-wide
SIGTERM/SIGINT handler on entry, and more than one of those running
concurrently would have the last one silently replace the rest — see
`main()`.

**The plain listener's own behaviour is exactly what it was before this file
existed**: same host unless `LIBRARY_PLAIN_HOST` says otherwise (and then
only to another loopback address — `mtls.require_loopback_plain_host`
refuses to start rather than honour anything else), same port, same log
level, the one FastAPI lifespan that already ran under the old `python -m
uvicorn` invocation. Everything below that concerns the TLS listeners is
additive, and if the TLS material is not staged, the plain listener still
starts — see `_build_tls_config`.

**The multi-machine epic's Library clause**
(issue_01M5FMP5GSE61PVQ4H96RK23FN) is `mtls.TLS_HOST`
(`LIBRARY_TLS_HOST`): unset, only the loopback mutual-TLS listener binds, as
it always has; set, a second mutual-TLS server also binds that address,
alongside the loopback one rather than in place of it, sharing the loopback
server's own `ssl.SSLContext`, `mtls.MTLSH11Protocol` and app — see
`_build_tls_remote_config`. Every request on either binding still passes
`machine_identity.MachineIdentityGate` — that gate is keyed on
`mtls.EXTENSION_KEY`, which `mtls.MTLSH11Protocol` sets regardless of which
address the listener using it is bound to, so no route is exempt on the
remote binding that is not already exempt on the loopback one.
"""
from __future__ import annotations

import asyncio
import ipaddress
import signal
import ssl
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
# would re-open the lockout this whole mechanism exists to close. **Every
# listener carries it**:
# the signal is process-wide, a wedged request may sit on any of their
# ports, and each server's `shutdown()` waits on its own connection set, so
# a bound missing from any one of them leaves it hanging the process anyway.
_GRACEFUL_SHUTDOWN_SECONDS = libapp._DRAIN_DEADLINE_SECONDS

# How long a mutual-TLS connection may sit idle between requests before the
# Library closes it. uvicorn's default is 5 s. Deck's machine key on the
# operator's laptop is presence-gated (platform/decisions/0065): a TLS
# handshake more than 300 s after the last approval shows the operator a
# Touch ID or password prompt. Deck's key test on that laptop (2026-11-03)
# showed that prompt once the window had lapsed. With a 5 s idle close,
# every Deck call after a pause reconnects, and once the window has lapsed
# that reconnect prompts, even for a background refresh the operator did
# not start. Deck sends a heartbeat every 60 s, and asked for 120 s, twice
# that interval: a late heartbeat keeps the connection open, while a fully
# missed one leaves a 120 s gap that races this timer and may close it. A
# prompt then comes only on a reconnect: a relaunch, a sleep, a drop, or
# that race. 0065 allows the Library to keep an idle connection open longer
# for exactly this reason.
#
# **Both mutual-TLS listeners, every client on them; the plain listener
# keeps uvicorn's default.** uvicorn sets this per listener, and the
# connection's certificate is what tells one client from another, so a
# Deck-only value would mean new per-connection code in
# `mtls.MTLSH11Protocol`. Nothing needs that: a client whose key has no
# presence gate pays no prompt for a reconnect, so a longer idle hold only
# keeps its socket open a little longer, and only a certified machine can
# open one here. Revocation is unaffected, because the certificate registry
# is read again on every request over an open connection
# (`machine_identity.MachineIdentityGate`, through `resolve_machine`;
# `mtls.MTLSH11Protocol` only stamps the certificate onto the connection).
# The plain listener has no handshake to save, and
# serves bearer-token clients on loopback, so it stays as it was.
_TLS_IDLE_TIMEOUT_SECONDS = 120


def _build_plain_config() -> uvicorn.Config:
    """The plain listener's own `uvicorn.Config` — loopback only.

    `require_loopback_plain_host` runs first and raises
    `mtls.NonLoopbackPlainHost` for anything `LIBRARY_PLAIN_HOST` names that
    is not loopback, so this never returns a config for an off-host bind:
    the plain listener carries no TLS of its own, and nothing downstream
    of this function is in a position to catch that mistake.
    """
    mtls.require_loopback_plain_host(mtls.PLAIN_HOST)
    config = uvicorn.Config(libapp.app, host=mtls.PLAIN_HOST,
                             port=mtls.PLAIN_PORT, log_level=LOG_LEVEL,
                             timeout_graceful_shutdown=_GRACEFUL_SHUTDOWN_SECONDS)
    config.load()
    return config


def _build_tls_config() -> uvicorn.Config | None:
    """A fully-armed mutual-TLS `uvicorn.Config` bound to loopback
    (`mtls.HOST`), or `None` if the TLS material at `mtls.TLS_CERT` /
    `TLS_KEY` / `TLS_CA` is missing or invalid.

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
        timeout_keep_alive=_TLS_IDLE_TIMEOUT_SECONDS,
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


def _certificate_names(host: str) -> bool:
    """Whether `mtls.TLS_CERT`'s subjectAltName carries `host`, as a DNS
    name or an IP address. An unreadable certificate names nothing."""
    try:
        der = ssl.PEM_cert_to_DER_cert(mtls.TLS_CERT.read_text())
    except (OSError, ValueError):
        return False
    try:
        wanted = str(ipaddress.ip_address(host))
    except ValueError:
        wanted = host.lower()
    return wanted in machine_identity.dns_and_ip_names(der)


def _build_tls_remote_config(ssl_context: ssl.SSLContext) -> uvicorn.Config | None:
    """A second mutual-TLS `uvicorn.Config`, bound to `mtls.TLS_HOST` at
    `mtls.TLS_REMOTE_PORT`, reusing `ssl_context` — the certificate, trust
    anchor and policy `_build_tls_config` already validated once for the
    loopback listener, not re-validated for a second server. `None` when
    `LIBRARY_TLS_HOST` is unset; the loopback listener binds unconditionally
    either way and is unaffected by this function returning `None`.

    `mtls.require_remote_tls_host` runs first and raises
    `mtls.NonRemoteTlsHost` for a `LIBRARY_TLS_HOST` that is empty or itself
    loopback, since the loopback listener built above already binds that
    address.
    """
    if mtls.TLS_HOST is None:
        return None
    mtls.require_remote_tls_host(mtls.TLS_HOST)
    if mtls.TLS_REMOTE_PORT is None:
        print("library: remote mutual-TLS listener not started — "
              "LIBRARY_TLS_REMOTE_PORT is not an integer", file=sys.stderr)
        raise mtls.NonRemoteTlsHost("LIBRARY_TLS_REMOTE_PORT is not an integer")
    # The server certificate must name the remote address. Its loopback
    # names serve a client that dials its own loopback through a tunnel
    # (platform/decisions/0066); a client dialing this address directly
    # would otherwise fail hostname verification, or turn it off to connect.
    if not _certificate_names(mtls.TLS_HOST):
        print(f"library: remote mutual-TLS listener not started — the server "
              f"certificate {mtls.TLS_CERT} does not name "
              f"LIBRARY_TLS_HOST={mtls.TLS_HOST!r}; reissue it with that name",
              file=sys.stderr)
        raise mtls.NonRemoteTlsHost(
            f"server certificate does not name {mtls.TLS_HOST!r}")
    config = uvicorn.Config(
        libapp.app, host=mtls.TLS_HOST, port=mtls.TLS_REMOTE_PORT,
        log_level=LOG_LEVEL, http=mtls.MTLSH11Protocol, lifespan="off",
        timeout_keep_alive=_TLS_IDLE_TIMEOUT_SECONDS,
        timeout_graceful_shutdown=_GRACEFUL_SHUTDOWN_SECONDS)
    config.load()
    config.ssl = ssl_context
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


async def _run_tls_survivably(server: uvicorn.Server, label: str) -> None:
    """`_run(server)` for a TLS listener specifically, with anything that
    escapes it caught rather than left to propagate. `label` names the
    listener's own `host:port` for the crash message, since there can now
    be two of these (loopback and remote) crashing independently.

    **`asyncio.gather` cancels every other task the moment any one of them
    raises**, with the default `return_exceptions=False` `main()` uses below.
    An ordinary request-handler error never reaches here — Starlette already
    catches those — but something in the accept/handshake path itself could,
    and "one process, several listeners, and the rest are unaffected" (this
    module's own docstring) is not true if that crash takes a sibling
    listener's task down with it, since the plain listener is the one
    carrying every request until the fleet finishes migrating (security
    review, 2026-09-01). Catching here, once, for the one task that must not
    be allowed to cancel its siblings, keeps `_run` itself free of a
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
    except (Exception, SystemExit):  # noqa: BLE001 — anything here must not cancel a sibling listener
        print(f"library: mutual-TLS listener on {label} crashed — "
              "continuing without it", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)


async def main() -> None:
    plain = uvicorn.Server(_build_plain_config())
    servers = [plain]
    # **`plain`'s task is bare `_run`, never `_run_tls_survivably`.** A
    # crash in the plain listener is not a survivable event to isolate —
    # there is nothing else left running to protect it from — so it keeps
    # `asyncio.gather`'s default propagate-and-cancel behaviour.
    tasks = [_run(plain)]
    print(f"library: plain listener on {mtls.PLAIN_HOST}:{mtls.PLAIN_PORT}",
          file=sys.stderr)

    tls_config = _build_tls_config()
    if tls_config is not None:
        tls_server = uvicorn.Server(tls_config)
        servers.append(tls_server)
        tasks.append(_run_tls_survivably(tls_server, f"{mtls.HOST}:{mtls.TLS_PORT}"))
        print(f"library: mutual-TLS listener on "
              f"{mtls.HOST}:{mtls.TLS_PORT}, registry "
              f"{machine_identity.__name__} enforced", file=sys.stderr)

        # A misconfigured remote address stops only the remote listener,
        # the same way missing TLS material stops only the TLS one: the
        # plain and loopback listeners are already built and carry every
        # local client. `require_remote_tls_host` has logged why.
        try:
            remote_config = _build_tls_remote_config(tls_config.ssl)
        except mtls.NonRemoteTlsHost:
            remote_config = None
        if remote_config is not None:
            remote_server = uvicorn.Server(remote_config)
            servers.append(remote_server)
            tasks.append(_run_tls_survivably(
                remote_server, f"{mtls.TLS_HOST}:{mtls.TLS_REMOTE_PORT}"))
            print(f"library: mutual-TLS listener on "
                  f"{mtls.TLS_HOST}:{mtls.TLS_REMOTE_PORT}, registry "
                  f"{machine_identity.__name__} enforced", file=sys.stderr)
    else:
        print(f"library: mutual-TLS listener not started (see the line "
              f"above) — serving {mtls.PLAIN_HOST}:{mtls.PLAIN_PORT} only",
              file=sys.stderr)

    # **ONE SIGNAL HANDLER FOR EVERY SERVER, INSTALLED ONCE, HERE — NOT
    # EACH `Server.serve()`'s OWN.** `Server.serve()` wraps itself in
    # `capture_signals()`, which calls `signal.signal(sig, self.handle_exit)`
    # — a process-wide registration, last-writer-wins. More than one server
    # calling `.serve()` concurrently in this loop would have the last
    # one's handler silently replace the rest, so a SIGTERM would reach
    # only whichever server registered last; the others' `main_loop`s would
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
