"""What is running right now, pulled from the Bosun and served near-live.

`issue_01M0EK69FHZGBNVBP58P06XNDP`. The operator, 2026-08-19: "The Bosun should feed the
Chart House live information, and the Chart House should serve near-live
information. Setting up push is probably wasteful, so it should just be a chain
pull — Orient/Deck from Chart House (Library), Chart House from Bosun."

**A CHAIN PULL, so clients keep talking only to the Library.** Deck and Orient
read this route exactly as they read every other one; the Library reads the
Bosun. Neither client becomes a second client of the Bosun, which is what keeps
`company/overview/convivy-builder`'s rule intact — each of Deck, Orient and
`agentctl` is a client of the Library and they do not talk to each other.

## Pass-through, and deliberately not a store

**The client-serving pull stores nothing** (the operator, 2026-08-19, correcting an
assumption in the routed writeup). "What is running now" is inherently
transient: the Bosun's own register drops a run the instant it ends, and
mirroring that shape is easier to reason about than fighting it. This module
polls, holds one bounded snapshot in memory, and serves it.

**The Chart House may separately pull from the Bosun for the LEDGER's purposes,
and that pull may store.** That is a different consumer with a different need
and it is not this module. Conflating them is how a pass-through view
accidentally becomes a store, or a store ends up bounded by a live view's
freshness needs it was never designed for.

## The freshness bound is the only staleness in the whole chain

The Bosun's read is sub-millisecond, local, and has no cache of its own — it
always answers with the current instant's truth. So **whatever bound this module
chooses IS the freshness Deck and Orient see**, and that makes it a real
parameter rather than a tuning knob (the same lesson ADR-0048's policy cache
states for a different kind of staleness).

`TTL_SECONDS` is two seconds. The reasoning, since a number with none is a
number nobody can revise:

- **It has to be short**, because "how long has this been running" is exactly
  the fact a stale read gets visibly wrong, and it is one of the few facts this
  view carries.
- **It does not have to be zero.** A page render makes several requests and a
  burst of them should cost the socket one read, not one each.
- **Two seconds is below what a person reading a dashboard can perceive** and
  still collapses a render's burst into a single upstream read.

The bound is stated in the response as `stale_after_seconds`, so a client renders what
it is actually looking at rather than inferring a freshness nobody wrote down.

## An unreachable socket is a normal state, not an error

The Bosun's launchd plist ships deliberately inactive; the socket does not exist
until someone bootstraps it, which is the Bosun's act or the operator's and not something
to trigger from here. So absence is designed for rather than raised: this
returns an empty list and says `available: false` with a reason, and the route
answers 200.

**That is the same fail-closed shape ADR-0048's policy cache uses, applied to
absence rather than to staleness** — the safe answer to "I cannot see what is
running" is "nothing is known to be running", never a page that will not render.
A 503 here would take out a crew view over a dependency that is optional by
design.

## The field allowlist does not get re-opened here

The Bosun's own response is an explicit allowlist, added after a `reserved`
field was found leaking through mid-spawn and closed by allowlisting rather than
by removing that one field (`agent-platform#417`). **An allowlist upstream only
holds if nothing downstream widens it back**, so this module names the fields it
forwards rather than passing through whatever arrives. A new field on the
Bosun's side reaches clients when somebody adds it here, deliberately.
"""
from __future__ import annotations

import json
import os
import re
import socket
import sqlite3
import ssl
import threading
import time
from pathlib import Path

import machine_identity
import machine_registry
import mtls
import store

# The Bosun's register, over the same socket shape `bin/session-view` already
# uses. Overridable so the tests can point at one they control.
SOCKET_PATH = Path(os.environ.get(
    "LIBRARY_RUN_REGISTER_SOCKET",
    str(Path.home() / ".local" / "run" / "run-register-api.sock")))

# The same register, reached over the network rather than this host's own
# socket — the multi-machine epic's Library clause
# (issue_01M5FMPE1Y2DQPBHYQ0P0J6VYW): platform/decisions/0050 carries this
# read by chain pull over a Unix socket and records that the socket does not
# survive the Library and the Bosun on different machines.
#
# **Both must be set for the remote transport to be used at all.** Unset,
# `SOCKET_PATH` above is read exactly as before. `REMOTE_MACHINE` is the
# bare machine name (`convivy://machine/<name>`'s `<name>`) the Bosun's host
# certificate must belong to — `_verify_remote_machine` is what checks it.
REMOTE_HOST = os.environ.get("LIBRARY_RUN_REGISTER_HOST", "")
REMOTE_MACHINE = os.environ.get("LIBRARY_RUN_REGISTER_MACHINE", "")

# The certificate the Library presents to the Bosun: this host's machine
# certificate, issued for `clientAuth`. `mtls.TLS_CERT` is the Library's own
# listener certificate, issued for `serverAuth` only, and the root issues the
# two purposes exclusively (root-ca's `extendedKeyUsage(clientAuth:)`), so a
# Bosun holding callers to `clientAuth` as the Library does would refuse it.
# No default: a configured remote with these unset refuses to dial.
CLIENT_CERT = os.environ.get("LIBRARY_RUN_REGISTER_CLIENT_CERT", "")
CLIENT_KEY = os.environ.get("LIBRARY_RUN_REGISTER_CLIENT_KEY", "")


def _resolve_remote_port(env=None) -> int | None:
    """`LIBRARY_RUN_REGISTER_PORT`, or `mtls.TLS_PORT` when it is unset —
    every mutual-TLS listener on this platform uses that port by
    convention, including the Bosun's. `None` for a value that is not an
    integer, the same shape `mtls.resolve_tls_remote_port` already uses for
    its own remote port setting."""
    env = os.environ if env is None else env
    try:
        return int(env.get("LIBRARY_RUN_REGISTER_PORT", str(mtls.TLS_PORT)))
    except ValueError:
        return None


REMOTE_PORT = _resolve_remote_port()


def _remote_configured() -> bool:
    return bool(REMOTE_HOST) and bool(REMOTE_MACHINE)


class _RemoteRegisterRefused(OSError):
    """Any reason the Bosun's remote mutual-TLS register cannot be trusted:
    missing client certificate material, or a peer certificate that fails
    the chain, the registry row, or the configured machine name.

    **An `OSError` subclass, deliberately.** `_fetch` and `run_facts` already
    catch `(OSError, socket.timeout)` around the connect they make — raising
    this rather than a bare exception means a remote refusal degrades
    exactly like a local socket that refuses to connect, with no second
    except clause to keep in step and nowhere for a cleartext fallback to
    hide.
    """

# See the module docstring. This is the freshness Deck and Orient see.
TTL_SECONDS = 2.0

# How long to wait on a Bosun that has accepted a connection and gone quiet.
# Short, because this sits inside a client's request: a hung upstream must
# degrade to "not available" quickly rather than holding the caller open.
#
# **MEASURED AGAINST THE LIVE REGISTER ON 2026-08-24, not chosen from taste.**
# Ten back-to-back reads and a read after three seconds' idle all returned in
# 0.00-0.01s, which matches the epic's description of a sub-millisecond local
# read with no cache of its own. Two seconds is two orders of magnitude of
# headroom over that.
#
# **This bounds the WHOLE read, not each `recv`** — see `_fetch`, where the
# deadline is re-armed every pass. The first version set one socket timeout and
# claimed a two-second degradation it did not deliver.
#
# **One contact did exceed it, once, and it is recorded rather than explained
# away.** The very first call from this module timed out at 2.00s; every read
# since — dozens — has been ~10ms, and the slow one could not be reproduced. So
# the cost of that blip is known and bounded: a failed read caches
# `available: false` for one TTL, which is two seconds of a crew view saying
# nothing is known to be running. That is the trade this timeout buys, and it is
# the right side of it, because the alternative is holding a page render open on
# an upstream that is optional by design. If the blip recurs, the fix is to
# serve the last good snapshot with an explicit age rather than to lengthen this.
TIMEOUT_SECONDS = 2.0

# **THE FIELDS THIS FORWARDS, NAMED RATHER THAN PASSED THROUGH.** See the
# docstring: the allowlist upstream only holds if nothing downstream re-opens it.
RUN_FIELDS = ("run_id", "agent", "task", "started_at", "repo", "worktree",
              "pid", "parent_run")

# `/sessions`'s row allowlist, the same discipline as `RUN_FIELDS` above,
# naming the nine fields `agent-platform/contracts/run-register-api-sessions`
# gives a session row. `working_on` is itself an object; `WORKING_ON_FIELDS`
# is that object's own allowlist, applied one level down.
SESSION_FIELDS = ("session", "crew", "state", "alive", "progressing",
                   "last_event_age", "ctx_pct", "agent_run_count")
WORKING_ON_FIELDS = ("text", "age", "since", "stale")

# **A CEILING ON THE BODY, because the read loop would otherwise accept whatever
# arrives** (reviewer, PR #51). The register's real answers are a few kilobytes;
# a hundred is four orders of magnitude of headroom and still bounds a Bosun
# that has gone wrong. Without it a large body is read fully into memory, and —
# because the module lock is held across the fetch — every other caller waits
# for the whole transfer.
MAX_BODY_BYTES = 100_000

_lock = threading.Lock()
_cache: tuple[float, dict] | None = None


_FIRST_PEM_CERT = re.compile(
    r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", re.DOTALL)


def _build_client_ssl_context() -> ssl.SSLContext | None:
    """A `CERT_REQUIRED`, TLS-1.3-floor client `SSLContext`, presenting this
    host's own machine certificate (`CLIENT_CERT`, `CLIENT_KEY`) and
    trusting the Library's own root, `mtls.TLS_CA`, the trust anchor its
    mutual-TLS listener already loads (platform/decisions/0066: "the Library
    presents its host's machine certificate"). The leaf in `CLIENT_CERT`
    must carry `clientAuth`; a `serverAuth` certificate such as
    `mtls.TLS_CERT` is refused here rather than by the Bosun.

    **`check_hostname` is off, and `_verify_remote_machine` is what replaces
    it rather than nothing** — the Bosun's certificate is named for a
    machine, not for the address this dials, and platform/decisions/0092
    states the analogous rule for the Individuator's own check of the
    Keel's server certificate: hostname verification is replaced by the
    fingerprint-and-registry check, never simply turned off.

    `None` when the material is unset or missing, when the client leaf lacks
    `clientAuth`, when `TLS_CA` fails the same owner/group/mode walk the
    inbound listener holds it to (`mtls._trust_anchor_walk_ok`), or when it
    will not load as a certificate chain and trust anchor, so a configured
    remote with no working client certificate degrades exactly like an
    unreachable Bosun rather than raising.
    """
    if not CLIENT_CERT or not CLIENT_KEY:
        return None
    client_cert, client_key = Path(CLIENT_CERT), Path(CLIENT_KEY)
    missing = [str(p) for p in (client_cert, client_key, mtls.TLS_CA)
               if not p.is_file()]
    if missing:
        return None
    try:
        pem = _FIRST_PEM_CERT.search(client_cert.read_text())
        leaf_der = ssl.PEM_cert_to_DER_cert(pem.group(0)) if pem else b""
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    if (machine_identity.CLIENT_AUTH_OID
            not in machine_identity.extended_key_usage_oids(leaf_der)):
        return None
    if not mtls._trust_anchor_walk_ok():
        return None
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    try:
        ctx.load_cert_chain(str(client_cert), str(client_key))
        ctx.load_verify_locations(str(mtls.TLS_CA))
    except ssl.SSLError:
        return None
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.check_hostname = False
    return ctx


_client_ctx_lock = threading.Lock()
_client_ctx: ssl.SSLContext | None = None
_client_ctx_built = False
_client_ctx_building = False
_client_ctx_refused_at: float | None = None
_client_ctx_generation = 0

# How long a refused build stands before the next dial tries again, so a
# transient failure (the walk timing out, a certificate mid-rotation) does
# not stand until restart, and a lasting one does not spawn a walk per dial.
CLIENT_CTX_RETRY_SECONDS = 60.0


def _warm_client_ssl_context() -> None:
    """Build the outbound context. Runs off the request path: building it
    runs the trust-anchor walk, a `serve.sh` subprocess with its own
    30-second timeout, which must never run inside a dial's 2-second
    deadline or under `snapshot()`'s lock. A built context is kept; a
    refusal is retried after `CLIENT_CTX_RETRY_SECONDS`. A `reset_cache()`
    during the build discards its result."""
    global _client_ctx, _client_ctx_built, _client_ctx_building
    global _client_ctx_refused_at
    with _client_ctx_lock:
        generation = _client_ctx_generation
    try:
        ctx = _build_client_ssl_context()
    except Exception:  # noqa: BLE001 — any failure is a refusal, retried later
        ctx = None
    with _client_ctx_lock:
        if generation != _client_ctx_generation:
            return
        _client_ctx_building = False
        if ctx is None:
            _client_ctx_refused_at = time.monotonic()
        else:
            _client_ctx, _client_ctx_built = ctx, True


def _client_ssl_context() -> ssl.SSLContext | None:
    """The outbound context once `_warm_client_ssl_context` has built it,
    else `None` at once, starting that build in a background thread when
    none is running and no refusal is fresher than
    `CLIENT_CTX_RETRY_SECONDS`. A dial before a build succeeds refuses and
    degrades as an unreachable Bosun does. Once built, the context is kept,
    as the inbound listener keeps the one it builds at startup, so a changed
    certificate or trust anchor takes effect at the next restart, or at
    `reset_cache()`."""
    global _client_ctx_building
    with _client_ctx_lock:
        if _client_ctx_built:
            return _client_ctx
        refused_recently = (
            _client_ctx_refused_at is not None
            and time.monotonic() - _client_ctx_refused_at
            < CLIENT_CTX_RETRY_SECONDS)
        if not _client_ctx_building and not refused_recently:
            _client_ctx_building = True
            threading.Thread(target=_warm_client_ssl_context, daemon=True,
                             name="run-register-client-ctx").start()
        return None


if _remote_configured():
    # Start the first build at import, so the first request usually finds
    # the context ready rather than refusing while it builds.
    _client_ssl_context()

def _verify_remote_machine(conn: ssl.SSLSocket) -> None:
    """Refuse `conn`'s peer certificate unless it is the active, unrevoked
    `machine_certificates` row for `REMOTE_MACHINE`.

    **A signature from the root is necessary and not sufficient**
    (platform/decisions/0066), so the chain check the handshake already made
    is not the whole of this. This reuses `machine_registry.status_for` —
    the one place a fingerprint becomes a registry answer, the same lookup
    the Bosun itself makes of the Library (platform/decisions/0092) — rather
    than a second query against `machine_certificates`.

    Raises `_RemoteRegisterRefused` for a connection with no peer
    certificate, a registry this cannot read, or a fingerprint that is not
    exactly `REMOTE_MACHINE`'s active row. Every case folds into the same
    refusal: a caller cannot tell a revoked machine from a machine-name
    mismatch from a corrupt database, which is the point — none of them
    should look more trustworthy than the others.
    """
    der = conn.getpeercert(binary_form=True)
    if not der:
        raise _RemoteRegisterRefused(
            "the Bosun's host presented no certificate")
    try:
        status = machine_registry.status_for(machine_identity.sha256_der(der))
    except (store.MisconfiguredDatabase, OSError, sqlite3.Error) as e:
        raise _RemoteRegisterRefused(
            f"could not read the machine registry ({e})") from e
    if not status.get("active") or status.get("machine") != REMOTE_MACHINE:
        raise _RemoteRegisterRefused(
            f"the Bosun's host certificate is not {REMOTE_MACHINE!r}'s "
            f"active registry row (status: "
            f"{status.get('status', 'unknown')!r})")


def _dial_register() -> socket.socket:
    """A socket connected to the Bosun's run register, ready for the request
    line: the Unix socket at `SOCKET_PATH`, or — when `REMOTE_HOST` and
    `REMOTE_MACHINE` are both set — a mutual-TLS connection to that host,
    verified against the Library's own machine registry before this
    returns.

    **Every failure is `OSError`** (`socket.timeout` included, and
    `_RemoteRegisterRefused` is one), the exact shape `_fetch` and
    `run_facts` already catch around their own connect — nothing downstream
    needs a second except clause for the remote case.

    **No cleartext fallback.** Once `REMOTE_HOST` and `REMOTE_MACHINE` are
    both set, a missing client certificate, a chain that does not verify, or
    a certificate that chains but is not the configured machine's active
    row all refuse outright; none of them tries `SOCKET_PATH` instead.
    """
    if _remote_configured():
        if REMOTE_PORT is None:
            raise _RemoteRegisterRefused(
                "LIBRARY_RUN_REGISTER_PORT is not an integer")
        ctx = _client_ssl_context()
        if ctx is None:
            raise _RemoteRegisterRefused(
                "a remote run register is configured but this host's "
                "mutual-TLS client certificate is missing, invalid, or "
                "not yet checked")
        conn = ctx.wrap_socket(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
        conn.settimeout(TIMEOUT_SECONDS)
        try:
            conn.connect((REMOTE_HOST, REMOTE_PORT))
            _verify_remote_machine(conn)
        except Exception:
            conn.close()
            raise
        return conn
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(TIMEOUT_SECONDS)
    try:
        conn.connect(str(SOCKET_PATH))
    except Exception:
        conn.close()
        raise
    return conn


def _read_register(path: str, list_field: str) -> tuple[dict | None, dict | None]:
    """One request/response cycle against the register: dial, send `GET
    path`, read to the whole-read deadline and `MAX_BODY_BYTES` cap, and
    parse the body as a JSON object whose `list_field` is a list.

    Shared by `_fetch` (`/runs`, `list_field="runs"`) and `_fetch_sessions`
    (`/sessions`, `list_field="sessions"`), which differ only in the request
    target, the envelope key checked here, and the per-row allowlist each
    applies to what it gets back. Never raises: `(None, error)` covers every
    failure this socket can produce, with `error` in the same
    `{"available": False, "reason": ...}` shape each caller already returns,
    so a caller need only splice in its own empty list field.
    """
    if not _remote_configured() and not SOCKET_PATH.exists():
        return None, {"available": False,
                      "reason": f"no run register at {SOCKET_PATH}"}
    # **A DEADLINE FOR THE WHOLE READ, NOT A TIMEOUT PER `recv`** (reviewer,
    # PR #51, blocking). `settimeout` bounds each individual socket call, so a
    # register that ignores `Connection: close` and trickles bytes — a
    # keep-alive response, or one answering slowly in small pieces — never trips
    # it and the loop below runs for as long as the bytes keep coming.
    #
    # That is not merely a slow route. `snapshot()` holds the module lock across
    # this fetch, and the route reaches it through `run_in_threadpool`, which
    # draws on one anyio limiter shared by every threadpooled route in this
    # service. An unbounded read here can therefore exhaust that pool and stall
    # checkout, resolve and the ledger writes — routes with no relationship to
    # this one. The `TIMEOUT_SECONDS` comment claimed a two-second degradation
    # that the code did not deliver.
    deadline = time.monotonic() + TIMEOUT_SECONDS
    conn = None
    try:
        conn = _dial_register()
        conn.sendall(f"GET {path} HTTP/1.1\r\nHost: localhost\r\n"
                     f"Connection: close\r\n\r\n".encode("ascii"))
        chunks = []
        total = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None, {"available": False,
                              "reason": f"the run register did not finish "
                                        f"answering within {TIMEOUT_SECONDS}s"}
            # Re-armed every pass, so the LAST recv can never wait past the
            # deadline either.
            conn.settimeout(remaining)
            chunk = conn.recv(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_BODY_BYTES:
                return None, {"available": False,
                              "reason": f"the run register's answer is "
                                        f"larger than {MAX_BODY_BYTES} bytes"}
            chunks.append(chunk)
        raw = b"".join(chunks)
    except (OSError, socket.timeout) as e:
        return None, {"available": False,
                      "reason": f"the run register did not answer ({e})"}
    finally:
        if conn is not None:
            conn.close()

    head, _, body = raw.partition(b"\r\n\r\n")
    status = head.split(b"\r\n")[0] if head else b""
    if b" 200 " not in status:
        return None, {"available": False,
                      "reason": f"the run register answered "
                                f"{status.decode('utf-8', 'replace')!r}"}
    try:
        payload = json.loads(body)
    # **`RecursionError` TOO, NOT ONLY `ValueError`** (security-reviewer,
    # PR #51). A deeply nested body raises `RecursionError` from `json.loads`,
    # which is not a `ValueError` and would have escaped as a 500 — breaking
    # this function's own "never raises" contract for exactly the untrusted
    # shape it exists to absorb.
    except (ValueError, RecursionError) as e:
        return None, {"available": False,
                      "reason": f"the run register's answer is not JSON ({e})"}
    if not isinstance(payload, dict) or not isinstance(payload.get(list_field), list):
        # SHAPE, NOT ONLY PARSEABILITY — the same guard the Reference Room's
        # JSON columns take. A payload that parses and is not the agreed shape
        # would otherwise reach the allowlist loop below as an AttributeError.
        return None, {"available": False,
                      "reason": "the run register's answer is not the agreed shape"}
    return payload, None


def _fetch() -> dict:
    """One read of the register. Never raises: every failure is an unavailable
    answer with a reason, because absence is a normal state here."""
    payload, error = _read_register("/runs", "runs")
    if error is not None:
        return {**error, "runs": []}
    runs = []
    for row in payload["runs"]:
        if not isinstance(row, dict):
            continue
        runs.append({field: row.get(field) for field in RUN_FIELDS})
    return {"available": True, "runs": runs,
            "generated_at": payload.get("generated_at"), "reason": None}


def _fetch_sessions() -> dict:
    """One read of the register's `/sessions`. Never raises, the same
    contract as `_fetch`, and never cached — see `sessions()`, below."""
    payload, error = _read_register("/sessions", "sessions")
    if error is not None:
        return {**error, "sessions": []}
    sessions_out = []
    for row in payload["sessions"]:
        if not isinstance(row, dict):
            continue
        out = {field: row.get(field) for field in SESSION_FIELDS}
        working_on = row.get("working_on")
        out["working_on"] = {
            field: (working_on.get(field) if isinstance(working_on, dict)
                    else None)
            for field in WORKING_ON_FIELDS}
        sessions_out.append(out)
    return {"available": True, "sessions": sessions_out,
            "generated_at": payload.get("generated_at"), "reason": None}


def snapshot(*, now: float | None = None) -> dict:
    """The current register, from cache when it is inside the bound.

    **One in-flight read at a time.** The lock is held across the fetch rather
    than only around the cache write, so a burst of concurrent requests on a
    cold cache makes ONE upstream read and the rest wait for it. Releasing
    around the fetch would let every request in the burst open its own socket,
    which is the load this cache exists to prevent.
    """
    global _cache
    now = time.monotonic() if now is None else now
    with _lock:
        if _cache is not None and (now - _cache[0]) < TTL_SECONDS:
            return _cache[1]
        fresh = _fetch()
        _cache = (now, fresh)
        return fresh


def reset_cache() -> None:
    """Drop the snapshot. For tests, and for a caller that has just been told
    the register moved."""
    global _cache, _client_ctx, _client_ctx_built, _client_ctx_building
    global _client_ctx_refused_at, _client_ctx_generation
    with _lock:
        _cache = None
    with _client_ctx_lock:
        _client_ctx, _client_ctx_built = None, False
        _client_ctx_building, _client_ctx_refused_at = False, None
        _client_ctx_generation += 1


def sessions() -> dict:
    """The register's live sessions, read fresh on every call.

    **Deliberately uncached, unlike `snapshot()`.**
    `agent-platform/contracts/run-register-api-sessions` states that the
    daemon builds `/sessions` fresh on every request, with no cache or TTL of
    its own — so there is no staleness here for a module-level cache to
    bound, and adding one would only invent a freshness gap the contract
    says does not exist.
    """
    return _fetch_sessions()


# ---------------------------------------------------------------------------
# run_facts: one run's auth-relevant facts, for the review-record write route
# (issue_01M2HS3Q36RE5F82QXRN4Z0D0K, agent-platform issue_01M2K5HRWWVPWTC90NQTFEA8RK
# for the Bosun's side, agreed 2026-09-15 and not yet built there).
#
# **A SEPARATE READ FROM `/runs`, DELIBERATELY UNCACHED.** `snapshot()`'s
# two-second bound is right for "what is running now", rendered on a page a
# person watches; this is an auth check made once per write, where a stale
# answer would mean deciding `run-verified` against facts that are already
# up to two seconds old on top of however old the write itself is. The cost
# of skipping the cache is one socket round trip per review write, which is
# the write route's own admission — not a render loop making several
# requests a second the way `/runs` is.
# ---------------------------------------------------------------------------

# `run-<8 lowercase hex>`, matching the run register's own id shape. Checked
# BEFORE this module sends anything, the same discipline `_merge_repo_scope`
# and every other route apply to a caller-supplied string that reaches a
# lower layer — here, the HTTP request line this module builds by hand.
RUN_ID_RE = re.compile(r"^run-[0-9a-f]{8}$")

# The fields this forwards from a FOUND run's facts, named rather than passed
# through — the same allowlist discipline `RUN_FIELDS` above states the
# reasoning for. `private` is handled separately: a private run's facts are
# `{"found": true, "private": true}` and nothing else ever reaches a
# caller-facing read. `include_private_facts` below is the one way past that.
#
# `posted_verdict` and `posted_pr` (library CQ·217, issue_01M5FM2A102PKMQVKFE7HM30F0)
# are what `agentctl review-post` itself wrote into the run's own `meta.json`
# when it posted — self-reported, like every other field here, not a
# property the Bosun observed independently. `_review_record_auth` binds a
# run-verified record's verdict to this pair so a re-post naming the same
# run_id cannot claim a different verdict and still read as attested by the
# run register.
RUN_FACTS_FIELDS = ("agent", "repo", "review_sha", "pr", "review_role",
                    "posted_verdict", "posted_pr")


def run_facts(run_id: str, *, include_private_facts: bool = False) -> dict:
    """One run's facts for the write route's `auth` decision. Never raises.

    **`include_private_facts` is an internal path, never a request field.**
    Every caller-facing read — `GET /runs` and any future one — calls this
    with the default `False` and gets `{"found": true, "private": true}` for
    a private run, same as always. `POST /review-records`
    (`create_review_record`) is the one caller that passes `True`: a private
    crew's own review record still needs `_review_record_auth` to compare a
    claimed role, repo, commit and PR against the real ones, or a private
    run could never reach `run-verified` at all (CQ·237,
    issue_01M5QE987V342E9NX422BTWW8J). No route reads this argument from a
    request body or query string — it is set exactly once, in that one
    Python call.

    A malformed `run_id` is refused without touching the socket — it cannot
    name a real run, so there is nothing to ask the Bosun and no reason to
    spend a round trip finding that out. It answers the same as an id the
    Bosun does not recognise: `{"available": True, "found": False}`.

    Everything past that point is `_fetch`'s shape, reproduced rather than
    shared: a distinct GET target, no cache, and its own deadline — sharing
    `_fetch` would mean a run-facts read taking the module lock `snapshot()`
    holds across its own fetch, which is a real contention two independent
    callers should not have. `_dial_register` is the one piece actually
    shared between them: it holds no lock of its own, so sharing it carries
    none of that contention.

    `{"available": False, "reason": ...}` covers every failure the socket
    can produce: unreachable, timed out, a non-200, or a body that is not the
    agreed JSON shape. The write route reads `available` first and refuses
    503 on `False` rather than treating it as `unverified` — a transient
    outage must not silently downgrade or supersede a record that a working
    read would have verified.
    """
    if not isinstance(run_id, str) or not RUN_ID_RE.match(run_id):
        return {"available": True, "found": False}
    if not _remote_configured() and not SOCKET_PATH.exists():
        return {"available": False,
                "reason": f"no run register at {SOCKET_PATH}"}
    deadline = time.monotonic() + TIMEOUT_SECONDS
    conn = None
    try:
        conn = _dial_register()
        conn.sendall(
            f"GET /runs/{run_id}/facts HTTP/1.1\r\nHost: localhost\r\n"
            f"Connection: close\r\n\r\n".encode("ascii"))
        chunks = []
        total = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"available": False,
                        "reason": f"the run register did not finish "
                                  f"answering within {TIMEOUT_SECONDS}s"}
            conn.settimeout(remaining)
            chunk = conn.recv(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_BODY_BYTES:
                return {"available": False,
                        "reason": f"the run register's answer is larger "
                                  f"than {MAX_BODY_BYTES} bytes"}
            chunks.append(chunk)
        raw = b"".join(chunks)
    except (OSError, socket.timeout) as e:
        return {"available": False,
                "reason": f"the run register did not answer ({e})"}
    finally:
        if conn is not None:
            conn.close()

    head, _, body = raw.partition(b"\r\n\r\n")
    status = head.split(b"\r\n")[0] if head else b""
    if b" 200 " not in status:
        return {"available": False,
                "reason": f"the run register answered "
                          f"{status.decode('utf-8', 'replace')!r}"}
    try:
        payload = json.loads(body)
    except (ValueError, RecursionError) as e:
        return {"available": False,
                "reason": f"the run register's answer is not JSON ({e})"}
    if not isinstance(payload, dict) or not isinstance(payload.get("found"), bool):
        return {"available": False,
                "reason": "the run register's answer is not the agreed shape"}
    if not payload["found"]:
        return {"available": True, "found": False}
    if payload.get("private") is True:
        if include_private_facts:
            return {"available": True, "found": True, "private": True,
                    **{field: payload.get(field) for field in RUN_FACTS_FIELDS}}
        return {"available": True, "found": True, "private": True}
    return {"available": True, "found": True,
            **{field: payload.get(field) for field in RUN_FACTS_FIELDS}}
